import asyncio
import json
import time

import pytest

from excelprotocol_fx import (
    MAX_MESSAGE_BYTES,
    DeviceSession,
    FxConnectionRegistry,
    FxOfflineError,
    FxTimeoutError,
    FxUnsupportedError,
    FxValidationError,
    SlidingWindowRateLimiter,
    hash_secret,
    make_device_credential,
    make_pairing_code,
    normalize_pairing_code,
    validate_command,
    validate_plugin_response,
    validate_settings,
)


class FakeWebSocket:
    def __init__(self):
        self.closed = False
        self.messages = []

    async def send_str(self, value):
        self.messages.append(json.loads(value))

    async def close(self, **_kwargs):
        self.closed = True


class TestFxSecurityPrimitives:
    def test_pairing_codes_are_normalized_and_credentials_are_high_entropy(self):
        code = make_pairing_code()
        assert len(code) == 9 and code[4] == "-"
        assert normalize_pairing_code(code.lower().replace("-", " ")) == code
        credential = make_device_credential()
        assert credential.startswith("fxd_") and len(credential) >= 60
        assert hash_secret(credential, "pepper") != hash_secret(credential, "other")

    def test_command_schema_rejects_unknown_fields_and_invalid_parameters(self):
        base = {
            "type": "trigger_effect", "event_id": "event-1", "effect": "ink_splatter",
            "duration_ms": 8000, "intensity": 0.7,
        }
        result = validate_command({**base, "parameters": {"splatters": 12, "drips": True, "opacity": 1, "size": 1.5}})
        assert result["queue"] is False
        assert result["parameters"] == {"splatters": 12, "drips": True, "opacity": 1, "size": 1.5}
        with pytest.raises(FxValidationError, match="Unknown field"):
            validate_command({**base, "url": "https://example.test"})
        with pytest.raises(FxValidationError, match="Unknown field"):
            validate_command({**base, "parameters": {"shell": "calc.exe"}})
        with pytest.raises(FxValidationError, match="intensity"):
            validate_command({**base, "intensity": 1.1})

    def test_dynamic_effect_duration_limits_are_enforced(self):
        effects = [{"id": "fog", "min_duration_ms": 1000, "max_duration_ms": 5000}]
        command = {"type": "trigger_effect", "event_id": "x", "effect": "fog", "duration_ms": 8000, "intensity": .5}
        with pytest.raises(FxValidationError, match="1000 through 5000"):
            validate_command(command, effects)

    def test_settings_are_strict_and_safe(self):
        assert validate_settings({"enabled": True, "max_simultaneous": 4})["enabled"] is True
        with pytest.raises(FxValidationError):
            validate_settings({"script": "anything"})
        with pytest.raises(FxValidationError):
            validate_settings({"chaos": {"min_effects": 4, "max_effects": 2}})
        with pytest.raises(FxValidationError, match="Missing field"):
            validate_command({"type": "trigger_chaos", "event_id": "event"})

    def test_response_size_and_schema_are_limited(self):
        with pytest.raises(FxValidationError):
            validate_plugin_response({"ok": True, "status": "ok", "private": "no"})
        with pytest.raises(FxValidationError, match="too large"):
            validate_plugin_response({"ok": True, "status": "ok", "error": "x" * MAX_MESSAGE_BYTES})
        with pytest.raises(FxValidationError):
            FxConnectionRegistry().receive_response("device", "unknown", {"ok": "not-a-boolean"})

    def test_raid_commands_are_strict_and_preserve_unicode_chat(self):
        assert validate_command({
            "type": "raid_start", "event_id": "twitch-raid:1", "viewers": 42,
        })["viewers"] == 42
        chat = validate_command({
            "type": "raid_chat", "event_id": "twitch-chat:1",
            "username": "Coder", "role": "vip", "message": "ship it 🔥",
        })
        assert chat["message"] == "ship it 🔥"
        with pytest.raises(FxValidationError, match="viewers"):
            validate_command({"type": "raid_start", "event_id": "x", "viewers": 0})
        with pytest.raises(FxValidationError, match="role"):
            validate_command({
                "type": "raid_chat", "event_id": "x", "username": "Coder",
                "role": "admin", "message": "hello",
            })

    def test_raid_status_response_accepts_plugin_read_shape(self):
        response = validate_plugin_response({
            "ok": True,
            "raid": {"phase": "fighting", "leaderboard": []},
        })
        assert response["status"] == "raid_status"
        assert response["raid"]["phase"] == "fighting"

    def test_sliding_rate_limit(self):
        limiter = SlidingWindowRateLimiter()
        assert limiter.allow("device", 2, 60, now=10)
        assert limiter.allow("device", 2, 60, now=11)
        assert not limiter.allow("device", 2, 60, now=12)
        assert limiter.allow("device", 2, 60, now=71)


class TestFxConnectionRegistry:
    def test_correlates_response_and_rejects_stale_or_wrong_device(self):
        async def scenario():
            registry = FxConnectionRegistry()
            ws = FakeWebSocket()
            await registry.register(DeviceSession("a", "guild-a", ws, frozenset({"commands.v1"}), "1", "31"))
            task = asyncio.create_task(registry.send_command("a", {"type": "trigger_random", "event_id": "event-a"}))
            await asyncio.sleep(0)
            request_id = ws.messages[0]["request_id"]
            assert not registry.receive_response("b", request_id, {"ok": True, "status": "triggered"})
            assert registry.receive_response("a", request_id, {"ok": True, "status": "triggered", "event_id": "event-a"})
            assert (await asyncio.wait_for(task, 1))["status"] == "triggered"
            assert not registry.receive_response("a", request_id, {"ok": True, "status": "triggered"})
        asyncio.run(scenario())

    def test_times_out_and_does_not_replay_after_reconnect(self):
        async def scenario():
            registry = FxConnectionRegistry()
            first = FakeWebSocket()
            await registry.register(DeviceSession("a", "guild", first, frozenset({"commands.v1"}), "1", "31"))
            with pytest.raises(FxTimeoutError):
                await registry.send_command("a", {"type": "clear_effects", "event_id": "old"}, timeout_seconds=.01, ttl_seconds=.02)
            second = FakeWebSocket()
            old = await registry.register(DeviceSession("a", "guild", second, frozenset({"commands.v1"}), "1", "31"))
            assert old is first
            assert second.messages == []
        asyncio.run(scenario())

    def test_disconnect_fails_pending_and_replacement_is_single_connection(self):
        async def scenario():
            registry = FxConnectionRegistry()
            ws = FakeWebSocket()
            session = DeviceSession("a", "guild", ws, frozenset({"commands.v1"}), "1", "31")
            await registry.register(session)
            task = asyncio.create_task(registry.send_command("a", {"type": "trigger_random", "event_id": "event"}))
            await asyncio.sleep(0)
            await registry.unregister("a", ws)
            with pytest.raises(FxOfflineError):
                await asyncio.wait_for(task, 1)
        asyncio.run(scenario())

    def test_capability_gates_commands(self):
        async def scenario():
            registry = FxConnectionRegistry()
            await registry.register(DeviceSession("a", "guild", FakeWebSocket(), frozenset(), "1", "31"))
            with pytest.raises(FxUnsupportedError):
                await registry.send_command("a", {"type": "trigger_random", "event_id": "event"})
        asyncio.run(scenario())

    def test_raid_capability_gates_and_correlates_start(self):
        async def scenario():
            registry = FxConnectionRegistry()
            unsupported = FakeWebSocket()
            await registry.register(DeviceSession("a", "guild", unsupported, frozenset({"commands.v1"}), "1", "31"))
            with pytest.raises(FxUnsupportedError, match="raid_battle.v1"):
                await registry.send_command("a", {"type": "raid_start", "event_id": "raid-1", "viewers": 5})

            ws = FakeWebSocket()
            await registry.register(DeviceSession("a", "guild", ws, frozenset({"raid_battle.v1"}), "1", "31"))
            task = asyncio.create_task(registry.send_command("a", {"type": "raid_start", "event_id": "raid-2", "viewers": 5}))
            await asyncio.sleep(0)
            request_id = ws.messages[0]["request_id"]
            assert registry.receive_response("a", request_id, {
                "ok": True, "status": "raid_started", "raid": {"phase": "fighting", "leaderboard": []},
            })
            assert (await task)["status"] == "raid_started"
        asyncio.run(scenario())

