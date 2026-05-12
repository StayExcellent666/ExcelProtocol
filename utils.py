"""Shared utilities for ExcelProtocol.

Keep this small and dependency-free so it can be imported anywhere
without circular-import worries.
"""

from datetime import datetime, timezone

# Common Twitch URL prefixes a user might paste in
_TWITCH_URL_PREFIXES = (
    "https://www.twitch.tv/",
    "http://www.twitch.tv/",
    "https://twitch.tv/",
    "http://twitch.tv/",
    "twitch.tv/",
)


def sanitise_streamer_name(raw: str) -> str:
    """Strip URLs, @-prefixes, and trailing query/path from a Twitch handle.

    Accepts inputs like:
      - https://twitch.tv/username
      - https://www.twitch.tv/username/about
      - twitch.tv/username?foo=bar
      - @username
      - USERNAME

    Returns the lowercased login portion only. Returns "" for empty input.
    """
    if not raw:
        return ""
    name = raw.strip()
    lower = name.lower()
    for prefix in _TWITCH_URL_PREFIXES:
        if lower.startswith(prefix):
            name = name[len(prefix):]
            break
    name = name.lstrip("@")
    # Strip any trailing path segments or query strings
    name = name.split("/")[0].split("?")[0].strip()
    return name.lower()


def utcnow() -> datetime:
    """Timezone-aware UTC now. Replacement for the deprecated datetime.utcnow()."""
    return datetime.now(timezone.utc)


def parse_twitch_iso(ts: str) -> datetime:
    """Parse a Twitch API ISO timestamp ('2024-01-15T12:34:56Z') as a UTC-aware datetime."""
    # Twitch consistently returns trailing Z; replace so fromisoformat accepts it
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


# ── Milestone math (pure, testable) ──────────────────────────────────────────

MILESTONE_DEFS = (
    (5,  "⏱️ **{user_name}** has been live for **5 HOURS!** They're not stopping anytime soon!"),
    (10, "💀 **{user_name}** has been live for **10 HOURS STRAIGHT.** Send help. 👀"),
)


def compute_hours_live(started_at: datetime, now: datetime) -> float:
    """Return hours since `started_at` as a float.

    Tolerates a naive `started_at` by tagging it as UTC — the on_ready restore
    path in bot.py can hand us a naive datetime parsed from SQLite's
    CURRENT_TIMESTAMP. Both sides need consistent tz info or subtraction raises.
    """
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return (now - started_at).total_seconds() / 3600


def should_fire_milestone(hours_live: float, milestone_hours: int) -> bool:
    """A milestone fires once `hours_live` reaches the threshold."""
    return hours_live >= milestone_hours


def is_already_offline_processed(name_lower: str, live_streamers, stream_starts) -> bool:
    """Return True if this streamer's offline event has already been processed.

    Used by handle_stream_offline to deduplicate Twitch's at-least-once webhook
    deliveries. EventSub redelivers under network blips and across restarts, so
    we may receive multiple stream.offline events for one actual offline.

    A streamer is "already processed" if BOTH:
      - they're not in the live_streamers set, AND
      - they're not in the stream_starts cache

    On a normal first-time offline, the streamer is in BOTH sets (populated
    when stream.online fired). The handler discards them from both, then a
    duplicate event sees empty state and skips.

    Edge case: on a fresh restart with no DB-restored state, both sets are
    empty for everyone — but `on_ready` populates them from
    `notification_messages` for actively-notified streamers. Streamers we
    have no record of legitimately have nothing to clean up, so a "duplicate"
    log is correct to skip.
    """
    return name_lower not in live_streamers and name_lower not in stream_starts


def classify_reconcile_action(
    name_lower: str,
    twitch_says_live: bool,
    in_live_streamers: bool,
    started_at,
    now,
    fresh_threshold_minutes: int = 10,
) -> str:
    """Classify what to do for one streamer during startup reconciliation.

    Returns one of:
      - 'notify'    — Twitch says live, we don't know, stream started recently.
                      Process as a new stream.online event (send notifications).
      - 'absorb'    — Twitch says live, we don't know, but stream is old. Add
                      to tracking silently without notifying — they've been
                      live since before our outage.
      - 'cleanup'   — Twitch says offline, we think they're live. Clean up
                      stale state (live_streamers, _stream_starts, mark_stream_ended).
      - 'no_action' — Everything matches. No-op.

    `started_at` is a tz-aware datetime from Twitch (or None for the cleanup
    case where we'd never read it). `now` is the current tz-aware datetime.
    `fresh_threshold_minutes` is how recently a stream had to start for us
    to treat it as "new" rather than "pre-existing."
    """
    if twitch_says_live and in_live_streamers:
        return 'no_action'
    if (not twitch_says_live) and (not in_live_streamers):
        return 'no_action'

    if twitch_says_live and not in_live_streamers:
        if started_at is None:
            # No timestamp info; assume pre-existing to be safe (don't spam)
            return 'absorb'
        if started_at.tzinfo is None:
            from datetime import timezone as _tz
            started_at = started_at.replace(tzinfo=_tz.utc)
        age_seconds = (now - started_at).total_seconds()
        if age_seconds < 0:
            # Clock skew — treat as fresh
            return 'notify'
        if age_seconds <= fresh_threshold_minutes * 60:
            return 'notify'
        return 'absorb'

    # twitch_says_live=False and in_live_streamers=True
    return 'cleanup'
