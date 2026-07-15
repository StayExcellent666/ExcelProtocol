"""Tests for dashboard_server.py — security-critical primitives.

Covers:
  - HMAC verification of EventSub webhook signatures
  - OAuth state TTL pruning
  - Per-guild session access checks
"""
import hmac as _hmac
import hashlib as _hashlib
import pytest

# Import after conftest sets env vars
import dashboard_server


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


class TestDevDashboardRoutes:
    """The developer panels must have matching backend endpoints."""

    def test_blacklist_and_stream_event_routes_registered(self):
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
                for name in (
                    "check_streams", "check_milestones", "cleanup_channels",
                    "monthly_leaderboard_cleanup", "poll_live_streamers_health",
                    "rotate_status", "refresh_broadcaster_tokens", "check_permissions",
                    "update_stat_channels", "check_streamer_renames", "cleanup_kicked_guilds",
                ):
                    setattr(self, name, FakeLoop())
            def is_ready(self): return True
            def is_closed(self): return False

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
        assert payload["eventsub"]["actual"] == 4
        assert payload["streaming"]["unique_streamers"] == 2
        assert len(payload["tasks"]) == 11

    @pytest.mark.asyncio
    async def test_health_check_rejects_regular_user(self):
        from aiohttp import web
        with pytest.raises(web.HTTPForbidden):
            await dashboard_server.get_operations_health({"session": {}})


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
        source = inspect.getsource(bot.TwitchNotifierBot._refresh_twitch_chat_credentials)
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
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            def post(self, *args, **kwargs): return FakeResponse()

        class FakeDb:
            saved = None
            def set_twitch_bot_credentials(self, access, refresh):
                self.saved = (access, refresh)

        class FakeConnection:
            _token = "old-access"

        class FakeChatBot:
            _connection = FakeConnection()

        class FakeSelf:
            _twitch_refresh_token = "old-refresh"
            _twitch_bot_token = "old-access"
            _twitch_token_last_success_at = None
            _twitch_token_last_error = "previous error"
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

    @pytest.mark.asyncio
    async def test_eventsub_sync_prunes_only_unmonitored_stream_subscriptions(self):
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
                return [{"streamer_name": "alice", "guild_id": 10, "twitch_user_id": "1"}]
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
