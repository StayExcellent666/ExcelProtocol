"""Tests for milestone time math.

The actual milestone-firing path in bot.py also depends on the bot, Discord,
and Twitch — those parts aren't tested here. What IS tested is the pure math:
given a `started_at` and a `now`, do we correctly decide whether to fire?
"""
from datetime import datetime, timedelta, timezone
from utils import compute_hours_live, should_fire_milestone, MILESTONE_DEFS


# ── compute_hours_live ────────────────────────────────────────────────────────

class TestComputeHoursLive:

    def test_one_hour(self):
        start = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        now   = datetime(2026, 5, 1, 13, 0, 0, tzinfo=timezone.utc)
        assert compute_hours_live(start, now) == 1.0

    def test_fractional_hours(self):
        start = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        now   = datetime(2026, 5, 1, 12, 30, 0, tzinfo=timezone.utc)
        assert compute_hours_live(start, now) == 0.5

    def test_five_hours_exact(self):
        start = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        now   = datetime(2026, 5, 1, 17, 0, 0, tzinfo=timezone.utc)
        assert compute_hours_live(start, now) == 5.0

    def test_naive_started_at_tolerated(self):
        """on_ready restore path may hand us a naive started_at parsed from SQLite.
        The helper must NOT raise 'can't subtract offset-naive and offset-aware'."""
        start_naive = datetime(2026, 5, 1, 12, 0, 0)  # no tzinfo
        now         = datetime(2026, 5, 1, 17, 0, 0, tzinfo=timezone.utc)
        # Should treat naive as UTC and not raise
        result = compute_hours_live(start_naive, now)
        assert result == 5.0

    def test_negative_when_now_before_start(self):
        """Edge case: if clock skews, computed hours can be negative.
        Should NOT crash — should_fire_milestone will simply return False."""
        start = datetime(2026, 5, 1, 13, 0, 0, tzinfo=timezone.utc)
        now   = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = compute_hours_live(start, now)
        assert result == -1.0


# ── should_fire_milestone ─────────────────────────────────────────────────────

class TestShouldFireMilestone:

    def test_below_threshold(self):
        # 4h59m, 5h milestone → no fire
        assert should_fire_milestone(4.99, 5) is False

    def test_at_threshold(self):
        # Exactly 5h → fire
        assert should_fire_milestone(5.0, 5) is True

    def test_above_threshold(self):
        assert should_fire_milestone(5.1, 5) is True
        assert should_fire_milestone(7.5, 5) is True

    def test_negative_hours_no_fire(self):
        # Clock-skew defense
        assert should_fire_milestone(-1.0, 5) is False

    def test_zero_hours_no_fire(self):
        assert should_fire_milestone(0.0, 5) is False

    def test_10h_milestone(self):
        assert should_fire_milestone(9.99, 10) is False
        assert should_fire_milestone(10.0, 10) is True
        assert should_fire_milestone(15.0, 10) is True


# ── End-to-end milestone gating logic ─────────────────────────────────────────
# These compose compute_hours_live + should_fire_milestone the same way
# check_milestones does, with realistic timestamps.

class TestMilestoneGating:

    def _check_milestones_at(self, started_at, now):
        """Replicate the gating loop in check_milestones (pure subset)."""
        hours = compute_hours_live(started_at, now)
        fired = []
        for milestone_hours, _template in MILESTONE_DEFS:
            if should_fire_milestone(hours, milestone_hours):
                fired.append(milestone_hours)
        return fired

    def test_fresh_stream_no_milestones(self):
        start = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        # 30 min in
        now = start + timedelta(minutes=30)
        assert self._check_milestones_at(start, now) == []

    def test_4h59m_no_milestones(self):
        start = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = start + timedelta(hours=4, minutes=59)
        assert self._check_milestones_at(start, now) == []

    def test_5h01m_fires_5h_only(self):
        start = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = start + timedelta(hours=5, minutes=1)
        assert self._check_milestones_at(start, now) == [5]

    def test_9h59m_fires_5h_only(self):
        start = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = start + timedelta(hours=9, minutes=59)
        assert self._check_milestones_at(start, now) == [5]

    def test_10h01m_fires_both(self):
        """At 10h01m both milestones should be ready to fire (DB dedup
        prevents 5h from firing twice — that's a separate concern)."""
        start = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = start + timedelta(hours=10, minutes=1)
        assert self._check_milestones_at(start, now) == [5, 10]

    def test_marathon_24h(self):
        start = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        now = start + timedelta(hours=24)
        assert self._check_milestones_at(start, now) == [5, 10]


# ── MILESTONE_DEFS structure ──────────────────────────────────────────────────

class TestMilestoneDefs:
    """Sanity-check the milestone constants used across bot + tests."""

    def test_two_milestones_defined(self):
        assert len(MILESTONE_DEFS) == 2

    def test_5h_and_10h(self):
        thresholds = [m[0] for m in MILESTONE_DEFS]
        assert thresholds == [5, 10]

    def test_templates_have_user_name_placeholder(self):
        """Every template must format with {user_name} or it'll crash at send time."""
        for milestone_hours, template in MILESTONE_DEFS:
            rendered = template.format(user_name="testuser")
            assert "testuser" in rendered, f"{milestone_hours}h template missing {{user_name}}"

    def test_thresholds_are_ascending(self):
        """check_milestones iterates in order; thresholds must be ascending so
        we can break early if needed (currently we don't, but it's a sanity check)."""
        thresholds = [m[0] for m in MILESTONE_DEFS]
        assert thresholds == sorted(thresholds)
