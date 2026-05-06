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
