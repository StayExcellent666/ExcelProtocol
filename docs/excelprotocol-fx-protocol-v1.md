# ExcelProtocol FX protocol v1

Status: bot/dashboard and native plugin transport implemented for the v1.1.0 release line. Persistent remote settings and remote Chaos remain capability-gated future additions.

## Transport and authentication

- Production endpoint: `wss://excelprotocol.fly.dev/api/fx/ws`
- The native plugin initiates the outbound TLS WebSocket. It must not open a local listener.
- Send `Authorization: Bearer <device_credential>` in the WebSocket upgrade request. Never put a credential in the URL.
- The credential is returned once by `POST /api/fx/pair` and must be stored in Windows Credential Manager. The server stores only an HMAC-SHA-256 digest.
- The plugin may omit `Origin`, as native WebSocket clients normally do. If it sends `Origin`, it must match an origin in `FX_ALLOWED_ORIGINS`.
- Maximum inbound or outbound message size is 16 KiB. Text JSON only.
- One connection is permitted per device. A newer authenticated connection replaces the older one.

Immediately after upgrade, the plugin must send this exact hello within 10 seconds:

```json
{
  "protocol": "excelprotocol-fx",
  "protocol_version": 1,
  "type": "hello",
  "device_id": "<paired UUID>",
  "plugin_version": "1.1.0",
  "obs_version": "31.0.0",
  "capabilities": ["commands.v1", "effects.list.v1", "effects.status.v1"]
}
```

Recognized capabilities are:

- `commands.v1`: current mutation commands.
- `effects.list.v1`: `list_effects` and metadata-driven dashboard controls.
- `effects.status.v1`: `get_status` and `get_active_effects`.
- `settings.apply.v1`: future persistent `apply_settings` command.
- `chaos.trigger.v1`: future `trigger_chaos` command.
- `raid_battle.v1`: Twitch Raid Battle start, chat participation, and status commands.

The server replies with `welcome`, including `heartbeat_seconds` and `max_message_bytes`. Send an application heartbeat about every 30 seconds:

```json
{"protocol":"excelprotocol-fx","protocol_version":1,"type":"heartbeat","sent_at":"2026-10-21T12:00:00Z"}
```

The server responds with `heartbeat_ack`. WebSocket ping/pong is also enabled. Reconnect on abnormal loss with full jitter: wait a random value from zero to `min(60 seconds, 1 second * 2^attempt)`, reset after a stable connection, and do not retry authentication failures until the user pairs again.

## Pairing

1. An authenticated Manage Server user creates a one-time code with `POST /api/guild/{guild_id}/fx/pairing-codes`.
2. The plugin posts `{pairing_code, device_name, plugin_version, obs_version}` to `POST /api/fx/pair` over HTTPS. `device_name` is a fallback; the name entered when the dashboard created the code is authoritative and can later be renamed.
3. The code expires after 10 minutes and is consumed atomically once.
4. The response contains `device_id`, `device_credential`, `wss_endpoint`, and protocol version. Store the credential in Windows Credential Manager and discard the pairing code.
5. Revocation closes an active socket and permanently invalidates its credential.

Do not log either request body, the pairing code, the credential, or viewer identity data.

## Command envelope

Every server command has a correlation ID and expiry independent of mutation deduplication:

```json
{
  "protocol": "excelprotocol-fx",
  "protocol_version": 1,
  "request_id": "728d48a7-15d3-4a92-b044-e7f187c11120",
  "expires_at": "2026-10-21T12:00:10Z",
  "command": {"type":"trigger_random","event_id":"9cd44da5-7be1-4efe-b3c9-e42f59919e24"}
}
```

The plugin must reject an expired envelope before executing it, deduplicate mutation `event_id` values, and return exactly one correlated response:

```json
{
  "protocol": "excelprotocol-fx",
  "protocol_version": 1,
  "request_id": "728d48a7-15d3-4a92-b044-e7f187c11120",
  "response": {"ok":true,"status":"triggered","event_id":"9cd44da5-7be1-4efe-b3c9-e42f59919e24"}
}
```

The server waits eight seconds for an acknowledgement. Live commands are sent only to the currently connected socket, expire after ten seconds, are never persisted, and are never replayed after reconnect. Unknown, duplicate, late, or wrong-device responses are ignored.

## Existing inner commands

Preserve the beta.3 command processor objects unchanged:

- `trigger_effect` with `event_id`, `effect`, `duration_ms`, `intensity`, optional strict `parameters`, and `queue`.
- `trigger_random` with `event_id`.
- `clear_effects` with `event_id`.
- `cancel_effect` with `event_id` and `effect`.
- `get_active_effects`, `list_effects`, and `get_status` without an event ID.

All limits and statuses in [the JSON Schema](./excelprotocol-fx-protocol-v1.schema.json) are normative. `list_effects` should return an `effects` array. Each item must provide `id`, display `name`, description, min/max/default duration, default intensity/cooldown/random weight, and parameter metadata (`type`, `min`, `max`, `step`, `default`, and optional label). This data is the dashboard source of truth.

## Future persistent settings command

Advertise `settings.apply.v1` only after implementing:

```json
{
  "type": "apply_settings",
  "revision": 4,
  "settings": {
    "enabled": true,
    "desktop_enabled": true,
    "stream_enabled": true,
    "overall_intensity": 0.8,
    "max_simultaneous": 2,
    "effects": {
      "fog": {"enabled":true,"intensity":0.6,"duration_ms":8000,"cooldown_ms":30000,"random_weight":50,"parameters":{}}
    },
    "chaos": {"allowed_effects":["fog"],"min_effects":1,"max_effects":3,"duration_ms":8000,"combined_intensity":1.2}
  }
}
```

Apply only a newer revision, persist it in plugin configuration, then reply `{ok:true,status:"ok"}`. The server stores desired settings and sends the latest revision after an authenticated reconnect. This is the only command intentionally synchronized after reconnect.

## Future Chaos command

Advertise `chaos.trigger.v1` only after implementing:

```json
{"type":"trigger_chaos","event_id":"<unique UUID>","duration_ms":8000,"intensity":0.8}
```

It uses the saved Chaos eligibility and limits. It has the same expiry, acknowledgement, cooldown, conflict, intensity, and deduplication behavior as other mutations.

No protocol version permits arbitrary script execution, filesystem access, shell commands, user-provided URLs, or mouse/keyboard control.

## Twitch Raid Battle

Devices advertising `raid_battle.v1` accept the following strict commands through the same authenticated, expiring envelope:

```json
{"type":"raid_start","event_id":"twitch-raid:<eventsub-message-id>","viewers":42}
{"type":"raid_chat","event_id":"twitch-chat:<message-id>","username":"DisplayName","role":"vip","message":"ship it 🔥"}
{"type":"get_raid_status"}
```

`viewers` is an integer from 1 through 1,000,000. Usernames and messages are capped at 32 and 500 characters. Chat roles are `mod`, `vip`, `sub`, or `default`; broadcaster/moderator takes precedence over VIP, which takes precedence over subscriber. The bot forwards chat only while server-side state says the battle is fighting, polls one status loop per guild about every two seconds, and treats the state as stale after results or the three-minute battle window.

The dashboard uses Twitch's signed EventSub message ID and chat message ID as mutation deduplication keys. It starts Live Test only while the authorized broadcaster is live and only after the plugin acknowledges `raid_started`. Twitch credentials remain server-side, and neither chat text nor viewer identity is written to application logs.

The plugin may acknowledge a valid message with `attack_cooldown` when that viewer has attacked within the previous 900 ms. This is an accepted, non-error response: it limits projectile spam without delaying `break;` or `!git push --force` roadblock commands.

