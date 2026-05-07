"""Tests for database.py — schema, migrations, and leaderboard SQL.

Each test gets a fresh temp DB via the `db` fixture in conftest.py.
"""
from datetime import datetime, timedelta, timezone


# ── Schema initialisation ─────────────────────────────────────────────────────

class TestSchema:
    """Migrations run on init and the right columns exist afterwards."""

    def test_stream_events_has_ended_at(self, db):
        conn = db.get_connection()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(stream_events)").fetchall()]
        conn.close()
        assert "ended_at" in cols, "stream_events.ended_at migration must run"
        assert "went_live_at" in cols
        assert "streamer_name" in cols
        assert "guild_id" in cols

    def test_global_stream_events_has_ended_at(self, db):
        conn = db.get_connection()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(global_stream_events)").fetchall()]
        conn.close()
        assert "ended_at" in cols

    def test_init_is_idempotent(self, tmp_db_path):
        """Calling init_database twice must not raise — migrations re-run safely."""
        from database import Database
        db1 = Database(db_path=tmp_db_path)
        # Simulate restart
        db2 = Database(db_path=tmp_db_path)
        # Just confirm we can still query
        conn = db2.get_connection()
        n = conn.execute("SELECT COUNT(*) FROM monitored_streamers").fetchone()[0]
        conn.close()
        assert n == 0

    def test_no_warnings_on_fresh_db(self, tmp_db_path, caplog):
        """Fresh DB init must not emit any WARNING-level logs from migrations.

        Regression test: previously the vc_settings migration tried to SELECT
        from a non-existent table on fresh DBs, raising a syntax error that
        was caught and logged as a warning.
        """
        import logging
        from database import Database
        with caplog.at_level(logging.WARNING, logger="database"):
            Database(db_path=tmp_db_path)
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings == [], f"Fresh DB init logged warnings: {[r.message for r in warnings]}"


# ── log_stream_event + mark_stream_ended ──────────────────────────────────────

class TestStreamEvents:

    def test_log_stream_event_without_started_at(self, db):
        """Old code path — no started_at, falls back to CURRENT_TIMESTAMP."""
        db.log_stream_event(guild_id=100, streamer_name="alice")
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT guild_id, streamer_name, went_live_at, ended_at FROM stream_events"
        ).fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == 100
        assert rows[0][1] == "alice"
        assert rows[0][2] is not None  # went_live_at populated
        assert rows[0][3] is None       # ended_at not yet

    def test_log_stream_event_with_started_at(self, db):
        """New code path — started_at from EventSub is honored."""
        explicit_start = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        db.log_stream_event(guild_id=100, streamer_name="alice", started_at=explicit_start)
        conn = db.get_connection()
        rows = conn.execute("SELECT went_live_at FROM stream_events").fetchall()
        conn.close()
        assert "2026-05-01" in rows[0][0]

    def test_log_stream_event_lowercases_name(self, db):
        db.log_stream_event(guild_id=100, streamer_name="ALICE")
        conn = db.get_connection()
        rows = conn.execute("SELECT streamer_name FROM stream_events").fetchall()
        conn.close()
        assert rows[0][0] == "alice"

    def test_mark_stream_ended_sets_ended_at(self, db):
        """The most-recent open row gets ended_at populated."""
        db.log_stream_event(guild_id=100, streamer_name="alice")
        db.mark_stream_ended("alice")
        conn = db.get_connection()
        row = conn.execute(
            "SELECT went_live_at, ended_at FROM stream_events WHERE streamer_name='alice'"
        ).fetchone()
        conn.close()
        assert row[1] is not None, "ended_at must be populated after mark_stream_ended"

    def test_mark_stream_ended_with_explicit_time(self, db):
        explicit_end = datetime(2026, 5, 1, 18, 30, 0, tzinfo=timezone.utc)
        db.log_stream_event(guild_id=100, streamer_name="alice")
        db.mark_stream_ended("alice", ended_at=explicit_end)
        conn = db.get_connection()
        ended = conn.execute(
            "SELECT ended_at FROM stream_events WHERE streamer_name='alice'"
        ).fetchone()[0]
        conn.close()
        assert "2026-05-01 18:30" in ended

    def test_mark_stream_ended_only_touches_open_rows(self, db):
        """If a row is already closed, a later mark_stream_ended doesn't overwrite."""
        end1 = datetime(2026, 5, 1, 18, 0, 0, tzinfo=timezone.utc)
        end2 = datetime(2026, 5, 1, 19, 0, 0, tzinfo=timezone.utc)
        db.log_stream_event(guild_id=100, streamer_name="alice")
        db.mark_stream_ended("alice", ended_at=end1)
        # Add a SECOND stream session then close it — the first row must remain at end1
        db.log_stream_event(guild_id=100, streamer_name="alice")
        db.mark_stream_ended("alice", ended_at=end2)
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT id, ended_at FROM stream_events WHERE streamer_name='alice' ORDER BY id"
        ).fetchall()
        conn.close()
        assert len(rows) == 2
        assert "18:00" in rows[0][1], f"first row should keep its 18:00 ended_at, got {rows[0][1]}"
        assert "19:00" in rows[1][1], f"second row should be closed at 19:00, got {rows[1][1]}"

    def test_mark_stream_ended_per_guild(self, db):
        """One streamer in two guilds — both their open rows get closed."""
        db.log_stream_event(guild_id=100, streamer_name="alice")
        db.log_stream_event(guild_id=200, streamer_name="alice")
        end = datetime(2026, 5, 1, 18, 0, 0, tzinfo=timezone.utc)
        db.mark_stream_ended("alice", ended_at=end)
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT guild_id, ended_at FROM stream_events WHERE streamer_name='alice'"
        ).fetchall()
        conn.close()
        assert len(rows) == 2
        # Both rows should have ended_at set
        for r in rows:
            assert r[1] is not None, f"guild {r[0]} row was not closed"


# ── update_streamer_login (rename cascade) ────────────────────────────────────

class TestRenameCascade:
    """update_streamer_login must rewrite related tables."""

    def test_renames_monitored_streamers(self, db):
        db.add_streamer(guild_id=100, streamer_name="oldname", channel_id=5000)
        affected = db.update_streamer_login("oldname", "newname")
        assert affected == 1
        # Old name should be gone
        conn = db.get_connection()
        rows = conn.execute("SELECT streamer_name FROM monitored_streamers").fetchall()
        conn.close()
        assert rows == [("newname",)]

    def test_renames_notification_messages(self, db):
        # Insert a notification_messages row directly
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO notification_messages (guild_id, streamer_name, channel_id, message_id, sent_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (100, "oldname", 5000, 999999)
        )
        conn.commit()
        conn.close()
        db.update_streamer_login("oldname", "newname")
        conn = db.get_connection()
        rows = conn.execute("SELECT streamer_name FROM notification_messages").fetchall()
        conn.close()
        assert rows == [("newname",)], "notification_messages should be renamed too"

    def test_renames_stream_events(self, db):
        db.log_stream_event(guild_id=100, streamer_name="oldname")
        db.update_streamer_login("oldname", "newname")
        conn = db.get_connection()
        rows = conn.execute("SELECT streamer_name FROM stream_events").fetchall()
        conn.close()
        assert all(r[0] == "newname" for r in rows)

    def test_rename_lowercases_input(self, db):
        db.add_streamer(guild_id=100, streamer_name="oldname", channel_id=5000)
        db.update_streamer_login("OLDNAME", "NEWNAME")  # uppercase input
        conn = db.get_connection()
        rows = conn.execute("SELECT streamer_name FROM monitored_streamers").fetchall()
        conn.close()
        assert rows == [("newname",)]


# ── get_server_leaderboard (the new SQL with hours/longest/streak) ────────────

class TestServerLeaderboard:
    """The leaderboard query computes hours, longest, and streak correctly."""

    def _insert_session(self, db, guild_id, streamer, start, end=None):
        """Helper: insert a row directly with explicit start/end times."""
        conn = db.get_connection()
        if end:
            conn.execute(
                "INSERT INTO stream_events (guild_id, streamer_name, went_live_at, ended_at) "
                "VALUES (?, ?, ?, ?)",
                (guild_id, streamer, start.strftime("%Y-%m-%d %H:%M:%S"),
                 end.strftime("%Y-%m-%d %H:%M:%S"))
            )
        else:
            conn.execute(
                "INSERT INTO stream_events (guild_id, streamer_name, went_live_at) VALUES (?, ?, ?)",
                (guild_id, streamer, start.strftime("%Y-%m-%d %H:%M:%S"))
            )
        conn.commit()
        conn.close()

    def test_empty_leaderboard(self, db):
        rows = db.get_server_leaderboard(guild_id=100)
        assert rows == []

    def test_counts_streams(self, db):
        now = datetime.now(timezone.utc)
        self._insert_session(db, 100, "alice", now)
        self._insert_session(db, 100, "alice", now)
        rows = db.get_server_leaderboard(guild_id=100)
        assert len(rows) == 1
        assert rows[0]["streamer_name"] == "alice"
        assert rows[0]["stream_count"] == 2

    def test_hours_streamed_only_counts_completed(self, db):
        """Open sessions (no ended_at) shouldn't contribute to hours_streamed."""
        now = datetime.now(timezone.utc)
        # Completed 3-hour session
        start1 = now - timedelta(hours=10)
        end1 = start1 + timedelta(hours=3)
        self._insert_session(db, 100, "alice", start1, end1)
        # Currently-live session — no end yet
        self._insert_session(db, 100, "alice", now - timedelta(hours=1))
        rows = db.get_server_leaderboard(guild_id=100)
        assert rows[0]["stream_count"] == 2
        # hours_streamed should be ~3.0, NOT 4.0 (the open session doesn't count)
        assert 2.9 <= rows[0]["hours_streamed"] <= 3.1, \
            f"hours_streamed {rows[0]['hours_streamed']}, expected ~3.0"

    def test_longest_session(self, db):
        now = datetime.now(timezone.utc)
        # Two completed sessions: 2h and 5h
        start1 = now - timedelta(hours=20)
        self._insert_session(db, 100, "alice", start1, start1 + timedelta(hours=2))
        start2 = now - timedelta(hours=10)
        self._insert_session(db, 100, "alice", start2, start2 + timedelta(hours=5))
        rows = db.get_server_leaderboard(guild_id=100)
        assert 4.9 <= rows[0]["longest_hours"] <= 5.1
        assert 6.9 <= rows[0]["hours_streamed"] <= 7.1

    def test_per_guild_scoping(self, db):
        """A streamer monitored in two guilds shows only the calling guild's count."""
        now = datetime.now(timezone.utc)
        self._insert_session(db, 100, "alice", now)
        self._insert_session(db, 200, "alice", now)
        rows = db.get_server_leaderboard(guild_id=100)
        assert rows[0]["stream_count"] == 1, "should only count guild 100's row"

    def test_streak_consecutive_days(self, db):
        """Streak counts consecutive days with at least one stream."""
        from datetime import date
        # Three consecutive days ending today
        for d in range(3):
            day = datetime.now(timezone.utc) - timedelta(days=d)
            self._insert_session(db, 100, "alice", day)
        rows = db.get_server_leaderboard(guild_id=100)
        assert rows[0]["streak_days"] == 3

    def test_streak_breaks_on_gap(self, db):
        """If yesterday is missing, streak is just today (1)."""
        # Today + 2 days ago (gap on day 1)
        today = datetime.now(timezone.utc)
        two_ago = today - timedelta(days=2)
        self._insert_session(db, 100, "alice", today)
        self._insert_session(db, 100, "alice", two_ago)
        rows = db.get_server_leaderboard(guild_id=100)
        assert rows[0]["streak_days"] == 1

    def test_ordering_by_stream_count(self, db):
        now = datetime.now(timezone.utc)
        # alice: 1 stream, bob: 3 streams
        self._insert_session(db, 100, "alice", now)
        for _ in range(3):
            self._insert_session(db, 100, "bob", now)
        rows = db.get_server_leaderboard(guild_id=100)
        assert rows[0]["streamer_name"] == "bob"
        assert rows[1]["streamer_name"] == "alice"
