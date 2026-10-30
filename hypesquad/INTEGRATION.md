# Bot integration contract

This document describes the future connection points. None are wired into the
current ExcelProtocol bot.

## 1. Guild opt-in

The dashboard verifies the caller owns or manages the Discord guild, validates
both channel IDs inside that guild, then calls:

```http
PUT /v1/guilds/123
Authorization: Bearer <service token>
Content-Type: application/json

{
  "enabled": true,
  "announcement_channel_id": "456",
  "leaderboard_channel_id": "789",
  "auto_weekly": true
}
```

The Hypesquad service trusts the bot as its authorization boundary. Never expose
the service token to a browser; the dashboard backend must proxy these calls.

## 2. Member Join/Link button

When a member clicks **Join Hypesquad**, the Discord adapter calls:

```http
POST /v1/members/link/start

{"discord_user_id":"42","guild_id":"123"}
```

It sends `authorization_url` back ephemerally. The OAuth callback proves Twitch
ownership, links the identities, joins the member to that guild's Hypesquad, and
discards the Twitch user token.

## 3. Selection

An authorized guild owner may request:

```http
POST /v1/events/manual

{"discord_user_id":"42","source_guild_id":"123"}
```

The built-in scheduler can instead select once a week from eligible members who
are live. Both flows create a restart-safe `waiting_live` event. The scheduler
polls waiting events and changes the event to `active` when Twitch reports the
featured streamer live.

## 4. Discord announcements

The bot polls `GET /v1/outbox`. A `hyperaid.started` payload contains:

```json
{
  "event_id": "...",
  "target_twitch_login": "featured_streamer",
  "guild_targets": [
    {"guild_id": "123", "announcement_channel_id": "456"}
  ]
}
```

For each target, post a Hyperaid embed with a persistent custom ID such as:

```text
hypesquad_join:<event_id>
```

Only acknowledge the outbox message after every delivery attempt has been
recorded. A later integration may add per-target delivery state if partial retry
is required.

## 5. Join button

The Discord persistent-view callback forwards:

```http
POST /v1/events/{event_id}/join

{"discord_user_id":"42","guild_id":"123"}
```

The service rejects unlinked users, users who did not join through that guild,
nonparticipating guilds, inactive events, duplicates, and the featured streamer.
Successful responses are suitable for an ephemeral confirmation.

## 6. Twitch activity

While an event is active, the existing Twitch chat bot temporarily joins the
featured channel. For each message it forwards metadata only:

```http
POST /v1/events/{event_id}/activity

{
  "twitch_user_id": "987",
  "message_id": "twitch-message-id",
  "occurred_at": "2026-10-30T20:15:00Z"
}
```

Do not send message text. The service ignores users who did not press Join and
deduplicates message IDs.

## 7. Stream completion

The scheduler requires two consecutive offline checks before finishing, which
reduces false endings during transient Twitch API failures. EventSub may also
call `POST /v1/events/{event_id}/finish` directly after a verified offline event.

The resulting `hyperaid.finished` outbox payload contains each opted-in guild's
leaderboard channel and rows, plus an optional global leaderboard. The Discord
adapter renders those rows and acknowledges the message.

## 8. Required integration safeguards

- Reuse the dashboard's live Discord permission revalidation for guild settings.
- Resolve all announcement and leaderboard channels through the route guild,
  never through a global channel lookup.
- Keep the Hypesquad service token server-side.
- Restore persistent Join views after bot restarts.
- Join/part Twitch chat channels only for active or waiting Hyperaids.
- Rate-limit member link starts, manual selections, joins, and activity forwarding.
- Add dashboard audit-log entries for opt-in, settings, manual selection, and moderation.
- Add owner moderation for suspensions, reports, and network-wide exclusions before launch.
- Update Terms and Privacy before enabling the feature for real members.

