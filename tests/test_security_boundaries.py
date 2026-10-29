from types import SimpleNamespace

import pytest

import dashboard_server
import reaction_roles


def _run_immediate(coroutine):
    """Drive a coroutine whose test doubles never perform real async I/O."""
    try:
        coroutine.send(None)
    except StopIteration as stopped:
        return stopped.value
    raise AssertionError("test unexpectedly reached real asynchronous I/O")


class _FakeGuild:
    def __init__(self, guild_id=100, *, member=None, channels=None, owner_id=999):
        self.id = guild_id
        self.owner_id = owner_id
        self.name = "Test Guild"
        self._member = member
        self._channels = channels or {}

    def get_member(self, user_id):
        return self._member if self._member and self._member.id == user_id else None

    async def fetch_member(self, user_id):
        if self._member and self._member.id == user_id:
            return self._member
        raise LookupError("member not found")

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


class _FakeBot:
    def __init__(self, guild):
        self.guild = guild

    def get_guild(self, guild_id):
        return self.guild if self.guild.id == guild_id else None

    def get_channel(self, channel_id):
        raise AssertionError("security boundary must not resolve channels globally")


class _AuthRequest(dict):
    path = "/api/guild/100/settings"
    method = "GET"
    headers = {}

    def __init__(self, token):
        super().__init__()
        self.cookies = {"ep_session": token}

    @property
    def match_info(self):
        return {"guild_id": "100"}


def test_revoked_manage_guild_permission_invalidates_stale_session(monkeypatch):
    permissions = SimpleNamespace(administrator=False, manage_guild=False)
    member = SimpleNamespace(id=42, guild_permissions=permissions)
    guild = _FakeGuild(member=member)
    session = {
        "user_id": "42",
        "guilds": [{"id": "100", "can_manage": True}],
        "_created_at": dashboard_server.datetime.now(
            dashboard_server.timezone.utc
        ).timestamp(),
    }
    monkeypatch.setattr(dashboard_server, "_bot_ref", _FakeBot(guild))
    monkeypatch.setitem(dashboard_server._sessions, "stale-token", session)

    called = False

    async def handler(_request):
        nonlocal called
        called = True
        return dashboard_server.web.Response(status=200)

    with pytest.raises(dashboard_server.web.HTTPForbidden):
        _run_immediate(
            dashboard_server.auth_middleware(_AuthRequest("stale-token"), handler)
        )

    assert called is False
    assert session["guilds"] == []


def test_current_manage_guild_permission_preserves_normal_access(monkeypatch):
    permissions = SimpleNamespace(administrator=False, manage_guild=True)
    member = SimpleNamespace(id=42, guild_permissions=permissions)
    guild = _FakeGuild(member=member)
    session = {
        "user_id": "42",
        "guilds": [{"id": "100", "can_manage": True}],
        "_created_at": dashboard_server.datetime.now(
            dashboard_server.timezone.utc
        ).timestamp(),
    }
    monkeypatch.setattr(dashboard_server, "_bot_ref", _FakeBot(guild))
    monkeypatch.setitem(dashboard_server._sessions, "valid-token", session)

    async def handler(_request):
        return dashboard_server.web.Response(status=204)

    response = _run_immediate(
        dashboard_server.auth_middleware(_AuthRequest("valid-token"), handler)
    )
    assert response.status == 204


def test_manage_roles_is_checked_separately_from_manage_guild(monkeypatch):
    permissions = SimpleNamespace(
        administrator=False, manage_guild=True, manage_roles=False
    )
    member = SimpleNamespace(id=42, guild_permissions=permissions)
    guild = _FakeGuild(member=member)
    monkeypatch.setattr(dashboard_server, "_bot_ref", _FakeBot(guild))

    allowed = _run_immediate(
        dashboard_server._session_has_live_guild_permission(
            {"user_id": "42"}, "100", "manage_roles"
        )
    )
    assert allowed is False


def test_channel_ids_are_resolved_only_inside_route_guild(monkeypatch):
    own_channel = SimpleNamespace(id=10)
    guild = _FakeGuild(channels={10: own_channel})
    monkeypatch.setattr(dashboard_server, "_bot_ref", _FakeBot(guild))

    assert dashboard_server._require_guild_channel("100", "10") is own_channel
    with pytest.raises(dashboard_server.web.HTTPBadRequest):
        dashboard_server._require_guild_channel("100", "999")


def test_cleanup_interval_rejects_delete_everything_encodings():
    assert dashboard_server._validate_cleanup_interval("24") == 24
    for unsafe in (0, -1, "-999", 8761, "not-a-number"):
        with pytest.raises(dashboard_server.web.HTTPBadRequest):
            dashboard_server._validate_cleanup_interval(unsafe)


def _role(*, role_id=10, position=1, managed=False, **permissions):
    defaults = {
        name: False for name in reaction_roles._PRIVILEGED_ROLE_PERMISSIONS
    }
    defaults.update(permissions)
    return SimpleNamespace(
        id=role_id,
        position=position,
        managed=managed,
        permissions=SimpleNamespace(**defaults),
    )


def test_privileged_reaction_roles_are_rejected_but_normal_roles_remain_valid():
    guild = SimpleNamespace(
        id=100,
        me=SimpleNamespace(top_role=SimpleNamespace(position=50)),
    )

    assert reaction_roles.role_assignment_block_reason(guild, _role()) is None
    assert "Privileged" in reaction_roles.role_assignment_block_reason(
        guild, _role(administrator=True)
    )
    assert "highest role" in reaction_roles.role_assignment_block_reason(
        guild, _role(position=50)
    )


def test_cleanup_worker_cannot_fall_back_to_cross_guild_channel():
    from bot import TwitchNotifierBot

    guild = _FakeGuild(channels={})

    async def log_to_channel(*_args, **_kwargs):
        return None

    fake_bot = SimpleNamespace(
        get_guild=lambda guild_id: guild if guild_id == 100 else None,
        get_channel=lambda _channel_id: pytest.fail(
            "cleanup attempted a global cross-guild channel lookup"
        ),
        log_to_channel=log_to_channel,
    )

    deleted = _run_immediate(
        TwitchNotifierBot.cleanup_channel(
            fake_bot, guild_id=100, channel_id=999, interval_hours=24,
            keep_pinned=True,
        )
    )
    assert deleted == 0


def test_cleanup_worker_preserves_legitimate_same_guild_channel():
    from bot import TwitchNotifierBot

    async def empty_history():
        if False:
            yield None

    channel = SimpleNamespace(
        name="cleanup",
        guild=SimpleNamespace(me=object()),
        permissions_for=lambda _member: SimpleNamespace(
            manage_messages=True, read_message_history=True
        ),
        history=lambda **_kwargs: empty_history(),
    )
    guild = _FakeGuild(channels={10: channel})

    async def log_to_channel(*_args, **_kwargs):
        return None

    fake_bot = SimpleNamespace(
        get_guild=lambda guild_id: guild if guild_id == 100 else None,
        log_to_channel=log_to_channel,
    )
    deleted = _run_immediate(
        TwitchNotifierBot.cleanup_channel(
            fake_bot, guild_id=100, channel_id=10, interval_hours=24,
            keep_pinned=True,
        )
    )
    assert deleted == 0
