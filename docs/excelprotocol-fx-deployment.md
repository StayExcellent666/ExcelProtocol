# ExcelProtocol FX deployment and rollback

## Bot/dashboard implementation

Implemented here:

- tenant-scoped pairing, device listing, rename, revoke, status, live command, and persistent settings APIs;
- authenticated `/api/fx/ws` WebSocket with TLS enforcement on Fly, origin policy, 16 KiB limits, strict schemas, capability negotiation, heartbeat, one active connection per device, correlation, expiry, timeouts, and acknowledgement handling;
- hashed one-time pairing codes and hashed device credentials;
- transient no-replay live commands, persistent desired-settings reconnect sync, safe audit metadata, and rate limiting;
- Channel Rewards pairing/device UI and an **ExcelProtocol FX Plugin** trigger type driven by `list_effects` metadata;
- EventSub redemption dispatch that sends the configured effect only to the currently connected device and never replays it;
- backend, schema, and frontend protocol tests.

Implemented in the `ExcelProtocol-FX` v1.1.0 release line:

- one-time HTTPS pairing from the plugin dock and device credentials stored in Windows Credential Manager;
- an outbound authenticated TLS WebSocket with no inbound listener;
- strict upgrade validation, 16 KiB text-frame limits, heartbeat/ping handling, correlated acknowledgements, expiry checks, and mutation deduplication through the existing command processor;
- `commands.v1`, `effects.list.v1`, and `effects.status.v1` capability reporting with metadata-driven effect controls;
- progressive full-jitter reconnects, revocation/authentication lockout, and no replay of disconnected live commands.

The future `settings.apply.v1` and `chaos.trigger.v1` capabilities remain deliberately unadvertised until their plugin handlers are implemented. Beta.3 and v1.0.0 do not support remote connectivity.

## Environment

Set these as Fly secrets before enabling plugin pairing:

- `FX_CREDENTIAL_PEPPER`: a new independent high-entropy random secret. Required for HMAC hashing. For a non-breaking rollout the app can derive a domain-separated fallback from `EVENTSUB_SECRET`, but production should set a separate value before issuing credentials. Rotating it invalidates every pairing code and device credential.
- `FX_WSS_BASE_URL`: public WSS origin, default `wss://excelprotocol.fly.dev`.
- `FX_ALLOWED_ORIGINS`: optional comma-separated explicit origins for native clients that send an Origin header. Defaults to `CORS_ALLOWED_ORIGINS`; wildcards are rejected.

Do not place credentials or pairing codes in environment variables, URLs, logs, analytics, crash dumps, or audit bodies.

## Database migration

Startup creates four idempotent SQLite tables:

- `fx_pairing_codes`
- `fx_devices`
- `fx_desired_settings`
- `fx_command_audit`

There is no destructive migration. Existing Discord, Twitch, overlay, reward, and notification tables are unchanged.

## Routes

Dashboard session plus Manage Server permission:

- `GET /api/guild/{guild_id}/fx`
- `POST /api/guild/{guild_id}/fx/pairing-codes`
- `PATCH /api/guild/{guild_id}/fx/devices/{device_id}`
- `DELETE /api/guild/{guild_id}/fx/devices/{device_id}`
- `POST /api/guild/{guild_id}/fx/commands`
- `PUT /api/guild/{guild_id}/fx/settings`

Native plugin:

- `POST /api/fx/pair` over HTTPS
- `GET /api/fx/ws` upgraded to WSS with `Authorization: Bearer <device_credential>`

## Deployment

1. Back up `/data/twitch_bot.db`.
2. Generate and set `FX_CREDENTIAL_PEPPER`; optionally set the other FX variables.
3. Deploy the bot/dashboard revision. Startup performs the additive migration.
4. Confirm `/health`, sign in, open **Streaming & Twitch → Channel Rewards**, and create/redeem one test pairing code with a development plugin build.
5. Add a trigger to a test reward, choose **ExcelProtocol FX Plugin**, select an effect, and verify one redemption produces one acknowledged effect. Confirm an offline redemption is discarded without replay and revocation closes the socket.
6. Release ExcelProtocol FX v1.1.0 or newer with [protocol v1](./excelprotocol-fx-protocol-v1.md). Do not claim beta.3 or v1.0.0 supports remote connectivity.

## Rollback

Roll back the app image/code revision. The additive FX tables may safely remain unused. To disable FX immediately without a code rollback, revoke devices in the dashboard. Do not rotate `FX_CREDENTIAL_PEPPER` unless intentionally invalidating every device. If a schema-only rollback is mandatory, first back up the database and drop only the four `fx_*` tables while the rolled-back app is stopped.

