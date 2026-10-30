# ExcelProtocol Hypesquad

Standalone, inactive-by-default service for cross-server **Hyperaids**.

This folder is intentionally disconnected from the existing ExcelProtocol bot:

- no root Python module imports it;
- the root `requirements.txt`, `Dockerfile`, `fly.toml`, dashboard, and bot startup are unchanged;
- pushing this folder does not expose routes, run a scheduler, or alter the bot database;
- it has its own package, database, service token, Twitch credentials, Dockerfile, API, and tests.

## What is ready

```text
Discord server opts in
        ↓
Member receives a one-time Twitch OAuth link
        ↓
Verified Discord ↔ Twitch identity joins the global Hypesquad
        ↓
Owner selection or weekly fair selection among live eligible members
        ↓
hyperaid.started outbox message lists every participating Discord channel
        ↓
Discord adapter posts embeds and forwards Join button interactions
        ↓
Twitch adapter forwards message IDs and author IDs (never message contents)
        ↓
Two consecutive offline checks end the event
        ↓
hyperaid.finished contains per-server and global leaderboards
```

The service measures **chat participation**, not watch time. Twitch does not
provide reliable per-viewer watch presence. Scoring is deliberately capped:

- 10 points for joining;
- 2 points for each distinct active minute;
- 1 message point at most every 30 seconds;
- duplicate Twitch message IDs are ignored.

Raw chat text is neither accepted nor stored. Activity receipts and minute
buckets are deleted when the event finishes; only aggregate leaderboard values
remain.

## Run it independently

```powershell
cd hypesquad
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Set the environment variables from `.env` in the shell or hosting platform,
then run:

```powershell
.\.venv\Scripts\excelprotocol-hypesquad.exe
```

Or build only this service:

```powershell
docker build -t excelprotocol-hypesquad .
```

The weekly scheduler defaults to **off**. Enabling it requires Twitch client
credentials. The service still cannot post to Discord until a Discord adapter
is deliberately connected.

## API security

All `/v1/*` endpoints require:

```http
Authorization: Bearer <HYPESQUAD_SERVICE_TOKEN>
```

The only public endpoints are `/health` and the one-time Twitch OAuth callback.
The service token must be at least 24 characters. OAuth state values are random,
stored only as SHA-256 hashes, expire after ten minutes, and are single-use.
Member Twitch access tokens are revoked after ownership verification and are
never stored.

## API surface

| Method | Route | Intended future caller |
|---|---|---|
| `PUT` | `/v1/guilds/{guild_id}` | Dashboard guild opt-in/settings |
| `GET` | `/v1/guilds/{guild_id}` | Dashboard settings |
| `POST` | `/v1/members/link/start` | Ephemeral Discord Join/Link button |
| `GET` | `/oauth/twitch/callback` | Twitch OAuth redirect |
| `GET` | `/v1/members/{discord_user_id}` | Discord profile/status command |
| `POST` | `/v1/members/{discord_user_id}/leave` | Discord leave button |
| `DELETE` | `/v1/members/{discord_user_id}` | Unlink and privacy deletion |
| `POST` | `/v1/events/manual` | Authorized server-owner selection |
| `POST` | `/v1/events/{event_id}/live` | EventSub or Twitch adapter |
| `POST` | `/v1/events/{event_id}/join` | Persistent Discord Join button |
| `POST` | `/v1/events/{event_id}/activity` | Twitch chat adapter |
| `POST` | `/v1/events/{event_id}/finish` | EventSub offline fallback |
| `POST` | `/v1/events/{event_id}/cancel` | Owner/moderation cancellation |
| `GET` | `/v1/events/{event_id}/leaderboards` | Dashboard/read model |
| `PUT` | `/v1/schedule` | Bot owner network schedule |
| `POST` | `/v1/schedule/tick` | Manual diagnostics only |
| `GET` | `/v1/outbox` | Discord delivery worker |
| `POST` | `/v1/outbox/{id}/ack` | Discord delivery acknowledgement |

See [INTEGRATION.md](INTEGRATION.md) for the exact adapter sequence and payloads.

## Fairness and abuse controls

- Only members of opted-in guilds are eligible.
- Discord and Twitch identities are one-to-one.
- Weekly selection considers only candidates Twitch reports live.
- The least-selected eligible group is chosen first, then randomized.
- A configurable cooldown (default 42 days) prevents repeat selection.
- Featured streamers cannot join their own Hyperaid.
- Only members who explicitly press Join can score.
- Duplicate messages, rapid spam, and repeated active-minute events do not farm points.
- Events and their participant aggregates survive process restarts.
- The outbox makes future Discord delivery retryable and idempotent.

Before integration, the main privacy policy needs an explicit Hypesquad section
covering cross-server identity linking, activity aggregates, public leaderboards,
retention, opt-out, unlinking, and moderation.

## Tests

```powershell
cd hypesquad
..\.venv\Scripts\python.exe -m pytest -q
```

