# Changelog — May 2026 Update

## Schema migrations (run automatically on first boot)

- `stream_events.ended_at` (TIMESTAMP, nullable) — populated by `mark_stream_ended` on `stream.offline`
- `global_stream_events.ended_at` (TIMESTAMP, nullable) — same
- `server_settings.welcome_channel_id` (INTEGER, nullable) — channel for welcome messages
- `server_settings.welcome_message` (TEXT, nullable) — template with `{user}` `{username}` `{server}` `{member_count}`

All four are `ALTER TABLE ADD COLUMN` and idempotent. See BACKUP_AND_RESTORE.md
before deploying.

## Bug fixes

- **Streamer rename cascade** — when a Twitch user renames, the rename now
  rewrites every related table (`notification_messages`, `milestone_sent`,
  `unresolvable_streamers`, `notification_log`, `stream_events`) and the
  in-memory `live_streamers` set + `_stream_starts` cache. Previously these
  were left stranded under the old name.
- **Twitch token refresh race** — added an `asyncio.Lock` around
  `TwitchAPI.get_access_token()` so two concurrent callers can't both POST
  to `/oauth2/token` at the same time.
- **Milestone polling** — replaced the 5-min Twitch poll with a local
  `_stream_starts` cache populated from EventSub. The bot now only hits
  Twitch when a milestone actually fires (down from "every 5 minutes for
  every live streamer"). Restored from `notification_messages.sent_at` on
  startup so milestones still work across deploys.
- **Discord deprecation** — `member.ban(delete_message_days=1)` →
  `delete_message_seconds=86400`. Required by `discord.py` 2.4+.
- **Python deprecation** — every `datetime.utcnow()` (deprecated in 3.12)
  swapped for `utils.utcnow()` (timezone-aware). Naive `datetime.strptime`
  on Twitch ISO strings replaced with `parse_twitch_iso`.
- **Code quality** — 32+ inline imports moved to top-of-file; ~22
  `except: pass` blocks given debug logs; duplicate `logger = ...` block
  in `dashboard_server.py` removed; URL-strip logic DRYed into
  `utils.sanitise_streamer_name` (was duplicated in 4 places).
- **Dashboard DB pooling** — replaced per-request `aiosqlite.connect`
  with a single long-lived async connection.
- **Stale assets** — deleted 55 obsolete `dashboard/dist/assets/index-*.js`
  files. Only the 2 currently referenced by `index.html` remain.
- **quickstart.sh** — rewritten; previous version referenced
  non-existent files (`.env.example`, `DEPLOYMENT.md`, `TESTING.md`) and
  the old bot name.

## New features

### Enhanced leaderboards (Feature #7)

`/leaderboard` and `/globalleaderboard` now show:

- Total streams (existing)
- Total hours streamed (sum of completed sessions this month)
- Longest single session this month
- Current consecutive-day streak (server leaderboard only)

Hours and longest only count *completed* sessions — a stream still in
progress contributes to stream-count but not duration. Streamers with
`null` `ended_at` rows (e.g., from before this update) keep their
existing stream count metric and just don't display the duration metrics.

### Welcome messages (Feature #8)

Per-server welcome message in a configurable channel. Variables:
- `{user}` — mention (`@User`)
- `{username}` — display name without mention
- `{server}` — server name
- `{member_count}` — current member count

Set via `PATCH /api/guild/{guild_id}/settings`:
```json
{ "welcome_channel_id": "1234567890", "welcome_message": "Welcome {user} to {server}! You're our {member_count}th member!" }
```

To disable, send `welcome_channel_id: null`. Read via
`GET /api/guild/{guild_id}/settings` (now returns `welcome_channel_id`
and `welcome_message`).

The welcome runs *after* the safety filter, so kicked/banned users aren't
welcomed. Bots are skipped. The dashboard frontend UI for this is not
included in this update — wire it into the React dashboard yourself.

## Removed

- `/twitchset` slash command — Twitch channel linking is now done via the
  dashboard. The other slash commands (`/twitchremove`, `/twitchstatus`,
  `/twitchstats`) still work. `/help` and the various error messages were
  updated to point at the dashboard.

## New file

- `utils.py` — shared helpers (`utcnow`, `sanitise_streamer_name`,
  `parse_twitch_iso`). Imported by `bot.py`, `dashboard_server.py`,
  `twitch_api.py`, `twitch_bot.py`, `birthday_cog.py`.

## Files modified

`bot.py`, `database.py`, `dashboard_server.py`, `twitch_api.py`,
`twitch_bot.py`, `birthday_cog.py`, `reaction_roles.py`,
`twitch_chat_cog.py`, `quickstart.sh`.

## Not yet done (for a future update)

- Frontend React UI for welcome message settings (backend is wired up,
  needs a settings tab section)
- pytest test suite (HMAC, OAuth state, `sanitise_streamer_name`,
  `_session_can_access_guild`, milestone time math) — recommended after
  this deploy is verified working

## Verifying after deploy

Watch the bot log channel for these messages on first boot:

```
Migration: added ended_at to stream_events
Migration: added ended_at to global_stream_events
Migration: added welcome_channel_id to server_settings
Migration: added welcome_message to server_settings
Database initialized at /data/twitch_bot.db
```

Then test:

1. **Streamer notification** — confirm a stream notification still posts
   normally (smoke test for `send_notification` + `log_stream_event` with
   the new `started_at` parameter).
2. **Milestone test** — if you have a streamer who's been live 5+ hours
   (or wait until you do), confirm the milestone embed posts. Should
   only happen once per session per server.
3. **Stream end** — when a streamer goes offline, check
   `stream_events.ended_at` is populated:
   ```sql
   SELECT streamer_name, went_live_at, ended_at
   FROM stream_events
   WHERE ended_at IS NOT NULL
   ORDER BY id DESC LIMIT 5;
   ```
4. **Leaderboard** — `/leaderboard` should show the new format with hours
   total / longest / streak suffix on each row (when the stream session
   has actually ended).
5. **Welcome message** — set one via the API:
   ```bash
   curl -X PATCH https://excelprotocol.fly.dev/api/guild/<your_guild_id>/settings \
     -H "Cookie: ep_session=<your_session_cookie>" \
     -H "Content-Type: application/json" \
     -d '{"welcome_channel_id":"<test_channel_id>","welcome_message":"hi {user}!"}'
   ```
   Have a test account join the server. Welcome should post.
