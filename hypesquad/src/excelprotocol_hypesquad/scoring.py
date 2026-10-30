"""Hypesquad activity scoring.

The service intentionally scores capped chat participation, not purported
watch time. Twitch does not expose reliable per-viewer watch presence.
"""

JOIN_POINTS = 10
ACTIVE_MINUTE_POINTS = 2
SCORED_MESSAGE_POINTS = 1
MESSAGE_SCORE_COOLDOWN_SECONDS = 30


def score_delta(*, new_active_minute: bool, scored_message: bool) -> int:
    return (
        (ACTIVE_MINUTE_POINTS if new_active_minute else 0)
        + (SCORED_MESSAGE_POINTS if scored_message else 0)
    )

