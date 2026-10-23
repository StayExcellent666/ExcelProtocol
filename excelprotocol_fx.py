"""ExcelProtocol FX transport primitives.

This module deliberately contains no OBS automation.  It validates the small,
versioned command vocabulary understood by the native plugin and tracks live
authenticated WebSocket sessions.  Live commands only exist in memory while a
device is connected; they are never queued for replay after a reconnect.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


PROTOCOL = "excelprotocol-fx"
PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 16 * 1024
DEFAULT_COMMAND_TTL_SECONDS = 10
DEFAULT_COMMAND_TIMEOUT_SECONDS = 8
PAIRING_TTL_SECONDS = 10 * 60
MAX_QUEUE_SIZE = 16

EFFECT_IDS = frozenset({
    "crash", "ink_splatter", "screen_cracks", "error_invasion",
    "corruption", "darkness", "creepy_eyes", "fog", "mouse_invasion",
    "jumpscare_lite",
})
READ_COMMANDS = frozenset({"get_active_effects", "list_effects", "get_status", "get_raid_status"})
MUTATION_COMMANDS = frozenset({
    "trigger_effect", "trigger_random", "clear_effects", "cancel_effect",
    "raid_start", "raid_chat", "raid_end",
})
FUTURE_COMMAND_CAPABILITIES = {
    "apply_settings": "settings.apply.v1",
    "trigger_chaos": "chaos.trigger.v1",
}
RESPONSE_STATUSES = frozenset({
    "triggered", "queued", "cleared", "cancelled", "cooldown", "busy",
    "conflict", "intensity_limit", "effect_disabled", "disabled",
    "duplicate_event", "invalid_event_id", "invalid_effect",
    "invalid_duration", "invalid_intensity", "invalid_parameters",
    "invalid_parameter", "unknown_effect", "no_available_effect",
    "unknown_command", "ok", "error", "raid_status", "raid_started",
    "raid_active", "invalid_raid_viewers", "invalid_raid_chat",
    "attack_queued", "attack_blocked", "attack_cooldown", "roadblock_cleared",
    "invalid_raid_result", "no_active_raid", "raid_still_active",
    "raid_victory", "raid_failed",
})


class FxValidationError(ValueError):
    """A protocol value did not match the documented schema."""


class FxOfflineError(RuntimeError):
    pass


class FxTimeoutError(TimeoutError):
    pass


class FxUnsupportedError(RuntimeError):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def hash_secret(value: str, pepper: str) -> str:
    if not value or not pepper:
        raise ValueError("A value and server-side pepper are required")
    return hmac.new(pepper.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def make_pairing_code() -> str:
    # 40 random bits, formatted for easy manual entry. Ambiguous glyphs are omitted.
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_pairing_code(value: Any) -> str:
    code = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    if len(code) != 8 or any(ch not in "23456789ABCDEFGHJKLMNPQRSTUVWXYZ" for ch in code):
        raise FxValidationError("Invalid pairing code")
    return f"{code[:4]}-{code[4:]}"


def make_device_credential() -> str:
    return "fxd_" + secrets.token_urlsafe(48)


def validate_device_name(value: Any) -> str:
    name = str(value or "").strip()
    if not 1 <= len(name) <= 64 or any(ord(ch) < 32 for ch in name):
        raise FxValidationError("Device name must be 1-64 printable characters")
    return name


def _require_exact_keys(value: dict, allowed: set[str], required: set[str] = frozenset()) -> None:
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise FxValidationError(f"Unknown field: {sorted(unknown)[0]}")
    if missing:
        raise FxValidationError(f"Missing field: {sorted(missing)[0]}")


def _effect_duration_bounds(effect_id: str, effects: list[dict] | None) -> tuple[int, int]:
    default = (100, 60_000)
    for item in effects or []:
        if item.get("id") != effect_id and item.get("effect") != effect_id:
            continue
        duration = item.get("duration_ms")
        if isinstance(duration, dict):
            low = duration.get("min", duration.get("minimum", default[0]))
            high = duration.get("max", duration.get("maximum", default[1]))
        else:
            low = item.get("min_duration_ms", default[0])
            high = item.get("max_duration_ms", default[1])
        try:
            return int(low), int(high)
        except (TypeError, ValueError):
            return default
    return default


def _validate_parameters(effect: str, value: Any) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise FxValidationError("parameters must be an object")
    allowed = {
        "ink_splatter": {"splatters", "drips", "opacity", "size"},
        "crash": {"style"},
    }.get(effect, set())
    _require_exact_keys(value, allowed)
    result = dict(value)
    if "splatters" in result and (type(result["splatters"]) is not int or not 1 <= result["splatters"] <= 12):
        raise FxValidationError("splatters must be an integer from 1 through 12")
    if "drips" in result and type(result["drips"]) is not bool:
        raise FxValidationError("drips must be a boolean")
    if "opacity" in result and (type(result["opacity"]) not in (int, float) or not 0.1 <= float(result["opacity"]) <= 1.0):
        raise FxValidationError("opacity must be from 0.1 through 1.0")
    if "size" in result and (type(result["size"]) not in (int, float) or not 0.5 <= float(result["size"]) <= 1.5):
        raise FxValidationError("size must be from 0.5 through 1.5")
    if "style" in result and (type(result["style"]) is not int or not 0 <= result["style"] <= 2):
        raise FxValidationError("style must be an integer from 0 through 2")
    return result


def validate_command(command: Any, effects: list[dict] | None = None) -> dict:
    if not isinstance(command, dict):
        raise FxValidationError("command must be an object")
    command_type = command.get("type")
    if command_type in READ_COMMANDS:
        _require_exact_keys(command, {"type"}, {"type"})
        return {"type": command_type}
    if command_type not in MUTATION_COMMANDS and command_type not in FUTURE_COMMAND_CAPABILITIES:
        raise FxValidationError("Unsupported command type")

    if command_type == "trigger_effect":
        _require_exact_keys(
            command,
            {"type", "event_id", "effect", "duration_ms", "intensity", "parameters", "queue"},
            {"type", "event_id", "effect", "duration_ms", "intensity"},
        )
    elif command_type == "cancel_effect":
        _require_exact_keys(command, {"type", "event_id", "effect"}, {"type", "event_id", "effect"})
    elif command_type == "apply_settings":
        _require_exact_keys(command, {"type", "settings", "revision"}, {"type", "settings", "revision"})
        revision = command["revision"]
        if type(revision) is not int or revision < 1:
            raise FxValidationError("revision must be a positive integer")
        return {"type": command_type, "settings": validate_settings(command["settings"]), "revision": revision}
    elif command_type == "trigger_chaos":
        _require_exact_keys(
            command,
            {"type", "event_id", "duration_ms", "intensity"},
            {"type", "event_id", "duration_ms", "intensity"},
        )
    elif command_type == "raid_start":
        _require_exact_keys(command, {"type", "event_id", "viewers"}, {"type", "event_id", "viewers"})
    elif command_type == "raid_chat":
        _require_exact_keys(
            command,
            {"type", "event_id", "username", "role", "message"},
            {"type", "event_id", "username", "role", "message"},
        )
    elif command_type == "raid_end":
        _require_exact_keys(command, {"type", "event_id", "victory"}, {"type", "event_id", "victory"})
    else:
        _require_exact_keys(command, {"type", "event_id"}, {"type", "event_id"})

    event_id = command.get("event_id")
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 128 or any(ord(ch) < 33 for ch in event_id):
        raise FxValidationError("event_id is required and must be at most 128 characters")

    result = {"type": command_type, "event_id": event_id}
    if command_type == "raid_start":
        viewers = command["viewers"]
        if type(viewers) is not int or not 1 <= viewers <= 1_000_000:
            raise FxValidationError("viewers must be an integer from 1 through 1000000")
        result["viewers"] = viewers
    elif command_type == "raid_chat":
        username = command["username"]
        role = command["role"]
        message = command["message"]
        if not isinstance(username, str) or not 1 <= len(username) <= 32 or any(ord(ch) < 32 for ch in username):
            raise FxValidationError("username must be 1-32 printable characters")
        if role not in {"mod", "vip", "sub", "subscriber", "default", "viewer"}:
            raise FxValidationError("role is invalid")
        if not isinstance(message, str) or not 1 <= len(message) <= 500:
            raise FxValidationError("message must be 1-500 characters")
        result.update({"username": username, "role": role, "message": message})
    elif command_type == "raid_end":
        if type(command["victory"]) is not bool:
            raise FxValidationError("victory must be a boolean")
        result["victory"] = command["victory"]
    if "effect" in command:
        effect = command["effect"]
        if effect not in EFFECT_IDS:
            raise FxValidationError("Unknown effect")
        result["effect"] = effect
    if "duration_ms" in command:
        duration = command["duration_ms"]
        if type(duration) is not int:
            raise FxValidationError("duration_ms must be an integer")
        low, high = (1000, 15_000) if command_type == "trigger_chaos" else _effect_duration_bounds(command.get("effect", ""), effects)
        if not low <= duration <= high:
            raise FxValidationError(f"duration_ms must be from {low} through {high}")
        result["duration_ms"] = duration
    if "intensity" in command:
        intensity = command["intensity"]
        if type(intensity) not in (int, float) or not 0.05 <= float(intensity) <= 1.0:
            raise FxValidationError("intensity must be from 0.05 through 1.0")
        result["intensity"] = float(intensity)
    if command_type == "trigger_effect":
        result["parameters"] = _validate_parameters(result["effect"], command.get("parameters"))
        queue = command.get("queue", False)
        if type(queue) is not bool:
            raise FxValidationError("queue must be a boolean")
        result["queue"] = queue
    return result


def validate_settings(value: Any) -> dict:
    """Validate future remote settings without inventing plugin effect metadata."""
    if not isinstance(value, dict):
        raise FxValidationError("settings must be an object")
    allowed = {"enabled", "desktop_enabled", "stream_enabled", "overall_intensity", "max_simultaneous", "effects", "chaos"}
    _require_exact_keys(value, allowed)
    out: dict[str, Any] = {}
    for key in ("enabled", "desktop_enabled", "stream_enabled"):
        if key in value:
            if type(value[key]) is not bool:
                raise FxValidationError(f"{key} must be a boolean")
            out[key] = value[key]
    if "overall_intensity" in value:
        number = value["overall_intensity"]
        if type(number) not in (int, float) or not 0.05 <= float(number) <= 1.0:
            raise FxValidationError("overall_intensity must be from 0.05 through 1.0")
        out["overall_intensity"] = float(number)
    if "max_simultaneous" in value:
        number = value["max_simultaneous"]
        if type(number) is not int or not 1 <= number <= 4:
            raise FxValidationError("max_simultaneous must be from 1 through 4")
        out["max_simultaneous"] = number
    if "effects" in value:
        if not isinstance(value["effects"], dict) or set(value["effects"]) - EFFECT_IDS:
            raise FxValidationError("effects contains an unknown effect")
        effect_out = {}
        for effect_id, config in value["effects"].items():
            if not isinstance(config, dict):
                raise FxValidationError("Each effect setting must be an object")
            _require_exact_keys(config, {"enabled", "intensity", "duration_ms", "cooldown_ms", "random_weight", "parameters"})
            item = {}
            if "enabled" in config:
                if type(config["enabled"]) is not bool:
                    raise FxValidationError("effect enabled must be a boolean")
                item["enabled"] = config["enabled"]
            if "intensity" in config:
                n = config["intensity"]
                if type(n) not in (int, float) or not 0.05 <= float(n) <= 1.0:
                    raise FxValidationError("effect intensity must be from 0.05 through 1.0")
                item["intensity"] = float(n)
            for key, low, high in (("duration_ms", 100, 60_000), ("cooldown_ms", 0, 3_600_000), ("random_weight", 0, 100)):
                if key in config:
                    n = config[key]
                    if type(n) is not int or not low <= n <= high:
                        raise FxValidationError(f"{key} is outside its allowed range")
                    item[key] = n
            if "parameters" in config:
                item["parameters"] = _validate_parameters(effect_id, config["parameters"])
            effect_out[effect_id] = item
        out["effects"] = effect_out
    if "chaos" in value:
        chaos = value["chaos"]
        if not isinstance(chaos, dict):
            raise FxValidationError("chaos must be an object")
        _require_exact_keys(chaos, {"allowed_effects", "min_effects", "max_effects", "duration_ms", "combined_intensity"})
        item = {}
        if "allowed_effects" in chaos:
            ids = chaos["allowed_effects"]
            if not isinstance(ids, list) or len(ids) != len(set(ids)) or any(x not in EFFECT_IDS for x in ids):
                raise FxValidationError("chaos allowed_effects is invalid")
            item["allowed_effects"] = ids
        for key, low, high in (("min_effects", 1, 4), ("max_effects", 1, 4), ("duration_ms", 1000, 15000)):
            if key in chaos:
                n = chaos[key]
                if type(n) is not int or not low <= n <= high:
                    raise FxValidationError(f"chaos {key} is outside its allowed range")
                item[key] = n
        if item.get("min_effects", 1) > item.get("max_effects", 4):
            raise FxValidationError("chaos min_effects cannot exceed max_effects")
        if "combined_intensity" in chaos:
            n = chaos["combined_intensity"]
            if type(n) not in (int, float) or not 0.1 <= float(n) <= 3.0:
                raise FxValidationError("chaos combined_intensity must be from 0.1 through 3.0")
            item["combined_intensity"] = float(n)
        out["chaos"] = item
    return out


def validate_plugin_response(value: Any) -> dict:
    if not isinstance(value, dict) or type(value.get("ok")) is not bool:
        raise FxValidationError("response must contain a boolean ok field")
    value = dict(value)
    if "status" not in value and isinstance(value.get("raid"), dict):
        value["status"] = "raid_status"
    if not isinstance(value.get("status"), str) or value["status"] not in RESPONSE_STATUSES:
        raise FxValidationError("response status is invalid")
    # The plugin owns response metadata. Limit its shape and total size while
    # retaining list/status fields needed by the dashboard.
    allowed = {
        "ok", "status", "event_id", "error", "effects", "active_effects", "active",
        "settings", "queue_size", "enabled", "active_count", "queued_count",
        "desktop", "stream", "source_count", "raid",
    }
    _require_exact_keys(value, allowed, {"ok", "status"})
    if len(json.dumps(value, separators=(",", ":")).encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise FxValidationError("response is too large")
    if "event_id" in value and (not isinstance(value["event_id"], str) or len(value["event_id"]) > 128):
        raise FxValidationError("response event_id is invalid")
    if "effects" in value and not isinstance(value["effects"], list):
        raise FxValidationError("effects must be an array")
    if "active_effects" in value and not isinstance(value["active_effects"], list):
        raise FxValidationError("active_effects must be an array")
    if "active" in value and not isinstance(value["active"], list):
        raise FxValidationError("active must be an array")
    if "error" in value and (not isinstance(value["error"], str) or len(value["error"]) > 1024):
        raise FxValidationError("error detail is invalid")
    if "queue_size" in value and (type(value["queue_size"]) is not int or not 0 <= value["queue_size"] <= MAX_QUEUE_SIZE):
        raise FxValidationError("queue_size is invalid")
    for effect in value.get("effects", []):
        if not isinstance(effect, dict):
            raise FxValidationError("effect metadata must contain objects")
        effect_id = effect.get("id", effect.get("effect"))
        if effect_id not in EFFECT_IDS:
            raise FxValidationError("effect metadata contains an unknown effect")
        if len(json.dumps(effect, separators=(",", ":")).encode("utf-8")) > 4096:
            raise FxValidationError("effect metadata item is too large")
    for active in value.get("active_effects", []):
        if not isinstance(active, dict) or active.get("effect") not in EFFECT_IDS:
            raise FxValidationError("active_effects contains an invalid effect")
        if "event_id" in active and (not isinstance(active["event_id"], str) or len(active["event_id"]) > 128):
            raise FxValidationError("active effect event_id is invalid")
    if "settings" in value:
        validate_settings(value["settings"])
    if "raid" in value:
        raid = value["raid"]
        if not isinstance(raid, dict):
            raise FxValidationError("raid must be an object")
        allowed_raid = {
            "phase", "viewers", "hp", "max_hp", "remaining_ms", "roadblock",
            "ice_active", "firewall_active", "warrior_count",
            "represented_viewers_per_warrior", "enemy_count", "enemies_defeated",
            "victory", "interaction_count", "test_mode", "leaderboard",
        }
        _require_exact_keys(raid, allowed_raid)
        if raid.get("phase") not in {"idle", "fighting", "results"}:
            raise FxValidationError("raid phase is invalid")
        if not isinstance(raid.get("leaderboard", []), list) or len(raid.get("leaderboard", [])) > 5:
            raise FxValidationError("raid leaderboard is invalid")
    return value


class SlidingWindowRateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_seconds: float, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits[key]
        cutoff = now - window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True


@dataclass
class DeviceSession:
    device_id: str
    guild_id: str
    ws: Any
    capabilities: frozenset[str]
    plugin_version: str
    obs_version: str
    connected_at: str = field(default_factory=utc_now_iso)
    last_heartbeat: str = field(default_factory=utc_now_iso)
    effects: list[dict] = field(default_factory=list)
    status: dict = field(default_factory=dict)
    active_effects: list[dict] = field(default_factory=list)


@dataclass
class PendingRequest:
    device_id: str
    command_type: str
    deadline: float
    future: asyncio.Future


class FxConnectionRegistry:
    def __init__(self) -> None:
        self.connections: dict[str, DeviceSession] = {}
        self.pending: dict[str, PendingRequest] = {}

    async def register(self, session: DeviceSession) -> Any | None:
        # Registry mutations contain no await point, so they are atomic on the
        # server's single asyncio event loop without a process-global Lock.
        previous = self.connections.get(session.device_id)
        self.connections[session.device_id] = session
        if previous and previous.ws is not session.ws:
            for request_id, pending in list(self.pending.items()):
                if pending.device_id == session.device_id:
                    if not pending.future.done():
                        pending.future.set_exception(FxOfflineError("Device connection was replaced"))
                    del self.pending[request_id]
        return previous.ws if previous and previous.ws is not session.ws else None

    async def unregister(self, device_id: str, ws: Any) -> None:
        current = self.connections.get(device_id)
        if current and current.ws is ws:
            del self.connections[device_id]
            for request_id, pending in list(self.pending.items()):
                if pending.device_id == device_id:
                    if not pending.future.done():
                        pending.future.set_exception(FxOfflineError("Device disconnected"))
                    del self.pending[request_id]

    def get(self, device_id: str) -> DeviceSession | None:
        return self.connections.get(device_id)

    async def send_command(
        self,
        device_id: str,
        command: dict,
        *,
        timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        ttl_seconds: float = DEFAULT_COMMAND_TTL_SECONDS,
    ) -> dict:
        session = self.connections.get(device_id)
        if not session or getattr(session.ws, "closed", False):
            raise FxOfflineError("OBS device is offline")
        required_capability = {
            "list_effects": "effects.list.v1",
            "get_status": "effects.status.v1",
            "get_active_effects": "effects.status.v1",
            "get_raid_status": "raid_battle.v1",
            "raid_start": "raid_battle.v1",
            "raid_chat": "raid_battle.v1",
            "raid_end": "raid_battle.v1",
        }.get(command.get("type"))
        if command.get("type") in MUTATION_COMMANDS and not command.get("type", "").startswith("raid_"):
            required_capability = "commands.v1"
        if required_capability and required_capability not in session.capabilities:
            raise FxUnsupportedError(f"Device does not report {required_capability}")
        capability = FUTURE_COMMAND_CAPABILITIES.get(command.get("type"))
        if capability and capability not in session.capabilities:
            raise FxUnsupportedError(f"Device does not report {capability}")
        command = validate_command(command, session.effects)
        request_id = str(uuid.uuid4())
        deadline = time.monotonic() + ttl_seconds
        expires_at = datetime.fromtimestamp(time.time() + ttl_seconds, timezone.utc).isoformat().replace("+00:00", "Z")
        envelope = {
            "protocol": PROTOCOL,
            "protocol_version": PROTOCOL_VERSION,
            "request_id": request_id,
            "expires_at": expires_at,
            "command": command,
        }
        encoded = json.dumps(envelope, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise FxValidationError("Command envelope is too large")
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = PendingRequest(device_id, command["type"], deadline, future)
        try:
            try:
                await session.ws.send_str(encoded)
            except (ConnectionError, OSError, RuntimeError) as exc:
                raise FxOfflineError("OBS device disconnected before receiving the command") from exc
            return await asyncio.wait_for(future, timeout=min(timeout_seconds, ttl_seconds))
        except asyncio.TimeoutError as exc:
            raise FxTimeoutError("OBS plugin did not acknowledge the command in time") from exc
        finally:
            self.pending.pop(request_id, None)

    def receive_response(self, device_id: str, request_id: Any, response: Any) -> bool:
        if not isinstance(request_id, str):
            return False
        # Validate every authenticated message, including late or unknown
        # correlations. Valid late responses are ignored; malformed ones cause
        # the WebSocket handler to close the connection.
        parsed = validate_plugin_response(response)
        pending = self.pending.get(request_id)
        if not pending or pending.device_id != device_id or time.monotonic() > pending.deadline:
            return False
        session = self.connections.get(device_id)
        if session:
            if pending.command_type == "list_effects" and isinstance(parsed.get("effects"), list):
                session.effects = parsed["effects"]
            elif pending.command_type == "get_active_effects":
                active = parsed.get("active_effects", parsed.get("active"))
                if isinstance(active, list):
                    session.active_effects = active
            elif pending.command_type == "get_status":
                session.status = {key: val for key, val in parsed.items() if key not in {"effects", "active_effects"}}
        if not pending.future.done():
            pending.future.set_result(parsed)
        return True


FX_REGISTRY = FxConnectionRegistry()

