"""Tests for utils.py — pure helper functions."""
from datetime import datetime, timezone, timedelta
from utils import (
    sanitise_streamer_name,
    utcnow,
    parse_twitch_iso,
    is_already_offline_processed,
)


# ── sanitise_streamer_name ────────────────────────────────────────────────────

class TestSanitiseStreamerName:
    """sanitise_streamer_name accepts URLs and returns a clean lowercase login."""

    def test_plain_username(self):
        assert sanitise_streamer_name("ninja") == "ninja"

    def test_lowercases_uppercase(self):
        assert sanitise_streamer_name("Ninja") == "ninja"
        assert sanitise_streamer_name("NINJA") == "ninja"

    def test_strips_at_prefix(self):
        assert sanitise_streamer_name("@ninja") == "ninja"
        assert sanitise_streamer_name("@@ninja") == "ninja"

    def test_strips_https_www_url(self):
        assert sanitise_streamer_name("https://www.twitch.tv/ninja") == "ninja"

    def test_strips_https_url(self):
        assert sanitise_streamer_name("https://twitch.tv/ninja") == "ninja"

    def test_strips_http_url(self):
        assert sanitise_streamer_name("http://twitch.tv/ninja") == "ninja"
        assert sanitise_streamer_name("http://www.twitch.tv/ninja") == "ninja"

    def test_strips_protocol_less_url(self):
        assert sanitise_streamer_name("twitch.tv/ninja") == "ninja"

    def test_strips_path_after_username(self):
        # When users paste profile / about / video URLs
        assert sanitise_streamer_name("https://twitch.tv/ninja/about") == "ninja"
        assert sanitise_streamer_name("https://twitch.tv/ninja/videos") == "ninja"

    def test_strips_query_string(self):
        assert sanitise_streamer_name("https://twitch.tv/ninja?foo=bar") == "ninja"

    def test_strips_path_and_query(self):
        assert sanitise_streamer_name("https://twitch.tv/ninja/about?foo=bar") == "ninja"

    def test_handles_whitespace(self):
        assert sanitise_streamer_name("  ninja  ") == "ninja"
        assert sanitise_streamer_name("\tninja\n") == "ninja"

    def test_mixed_case_url(self):
        # URL prefix matched case-insensitively
        assert sanitise_streamer_name("HTTPS://Twitch.TV/Ninja") == "ninja"

    def test_empty_string(self):
        assert sanitise_streamer_name("") == ""

    def test_just_whitespace(self):
        assert sanitise_streamer_name("   ") == ""

    def test_underscore_username(self):
        # Twitch usernames allow underscores
        assert sanitise_streamer_name("https://twitch.tv/some_streamer_123") == "some_streamer_123"


# ── utcnow ────────────────────────────────────────────────────────────────────

class TestUtcnow:
    def test_is_timezone_aware(self):
        now = utcnow()
        assert now.tzinfo is not None, "utcnow() must return a timezone-aware datetime"

    def test_is_utc(self):
        now = utcnow()
        # tzinfo.utcoffset returns timedelta(0) for UTC
        assert now.utcoffset() == timedelta(0)

    def test_close_to_now(self):
        # Sanity: utcnow should be within a couple of seconds of system clock
        now = utcnow()
        sys_now = datetime.now(timezone.utc)
        assert abs((sys_now - now).total_seconds()) < 5


# ── parse_twitch_iso ──────────────────────────────────────────────────────────

class TestParseTwitchIso:
    """Twitch returns 'YYYY-MM-DDTHH:MM:SSZ' timestamps. Parser must produce
    a timezone-aware UTC datetime that supports arithmetic with utcnow()."""

    def test_parses_z_suffix(self):
        dt = parse_twitch_iso("2024-01-15T12:34:56Z")
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15
        assert dt.hour == 12
        assert dt.minute == 34
        assert dt.second == 56

    def test_returns_timezone_aware(self):
        dt = parse_twitch_iso("2024-01-15T12:34:56Z")
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(0)

    def test_arithmetic_with_utcnow(self):
        # Critical: parse_twitch_iso output must be subtractable from utcnow()
        # without raising "can't subtract offset-naive and offset-aware datetimes"
        dt = parse_twitch_iso("2024-01-15T12:34:56Z")
        delta = utcnow() - dt
        assert delta.total_seconds() > 0  # past time → positive delta


# ── is_already_offline_processed ──────────────────────────────────────────────

class TestIsAlreadyOfflineProcessed:
    """Idempotency guard for handle_stream_offline.

    Returns True only when the streamer is absent from BOTH tracking sets,
    meaning a previous offline event already cleaned them up.
    """

    def test_actively_live_streamer_not_processed(self):
        """Streamer in both sets — first offline event, must process."""
        live = {"alice"}
        starts = {"alice": datetime.now(timezone.utc)}
        assert is_already_offline_processed("alice", live, starts) is False

    def test_only_in_live_set_not_processed(self):
        """Streamer in live_streamers but not _stream_starts (e.g., on_ready
        restored from DB but couldn't parse the timestamp)."""
        live = {"alice"}
        starts = {}
        assert is_already_offline_processed("alice", live, starts) is False

    def test_only_in_stream_starts_not_processed(self):
        """Defensive — shouldn't normally happen but if state is inconsistent
        we err on the side of processing."""
        live = set()
        starts = {"alice": datetime.now(timezone.utc)}
        assert is_already_offline_processed("alice", live, starts) is False

    def test_already_cleaned_up_returns_true(self):
        """The duplicate scenario — both sets empty for this streamer."""
        live = {"bob"}  # other streamers may be live
        starts = {"bob": datetime.now(timezone.utc)}
        assert is_already_offline_processed("alice", live, starts) is True

    def test_empty_state_returns_true(self):
        """Bot just started with no restored state — duplicate offline events
        for streamers we never tracked are correctly treated as already-done."""
        assert is_already_offline_processed("alice", set(), {}) is True

    def test_uses_lowercase_key(self):
        """Caller is responsible for lowercasing — function does NOT case-fold.
        This documents the contract."""
        live = {"alice"}  # lowercase
        starts = {"alice": datetime.now(timezone.utc)}
        # If caller passes uppercase, function won't find it → returns True
        assert is_already_offline_processed("ALICE", live, starts) is True
        # Lowercase lookup works correctly
        assert is_already_offline_processed("alice", live, starts) is False
