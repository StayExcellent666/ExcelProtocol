# Changelog

## Round 2 — Tests + bug fixes (May 2026)

### Removed

- **Per-member welcome message feature** — was added in Round 1, never used.
  Removed `_send_welcome` from bot.py, the welcome migrations from
  database.py, and the welcome PATCH/GET handlers from dashboard_server.py.
  The two columns (`server_settings.welcome_channel_id` and
  `server_settings.welcome_message`) remain in production DBs as harmless
  unused columns — schema downgrades are riskier than dead schema, so we
  leave them.

### Bug fixes (caught by the new test suite)

- **`reaction_roles.body_text` migration ran before the table was created.**
  Worked in production (existing DBs already had the table from earlier
  builds), but broke fresh-DB scenarios with a "no such table" error caught
  by the test suite's idempotent-init test. Moved the migration to run
  immediately after the `CREATE TABLE reaction_roles` statement.

- **`vc_settings` migration logged a noisy warning on fresh DBs.** When the
  table didn't exist yet, the migration tried to read columns from it and
  caught a syntax error, logging it as a warning. Added an early-bail when
  the table doesn't exist. Test added to assert no warnings on fresh init.

### Refactors enabling tests

These are pure code reshaping — no behavior change:

- Extracted `verify_eventsub_signature(secret, msg_id, ts, body, sig) -> bool`
  from inside `eventsub_callback` in dashboard_server.py.
- Extracted `_prune_oauth_states(states, ttl_seconds, now)` from inside
  `auth_login` in dashboard_server.py.
- Moved milestone time math (`compute_hours_live`, `should_fire_milestone`,
  `MILESTONE_DEFS`) from bot.py into utils.py so tests can import them
  without instantiating a Discord bot.

### Test suite

84 pytest tests covering:

- **test_utils.py** (21 tests) — sanitise_streamer_name URL parsing edge
  cases, utcnow timezone-awareness, parse_twitch_iso round-trip.
- **test_database.py** (23 tests) — schema migrations, idempotent init,
  fresh-DB-no-warnings regression, mark_stream_ended correctness across
  multi-guild and multi-session edge cases, rename cascade, leaderboard SQL
  math (hours, longest, streak, per-guild scoping).
- **test_dashboard.py** (19 tests) — HMAC verification rejects tampered
  body / msg ID / timestamp / wrong secret / empty signature, OAuth state
  TTL pruning, _session_can_access_guild permission gating.
- **test_milestones.py** (21 tests) — compute_hours_live correctness
  including naive-datetime tolerance, should_fire_milestone threshold
  behavior, end-to-end gating logic from 4h59m through 24h marathons.

### CI

- New `.github/workflows/tests.yml` runs pytest on every PR to main.
- Updated `.github/workflows/deploy.yml` to gate deploy on a new `test` job —
  if tests fail, deploy doesn't run.

### Running tests locally

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

Should print `84 passed`. If you ever add a feature touching the leaderboard,
HMAC, milestones, or schema, add a test for it before deploy.

---

## Round 1 — May 2026

### Schema migrations (run automatically on first boot)

- `stream_events.ended_at` (TIMESTAMP, nullable) — populated by
  `mark_stream_ended` on `stream.offline`
- `global_stream_events.ended_at` (TIMESTAMP, nullable) — same

### Bug fixes

- **Streamer rename cascade** — when a Twitch user renames, the rename now
  rewrites every related table (notification_messages, milestone_sent,
  unresolvable_streamers, notification_log, stream_events) and the
  in-memory live_streamers set + _stream_starts cache.
- **Twitch token refresh race** — added an asyncio.Lock around
  TwitchAPI.get_access_token().
- **Milestone polling** — replaced the 5-min Twitch poll with a local
  _stream_starts cache populated from EventSub. The bot now only hits
  Twitch when a milestone actually fires.
- **Discord deprecation** — member.ban(delete_message_days=1) →
  delete_message_seconds=86400.
- **Python deprecation** — datetime.utcnow() swapped for utils.utcnow()
  (timezone-aware).
- **Dashboard DB pooling** — replaced per-request aiosqlite.connect with a
  single long-lived async connection.
- **Stale assets** — deleted 55 obsolete dashboard/dist/assets/index-*.js
  files.

### New features

- **Enhanced leaderboards** — /leaderboard and /globalleaderboard now show
  hours streamed, longest session, and consecutive-day streak alongside the
  existing stream count.

### Removed

- /twitchset slash command — Twitch channel linking is now done via the
  dashboard.

### New file

- utils.py — shared helpers (utcnow, sanitise_streamer_name, parse_twitch_iso).
