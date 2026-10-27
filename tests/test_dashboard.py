"""Tests for dashboard_server.py — security-critical primitives.

Covers:
  - HMAC verification of EventSub webhook signatures
  - OAuth state TTL pruning
  - Per-guild session access checks
"""
import hmac as _hmac
import hashlib as _hashlib
import inspect
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import pytest

# Import after conftest sets env vars
import dashboard_server


class TestDashboardSecurityHardening:
    def test_cors_uses_explicit_production_origin(self):
        assert dashboard_server.CORS_ALLOWED_ORIGINS == (
            "https://excelprotocol.fly.dev",
        )
        source = inspect.getsource(dashboard_server.create_dashboard_app)
        assert '"*": ResourceOptions' not in source
        assert "for origin in CORS_ALLOWED_ORIGINS" in source

    def test_security_headers_cover_api_responses(self):
        from aiohttp import web

        response = web.Response()
        dashboard_server._apply_security_headers(
            SimpleNamespace(path="/api/me"), response
        )

        assert response.headers["Strict-Transport-Security"].startswith("max-age=")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert response.headers["Cache-Control"] == "no-store"

    def test_fly_forces_https(self):
        fly_config = (Path(__file__).parents[1] / "fly.toml").read_text(encoding="utf-8")
        assert "force_https = true" in fly_config

    def test_sensitive_local_files_are_gitignored(self):
        ignore = (Path(__file__).parents[1] / ".gitignore").read_text(encoding="utf-8")
        assert "*.db" in ignore
        assert ".env" in ignore


class TestTwitchRewardManagement:
    def test_public_reward_includes_native_cooldown_and_manageability(self):
        reward = dashboard_server._public_twitch_reward({
            "id": "reward-1", "title": "Ink me", "cost": 250,
            "is_enabled": True, "background_color": "#00F5D4",
            "global_cooldown_setting": {
                "is_enabled": True, "global_cooldown_seconds": 300,
            },
            "cooldown_expires_at": "2026-10-21T12:05:00Z",
        }, {"reward-1"})
        assert reward["manageable"] is True
        assert reward["is_global_cooldown_enabled"] is True
        assert reward["global_cooldown_seconds"] == 300

    def test_create_reward_payload_supports_twitch_native_cooldown(self):
        payload = dashboard_server._twitch_reward_payload({
            "title": "Ink me", "cost": 250, "is_enabled": True,
            "is_global_cooldown_enabled": True,
            "global_cooldown_seconds": 300,
        }, creating=True)
        assert payload == {
            "title": "Ink me", "cost": 250, "is_enabled": True,
            "is_global_cooldown_enabled": True,
            "global_cooldown_seconds": 300,
        }

    def test_cooldown_rejects_values_twitch_will_not_show(self):
        with pytest.raises(dashboard_server.web.HTTPBadRequest) as exc:
            dashboard_server._twitch_reward_payload({
                "is_global_cooldown_enabled": True,
                "global_cooldown_seconds": 59,
            }, creating=False)
        assert "1 minute" in exc.value.reason


class TestReactionRoleConfig:
    def test_dropdown_is_a_supported_panel_type(self):
        assert dashboard_server._validate_reaction_role_config(
            "dropdown", [{"role_id": "1"}], None
        ) is None

    def test_retired_modal_panel_type_is_rejected(self):
        with pytest.raises(dashboard_server.web.HTTPBadRequest):
            dashboard_server._validate_reaction_role_config(
                "modal", [{"role_id": "1"}], None
            )

    def test_discord_option_limit_is_enforced(self):
        with pytest.raises(dashboard_server.web.HTTPBadRequest) as exc:
            dashboard_server._validate_reaction_role_config(
                "dropdown", [{"role_id": str(i)} for i in range(26)], None
            )
        assert "25" in exc.value.reason


class _FakeDashboardRequest(dict):
    @property
    def match_info(self):
        return self["match_info"]

    @property
    def rel_url(self):
        return SimpleNamespace(query=self.get("query", {}))


class _FakeFxRequest(_FakeDashboardRequest):
    def __init__(self, payload, *, match_info=None, session=None, headers=None, remote="127.0.0.1"):
        super().__init__({"match_info": match_info or {}, "session": session or {"dev": True, "user_id": "owner"}})
        self._body = json.dumps(payload).encode()
        self.headers = headers or {}
        self.remote = remote

    @property
    def content_length(self):
        return len(self._body)

    async def read(self):
        return self._body

    async def json(self):
        return json.loads(self._body)


def _sqlite_fetchone(path, query, params=()):
    """Fetch one test row without leaving a Windows file handle open."""
    connection = sqlite3.connect(path)
    try:
        return connection.execute(query, params).fetchone()
    finally:
        connection.close()


class _FakeAuditRequest(dict):
    def __init__(self, *, session, guild_id="123", method="POST", path=None, query=None):
        super().__init__({"session": session, "match_info": {"guild_id": guild_id}, "query": query or {}})
        self.method = method
        self.path = path or f"/api/guild/{guild_id}/settings"

    @property
    def match_info(self):
        return self["match_info"]

    @property
    def rel_url(self):
        return SimpleNamespace(query=self.get("query", {}))


class TestDashboardChangeAudit:
    @pytest.mark.asyncio
    async def test_successful_server_manager_change_records_safe_metadata(self, db, monkeypatch):
        monkeypatch.setattr(dashboard_server, "DB_PATH", db.db_path)
        request = _FakeAuditRequest(
            session={"user_id": "42", "username": "mod", "guilds": [{"id": "123"}]},
            path="/api/guild/123/broadcaster/triggers",
        )
        request["audit_action"] = "Added reward trigger"
        request["audit_detail"] = {"reward_id": "reward-1", "trigger_type": "fx"}

        async def handler(_request):
            return dashboard_server.web.json_response({"ok": True})

        await dashboard_server.admin_audit_middleware(request, handler)
        row = _sqlite_fetchone(
            db.db_path,
            "SELECT actor_id, actor_username, actor_role, guild_id, action, detail "
            "FROM dashboard_change_log",
        )
        assert row[:5] == ("42", "mod", "server_manager", "123", "Added reward trigger")
        assert json.loads(row[5]) == {"reward_id": "reward-1", "trigger_type": "fx"}

    @pytest.mark.asyncio
    async def test_global_dashboard_change_is_recorded_without_a_guild_route(self, db, monkeypatch):
        monkeypatch.setattr(dashboard_server, "DB_PATH", db.db_path)
        request = _FakeAuditRequest(
            session={"dev": True, "user_id": "1", "username": "owner"},
            guild_id="",
            path="/api/dev/bot-statuses",
        )

        async def handler(_request):
            return dashboard_server.web.json_response({"ok": True})

        await dashboard_server.admin_audit_middleware(request, handler)
        row = _sqlite_fetchone(
            db.db_path,
            "SELECT actor_role, guild_id, guild_name, action FROM dashboard_change_log",
        )
        assert row == ("bot_owner", "global", "Global dashboard", "Changed bot statuses")

    @pytest.mark.asyncio
    async def test_unauthenticated_automatic_api_activity_is_not_recorded(self, db, monkeypatch):
        monkeypatch.setattr(dashboard_server, "DB_PATH", db.db_path)
        request = _FakeAuditRequest(
            session={}, guild_id="", path="/api/eventsub/callback",
        )
        request.pop("session")

        async def handler(_request):
            return dashboard_server.web.Response(status=204)

        await dashboard_server.admin_audit_middleware(request, handler)
        assert _sqlite_fetchone(
            db.db_path, "SELECT COUNT(*) FROM dashboard_change_log"
        )[0] == 0

    @pytest.mark.asyncio
    async def test_private_log_filters_by_server_and_requires_admin(self, db, monkeypatch):
        monkeypatch.setattr(dashboard_server, "DB_PATH", db.db_path)
        monkeypatch.setattr(dashboard_server, "_bot_ref", None)
        conn = sqlite3.connect(db.db_path)
        conn.executemany(
            """INSERT INTO dashboard_change_log
               (actor_id, actor_username, actor_role, guild_id, method, endpoint, action)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                ("1", "owner", "bot_owner", "123", "POST", "/one", "Changed one"),
                ("2", "admin", "dashboard_admin", "456", "DELETE", "/two", "Deleted two"),
            ],
        )
        conn.commit(); conn.close()

        request = _FakeAuditRequest(
            session={"admin": True, "user_id": "2", "username": "admin"},
            query={"guild_id": "456", "limit": "100", "offset": "0"},
        )
        response = await dashboard_server.admin_audit_log(request)
        payload = json.loads(response.body)
        assert payload["total"] == 1
        assert payload["entries"][0]["guild_id"] == "456"

        denied = _FakeAuditRequest(
            session={"user_id": "3", "username": "manager"},
            query={},
        )
        with pytest.raises(dashboard_server.web.HTTPForbidden):
            await dashboard_server.admin_audit_log(denied)


class TestFxPairingAndIsolation:
    @pytest.mark.asyncio
    async def test_pairing_code_is_one_time(self, db, monkeypatch):
        from excelprotocol_fx import hash_secret, make_pairing_code

        monkeypatch.setattr(dashboard_server, "DB_PATH", db.db_path)
        monkeypatch.setattr(dashboard_server, "FX_CREDENTIAL_PEPPER", "test-fx-pepper")
        code = make_pairing_code()
        conn = sqlite3.connect(db.db_path)
        conn.execute(
            """INSERT INTO fx_pairing_codes
               (guild_id, created_by_user_id, device_name, code_hash, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            ("guild-a", "owner", "Dashboard Studio", hash_secret(code, "test-fx-pepper"),
             (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()),
        )
        conn.commit()
        conn.close()
        request = _FakeFxRequest({
            "pairing_code": code, "device_name": "Studio PC",
            "plugin_version": "1.1.0", "obs_version": "31.0",
        })
        response = await dashboard_server.pair_fx_device(request)
        payload = json.loads(response.body)
        assert response.status == 201
        assert payload["device_credential"].startswith("fxd_")
        stored = _sqlite_fetchone(
            db.db_path,
            "SELECT credential_hash, revoked_at, name FROM fx_devices WHERE id = ?",
            (payload["device_id"],),
        )
        assert stored[0] != payload["device_credential"]
        assert stored[1] is None
        assert stored[2] == "Dashboard Studio"
        with pytest.raises(dashboard_server.web.HTTPBadRequest):
            await dashboard_server.pair_fx_device(request)

    @pytest.mark.asyncio
    async def test_expired_pairing_code_is_rejected(self, db, monkeypatch):
        from excelprotocol_fx import hash_secret, make_pairing_code

        monkeypatch.setattr(dashboard_server, "DB_PATH", db.db_path)
        monkeypatch.setattr(dashboard_server, "FX_CREDENTIAL_PEPPER", "test-fx-pepper")
        code = make_pairing_code()
        conn = sqlite3.connect(db.db_path)
        conn.execute(
            """INSERT INTO fx_pairing_codes
               (guild_id, created_by_user_id, code_hash, expires_at)
               VALUES (?, ?, ?, ?)""",
            ("guild-a", "owner", hash_secret(code, "test-fx-pepper"),
             (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()),
        )
        conn.commit()
        conn.close()
        with pytest.raises(dashboard_server.web.HTTPBadRequest):
            await dashboard_server.pair_fx_device(_FakeFxRequest({"pairing_code": code, "device_name": "Old"}))

    @pytest.mark.asyncio
    async def test_tenant_cannot_rename_or_revoke_another_guild_device(self, db, monkeypatch):
        monkeypatch.setattr(dashboard_server, "DB_PATH", db.db_path)
        conn = sqlite3.connect(db.db_path)
        conn.execute(
            """INSERT INTO fx_devices
               (id, guild_id, paired_by_user_id, name, credential_hash)
               VALUES ('device-b', 'guild-b', 'owner-b', 'Other OBS', 'hash-b')"""
        )
        conn.commit()
        conn.close()
        request = _FakeFxRequest(
            {"name": "Stolen"}, match_info={"guild_id": "guild-a", "device_id": "device-b"},
        )
        with pytest.raises(dashboard_server.web.HTTPNotFound):
            await dashboard_server.rename_fx_device(request)
        with pytest.raises(dashboard_server.web.HTTPNotFound):
            await dashboard_server.revoke_fx_device(request)

    @pytest.mark.asyncio
    async def test_revocation_invalidates_device_record(self, db, monkeypatch):
        monkeypatch.setattr(dashboard_server, "DB_PATH", db.db_path)
        conn = sqlite3.connect(db.db_path)
        conn.execute(
            """INSERT INTO fx_devices
               (id, guild_id, paired_by_user_id, name, credential_hash)
               VALUES ('device-a', 'guild-a', 'owner-a', 'Studio OBS', 'hash-a')"""
        )
        conn.commit()
        conn.close()
        request = _FakeFxRequest(
            {}, match_info={"guild_id": "guild-a", "device_id": "device-a"},
        )
        response = await dashboard_server.revoke_fx_device(request)
        assert response.status == 200
        revoked_at = _sqlite_fetchone(
            db.db_path,
            "SELECT revoked_at FROM fx_devices WHERE id = 'device-a'"
        )[0]
        assert revoked_at is not None

    @pytest.mark.asyncio
    async def test_fx_reward_trigger_is_tenant_scoped_and_persisted(self, db, monkeypatch):
        monkeypatch.setattr(dashboard_server, "DB_PATH", db.db_path)
        conn = sqlite3.connect(db.db_path)
        conn.execute(
            """INSERT INTO fx_devices
               (id, guild_id, paired_by_user_id, name, credential_hash)
               VALUES ('device-a', 'guild-a', 'owner-a', 'Studio OBS', 'hash-a')"""
        )
        conn.commit()
        conn.close()
        payload = {
            "reward_id": "reward-1", "reward_title": "Fog me",
            "trigger_type": "fx", "fx_device_id": "device-a",
            "fx_effect_id": "fog", "fx_duration_ms": 8000,
            "fx_intensity": 0.7, "fx_parameters": {}, "fx_queue": True,
        }
        response = await dashboard_server.upsert_reward_trigger(
            _FakeFxRequest(payload, match_info={"guild_id": "guild-a"})
        )
        assert response.status == 200
        stored = _sqlite_fetchone(
            db.db_path,
            """SELECT trigger_type, fx_device_id, fx_effect_id, fx_duration_ms,
                      fx_intensity, fx_parameters_json, fx_queue, video_url
               FROM reward_triggers WHERE guild_id = 'guild-a' AND reward_id = 'reward-1'"""
        )
        assert stored == ("fx", "device-a", "fog", 8000, 0.7, "{}", 1, "")

        payload["fx_device_id"] = "device-from-another-guild"
        with pytest.raises(dashboard_server.web.HTTPBadRequest):
            await dashboard_server.upsert_reward_trigger(
                _FakeFxRequest(payload, match_info={"guild_id": "guild-a"})
            )

    @pytest.mark.asyncio
    async def test_reward_redemption_dispatches_correlated_fx_command(self, monkeypatch):
        sent = []
        finished = []

        async def reserve(*_args):
            return 42

        async def finish(audit_id, status):
            finished.append((audit_id, status))

        async def send(device_id, command):
            sent.append((device_id, command))
            return {"ok": True, "status": "triggered", "event_id": command["event_id"]}

        monkeypatch.setattr(dashboard_server, "_fx_reserve_audit", reserve)
        monkeypatch.setattr(dashboard_server, "_fx_finish_audit", finish)
        monkeypatch.setattr(dashboard_server.FX_REGISTRY, "send_command", send)
        await dashboard_server._dispatch_fx_reward("guild-a", {
            "fx_device_id": "device-a", "fx_effect_id": "fog",
            "fx_duration_ms": 5000, "fx_intensity": 0.5,
            "fx_parameters_json": "{}", "fx_queue": 0,
        }, "redemption-1")
        assert sent[0][0] == "device-a"
        assert sent[0][1] == {
            "type": "trigger_effect", "event_id": "redemption-1",
            "effect": "fog", "duration_ms": 5000, "intensity": 0.5,
            "parameters": {}, "queue": False,
        }
        assert finished == [(42, "triggered")]


class TestFxRaidBattleIntegration:
    def test_role_precedence_matches_twitch_badges(self):
        base = {"chatter_user_id": "viewer", "broadcaster_user_id": "channel"}
        assert dashboard_server._fx_chat_role({**base, "badges": []}) == "default"
        assert dashboard_server._fx_chat_role({**base, "badges": [{"set_id": "subscriber"}]}) == "sub"
        assert dashboard_server._fx_chat_role({**base, "badges": [{"set_id": "subscriber"}, {"set_id": "vip"}]}) == "vip"
        assert dashboard_server._fx_chat_role({**base, "badges": [{"set_id": "vip"}, {"set_id": "moderator"}]}) == "mod"
        assert dashboard_server._fx_chat_role({**base, "chatter_user_id": "channel"}) == "mod"

    def test_chat_readiness_requires_both_cloud_bot_grants(self, monkeypatch):
        monkeypatch.setattr(dashboard_server, "_bot_ref", SimpleNamespace(
            _twitch_bot_scopes={"user:read:chat", "user:bot"},
        ))
        ready, reason = dashboard_server._fx_raid_chat_ready({"scopes_json": '["channel:bot"]'})
        assert ready and reason is None
        ready, reason = dashboard_server._fx_raid_chat_ready({"scopes_json": "[]"})
        assert not ready and "channel:bot" in reason

    @pytest.mark.asyncio
    async def test_chat_is_not_forwarded_without_active_fight(self, monkeypatch):
        sent = []

        async def fetch(_query, _params=()):
            return [{"guild_id": "guild-a"}]

        async def send(*args):
            sent.append(args)

        monkeypatch.setattr(dashboard_server, "db_fetch", fetch)
        monkeypatch.setattr(dashboard_server.FX_REGISTRY, "send_command", send)
        monkeypatch.setattr(dashboard_server, "_bot_ref", SimpleNamespace(_twitch_bot_user_id="bot"))
        dashboard_server._fx_raid_sessions.clear()
        await dashboard_server._fx_handle_raid_chat({
            "broadcaster_user_id": "channel", "message_id": "message-1",
            "chatter_user_id": "viewer", "chatter_user_name": "Viewer",
            "message": {"text": "hello"}, "badges": [],
        })
        assert sent == []

    def test_eventsub_registration_and_callback_cover_raids_and_chat(self):
        register = inspect.getsource(dashboard_server._register_eventsub)
        callback = inspect.getsource(dashboard_server.eventsub_callback)
        assert '"channel.raid"' in register
        assert '"to_broadcaster_user_id"' in register
        assert '"channel.chat.message"' in register
        assert '"user_id": bot_user_id' in register
        assert "_fx_handle_twitch_raid" in callback
        assert "_fx_handle_raid_chat" in callback

    def test_fx_page_is_a_real_menu_item_with_installer_and_live_test(self):
        source = (Path(__file__).parent.parent / "dashboard" / "src" / "App.jsx").read_text(encoding="utf-8")
        assert 'id:"fx"' in source
        assert 'activeTab==="fx"' in source
        assert "Download Installer" in source
        assert "Start Live Test" in source
        assert '`/api/guild/${guildId}/fx/raid`' in source


class TestWelcomePreview:
    @pytest.mark.asyncio
    async def test_renders_png_for_accessible_guild(self, monkeypatch):
        class FakeGuild:
            name = "Preview Server"

            @staticmethod
            def get_member(_user_id):
                return None

        class FakeBot:
            @staticmethod
            def get_guild(guild_id):
                return FakeGuild() if guild_id == 100 else None

        async def fake_fetch(query, params=()):
            assert params == (100,)
            if "welcome_settings" in query:
                return [{"welcome_message": "Hello {user}", "goodbye_message": None}]
            if "server_settings" in query:
                return [{"embed_color": 0x123456}]
            raise AssertionError(f"Unexpected query: {query}")

        async def fake_render(**kwargs):
            assert kwargs["username"] == "previewer"
            assert kwargs["server_name"] == "Preview Server"
            assert kwargs["accent_color"] == 0x123456
            assert kwargs["action"] == "welcome"
            assert kwargs["custom_message"] == "Hello {user}"
            return b"\x89PNG\r\n\x1a\npreview"

        monkeypatch.setattr(dashboard_server, "_bot_ref", FakeBot())
        monkeypatch.setattr(dashboard_server, "db_fetch", fake_fetch)
        monkeypatch.setattr("welcome_banner.render_welcome_banner", fake_render)
        request = _FakeDashboardRequest({
            "session": {"user_id": "42", "username": "previewer", "avatar": None},
            "match_info": {"guild_id": "100"},
            "query": {"action": "welcome"},
        })

        response = await dashboard_server.get_welcome_preview(request)

        assert response.content_type == "image/png"
        assert response.body.startswith(b"\x89PNG")
        assert response.headers["Cache-Control"] == "no-store"

    @pytest.mark.asyncio
    async def test_rejects_unknown_preview_action(self):
        from aiohttp import web

        request = _FakeDashboardRequest({
            "session": {"user_id": "42", "username": "previewer"},
            "match_info": {"guild_id": "100"},
            "query": {"action": "surprise"},
        })
        with pytest.raises(web.HTTPBadRequest):
            await dashboard_server.get_welcome_preview(request)


# ── HMAC signature verification ───────────────────────────────────────────────

class TestVerifyEventsubSignature:
    """The HMAC verifier rejects tampered or wrongly-signed webhook payloads."""

    SECRET = b"top-secret-eventsub-key"
    MSG_ID = "abc-123"
    MSG_TIMESTAMP = "2024-01-15T12:34:56Z"
    BODY = b'{"event":"test"}'

    def _sign(self, secret, msg_id, ts, body):
        """Helper: produce a signature header value the way Twitch would."""
        message = (msg_id + ts + body.decode()).encode()
        return "sha256=" + _hmac.new(secret, message, _hashlib.sha256).hexdigest()

    def test_valid_signature_accepted(self):
        good_sig = self._sign(self.SECRET, self.MSG_ID, self.MSG_TIMESTAMP, self.BODY)
        assert dashboard_server.verify_eventsub_signature(
            self.SECRET, self.MSG_ID, self.MSG_TIMESTAMP, self.BODY, good_sig
        ) is True

    def test_wrong_secret_rejected(self):
        wrong_sig = self._sign(b"different-secret", self.MSG_ID, self.MSG_TIMESTAMP, self.BODY)
        assert dashboard_server.verify_eventsub_signature(
            self.SECRET, self.MSG_ID, self.MSG_TIMESTAMP, self.BODY, wrong_sig
        ) is False

    def test_tampered_body_rejected(self):
        good_sig = self._sign(self.SECRET, self.MSG_ID, self.MSG_TIMESTAMP, self.BODY)
        tampered = b'{"event":"injected"}'
        assert dashboard_server.verify_eventsub_signature(
            self.SECRET, self.MSG_ID, self.MSG_TIMESTAMP, tampered, good_sig
        ) is False

    def test_tampered_msg_id_rejected(self):
        good_sig = self._sign(self.SECRET, self.MSG_ID, self.MSG_TIMESTAMP, self.BODY)
        assert dashboard_server.verify_eventsub_signature(
            self.SECRET, "different-id", self.MSG_TIMESTAMP, self.BODY, good_sig
        ) is False

    def test_tampered_timestamp_rejected(self):
        good_sig = self._sign(self.SECRET, self.MSG_ID, self.MSG_TIMESTAMP, self.BODY)
        assert dashboard_server.verify_eventsub_signature(
            self.SECRET, self.MSG_ID, "1999-01-01T00:00:00Z", self.BODY, good_sig
        ) is False

    def test_empty_signature_rejected(self):
        assert dashboard_server.verify_eventsub_signature(
            self.SECRET, self.MSG_ID, self.MSG_TIMESTAMP, self.BODY, ""
        ) is False

    def test_malformed_signature_rejected(self):
        # Missing sha256= prefix
        assert dashboard_server.verify_eventsub_signature(
            self.SECRET, self.MSG_ID, self.MSG_TIMESTAMP, self.BODY, "abcdef"
        ) is False

    def test_uses_constant_time_compare(self):
        """Confirm we're using hmac.compare_digest, not == (timing-attack resistant).

        Indirect test: we just confirm the function uses the imported hmac module,
        which we trust to use compare_digest. This is checked at the source level
        in the actual code path.
        """
        import inspect
        src = inspect.getsource(dashboard_server.verify_eventsub_signature)
        assert "compare_digest" in src, \
            "verify_eventsub_signature should use hmac.compare_digest for constant-time comparison"


# ── OAuth state TTL ───────────────────────────────────────────────────────────

class TestPruneOauthStates:
    """The OAuth state store must drop entries older than 10 minutes."""

    def test_recent_states_kept(self):
        states = {"recent": 1000.0, "also-recent": 1000.0}
        # "now" = 1300 (5 min later, well within 10-min TTL)
        dashboard_server._prune_oauth_states(states, ttl_seconds=600, now=1300.0)
        assert "recent" in states
        assert "also-recent" in states

    def test_expired_states_dropped(self):
        states = {"old": 1000.0}
        # "now" = 2000 (16 min later, past 10-min TTL)
        n_dropped = dashboard_server._prune_oauth_states(states, ttl_seconds=600, now=2000.0)
        assert "old" not in states
        assert n_dropped == 1

    def test_mixed_states(self):
        states = {"old1": 1000.0, "old2": 1100.0, "fresh": 1900.0}
        # "now" = 1950
        dashboard_server._prune_oauth_states(states, ttl_seconds=600, now=1950.0)
        # cutoff = 1950 - 600 = 1350; old1 (1000) and old2 (1100) are stale
        assert "old1" not in states
        assert "old2" not in states
        assert "fresh" in states

    def test_at_cutoff_boundary(self):
        # Exactly at the boundary should be kept (strictly less than cutoff is dropped)
        states = {"borderline": 1000.0}
        dashboard_server._prune_oauth_states(states, ttl_seconds=600, now=1600.0)
        assert "borderline" in states  # 1000 == cutoff (1600 - 600), kept

    def test_empty_dict_doesnt_crash(self):
        states = {}
        n = dashboard_server._prune_oauth_states(states, ttl_seconds=600, now=1000.0)
        assert n == 0


# ── _session_can_access_guild ─────────────────────────────────────────────────

class TestSessionCanAccessGuild:
    """Per-guild session access: only members of that guild may read its data."""

    def test_dev_token_full_access(self):
        session = {"dev": True, "guilds": []}
        assert dashboard_server._session_can_access_guild(session, "12345") is True

    def test_member_can_access(self):
        session = {"guilds": [{"id": "100"}, {"id": "200"}]}
        assert dashboard_server._session_can_access_guild(session, "100") is True
        assert dashboard_server._session_can_access_guild(session, "200") is True

    def test_non_member_denied(self):
        session = {"guilds": [{"id": "100"}]}
        assert dashboard_server._session_can_access_guild(session, "999") is False

    def test_string_int_normalisation(self):
        """Guild IDs may come as int or str — comparison should normalise."""
        session = {"guilds": [{"id": 100}]}  # int
        assert dashboard_server._session_can_access_guild(session, "100") is True
        # Other direction
        session = {"guilds": [{"id": "100"}]}  # str
        assert dashboard_server._session_can_access_guild(session, 100) is True

    def test_empty_guilds_denies(self):
        session = {"guilds": []}
        assert dashboard_server._session_can_access_guild(session, "100") is False

    def test_missing_guilds_field_denies(self):
        session = {}  # no guilds key at all
        assert dashboard_server._session_can_access_guild(session, "100") is False


class TestGuildFortunaAccess:
    class FakeRequest(dict):
        @property
        def match_info(self):
            return self["match_info"]

    def test_verified_server_manager_is_allowed(self):
        request = self.FakeRequest({
            "session": {"guilds": [{"id": "100", "can_manage": True}]},
            "match_info": {"guild_id": "100"},
        })
        assert dashboard_server._require_fortuna_guild_manager(request) == "100"

    def test_plain_member_flag_is_not_enough(self):
        from aiohttp import web

        request = self.FakeRequest({
            "session": {"guilds": [{"id": "100"}]},
            "match_info": {"guild_id": "100"},
        })
        with pytest.raises(web.HTTPForbidden):
            dashboard_server._require_fortuna_guild_manager(request)

    def test_manager_of_another_server_is_denied(self):
        from aiohttp import web

        request = self.FakeRequest({
            "session": {"guilds": [{"id": "200", "can_manage": True}]},
            "match_info": {"guild_id": "100"},
        })
        with pytest.raises(web.HTTPForbidden):
            dashboard_server._require_fortuna_guild_manager(request)

    def test_bot_owner_and_configured_admin_are_allowed(self):
        for role in ("dev", "admin"):
            request = self.FakeRequest({
                "session": {role: True, "guilds": []},
                "match_info": {"guild_id": "100"},
            })
            assert dashboard_server._require_fortuna_guild_manager(request) == "100"

    @pytest.mark.asyncio
    async def test_fortuna_resolves_only_the_server_broadcaster_link(self, monkeypatch):
        async def fake_fetch(query, params=()):
            assert "FROM broadcaster_tokens" in query
            assert params == ("100",)
            return [{
                "guild_id": 100,
                "twitch_broadcaster_id": "twitch-42",
                "twitch_broadcaster_login": "stayexcellent666",
            }]

        monkeypatch.setattr(dashboard_server, "db_fetch", fake_fetch)
        request = self.FakeRequest({
            "session": {"guilds": [{"id": "100", "can_manage": True}]},
            "match_info": {"guild_id": "100"},
        })
        channel = await dashboard_server._fortuna_guild_channel(request)
        assert channel["twitch_broadcaster_id"] == "twitch-42"

    @pytest.mark.asyncio
    async def test_key_creation_ignores_request_twitch_identity(self, monkeypatch):
        captured = {}

        async def fake_channel(_request, required=True):
            return {
                "twitch_broadcaster_id": "bound-42",
                "twitch_broadcaster_login": "bound_channel",
            }

        async def fake_insert(query, params=()):
            captured["query"] = query
            captured["params"] = params
            return 7

        class JsonRequest(self.FakeRequest):
            async def json(self):
                return {
                    "label": "Studio OBS",
                    "twitch_broadcaster_id": "attacker",
                    "twitch_broadcaster_login": "wrong_channel",
                }

        monkeypatch.setattr(dashboard_server, "_fortuna_guild_channel", fake_channel)
        monkeypatch.setattr(dashboard_server, "db_insert", fake_insert)
        request = JsonRequest({
            "session": {"guilds": [{"id": "100", "can_manage": True}]},
            "match_info": {"guild_id": "100"},
        })

        response = await dashboard_server.create_guild_fortuna_plugin_key(request)

        assert response.status == 201
        assert captured["params"][0:4] == (
            "100", "bound-42", "bound_channel", "Studio OBS",
        )
        assert "attacker" not in captured["params"]


class TestDevDashboardRoutes:
    def test_memory_diagnostics_reports_instance_and_cache_counts(self, monkeypatch):
        class FakeGuild:
            members = [object(), object(), object()]
            channels = [object(), object()]
            roles = [object()]

        class FakeBot:
            guilds = [FakeGuild()]
            cached_messages = [object(), object(), object(), object()]

        mib = 1024 * 1024
        monkeypatch.setattr(
            dashboard_server,
            "_cgroup_memory_usage",
            lambda: (128 * mib, 512 * mib),
        )

        payload = dashboard_server._memory_diagnostics(FakeBot())

        assert payload["instance"] == {
            "used_mb": 128.0,
            "available_mb": 384.0,
            "limit_mb": 512.0,
            "percent": 25.0,
            "source": "cgroup",
        }
        assert payload["discord"]["cached_members"] == 3
        assert payload["discord"]["cached_channels"] == 2
        assert payload["discord"]["cached_roles"] == 1
        assert payload["discord"]["cached_messages"] == 4

    """The developer panels must have matching backend endpoints."""

    def test_blacklist_and_stream_event_routes_registered(self, monkeypatch):
        # Static build paths can be inaccessible in restricted local test
        # sandboxes; they are unrelated to these API route assertions.
        monkeypatch.setattr(dashboard_server.os.path, "exists", lambda _path: False)
        app = dashboard_server.create_dashboard_app()
        routes = {
            (route.method, route.resource.canonical)
            for route in app.router.routes()
        }
        assert ("GET", "/api/dev/leaderboard-blacklist") in routes
        assert ("POST", "/api/dev/leaderboard-blacklist") in routes
        assert ("DELETE", "/api/dev/leaderboard-blacklist/{streamer_name}") in routes
        assert ("GET", "/api/dev/stream-events") in routes
        assert ("GET", "/api/dev/health-check") in routes
        assert ("GET", "/api/admin/fortuna") in routes
        assert ("GET", "/api/admin/fortuna/{giveaway_id}") in routes
        assert ("GET", "/api/admin/fortuna-control") in routes
        assert ("POST", "/api/admin/fortuna-control/start") in routes
        assert ("POST", "/api/admin/fortuna-control/spin") in routes
        assert ("POST", "/api/admin/fortuna-control/cancel") in routes
        assert ("GET", "/api/guild/{guild_id}/fortuna/control") in routes
        assert ("POST", "/api/guild/{guild_id}/fortuna/start") in routes
        assert ("POST", "/api/guild/{guild_id}/fortuna/spin") in routes
        assert ("POST", "/api/guild/{guild_id}/fortuna/cancel") in routes
        assert ("POST", "/api/guild/{guild_id}/fortuna/test") in routes
        assert ("GET", "/api/guild/{guild_id}/fortuna/history") in routes
        assert ("GET", "/api/guild/{guild_id}/fortuna/history/{giveaway_id}") in routes
        assert ("GET", "/api/guild/{guild_id}/fortuna/plugin-keys") in routes
        assert ("POST", "/api/guild/{guild_id}/fortuna/plugin-keys") in routes
        assert ("DELETE", "/api/guild/{guild_id}/fortuna/plugin-keys/{key_id}") in routes
        assert ("GET", "/api/guild/{guild_id}/fortuna/overlay") in routes
        assert ("PATCH", "/api/guild/{guild_id}/fortuna/overlay") in routes
        assert ("GET", "/fortuna-overlay/{token}") in routes
        assert ("GET", "/fortuna-overlay/{token}/ws") in routes
        assert ("GET", "/auth/twitch/bot/login") in routes
        assert ("GET", "/api/dev/bot-statuses") in routes
        assert ("POST", "/api/dev/bot-statuses") in routes
        assert ("POST", "/api/dev/twitch-chat/send") in routes
        assert ("GET", "/api/dev/server-info/{guild_id}") in routes
        assert ("PATCH", "/api/dev/server-info/{guild_id}/permission-dm") in routes
        assert ("POST", "/api/guild/{guild_id}/twitch/overlay-appearance") in routes
        assert ("POST", "/api/guild/{guild_id}/twitch/chat-sound") in routes
        assert ("POST", "/api/guild/{guild_id}/twitch/chat-sound-test") in routes
        assert ("GET", "/media/chat-sounds/{sound_name}.wav") in routes
        assert ("GET", "/api/dev/admins") in routes
        assert ("POST", "/api/dev/admins") in routes
        assert ("DELETE", "/api/dev/admins/{user_id}") in routes
        assert ("GET", "/api/admin/suggestions") in routes
        assert ("POST", "/api/admin/suggestions/{suggestion_id}/comments") in routes
        assert ("DELETE", "/api/admin/suggestions/{suggestion_id}") in routes
        assert ("POST", "/api/fx/pair") in routes
        assert ("GET", "/api/fx/ws") in routes
        assert ("GET", "/api/guild/{guild_id}/fx") in routes
        assert ("POST", "/api/guild/{guild_id}/fx/pairing-codes") in routes
        assert ("PATCH", "/api/guild/{guild_id}/fx/devices/{device_id}") in routes
        assert ("DELETE", "/api/guild/{guild_id}/fx/devices/{device_id}") in routes
        assert ("POST", "/api/guild/{guild_id}/fx/commands") in routes
        assert ("PUT", "/api/guild/{guild_id}/fx/settings") in routes

    def test_fortuna_overlay_layout_is_clamped_to_canvas(self):
        layout = dashboard_server._fortuna_normalize_overlay_layout({
            "reel": {"x": 95, "y": -5, "w": 50, "h": 10},
            "small_timer": {"x": "bad", "w": 2},
            "big_timer": {"x": 8, "y": 9, "w": 42, "h": 45},
        })
        assert layout["reel"] == {"x": 50.0, "y": 0.0, "w": 50.0, "h": 20.0}
        assert layout["small_timer"] == dashboard_server.FORTUNA_DEFAULT_OVERLAY_LAYOUT["small_timer"]
        assert layout["big_timer"] == {"x": 8.0, "y": 9.0, "w": 42.0, "h": 45.0}

    def test_fortuna_overlay_rejects_invalid_global_color_and_migrates_legacy_color(self):
        layout = dashboard_server._fortuna_normalize_overlay_layout({
            "color": "javascript:bad",
        })
        assert layout["color"] == "#28e5e1"
        migrated = dashboard_server._fortuna_normalize_overlay_layout({
            "small_timer": {"color": "#FF4D8D"},
        })
        assert migrated["color"] == "#ff4d8d"

    def test_twitch_login_normalisation(self):
        assert dashboard_server._normalise_twitch_login(" @Some_Streamer ") == "some_streamer"

    def test_invalid_twitch_login_rejected(self):
        import pytest
        from aiohttp import web
        with pytest.raises(web.HTTPBadRequest):
            dashboard_server._normalise_twitch_login("not a twitch login")

    def test_owner_and_admin_are_authorised(self):
        dashboard_server._require_dev_or_admin({"session": {"dev": True}})
        dashboard_server._require_dev_or_admin({"session": {"admin": True}})

    def test_regular_user_is_denied(self):
        import pytest
        from aiohttp import web
        with pytest.raises(web.HTTPForbidden):
            dashboard_server._require_dev_or_admin({"session": {}})

    def test_server_info_is_owner_only(self):
        from aiohttp import web
        dashboard_server._require_owner({"session": {"dev": True}})
        with pytest.raises(web.HTTPForbidden):
            dashboard_server._require_owner({"session": {"admin": True}})

    @pytest.mark.asyncio
    async def test_owner_twitch_console_joins_sends_and_audits(self, monkeypatch):
        import json

        sent = []
        joined = []
        writes = []

        class FakeChannel:
            name = "friendly_streamer"

            async def send(self, message):
                sent.append(message)

        class FakeChatBot:
            def __init__(self):
                self.connected_channels = []

            async def join_channels(self, channels):
                joined.extend(channels)
                self.connected_channels.append(FakeChannel())

            async def send_chat(self, _channel_name, message, channel):
                await channel.send(message)
                return "helix"

        class FakeBot:
            twitch_chat_bot = FakeChatBot()

        class Request(dict):
            async def json(self):
                return {
                    "channel": "@Friendly_Streamer",
                    "message": "Happy birthday! 🎉",
                }

        async def fake_execute(query, params=()):
            writes.append((query, params))

        monkeypatch.setattr(dashboard_server, "_bot_ref", FakeBot())
        monkeypatch.setattr(dashboard_server, "db_execute", fake_execute)
        monkeypatch.setattr(dashboard_server, "_twitch_chat_console_last_sent_at", 0.0)
        request = Request({
            "session": {"dev": True, "user_id": "42", "username": "Owner"},
        })

        response = await dashboard_server.owner_twitch_chat_send(request)
        payload = json.loads(response.text)

        assert joined == ["friendly_streamer"]
        assert sent == ["Happy birthday! 🎉"]
        assert payload["ok"] is True
        assert payload["joined_now"] is True
        assert payload["delivery"] == "helix"
        audit_params = writes[0][1]
        assert audit_params[:5] == (
            "42", "Owner", "twitch:friendly_streamer", "POST",
            "/api/dev/twitch-chat/send",
        )
        assert "Happy birthday!" in audit_params[5]

    @pytest.mark.asyncio
    async def test_twitch_console_is_owner_only_and_blocks_slash_commands(self, monkeypatch):
        from aiohttp import web

        class Request(dict):
            def __init__(self, session, message="hello"):
                super().__init__(session=session)
                self.message = message

            async def json(self):
                return {"channel": "streamer", "message": self.message}

        with pytest.raises(web.HTTPForbidden):
            await dashboard_server.owner_twitch_chat_send(
                Request({"admin": True, "user_id": "99"})
            )

        monkeypatch.setattr(
            dashboard_server,
            "_bot_ref",
            SimpleNamespace(twitch_chat_bot=SimpleNamespace()),
        )
        with pytest.raises(web.HTTPBadRequest, match="Slash commands are blocked"):
            await dashboard_server.owner_twitch_chat_send(
                Request({"dev": True}, "/ban somebody")
            )

    @pytest.mark.asyncio
    async def test_twitch_console_enforces_send_cooldown(self, monkeypatch):
        from aiohttp import web

        class FakeChannel:
            name = "streamer"

            async def send(self, _message):
                return None

        class Request(dict):
            async def json(self):
                return {"channel": "streamer", "message": "hello"}

        async def fake_execute(_query, _params=()):
            return None

        class FakeChatBot:
            connected_channels = [FakeChannel()]

            async def send_chat(self, _channel_name, message, channel):
                await channel.send(message)
                return "helix"

        monkeypatch.setattr(
            dashboard_server,
            "_bot_ref",
            SimpleNamespace(twitch_chat_bot=FakeChatBot()),
        )
        monkeypatch.setattr(dashboard_server, "db_execute", fake_execute)
        monkeypatch.setattr(dashboard_server, "_twitch_chat_console_last_sent_at", 0.0)
        request = Request(session={"dev": True, "user_id": "42", "username": "Owner"})

        await dashboard_server.owner_twitch_chat_send(request)
        with pytest.raises(web.HTTPTooManyRequests):
            await dashboard_server.owner_twitch_chat_send(request)

    @pytest.mark.asyncio
    async def test_dashboard_admin_add_applies_live_and_secret_fallback_cannot_be_removed(self, monkeypatch):
        from aiohttp import web
        executed = []

        async def fake_execute(query, params=()):
            executed.append((query, params))

        class Request(dict):
            match_info = {"user_id": "123456789012345678"}
            async def json(self):
                return {"user_id": "123456789012345678"}

        session = {"user_id": "123456789012345678", "admin": False}
        monkeypatch.setattr(dashboard_server, "_bot_ref", None)
        monkeypatch.setattr(dashboard_server, "_sessions", {"active": session})
        monkeypatch.setattr(dashboard_server, "db_execute", fake_execute)
        response = await dashboard_server.add_dashboard_admin(Request(session={"dev": True, "user_id": "1"}))
        assert response.status == 200
        assert session["admin"] is True
        assert executed[0][1][0] == "123456789012345678"

        monkeypatch.setattr(dashboard_server, "ADMIN_IDS", {"123456789012345678"})
        with pytest.raises(web.HTTPBadRequest):
            await dashboard_server.delete_dashboard_admin(Request(session={"dev": True}))

    @pytest.mark.asyncio
    async def test_suggestion_inbox_allows_admin_but_admin_manager_is_owner_only(self, monkeypatch):
        from aiohttp import web

        async def fake_fetch(query, params=()):
            return []

        monkeypatch.setattr(dashboard_server, "db_fetch", fake_fetch)
        response = await dashboard_server.get_suggestion_inbox({"session": {"admin": True}})
        assert response.status == 200
        with pytest.raises(web.HTTPForbidden):
            await dashboard_server.get_dashboard_admins({"session": {"admin": True}})

    @pytest.mark.asyncio
    async def test_suggestion_is_saved_even_when_dm_is_not_configured(self, monkeypatch):
        import json
        captured = {}

        async def fake_insert(query, params=()):
            captured["query"] = query
            captured["params"] = params
            return 12

        class Request(dict):
            async def json(self):
                return {"text": "Add a useful thing", "guild_id": "100"}

        monkeypatch.setattr(dashboard_server, "db_insert", fake_insert)
        monkeypatch.setattr(dashboard_server, "BOT_OWNER_ID", "")
        response = await dashboard_server.post_suggestion(
            Request(session={"user_id": "42", "username": "viewer"})
        )
        payload = json.loads(response.text)
        assert payload == {"ok": True, "id": 12, "dm_sent": False}
        assert captured["params"] == ("42", "viewer", "100", "Add a useful thing")

    @pytest.mark.asyncio
    async def test_server_info_returns_live_identity_and_configuration(self, monkeypatch):
        import json
        from datetime import datetime, timezone

        class Object:
            def __init__(self, **values):
                self.__dict__.update(values)

        permissions = Object(view_channel=True, send_messages=True, embed_links=True)
        bot_member = Object(
            joined_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
            guild_permissions=permissions,
            top_role=Object(id=20, name="ExcelProtocol", position=3),
        )
        owner = Object(id=9, name="owner_name", display_name="Owner", display_avatar=None)
        channel = Object(id=30, name="live-alerts")
        guild = Object(
            id=100, name="Test Server", icon=None, member_count=42,
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            owner_id=9, owner=owner, me=bot_member, roles=[1, 2, 3, 4],
            get_member=lambda user_id: owner if user_id == 9 else None,
            get_channel=lambda channel_id: channel if channel_id == 30 else None,
        )
        fake_database = Object(get_permission_dm_muted=lambda guild_id: guild_id == 100)
        fake_bot = Object(get_guild=lambda guild_id: guild if guild_id == 100 else None, db=fake_database)

        async def fake_fetch(query, params=()):
            if "FROM server_settings" in query:
                return [{"notification_channel_id": 30}]
            if "FROM notification_log" in query and "COUNT(*)" in query:
                return [{"total": 2, "sent": 2, "failed": 0}]
            if "COUNT(*) AS c" in query:
                return [{"c": 0}]
            return []

        class Request(dict):
            match_info = {"guild_id": "100"}

        monkeypatch.setattr(dashboard_server, "_bot_ref", fake_bot)
        monkeypatch.setattr(dashboard_server, "db_fetch", fake_fetch)
        monkeypatch.setattr(dashboard_server, "_sessions", {})
        response = await dashboard_server.get_owner_server_info(Request(session={"dev": True}))
        payload = json.loads(response.text)
        assert payload["server"]["name"] == "Test Server"
        assert payload["owner"]["username"] == "owner_name"
        assert payload["channels"]["Notification"][0]["name"] == "#live-alerts"
        assert payload["notifications"]["last_24h"] == {"total": 2, "sent": 2, "failed": 0}
        assert payload["alerts"]["permission_dm_muted"] is True

    @pytest.mark.asyncio
    async def test_permission_dm_mute_is_owner_only_and_persists(self, monkeypatch):
        from aiohttp import web

        class FakeDatabase:
            def __init__(self):
                self.saved = None
            def set_permission_dm_muted(self, guild_id, muted):
                self.saved = (guild_id, muted)

        class FakeBot:
            def __init__(self):
                self.db = FakeDatabase()
            def get_guild(self, guild_id):
                return object() if guild_id == 100 else None

        class Request(dict):
            match_info = {"guild_id": "100"}
            async def json(self):
                return {"muted": True}

        fake_bot = FakeBot()
        monkeypatch.setattr(dashboard_server, "_bot_ref", fake_bot)
        with pytest.raises(web.HTTPForbidden):
            await dashboard_server.set_owner_permission_dm_mute(Request(session={"admin": True}))
        response = await dashboard_server.set_owner_permission_dm_mute(Request(session={"dev": True}))
        assert response.status == 200
        assert fake_bot.db.saved == (100, True)

    @pytest.mark.asyncio
    async def test_notification_pause_is_owner_only_preserves_config_and_resyncs(self, monkeypatch):
        import asyncio
        from aiohttp import web

        calls = []

        class FakeDatabase:
            def set_notifications_paused(self, guild_id, paused):
                calls.append(("save", guild_id, paused))

        class FakeBot:
            db = FakeDatabase()
            error_alerts_sent = {"perm_issue:100": object()}

            def get_guild(self, guild_id):
                return object() if guild_id == 100 else None

            async def _sync_eventsub_subscriptions(self, alert_on_mismatch=True):
                calls.append(("sync", alert_on_mismatch))

        class Request(dict):
            match_info = {"guild_id": "100"}
            async def json(self):
                return {"paused": True}

        async def fake_execute(query, params=()):
            calls.append(("db", query, params))

        fake_bot = FakeBot()
        monkeypatch.setattr(dashboard_server, "_bot_ref", fake_bot)
        monkeypatch.setattr(dashboard_server, "db_execute", fake_execute)

        with pytest.raises(web.HTTPForbidden):
            await dashboard_server.set_server_notifications_paused(Request(session={"admin": True}))

        response = await dashboard_server.set_server_notifications_paused(Request(session={"dev": True}))
        await asyncio.sleep(0)

        assert response.status == 200
        assert ("save", 100, True) in calls
        assert any(call[0] == "db" and "DELETE FROM permission_issues" in call[1] for call in calls)
        assert ("sync", False) in calls

    @pytest.mark.asyncio
    async def test_paused_notification_delivery_returns_before_discord_work(self):
        import bot

        class FakeDatabase:
            @staticmethod
            def get_notifications_paused(guild_id):
                assert guild_id == 100
                return True

        fake = type("Fake", (), {"db": FakeDatabase()})()
        await bot.TwitchNotifierBot.send_notification(
            fake,
            {"guild_id": 100, "channel_id": 200},
            {"user_login": "alice"},
        )

    @pytest.mark.asyncio
    async def test_twitch_bot_login_is_owner_only(self, monkeypatch):
        from aiohttp import web
        monkeypatch.setattr(dashboard_server, "get_session", lambda request: {"admin": True})
        with pytest.raises(web.HTTPForbidden):
            await dashboard_server.twitch_bot_login({"cookies": {}})

    def test_twitch_connect_button_requires_owner_view(self):
        from pathlib import Path
        source = (Path(__file__).parents[1] / "dashboard" / "src" / "App.jsx").read_text(encoding="utf-8")
        assert "ownerView && data.twitch_chat_token?.owner_can_connect" in source
        assert "<HealthCheckTab ownerView={effectivelyDev} />" in source

    @pytest.mark.asyncio
    async def test_bot_status_settings_are_owner_only_and_apply_live(self, monkeypatch):
        from aiohttp import web

        class FakeDatabase:
            def __init__(self):
                self.saved = None

            def set_bot_statuses(self, statuses):
                self.saved = statuses

        class FakeBot:
            def __init__(self):
                self.db = FakeDatabase()
                self._bot_statuses = []
                self._bot_status_index = 5
                self.applied = False

            async def apply_configured_status(self):
                self.applied = True
                self._bot_status_index = 1

        class FakeRequest(dict):
            async def json(self):
                return {"statuses": [{"type": "watching", "text": "the community"}]}

        bot = FakeBot()
        monkeypatch.setattr(dashboard_server, "_bot_ref", bot)
        with pytest.raises(web.HTTPForbidden):
            await dashboard_server.set_bot_statuses(FakeRequest(session={"admin": True}))

        response = await dashboard_server.set_bot_statuses(FakeRequest(session={"dev": True}))
        assert response.status == 200
        assert bot.db.saved == [{"type": "watching", "text": "the community"}]
        assert bot._bot_status_index == 1
        assert bot.applied is True

    @pytest.mark.asyncio
    async def test_admin_global_stats_includes_all_leaderboards(self, monkeypatch):
        import json

        class FakeDatabase:
            def __init__(self):
                self.sorts = []

            def get_global_leaderboard(self, limit, sort_by):
                self.sorts.append((limit, sort_by))
                return [{"streamer_name": sort_by, "total_streams": 1,
                         "server_count": 1, "hours_streamed": 2.0,
                         "longest_hours": 2.0}]

        class FakeTwitch:
            async def get_subscriptions(self):
                return []

        class FakeBot:
            def __init__(self):
                self.db = FakeDatabase()
                self.twitch = FakeTwitch()
                self.live_streamers = set()

        async def fake_db_fetch(query, params=()):
            if query.lstrip().startswith("SELECT COUNT"):
                return [{"c": 0}]
            return []

        fake_bot = FakeBot()
        monkeypatch.setattr(dashboard_server, "_bot_ref", fake_bot)
        monkeypatch.setattr(dashboard_server, "db_fetch", fake_db_fetch)

        response = await dashboard_server.get_global_stats(
            {"session": {"admin": True}}
        )
        payload = json.loads(response.text)

        assert payload["global_leaderboard_consistency"][0]["streamer_name"] == "consistency"
        assert payload["global_leaderboard_hours"][0]["streamer_name"] == "hours"
        assert payload["global_leaderboard_longest"][0]["streamer_name"] == "longest"
        assert fake_bot.db.sorts == [
            (15, "consistency"), (15, "hours"), (15, "longest")
        ]

    @pytest.mark.asyncio
    async def test_admin_health_check_returns_read_only_snapshot(self, monkeypatch):
        import json
        from datetime import datetime, timedelta, timezone

        class FakeLoop:
            current_loop = 3
            _last_iteration = datetime.now(timezone.utc) - timedelta(minutes=5)
            next_iteration = datetime.now(timezone.utc) + timedelta(minutes=10)
            def is_running(self): return True
            def failed(self): return False

        class FakeTask:
            def done(self): return False

        class FakeBot:
            def __init__(self):
                self.start_time = datetime.now(timezone.utc) - timedelta(hours=2)
                self.latency = 0.042
                self.guilds = [object(), object()]
                self.live_streamers = {"one"}
                self._recent_orphan_closures = []
                self._eventsub_sync_stats = {"expected": 4, "actual": 4, "failed": 0}
                self._eventsub_last_success_at = datetime.now(timezone.utc)
                self._last_reconcile_at = datetime.now(timezone.utc)
                self._last_reconcile_counts = {"cleanup": 1, "no_action": 1}
                self._twitch_bot_token = "token"
                self._twitch_chat_state = "connected"
                self._twitch_chat_task = FakeTask()
                self._twitch_chat_supervisor_task = FakeTask()
                self._twitch_chat_restart_count = 1
                self._twitch_chat_last_ready_at = datetime.now(timezone.utc)
                self._twitch_chat_last_activity_at = datetime.now(timezone.utc)
                self.twitch_chat_bot = object()
                for name in (
                    "check_streams", "check_milestones", "cleanup_channels",
                    "monthly_leaderboard_cleanup", "poll_live_streamers_health",
                    "rotate_status", "refresh_broadcaster_tokens", "check_permissions",
                    "update_stat_channels", "check_streamer_renames", "cleanup_kicked_guilds",
                ):
                    setattr(self, name, FakeLoop())
            def is_ready(self): return True
            def is_closed(self): return False
            def _registered_twitch_chat_channels(self): return ["one", "two"]
            def _twitch_chat_joined_count(self, _chat_bot=None): return 2

        async def fake_db_fetch(query, params=()):
            if "permission_issues" in query or "status != 'sent'" in query:
                return [{"c": 0}]
            if "notification_messages" in query:
                return [{"c": 1}]
            if "COUNT(DISTINCT streamer_name)" in query:
                return [{"c": 2}]
            return [{"c": 3}]

        monkeypatch.setattr(dashboard_server, "_bot_ref", FakeBot())
        monkeypatch.setattr(dashboard_server, "db_fetch", fake_db_fetch)
        response = await dashboard_server.get_operations_health(
            {"session": {"admin": True}}
        )
        payload = json.loads(response.text)
        assert payload["status"] == "healthy"
        assert payload["bot"]["ready"] is True
        assert payload["memory"]["process"]["rss_mb"] >= 0
        assert payload["memory"]["instance"]["limit_mb"] > 0
        assert payload["memory"]["discord"]["guilds"] == 2
        assert "overlay_websockets" in payload["memory"]["connections"]
        assert payload["eventsub"]["actual"] == 4
        assert payload["twitch_chat"]["connected"] is True
        assert payload["twitch_chat"]["joined_channels"] == 2
        assert payload["twitch_chat"]["restart_count"] == 1
        assert payload["streaming"]["unique_streamers"] == 2
        assert len(payload["tasks"]) == 11

    @pytest.mark.asyncio
    async def test_health_check_rejects_regular_user(self):
        from aiohttp import web
        with pytest.raises(web.HTTPForbidden):
            await dashboard_server.get_operations_health({"session": {}})

    @pytest.mark.asyncio
    async def test_fortuna_history_returns_admin_summary(self, monkeypatch):
        import json

        async def fake_db_fetch(query, params=()):
            assert "datetime('now', '-30 days')" in query
            assert params == ()
            return [{
                "id": 7,
                "twitch_broadcaster_id": "100",
                "twitch_broadcaster_login": "stayexcellent666",
                "reward_id": "reward-1",
                "reward_title": "Giveaway Entry",
                "title": "Launch Giveaway",
                "status": "completed",
                "started_at": "2026-09-07 12:00:00",
                "ended_at": "2026-09-07 12:10:00",
                "winner_twitch_user_id": "viewer-1",
                "winner_twitch_login": "viewer",
                "winner_display_name": "Viewer",
                "winner_selected_at": "2026-09-07 12:10:00",
                "total_entries": 4,
                "eligible_entries": 3,
                "unique_entrants": 3,
                "runtime_seconds": 600,
            }]

        monkeypatch.setattr(dashboard_server, "db_fetch", fake_db_fetch)
        response = await dashboard_server.get_fortuna_history(
            {"session": {"admin": True}}
        )
        payload = json.loads(response.text)

        assert response.headers["Cache-Control"] == "no-store"
        assert payload["window_days"] == 30
        assert payload["summary"] == {
            "giveaways": 1,
            "total_entries": 4,
            "eligible_entries": 3,
            "winners": 1,
            "average_runtime_seconds": 600,
        }
        assert payload["giveaways"][0]["winner_display_name"] == "Viewer"

    @pytest.mark.asyncio
    async def test_fortuna_detail_returns_all_entries(self, monkeypatch):
        import json

        async def fake_db_fetch(query, params=()):
            assert params == (7,)
            if "FROM fortuna_giveaways" in query:
                return [{"id": 7, "title": "Launch Giveaway", "total_entries": 2}]
            assert "FROM fortuna_entries" in query
            return [
                {"id": 1, "twitch_display_name": "One", "eligible": 1},
                {"id": 2, "twitch_display_name": "Two", "eligible": 0},
            ]

        class FakeRequest(dict):
            @property
            def match_info(self):
                return self["match_info"]

        monkeypatch.setattr(dashboard_server, "db_fetch", fake_db_fetch)
        response = await dashboard_server.get_fortuna_giveaway(FakeRequest({
            "session": {"dev": True},
            "match_info": {"giveaway_id": "7"},
        }))
        payload = json.loads(response.text)

        assert response.headers["Cache-Control"] == "no-store"
        assert payload["giveaway"]["id"] == 7
        assert [entry["twitch_display_name"] for entry in payload["entries"]] == ["One", "Two"]

    @pytest.mark.asyncio
    async def test_fortuna_routes_reject_regular_users(self):
        from aiohttp import web

        with pytest.raises(web.HTTPForbidden):
            await dashboard_server.get_fortuna_history({"session": {}})
        with pytest.raises(web.HTTPForbidden):
            await dashboard_server.get_fortuna_giveaway({
                "session": {},
                "match_info": {"giveaway_id": "1"},
            })
        with pytest.raises(web.HTTPForbidden):
            await dashboard_server.get_fortuna_control({"session": {}})

    def test_fortuna_frontend_uses_the_active_server_binding(self):
        from pathlib import Path

        source = (Path(__file__).parent.parent / "dashboard" / "src" / "App.jsx").read_text(encoding="utf-8")
        assert 'id:"fortuna"' in source
        assert 'activeTab==="fortuna"' in source
        assert '<FortunaTab guildId={activeGuild} />' in source
        assert '`/api/guild/${guildId}/fortuna/control`' in source
        assert '`/api/guild/${guildId}/fortuna/overlay`' in source
        assert "/api/admin/fortuna" not in source
        assert 'label:"Fortuna"' in source

    def test_dashboard_navigation_uses_reorganized_sections(self):
        from pathlib import Path

        source = (Path(__file__).parent.parent / "dashboard" / "src" / "App.jsx").read_text(encoding="utf-8")
        assert 'label="Overview"' in source
        assert 'label="Streaming & Twitch"' in source
        assert 'label="Server Settings"' in source
        assert '>Administration</div>' in source
        assert source.count('label="Suggestions & Support"') == 2
        assert 'label="Contact"' not in source

    def test_established_servers_use_guarded_configuration_assistant(self):
        from pathlib import Path

        source = (Path(__file__).parent.parent / "dashboard" / "src" / "App.jsx").read_text(encoding="utf-8")
        assert 'serverEstablished === false && <NavItem' in source
        assert 'id:"advancedsettings"' in source
        assert 'Configuration Assistant' in source
        assert 'It never deletes existing content' in source
        assert 'LIVE · ${obsSourceCount} ONLINE' in source
        assert "No plugin installation or pairing key is needed" in source
        assert "Run 15s Overlay Test" in source
        assert "GLOBAL OVERLAY COLOR" in source
        assert 'Download OBS Plugin' not in source
        assert '`/api/guild/${guildId}/fortuna/plugin-keys`' not in source
        overlay = (Path(__file__).parent.parent / "fortuna_overlay.html").read_text(encoding="utf-8")
        assert 'event.type==="layout"' in overlay

    def test_video_overlay_disables_captions_and_supports_appearance_settings(self):
        import inspect
        source = inspect.getsource(dashboard_server.overlay_page)
        assert "cc_load_policy: 0" in source
        assert 'setOption("captions", "track"' in source
        assert 'msg.type === "set_appearance"' in source
        assert 'classList.toggle("tv-frame"' in source

    def test_reward_videos_preempt_and_then_resume_command_videos(self):
        import inspect
        source = inspect.getsource(dashboard_server.overlay_page)
        push_source = inspect.getsource(dashboard_server.push_play_to_overlay)
        webhook_source = inspect.getsource(dashboard_server.eventsub_callback)

        assert '"source": "command"' in push_source
        assert '"source": "reward"' in webhook_source
        assert "const priorityQueue = [];" in source
        assert "function preemptCommandForReward()" in source
        assert "queue.unshift({{ ...currentItem, start_seconds: resumeAt }});" in source
        assert "priorityQueue.length ? priorityQueue.shift() : queue.shift()" in source

    def test_video_overlay_recovers_stale_socket_after_process_restart(self):
        import inspect

        page_source = inspect.getsource(dashboard_server.overlay_page)
        socket_source = inspect.getsource(dashboard_server.overlay_ws)

        assert 'payload.get("type") == "ping"' in socket_source
        assert '"type": "pong"' in socket_source
        assert 'SOCKET_STALE_AFTER_MS = 30000' in page_source
        assert 'replaceStaleSocket("Overlay socket heartbeat timed out; reconnecting")' in page_source
        assert 'socket.send(JSON.stringify({{ type: "ping"' in page_source
        assert '"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"' in page_source

    def test_overlay_chat_sound_is_independent_of_video_queue(self):
        import inspect
        from pathlib import Path

        source = inspect.getsource(dashboard_server.overlay_page)
        assert 'msg.type === "chat_notification"' in source
        assert "playChatNotification(msg.sound, msg.volume);" in source
        expected = {
            "soft_bong", "confirmation_chime", "glass_tap", "soft_pluck",
            "water_drop", "ui_select", "open_tone", "question_chime",
        }
        assert set(dashboard_server.CHAT_NOTIFICATION_SOUND_FILES) == expected
        for filename in dashboard_server.CHAT_NOTIFICATION_SOUND_FILES.values():
            assert (Path(dashboard_server.CHAT_NOTIFICATION_SOUND_DIR) / filename).is_file()
        keepalive = (
            Path(dashboard_server.CHAT_NOTIFICATION_SOUND_DIR)
            / dashboard_server.CHAT_NOTIFICATION_KEEPALIVE_FILE
        )
        assert keepalive.is_file()
        assert "/media/chat-sounds/" in source
        assert "new Audio(" in source
        assert 'audioElement.preload = "auto"' in source
        assert "await audioElement.play()" in source
        assert "AudioContext" not in source
        assert 'new Audio("/media/chat-sounds/_keepalive.wav")' in source
        assert "audioElement.loop = true" in source
        assert "startChatAudioKeepAlive();" in source
        assert "chatSoundNames.forEach(loadChatSound)" in source
        chat_playback = source.split(
            "async function playChatNotification", 1
        )[1].split("function formatTime", 1)[0]
        assert "priorityQueue" not in chat_playback
        assert "queue." not in chat_playback
        assert "playing =" not in chat_playback
        assert "player.stopVideo" not in chat_playback
        assert "player.setVolume" not in chat_playback

    def test_custom_command_editor_has_variable_buttons_and_live_preview(self):
        from pathlib import Path
        source = (Path(__file__).parent.parent / "dashboard" / "src" / "App.jsx").read_text(encoding="utf-8")
        assert "CUSTOM_COMMAND_VARIABLES" in source
        assert 'token:"$target"' in source
        assert 'token:"$args"' in source
        assert 'token:"$random(1,100)"' in source
        assert 'token:"$choice(yes|no|maybe)"' in source
        assert "LIVE PREVIEW" in source

    def test_landing_page_reflects_current_product_pillars(self):
        import inspect

        source = inspect.getsource(dashboard_server.landing_page)
        assert "Discord · Twitch · OBS · one command center" in source
        assert 'id="platform"' in source
        assert "ExcelFortuna" in source
        assert 'class="system-stage"' in source
        assert 'data-system="twitch"' in source
        assert "Commands and rewards feel native to your channel." in source
        assert 'id="reliability"' in source
        assert 'class="protocol-rail"' in source
        assert 'id="scroll-progress"' in source
        assert "IntersectionObserver" in source
        assert "prefers-reduced-motion:reduce" in source
        assert "commandExamples" in source
        assert "updateProtocolCore" in source
        assert 'class="core-logo" src="/app/protocol.png"' in source
        assert 'rel="icon" href="/app/protocol.png?v=2"' in source
        assert 'id="core-step"' in source
        assert 'id="feature-console"' in source
        assert 'id="console-reticle"' in source
        assert "setFeatureSignal" in source
        assert "The bot joins with all required permissions" not in source

    def test_legal_pages_disclose_current_data_and_retention(self):
        import inspect

        terms = inspect.getsource(dashboard_server.terms_page)
        privacy = inspect.getsource(dashboard_server.privacy_page)
        assert "October 21, 2026" in terms
        assert "Twitch chat commands, clip creation" in terms
        assert "seven-day re-invite grace period" in terms
        assert "October 21, 2026" in privacy
        assert "per-viewer Twitch ID or login and use count" in privacy
        assert "Giveaway configuration" in privacy
        assert "Automated Moderation" in privacy
        assert "current and previous calendar month" in privacy

    @pytest.mark.asyncio
    async def test_shared_fortuna_start_engine_broadcasts_state(self, monkeypatch):
        broadcasts = []
        scheduled = []

        async def no_open(_broadcaster_id):
            return None

        async def fake_insert(_query, params=()):
            assert params[0:2] == ("100", "stayexcellent666")
            return 42

        async def fake_broadcast(broadcaster_id, payload):
            broadcasts.append((broadcaster_id, payload))

        monkeypatch.setattr(dashboard_server, "_fortuna_open_giveaway", no_open)
        monkeypatch.setattr(dashboard_server, "db_insert", fake_insert)
        monkeypatch.setattr(dashboard_server, "_fortuna_broadcast", fake_broadcast)
        monkeypatch.setattr(
            dashboard_server, "_fortuna_schedule_deadline",
            lambda giveaway_id, seconds: scheduled.append((giveaway_id, seconds)),
        )
        monkeypatch.setattr(
            dashboard_server, "_fortuna_schedule_live_notice",
            lambda giveaway_id: scheduled.append((giveaway_id, "notice")),
        )

        result = await dashboard_server._fortuna_start_giveaway(
            "100", "StayExcellent666", {
                "title": "Dashboard Draw", "entry_mode": "chat_command",
                "chat_command": "join", "duration_seconds": 3600,
                "target_entries": 25, "spin_duration_ms": 9000,
            },
        )

        assert result["id"] == 42
        assert result["chat_command"] == "!join"
        assert scheduled == [(42, 3600), (42, "notice")]
        assert [payload["type"] for _, payload in broadcasts] == [
            "reset_entries", "state",
        ]
        assert broadcasts[1][1]["remaining_seconds"] == 3600
        dashboard_server._fortuna_chat_commands.pop("stayexcellent666", None)

    @pytest.mark.asyncio
    async def test_fortuna_control_reports_live_countdown(self, monkeypatch):
        import json

        class FakeRequest(dict):
            query = {"broadcaster_id": "100"}

        async def fake_fetch(query, params=()):
            assert params == ("100",)
            if "FROM fortuna_plugin_keys" in query:
                return [{
                    "twitch_broadcaster_id": "100",
                    "twitch_broadcaster_login": "stayexcellent666",
                }]
            return [{
                "id": 42, "status": "open", "title": "Dashboard Draw",
                "entry_mode": "chat_command", "chat_command": "!enter",
                "reward_title": "Giveaway Entry", "target_entries": 0,
                "duration_seconds": 3600, "spin_duration_ms": 8000,
                "entry_count": 7, "started_at": "",
                "winner_announced": 0,
            }]

        monkeypatch.setattr(dashboard_server, "db_fetch", fake_fetch)
        response = await dashboard_server.get_fortuna_control(FakeRequest({
            "session": {"admin": True},
        }))
        payload = json.loads(response.text)

        assert payload["giveaway"]["status"] == "open"
        assert payload["giveaway"]["entry_count"] == 7
        assert payload["giveaway"]["remaining_seconds"] == 3600
        assert payload["channel"]["twitch_broadcaster_login"] == "stayexcellent666"
        assert payload["channel"]["browser_sources"] == 0

    def test_fortuna_plugin_tokens_are_hashed_deterministically(self):
        assert dashboard_server._fortuna_token_hash("secret") == (
            "2bb80d537b1da3e38bd30361aa855686bde0"
            "eacd7162fef6a25fe97bf527a25b"
        )
        assert dashboard_server._fortuna_token_hash("secret") != "secret"

    def test_fortuna_live_notice_describes_each_entry_mode(self):
        assert dashboard_server._fortuna_entry_instruction({
            "entry_mode": "chat_command", "chat_command": "!lucky",
        }) == "Type !lucky to enter"
        assert dashboard_server._fortuna_entry_instruction({
            "entry_mode": "channel_reward", "reward_title": "Golden Ticket",
        }) == 'Redeem "Golden Ticket" to enter'
        assert dashboard_server._fortuna_time_label(3660) == "in 1h 1m"
        assert dashboard_server._fortuna_time_label(0) == "when the host closes entries"

    @pytest.mark.asyncio
    async def test_fortuna_preserves_existing_pin_and_uses_chat_reminder(self, monkeypatch):
        import asyncio

        messages = []
        giveaway = {
            "id": 7, "twitch_broadcaster_id": "100",
            "twitch_broadcaster_login": "stayexcellent666",
        }

        async def fake_notice(_giveaway_id):
            return giveaway, "Giveaway is live"

        async def fake_auth(_broadcaster_id):
            return {"access_token": "token"}

        async def fake_existing_pin(_broadcaster_id, _token):
            return {"message_id": "creator-pin"}

        async def fake_send(channel, message):
            messages.append((channel, message))
            return True

        async def stop_after_initial_message(_seconds):
            raise asyncio.CancelledError

        monkeypatch.setattr(dashboard_server, "_fortuna_notice_message", fake_notice)
        monkeypatch.setattr(dashboard_server, "_fortuna_broadcaster_auth", fake_auth)
        monkeypatch.setattr(dashboard_server, "_fortuna_get_pinned_message", fake_existing_pin)
        monkeypatch.setattr(dashboard_server, "_fortuna_send_chat", fake_send)
        monkeypatch.setattr(dashboard_server.asyncio, "sleep", stop_after_initial_message)

        with pytest.raises(asyncio.CancelledError):
            await dashboard_server._fortuna_live_notice_worker(7)

        assert messages == [("stayexcellent666", "Giveaway is live")]
        assert dashboard_server._fortuna_notice_state[7]["pinned"] is False
        dashboard_server._fortuna_notice_state.pop(7, None)

    @pytest.mark.asyncio
    async def test_matching_fortuna_redemption_is_recorded_and_broadcast(self, monkeypatch):
        inserts = []
        broadcasts = []
        confirmations = []

        async def fake_open(_broadcaster_id):
            return {
                "id": 7, "reward_id": None, "reward_title": "Giveaway Entry",
                "target_entries": 0,
                "twitch_broadcaster_login": "stayexcellent666",
            }

        async def fake_fetch(query, params=()):
            if "SELECT 1 FROM fortuna_entries" in query:
                return []
            if "COUNT(DISTINCT twitch_user_id)" in query:
                return [{"count": 1}]
            raise AssertionError(query)

        async def fake_execute(*_args, **_kwargs):
            return None

        async def fake_insert(query, params=()):
            inserts.append((query, params))
            return 1

        async def fake_broadcast(broadcaster_id, payload):
            broadcasts.append((broadcaster_id, payload))

        async def fake_send_chat(channel_login, message):
            confirmations.append((channel_login, message))
            return True

        monkeypatch.setattr(dashboard_server, "_fortuna_open_giveaway", fake_open)
        monkeypatch.setattr(dashboard_server, "db_fetch", fake_fetch)
        monkeypatch.setattr(dashboard_server, "db_execute", fake_execute)
        monkeypatch.setattr(dashboard_server, "db_insert", fake_insert)
        monkeypatch.setattr(dashboard_server, "_fortuna_broadcast", fake_broadcast)
        monkeypatch.setattr(dashboard_server, "_fortuna_send_chat", fake_send_chat)

        accepted = await dashboard_server._fortuna_record_redemption({
            "id": "redemption-1",
            "broadcaster_user_id": "100",
            "user_id": "viewer-1",
            "user_login": "viewer",
            "user_name": "Viewer",
            "redeemed_at": "2026-09-07T20:00:00Z",
            "reward": {"id": "reward-1", "title": "Giveaway Entry"},
        })

        assert accepted is True
        assert len(inserts) == 1
        assert inserts[0][1][1] == "redemption-1"
        assert broadcasts == [("100", {
            "type": "entry", "user_id": "viewer-1",
            "display_name": "Viewer", "entry_count": 1,
        })]
        assert confirmations == [("stayexcellent666",
                                  "@viewer, you're entered! Total entrants: 1.")]

    @pytest.mark.asyncio
    async def test_duplicate_reward_redemption_gets_total_confirmation(self, monkeypatch):
        confirmations = []

        async def fake_open(_broadcaster_id):
            return {
                "id": 7, "entry_mode": "channel_reward",
                "reward_id": "reward-1", "reward_title": "Giveaway Entry",
                "target_entries": 0,
                "twitch_broadcaster_login": "stayexcellent666",
            }

        async def fake_fetch(query, params=()):
            if "SELECT 1 FROM fortuna_entries" in query:
                return [{"1": 1}]
            if "COUNT(DISTINCT twitch_user_id)" in query:
                return [{"count": 8}]
            raise AssertionError(query)

        async def fake_insert(*_args, **_kwargs):
            return 1

        async def fake_send_chat(channel_login, message):
            confirmations.append((channel_login, message))
            return True

        monkeypatch.setattr(dashboard_server, "_fortuna_open_giveaway", fake_open)
        monkeypatch.setattr(dashboard_server, "db_fetch", fake_fetch)
        monkeypatch.setattr(dashboard_server, "db_insert", fake_insert)
        monkeypatch.setattr(dashboard_server, "_fortuna_send_chat", fake_send_chat)

        handled = await dashboard_server._fortuna_record_redemption({
            "id": "redemption-2", "broadcaster_user_id": "100",
            "user_id": "viewer-1", "user_login": "viewer",
            "user_name": "Viewer",
            "reward": {"id": "reward-1", "title": "Giveaway Entry"},
        })

        assert handled is True
        assert confirmations == [("stayexcellent666",
                                  "@viewer, you're already entered. Total entrants: 8.")]

    @pytest.mark.asyncio
    async def test_matching_fortuna_chat_command_records_entry(self, monkeypatch):
        inserts = []
        broadcasts = []
        confirmations = []

        class FakeChannel:
            async def send(self, message):
                confirmations.append(message)

        async def fake_fetch(query, params=()):
            if "FROM fortuna_giveaways" in query:
                return [{
                    "id": 7, "twitch_broadcaster_id": "100",
                    "entry_mode": "chat_command", "chat_command": "!enter",
                    "target_entries": 0,
                }]
            if "SELECT 1 FROM fortuna_entries" in query:
                return []
            if "COUNT(DISTINCT twitch_user_id)" in query:
                return [{"count": 1}]
            raise AssertionError(query)

        async def fake_insert(query, params=()):
            inserts.append((query, params))
            return 1

        async def fake_broadcast(broadcaster_id, payload):
            broadcasts.append((broadcaster_id, payload))

        monkeypatch.setattr(dashboard_server, "db_fetch", fake_fetch)
        monkeypatch.setattr(dashboard_server, "db_insert", fake_insert)
        monkeypatch.setattr(dashboard_server, "_fortuna_broadcast", fake_broadcast)
        monkeypatch.setitem(
            dashboard_server._fortuna_chat_commands,
            "stayexcellent666", "!enter",
        )

        handled = await dashboard_server._fortuna_record_chat_entry(
            "StayExcellent666", "!ENTER", "message-1", "viewer-1",
            "viewer", "Viewer", FakeChannel(),
        )

        assert handled is True
        assert inserts[0][1][1] == "chat:7:message-1"
        assert broadcasts == [("100", {
            "type": "entry", "user_id": "viewer-1",
            "display_name": "Viewer", "entry_count": 1,
        })]
        assert confirmations == [
            "@viewer, you're entered! Total entrants: 1."
        ]

    @pytest.mark.asyncio
    async def test_fortuna_chat_command_ignores_other_commands(self, monkeypatch):
        async def fake_fetch(_query, _params=()):
            return [{
                "id": 7, "entry_mode": "chat_command",
                "chat_command": "!enter",
            }]

        monkeypatch.setattr(dashboard_server, "db_fetch", fake_fetch)
        monkeypatch.setitem(
            dashboard_server._fortuna_chat_commands, "channel", "!enter",
        )
        handled = await dashboard_server._fortuna_record_chat_entry(
            "channel", "!uptime", "message-1", "viewer-1", "viewer", "Viewer",
        )
        assert handled is False

    @pytest.mark.asyncio
    async def test_duplicate_fortuna_chat_entry_gets_total_confirmation(self, monkeypatch):
        confirmations = []
        inserts = []

        class FakeChannel:
            async def send(self, message):
                confirmations.append(message)

        async def fake_fetch(query, params=()):
            if "FROM fortuna_giveaways" in query:
                return [{
                    "id": 7, "twitch_broadcaster_id": "100",
                    "entry_mode": "chat_command", "chat_command": "!enter",
                    "target_entries": 0,
                }]
            if "SELECT 1 FROM fortuna_entries" in query:
                return [{"1": 1}]
            if "COUNT(DISTINCT twitch_user_id)" in query:
                return [{"count": 8}]
            raise AssertionError(query)

        async def fake_insert(query, params=()):
            inserts.append((query, params))
            return 1

        monkeypatch.setattr(dashboard_server, "db_fetch", fake_fetch)
        monkeypatch.setattr(dashboard_server, "db_insert", fake_insert)
        monkeypatch.setitem(
            dashboard_server._fortuna_chat_commands, "channel", "!enter",
        )

        handled = await dashboard_server._fortuna_record_chat_entry(
            "channel", "!enter", "message-2", "viewer-1",
            "viewer", "Viewer", FakeChannel(),
        )

        assert handled is True
        assert inserts[0][1][-2:] == (0, "Duplicate entry")
        assert confirmations == [
            "@viewer, you're already entered. Total entrants: 8."
        ]

    @pytest.mark.asyncio
    async def test_twitch_chat_routes_matching_command_to_fortuna(self, monkeypatch):
        import asyncio
        from twitch_bot import TwitchChatBot

        received = []

        async def fake_record(*args):
            received.append(args)
            return True

        class FakeAuthor:
            id = "viewer-1"
            name = "viewer"
            display_name = "Viewer"

        class FakeChannel:
            name = "StayExcellent666"

        class FakeMessage:
            echo = False
            content = "!enter"
            id = "message-1"
            author = FakeAuthor()
            channel = FakeChannel()

        class FakeChatBot:
            async def _notify_chat_activity(self, *_args):
                pass

            async def _handle_builtin(self, *_args):
                raise AssertionError("A matching Fortuna entry must be consumed first")

            async def _handle_custom(self, *_args):
                raise AssertionError("A matching Fortuna entry must be consumed first")

        monkeypatch.setattr(
            dashboard_server, "_fortuna_record_chat_entry", fake_record,
        )
        await TwitchChatBot.event_message(FakeChatBot(), FakeMessage())
        await asyncio.sleep(0)

        assert received == [(
            "stayexcellent666", "!enter", "message-1",
            "viewer-1", "viewer", "Viewer", FakeMessage.channel,
        )]

    def test_chat_sound_ignores_messages_sent_by_the_bot(self):
        from twitch_bot import _is_own_chat_message

        assert _is_own_chat_message(
            SimpleNamespace(id="bot-42", name="ExcelTwitch"),
            "bot-42", "ExcelTwitch",
        ) is True
        # Login matching remains a fallback if Twitch omits the user id.
        assert _is_own_chat_message(
            SimpleNamespace(id="", name="ExcelTwitch"),
            "bot-42", "ExcelTwitch",
        ) is True
        assert _is_own_chat_message(
            SimpleNamespace(id="viewer-1", name="Viewer"),
            "bot-42", "ExcelTwitch",
        ) is False
        event_source = inspect.getsource(__import__("twitch_bot").TwitchChatBot.event_message)
        assert "and not is_bot_message" in event_source

    @pytest.mark.asyncio
    async def test_fortuna_test_spin_drives_wheel_and_labels_chat_message(self, monkeypatch):
        broadcasts = []
        messages = []

        class FakeChannel:
            name = "stayexcellent666"

            async def send(self, message):
                messages.append(message)

        class FakeChatBot:
            connected_channels = [FakeChannel()]

            async def send_chat(self, _channel_name, message, channel):
                await channel.send(message)
                return "helix"

        class FakeBot:
            twitch_chat_bot = FakeChatBot()

        async def fake_broadcast(broadcaster_id, payload):
            broadcasts.append((broadcaster_id, payload))
            if payload["type"] == "spin":
                dashboard_server._fortuna_test_completions[broadcaster_id]["event"].set()

        async def no_wait(_seconds):
            return None

        monkeypatch.setattr(dashboard_server, "_bot_ref", FakeBot())
        monkeypatch.setattr(dashboard_server, "_fortuna_broadcast", fake_broadcast)
        monkeypatch.setattr(dashboard_server.asyncio, "sleep", no_wait)
        monkeypatch.setattr(dashboard_server.secrets, "randbelow", lambda _size: 2)

        await dashboard_server._fortuna_run_test_spin(
            "100", "StayExcellent666", 2500,
        )

        event_types = [payload["type"] for _, payload in broadcasts]
        assert event_types == [
            "reset_entries", "state", *("entry" for _ in range(12)),
            "spin", "winner",
        ]
        spin = next(payload for _, payload in broadcasts if payload["type"] == "spin")
        assert spin["winner_display_name"] == "NovaNoodle"
        assert spin["spin_duration_ms"] == 2500
        assert len(messages) == 1
        assert "[TEST]" in messages[0]
        assert "NovaNoodle" in messages[0]
        assert "no prize" in messages[0]
        assert "100" not in dashboard_server._fortuna_test_channels

    @pytest.mark.asyncio
    async def test_fortuna_completed_spin_reveals_and_announces_only_once(self, monkeypatch):
        broadcasts = []
        announcements = []
        giveaway = {"id": 77, "twitch_broadcaster_id": "100"}
        winner = {"twitch_user_id": "viewer-1", "twitch_display_name": "Viewer"}

        async def fake_broadcast(broadcaster_id, payload):
            broadcasts.append((broadcaster_id, payload))

        async def fake_announce(giveaway_arg, winner_arg):
            announcements.append((giveaway_arg, winner_arg))
            return True

        dashboard_server._fortuna_reveal_claims.discard(77)
        monkeypatch.setattr(dashboard_server, "_fortuna_broadcast", fake_broadcast)
        monkeypatch.setattr(dashboard_server, "_fortuna_announce_winner", fake_announce)

        assert await dashboard_server._fortuna_complete_reveal(giveaway, winner) is True
        assert await dashboard_server._fortuna_complete_reveal(giveaway, winner) is False
        assert broadcasts == [("100", {
            "type": "winner", "winner_id": "viewer-1",
            "winner_display_name": "Viewer",
        })]
        assert announcements == [(giveaway, winner)]
        dashboard_server._fortuna_reveal_claims.discard(77)


class TestOperationalSafetyRegressions:
    def test_health_poll_waits_for_reconciliation_event(self):
        import inspect
        import bot
        source = inspect.getsource(bot.TwitchNotifierBot.before_poll_live_streamers_health)
        assert "_reconciliation_complete.wait()" in source
        assert "asyncio.sleep(60)" not in source

    def test_token_refresh_never_invokes_fly_cli(self):
        import inspect
        import bot
        source = (
            inspect.getsource(bot.TwitchNotifierBot._refresh_twitch_chat_credentials)
            + inspect.getsource(bot.TwitchNotifierBot._refresh_twitch_chat_credentials_locked)
        )
        assert "set_twitch_bot_credentials" in source
        assert "create_subprocess_exec" not in source
        assert "secrets set" not in source.lower()

    @pytest.mark.asyncio
    async def test_token_refresh_persists_rotated_pair_and_updates_connection(self, monkeypatch):
        import aiohttp
        import bot

        class FakeResponse:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def json(self):
                return {"access_token": "new-access", "refresh_token": "new-refresh"}

        class FakeSession:
            last_kwargs = None
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            def post(self, *args, **kwargs):
                self.last_kwargs = kwargs
                FakeSession.last_kwargs = kwargs
                return FakeResponse()

        class FakeDb:
            saved = None
            def set_twitch_bot_credentials(self, access, refresh):
                self.saved = (access, refresh)

        class FakeConnection:
            _token = "old-access"

        class FakeChatBot:
            _connection = FakeConnection()

            def update_user_token(self, token):
                self._connection._token = token

            def reset_helix_delivery(self, _bot_id=None):
                pass

        class FakeSelf:
            _twitch_refresh_token = "old-refresh"
            _twitch_bot_token = "old-access"
            _twitch_token_last_success_at = None
            _twitch_token_last_error = "previous error"
            _twitch_bot_user_id = "bot-42"
            _twitch_bot_scopes = set()
            twitch_chat_bot = FakeChatBot()
            db = FakeDb()

        monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
        fake = FakeSelf()
        await bot.TwitchNotifierBot._refresh_twitch_chat_credentials(fake)

        assert fake.db.saved == ("new-access", "new-refresh")
        assert fake._twitch_bot_token == "new-access"
        assert fake._twitch_refresh_token == "new-refresh"
        assert fake.twitch_chat_bot._connection._token == "new-access"
        assert fake._twitch_token_last_error is None
        assert "data" in FakeSession.last_kwargs
        assert "params" not in FakeSession.last_kwargs

    @pytest.mark.asyncio
    async def test_eventsub_sync_prunes_unmonitored_or_paused_stream_subscriptions(self):
        import bot

        subscriptions = [
            {"id": "keep-online", "type": "stream.online", "condition": {"broadcaster_user_id": "1"}},
            {"id": "keep-offline", "type": "stream.offline", "condition": {"broadcaster_user_id": "1"}},
            {"id": "old-online", "type": "stream.online", "condition": {"broadcaster_user_id": "2"}},
            {"id": "old-offline", "type": "stream.offline", "condition": {"broadcaster_user_id": "2"}},
            {"id": "keep-reward", "type": "channel.channel_points_custom_reward_redemption.add", "condition": {"broadcaster_user_id": "2"}},
        ]

        class FakeDb:
            def get_all_streamers(self):
                return [
                    {"streamer_name": "alice", "guild_id": 10, "twitch_user_id": "1"},
                    {"streamer_name": "old_alice_login", "guild_id": 11, "twitch_user_id": "1"},
                    {"streamer_name": "paused_creator", "guild_id": 12, "twitch_user_id": "2", "notifications_paused": True},
                ]
            def clear_unresolvable_streamers(self): pass

        class FakeTwitch:
            deleted = []
            async def get_subscriptions(self): return list(subscriptions)
            async def delete_subscription(self, sub_id):
                self.deleted.append(sub_id)
                return True
            async def register_stream_subscription(self, *args):
                raise AssertionError("existing monitored subscriptions should be retained")

        class FakeBot:
            db = FakeDb()
            twitch = FakeTwitch()
            _last_pruned_subscriptions = 0
            _eventsub_sync_stats = {}
            def get_guild(self, guild_id): return None
            async def _eventsub_config(self): return ("https://example.test/callback", "secret")

        fake = FakeBot()
        await bot.TwitchNotifierBot._TwitchNotifierBot__do_eventsub_sync(fake, alert_on_mismatch=False)

        assert set(fake.twitch.deleted) == {"old-online", "old-offline"}
        assert "keep-reward" not in fake.twitch.deleted
        assert fake._last_pruned_subscriptions == 2
        assert fake._eventsub_sync_stats["actual"] == 2
        assert fake._eventsub_sync_stats["expected"] == 2
class TestTwitchClipCommand:
    @pytest.mark.asyncio
    async def test_clip_uses_saved_cooldown_and_config(self):
        from twitch_bot import TwitchChatBot

        config = {
            "guild_id": 1, "duration": 45, "cooldown": 60,
            "broadcaster_id": "22", "access_token": "token",
        }
        calls = []

        class FakeDb:
            def get_clip_config(self, channel):
                assert channel == "streamer"
                return config

        class FakeAuthor:
            is_mod = False
            name = "viewer"

        class FakeMessage:
            author = FakeAuthor()

        class FakeBot:
            db = FakeDb()

            async def _check_cooldown(self, channel, command, seconds):
                calls.append((channel, command, seconds))
                return True

            async def _create_clip(self, message, received_config):
                calls.append((message, received_config))

        message = FakeMessage()
        handled = await TwitchChatBot._handle_builtin(
            FakeBot(), message, "!clip", "", "streamer"
        )

        assert handled is True
        assert calls == [
            ("streamer", "!clip", 60),
            (message, config),
        ]

    @pytest.mark.asyncio
    async def test_disabled_clip_falls_through_to_custom_commands(self):
        from twitch_bot import TwitchChatBot

        class FakeDb:
            def get_clip_config(self, _channel):
                return None

        class FakeAuthor:
            is_mod = False
            name = "viewer"

        class FakeMessage:
            author = FakeAuthor()

        class FakeBot:
            db = FakeDb()

        assert await TwitchChatBot._handle_builtin(
            FakeBot(), FakeMessage(), "!clip", "", "streamer"
        ) is False
