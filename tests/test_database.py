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

    def test_mark_stream_ended_cross_midnight_stream(self, db):
        """Round 9 regression: a stream that starts before midnight UTC and
        ends after midnight UTC must be tracked correctly. Earlier code used
        `date(went_live_at) = date('now')` which would fail to match across
        midnight."""
        from datetime import datetime as _dt, timedelta as _td

        # Simulate: stream went live 5 hours ago (before midnight) and is
        # ending now (after midnight). Insert directly to control timestamps.
        five_hours_ago = (_dt.now(timezone.utc) - _td(hours=5)).replace(tzinfo=None)
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO stream_events (guild_id, streamer_name, went_live_at, ended_at) "
            "VALUES (?, ?, ?, NULL)",
            (100, "alice", five_hours_ago.strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.execute(
            "INSERT INTO global_stream_events (streamer_name, stream_date, went_live_at, ended_at) "
            "VALUES (?, date(?), ?, NULL)",
            ("alice", five_hours_ago.strftime('%Y-%m-%d %H:%M:%S'),
             five_hours_ago.strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        conn.close()

        # End the stream now
        db.mark_stream_ended("alice", ended_at=_dt.now(timezone.utc))

        # Both rows should be closed even though went_live_at is from yesterday
        # (or today depending on what time of day the test runs at)
        conn = db.get_connection()
        per_server = conn.execute(
            "SELECT ended_at FROM stream_events WHERE streamer_name='alice'"
        ).fetchone()
        global_ = conn.execute(
            "SELECT ended_at FROM global_stream_events WHERE streamer_name='alice'"
        ).fetchone()
        conn.close()
        assert per_server[0] is not None, \
            "Cross-midnight stream's per-server row should have been closed"
        assert global_[0] is not None, \
            "Cross-midnight stream's global row should have been closed"

    def test_mark_stream_ended_24_hour_stream(self, db):
        """A 24-hour charity stream / subathon must track its duration correctly.
        Tests the realistic upper bound for legitimate streams."""
        from datetime import datetime as _dt, timedelta as _td

        # Stream started 24h ago
        day_ago = (_dt.now(timezone.utc) - _td(hours=24)).replace(tzinfo=None)
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO stream_events (guild_id, streamer_name, went_live_at, ended_at) "
            "VALUES (?, ?, ?, NULL)",
            (100, "alice", day_ago.strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        conn.close()

        # End now — 24 hours later
        now = _dt.now(timezone.utc)
        db.mark_stream_ended("alice", ended_at=now)

        # Compute the recorded duration
        conn = db.get_connection()
        row = conn.execute(
            "SELECT (julianday(ended_at) - julianday(went_live_at)) * 24 AS hours "
            "FROM stream_events WHERE streamer_name='alice'"
        ).fetchone()
        conn.close()
        assert row[0] is not None, "24h stream's ended_at must be set"
        # Allow ±10 minutes of tolerance for test timing
        assert 23.8 <= row[0] <= 24.2, f"Expected ~24h duration, got {row[0]}h"

    def test_mark_stream_ended_47_hour_stream_within_window(self, db):
        """47h stream (just inside the 48h Twitch cap) must close correctly."""
        from datetime import datetime as _dt, timedelta as _td

        forty_seven_h_ago = (_dt.now(timezone.utc) - _td(hours=47)).replace(tzinfo=None)
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO stream_events (guild_id, streamer_name, went_live_at, ended_at) "
            "VALUES (?, ?, ?, NULL)",
            (100, "alice", forty_seven_h_ago.strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        conn.close()

        db.mark_stream_ended("alice", ended_at=_dt.now(timezone.utc))

        conn = db.get_connection()
        row = conn.execute(
            "SELECT ended_at FROM stream_events WHERE streamer_name='alice'"
        ).fetchone()
        conn.close()
        assert row[0] is not None, "47h stream should still be within the 48h window"

    def test_mark_stream_ended_49_hour_orphan_stays_null(self, db):
        """A 49-hour-old NULL row is an orphan (bot missed offline event) and
        must NOT be clobbered by a later unrelated offline event. This is
        the original Round 5 protection — preserved with the 48h window."""
        from datetime import datetime as _dt, timedelta as _td

        forty_nine_h_ago = (_dt.now(timezone.utc) - _td(hours=49)).replace(tzinfo=None)
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO stream_events (guild_id, streamer_name, went_live_at, ended_at) "
            "VALUES (?, ?, ?, NULL)",
            (100, "alice", forty_nine_h_ago.strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        conn.close()

        db.mark_stream_ended("alice", ended_at=_dt.now(timezone.utc))

        conn = db.get_connection()
        row = conn.execute(
            "SELECT ended_at FROM stream_events WHERE streamer_name='alice'"
        ).fetchone()
        conn.close()
        assert row[0] is None, \
            "49h-old orphan must NOT be clobbered — that's the original bug we fixed"

    def test_mark_stream_ended_multiple_open_rows_only_closes_latest(self, db):
        """Round 11 regression: if multiple NULL rows exist for the same
        streamer in the 48h window (because previous offline events were
        missed), the new offline event must ONLY close the most recent one.
        Closing all of them assigns the same ended_at to multiple rows
        and produces fake cross-day durations on the older rows."""
        from datetime import datetime as _dt, timedelta as _td

        # Insert TWO open rows on consecutive days (simulating bardocksenpai's
        # corruption pattern: stream.online fired twice but a stream.offline
        # was missed between them)
        thirty_h_ago = (_dt.now(timezone.utc) - _td(hours=30)).replace(tzinfo=None)
        five_h_ago = (_dt.now(timezone.utc) - _td(hours=5)).replace(tzinfo=None)
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO global_stream_events (streamer_name, stream_date, went_live_at, ended_at) "
            "VALUES (?, date(?), ?, NULL)",
            ("alice", thirty_h_ago.strftime('%Y-%m-%d %H:%M:%S'),
             thirty_h_ago.strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.execute(
            "INSERT INTO global_stream_events (streamer_name, stream_date, went_live_at, ended_at) "
            "VALUES (?, date(?), ?, NULL)",
            ("alice", five_h_ago.strftime('%Y-%m-%d %H:%M:%S'),
             five_h_ago.strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        conn.close()

        # End the current stream now
        db.mark_stream_ended("alice", ended_at=_dt.now(timezone.utc))

        conn = db.get_connection()
        rows = conn.execute(
            "SELECT went_live_at, ended_at FROM global_stream_events "
            "WHERE streamer_name='alice' ORDER BY id"
        ).fetchall()
        conn.close()

        # Older row (30h-ago) must stay NULL — it's the orphan from the
        # missed offline event. Only the newest row should be closed.
        assert rows[0][1] is None, \
            f"Older NULL row should not have been closed, got ended_at={rows[0][1]}"
        assert rows[1][1] is not None, \
            "Most recent NULL row should have been closed"

    def test_mark_stream_ended_per_server_multiple_open_only_closes_latest(self, db):
        """Same MAX(id) protection on per-server stream_events."""
        from datetime import datetime as _dt, timedelta as _td

        thirty_h_ago = (_dt.now(timezone.utc) - _td(hours=30)).replace(tzinfo=None)
        five_h_ago = (_dt.now(timezone.utc) - _td(hours=5)).replace(tzinfo=None)
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO stream_events (guild_id, streamer_name, went_live_at, ended_at) "
            "VALUES (?, ?, ?, NULL)",
            (100, "alice", thirty_h_ago.strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.execute(
            "INSERT INTO stream_events (guild_id, streamer_name, went_live_at, ended_at) "
            "VALUES (?, ?, ?, NULL)",
            (100, "alice", five_h_ago.strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        conn.close()

        db.mark_stream_ended("alice", ended_at=_dt.now(timezone.utc))

        conn = db.get_connection()
        rows = conn.execute(
            "SELECT ended_at FROM stream_events WHERE streamer_name='alice' "
            "AND guild_id=100 ORDER BY id"
        ).fetchall()
        conn.close()
        assert rows[0][0] is None, "Older orphan must stay NULL"
        assert rows[1][0] is not None, "Latest row must be closed"

    def test_multi_session_same_day_each_gets_own_row(self, db):
        """Round 14: each stream.online creates its OWN row in
        global_stream_events. A streamer who crashes and resumes twice on
        the same day has 3 separate rows, each with accurate duration."""
        now = datetime.now(timezone.utc)
        # Three sessions same day with gaps representing crashes
        s1_start = now - timedelta(hours=6)
        s1_end = s1_start + timedelta(hours=1)
        s2_start = s1_end + timedelta(minutes=10)
        s2_end = s2_start + timedelta(hours=1)
        s3_start = s2_end + timedelta(minutes=20)

        db.log_stream_event(guild_id=100, streamer_name="alice", started_at=s1_start)
        db.mark_stream_ended("alice", ended_at=s1_end)
        db.log_stream_event(guild_id=100, streamer_name="alice", started_at=s2_start)
        db.mark_stream_ended("alice", ended_at=s2_end)
        db.log_stream_event(guild_id=100, streamer_name="alice", started_at=s3_start)
        # Third session left open (streamer still live)

        conn = db.get_connection()
        rows = conn.execute(
            "SELECT went_live_at, ended_at FROM global_stream_events "
            "WHERE streamer_name='alice' ORDER BY id"
        ).fetchall()
        conn.close()

        # All THREE rows should exist (old UNIQUE constraint would have blocked
        # rows 2 and 3 from being inserted)
        assert len(rows) == 3, f"Expected 3 rows for 3 sessions, got {len(rows)}"
        # Sessions 1 and 2 closed, session 3 still open
        assert rows[0][1] is not None, "Session 1 should be closed"
        assert rows[1][1] is not None, "Session 2 should be closed"
        assert rows[2][1] is None, "Session 3 should still be open"

    def test_mark_stream_ended_does_not_clobber_historical_global_row(self, db):
        """Regression test for the Round 5 bug.

        global_stream_events has UNIQUE(streamer_name, stream_date), so a
        second go-live on the same day doesn't insert a new row. If the bot
        receives a SECOND offline event for that day after the first one
        already set ended_at, the historical (yesterday's, last week's,
        last month's) NULL-ended_at row must NOT be clobbered.

        The bug originally caused 'streamed for 217 hours' nonsense in
        /globalleaderboard. This test ensures it stays fixed.
        """
        # Simulate a HISTORICAL open row from a past day where the bot crashed
        # before receiving offline. Insert directly to bypass the date('now') logic.
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO global_stream_events (streamer_name, stream_date, went_live_at, ended_at) "
            "VALUES (?, ?, ?, NULL)",
            ("alice", "2026-04-01", "2026-04-01 12:00:00")
        )
        conn.commit()
        conn.close()

        # Today, alice goes live — UNIQUE(streamer_name, stream_date) allows it
        # because the historical row has stream_date='2026-04-01'.
        # Insert today's row directly so we don't depend on sqlite's date('now').
        from datetime import datetime as _dt
        today_str = _dt.now().strftime("%Y-%m-%d")
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO global_stream_events (streamer_name, stream_date, went_live_at, ended_at) "
            "VALUES (?, ?, datetime('now'), NULL)",
            ("alice", today_str)
        )
        conn.commit()
        conn.close()

        # Now mark_stream_ended fires
        end = datetime.now(timezone.utc)
        db.mark_stream_ended("alice", ended_at=end)

        # Verify: today's row got ended_at, but APRIL row did NOT
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT stream_date, ended_at FROM global_stream_events WHERE streamer_name='alice' ORDER BY stream_date"
        ).fetchall()
        conn.close()
        april_row = next(r for r in rows if r[0] == "2026-04-01")
        today_row = next(r for r in rows if r[0] == today_str)
        assert april_row[1] is None, \
            f"Historical April row was clobbered with ended_at={april_row[1]} — the bug is back"
        assert today_row[1] is not None, "Today's row should have been closed"

    def test_mark_stream_ended_does_not_clobber_historical_per_server_row(self, db):
        """Per-server stream_events: same protection. If a streamer was logged
        days ago and never closed (bot crash), today's offline must not steal
        the old row's ended_at slot."""
        # Insert a historical open row directly
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO stream_events (guild_id, streamer_name, went_live_at, ended_at) "
            "VALUES (?, ?, ?, NULL)",
            (100, "alice", "2026-04-01 12:00:00")
        )
        conn.commit()
        conn.close()

        # Today, alice goes live in the same guild
        db.log_stream_event(guild_id=100, streamer_name="alice")
        # And goes offline
        db.mark_stream_ended("alice", ended_at=datetime.now(timezone.utc))

        # Verify: today's row closed, April row still NULL
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT date(went_live_at), ended_at FROM stream_events WHERE streamer_name='alice' ORDER BY id"
        ).fetchall()
        conn.close()
        april_row = next(r for r in rows if r[0] == "2026-04-01")
        assert april_row[1] is None, \
            f"Historical April row clobbered with ended_at={april_row[1]}"

    def test_cleanup_migration_resets_long_sessions(self, tmp_db_path):
        """The one-time cleanup migration must reset rows where ended_at is
        more than 12 hours after went_live_at (corrupted Twitch sessions
        produced by the old buggy code; >12h is overwhelmingly cross-day
        corruption rather than a real marathon stream)."""
        from database import Database
        db = Database(db_path=tmp_db_path)
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO global_stream_events (streamer_name, stream_date, went_live_at, ended_at) "
            "VALUES (?, ?, ?, ?)",
            ("alice", "2026-05-01", "2026-05-01 00:00:00", "2026-05-10 00:00:00")  # 9 days "long"
        )
        conn.execute(
            "INSERT INTO global_stream_events (streamer_name, stream_date, went_live_at, ended_at) "
            "VALUES (?, ?, ?, ?)",
            ("bob", "2026-05-01", "2026-05-01 12:00:00", "2026-05-01 18:00:00")  # 6h, valid
        )
        conn.execute(
            "INSERT INTO global_stream_events (streamer_name, stream_date, went_live_at, ended_at) "
            "VALUES (?, ?, ?, ?)",
            ("carol", "2026-05-01", "2026-05-01 01:00:00", "2026-05-02 00:00:00")  # 23h, corrupted
        )
        conn.commit()
        conn.close()

        # The fixture's `db` instance was created with an empty DB and ran the
        # cleanup with no rows present. We need a NEW Database to trigger the
        # cleanup now that the corrupted rows exist. But the meta flag is
        # already set from the first init, so we need to remove it first.
        conn = db.get_connection()
        conn.execute("DELETE FROM meta WHERE key='ended_at_cleanup_v2'")
        conn.commit()
        conn.close()

        # Re-initialize — cleanup migration should run now
        db2 = Database(db_path=tmp_db_path)
        conn = db2.get_connection()
        rows = {r[0]: r[1] for r in conn.execute(
            "SELECT streamer_name, ended_at FROM global_stream_events"
        ).fetchall()}
        conn.close()
        assert rows["alice"] is None, "Corrupted 9-day row should have been reset to NULL"
        assert rows["bob"] is not None, "Valid 6h row should NOT have been reset"
        assert rows["carol"] is None, "23h cross-day row should have been reset to NULL"

    def test_cleanup_migration_runs_only_once(self, tmp_db_path):
        """Cleanup v2 must be gated by a meta flag — after it runs once, it
        cannot run again even if legit 13h+ marathon streams exist later."""
        from database import Database
        db = Database(db_path=tmp_db_path)

        # First boot ran the migration (with no corrupted data). Flag should be set.
        conn = db.get_connection()
        flag = conn.execute("SELECT value FROM meta WHERE key='ended_at_cleanup_v2'").fetchone()
        conn.close()
        assert flag is not None, "Meta flag should be set after first init"

        # Insert a legit 14h marathon row AFTER initial cleanup ran
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO global_stream_events (streamer_name, stream_date, went_live_at, ended_at) "
            "VALUES (?, ?, ?, ?)",
            ("marathon", "2026-05-15", "2026-05-15 10:00:00", "2026-05-16 00:00:00")  # 14h
        )
        conn.commit()
        conn.close()

        # Restart (re-init Database) — flag is still set, so cleanup must NOT run
        db2 = Database(db_path=tmp_db_path)
        conn = db2.get_connection()
        row = conn.execute(
            "SELECT ended_at FROM global_stream_events WHERE streamer_name='marathon'"
        ).fetchone()
        conn.close()
        assert row[0] is not None, \
            "Legit 14h row was nulled — cleanup should NOT have re-run after meta flag was set"


# ── on_ready stream-start restoration (regression for false milestone bug) ────

class TestOnReadyStreamStartsRestore:
    """When the bot restarts, _stream_starts is restored from
    notification_messages.sent_at as a fallback for the original
    EventSub started_at.

    Round 6 fixes a bug where MIN(sent_at) was used instead of MAX, which
    caused milestones to fire spuriously at restart for streamers who had
    old notifications in the DB. These tests verify the SQL produces the
    right values.
    """

    def _insert_notification(self, db, streamer, sent_at_iso, guild_id=100,
                             channel_id=5000, message_id=None):
        """Helper: insert a notification_messages row with a specific timestamp."""
        if message_id is None:
            # Generate unique IDs
            self._counter = getattr(self, '_counter', 0) + 1
            message_id = 1000000 + self._counter
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO notification_messages (guild_id, streamer_name, channel_id, message_id, sent_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, streamer.lower(), channel_id, message_id, sent_at_iso)
        )
        conn.commit()
        conn.close()

    def test_query_returns_max_sent_at_not_min(self, db):
        """If a streamer has multiple notifications, we want the MOST recent
        one, not the oldest. The buggy version used MIN(sent_at) which
        returned a 7-day-old timestamp and triggered false milestones."""
        # 7 days ago and 1 hour ago for the same streamer
        from datetime import datetime as _dt, timedelta as _td, timezone
        old = (_dt.now(timezone.utc).replace(tzinfo=None) - _td(days=7)).strftime('%Y-%m-%d %H:%M:%S')
        recent = (_dt.now(timezone.utc).replace(tzinfo=None) - _td(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
        self._insert_notification(db, "alice", old)
        self._insert_notification(db, "alice", recent)

        # Run the same query the bot's on_ready uses
        conn = db.get_connection()
        row = conn.execute(
            "SELECT streamer_name, MAX(sent_at) AS most_recent "
            "FROM notification_messages "
            "WHERE sent_at > datetime('now', '-12 hours') "
            "GROUP BY streamer_name"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "alice"
        # Should match `recent` (1 hour ago), not `old` (7 days ago)
        assert recent in row[1] or row[1] in recent

    def test_query_filters_old_notifications(self, db):
        """A streamer with ONLY old notifications (>12h) is excluded from
        the start-time restore. Their entry in live_streamers can still be
        populated by the separate broader query, but they get no start time
        — preventing the milestone false-fire bug entirely."""
        from datetime import datetime as _dt, timedelta as _td, timezone
        old = (_dt.now(timezone.utc).replace(tzinfo=None) - _td(days=2)).strftime('%Y-%m-%d %H:%M:%S')
        self._insert_notification(db, "alice", old)

        conn = db.get_connection()
        rows = conn.execute(
            "SELECT streamer_name FROM notification_messages "
            "WHERE sent_at > datetime('now', '-12 hours') "
            "GROUP BY streamer_name"
        ).fetchall()
        conn.close()
        assert rows == [], "Streamer with only old notifications should be filtered out"

    def test_live_streamers_query_uses_7day_window(self, db):
        """The broader live_streamers restore query uses a 7-day window so
        cleanup-tracking still works for streamers notified up to a week ago,
        but not ancient entries."""
        from datetime import datetime as _dt, timedelta as _td, timezone
        within = (_dt.now(timezone.utc).replace(tzinfo=None) - _td(days=3)).strftime('%Y-%m-%d %H:%M:%S')
        ancient = (_dt.now(timezone.utc).replace(tzinfo=None) - _td(days=10)).strftime('%Y-%m-%d %H:%M:%S')
        self._insert_notification(db, "alice", within)
        self._insert_notification(db, "bob", ancient)

        conn = db.get_connection()
        rows = conn.execute(
            "SELECT DISTINCT streamer_name FROM notification_messages "
            "WHERE sent_at > datetime('now', '-7 days')"
        ).fetchall()
        conn.close()
        names = {r[0] for r in rows}
        assert "alice" in names
        assert "bob" not in names, "10-day-old entry should not be restored"

    def test_handles_streamer_with_no_recent_notifications(self, db):
        """Edge case: streamer in live_streamers (recent enough for cleanup)
        but no notifications in the last 12h. Their start time is just not
        restored — the milestone code will skip them on next check."""
        from datetime import datetime as _dt, timedelta as _td, timezone
        # 2 days ago — within 7d (live_streamers) but outside 12h (no start)
        ts = (_dt.now(timezone.utc).replace(tzinfo=None) - _td(days=2)).strftime('%Y-%m-%d %H:%M:%S')
        self._insert_notification(db, "alice", ts)

        conn = db.get_connection()
        live_rows = conn.execute(
            "SELECT DISTINCT streamer_name FROM notification_messages "
            "WHERE sent_at > datetime('now', '-7 days')"
        ).fetchall()
        start_rows = conn.execute(
            "SELECT streamer_name FROM notification_messages "
            "WHERE sent_at > datetime('now', '-12 hours') "
            "GROUP BY streamer_name"
        ).fetchall()
        conn.close()
        assert ("alice",) in live_rows
        assert start_rows == []


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
        """stream_count counts distinct DAYS, not session rows. Two sessions
        on the same day = 1 day (e.g., stream crashed and resumed)."""
        now = datetime.now(timezone.utc)
        self._insert_session(db, 100, "alice", now)
        self._insert_session(db, 100, "alice", now)  # same day = same count
        rows = db.get_server_leaderboard(guild_id=100)
        assert len(rows) == 1
        assert rows[0]["streamer_name"] == "alice"
        assert rows[0]["stream_count"] == 1, \
            f"Two same-day sessions = 1 day, got {rows[0]['stream_count']}"

    def test_counts_distinct_days(self, db):
        """Sessions on different days each count as a separate day."""
        now = datetime.now(timezone.utc)
        self._insert_session(db, 100, "alice", now - timedelta(days=2))
        self._insert_session(db, 100, "alice", now - timedelta(days=1))
        self._insert_session(db, 100, "alice", now)
        rows = db.get_server_leaderboard(guild_id=100)
        assert rows[0]["stream_count"] == 3

    def test_hours_streamed_only_counts_completed(self, db):
        """Open sessions (no ended_at) shouldn't contribute to hours_streamed.
        But they DO contribute to stream_count (days) if on a unique day."""
        now = datetime.now(timezone.utc)
        # Completed 3-hour session 10 hours ago (same day as 'now' or yesterday)
        start1 = now - timedelta(hours=10)
        end1 = start1 + timedelta(hours=3)
        self._insert_session(db, 100, "alice", start1, end1)
        # Currently-live session — no end yet, possibly same day
        self._insert_session(db, 100, "alice", now - timedelta(hours=1))
        rows = db.get_server_leaderboard(guild_id=100)
        # stream_count is days — could be 1 or 2 depending on whether they
        # cross midnight in this test run. Use >=1 to be robust.
        assert rows[0]["stream_count"] >= 1
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
        """stream_count counts distinct days. Bob on 3 different days
        ranks above alice on 1 day."""
        now = datetime.now(timezone.utc)
        self._insert_session(db, 100, "alice", now)
        # Spread bob across 3 distinct days so he gets day_count=3
        for d in range(3):
            self._insert_session(db, 100, "bob", now - timedelta(days=d))
        rows = db.get_server_leaderboard(guild_id=100)
        assert rows[0]["streamer_name"] == "bob"
        assert rows[1]["streamer_name"] == "alice"

    def test_sort_by_hours(self, db):
        """sort_by='hours' orders by total hours streamed, not stream count."""
        now = datetime.now(timezone.utc)
        # alice: 1 long 8-hour stream → fewer streams but more hours
        self._insert_session(db, 100, "alice", now - timedelta(hours=10),
                             end=now - timedelta(hours=2))
        # bob: 3 short streams totaling 3 hours
        for i in range(3):
            self._insert_session(db, 100, "bob",
                                 now - timedelta(hours=20 + i),
                                 end=now - timedelta(hours=19 + i))

        rows = db.get_server_leaderboard(guild_id=100, sort_by='hours')
        assert rows[0]["streamer_name"] == "alice", \
            f"Hours sort: alice (8h) should be #1, got {rows[0]['streamer_name']}"
        assert rows[1]["streamer_name"] == "bob"
        assert 7.9 <= rows[0]["hours_streamed"] <= 8.1

    def test_sort_by_longest(self, db):
        """sort_by='longest' orders by single-longest-session."""
        now = datetime.now(timezone.utc)
        # alice: 2 streams of 3h each (6h total, but no single stream >3h)
        self._insert_session(db, 100, "alice", now - timedelta(hours=20),
                             end=now - timedelta(hours=17))
        self._insert_session(db, 100, "alice", now - timedelta(hours=10),
                             end=now - timedelta(hours=7))
        # bob: 1 long 5h stream (less total hours, but longest single session)
        self._insert_session(db, 100, "bob", now - timedelta(hours=8),
                             end=now - timedelta(hours=3))

        rows = db.get_server_leaderboard(guild_id=100, sort_by='longest')
        assert rows[0]["streamer_name"] == "bob", \
            f"Longest sort: bob (5h) should be #1, got {rows[0]['streamer_name']}"
        assert rows[1]["streamer_name"] == "alice"
        assert 4.9 <= rows[0]["longest_hours"] <= 5.1

    def test_sort_hours_filters_streamers_with_no_completed_sessions(self, db):
        """A streamer with only open sessions (no ended_at) shouldn't appear
        in the hours leaderboard at all — they'd just be a row of zeros."""
        now = datetime.now(timezone.utc)
        # alice: 1 completed 2-hour stream
        self._insert_session(db, 100, "alice", now - timedelta(hours=4),
                             end=now - timedelta(hours=2))
        # bob: 5 streams but NONE completed (all ended_at IS NULL)
        for i in range(5):
            self._insert_session(db, 100, "bob", now - timedelta(hours=i*2))

        # Consistency: both appear (bob #1 with 5 streams)
        rows_c = db.get_server_leaderboard(guild_id=100, sort_by='consistency')
        names = [r["streamer_name"] for r in rows_c]
        assert "alice" in names and "bob" in names

        # Hours: only alice (bob has 0 hours)
        rows_h = db.get_server_leaderboard(guild_id=100, sort_by='hours')
        names_h = [r["streamer_name"] for r in rows_h]
        assert "alice" in names_h, "alice should be in hours leaderboard"
        assert "bob" not in names_h, "bob has no completed sessions, shouldn't be in hours sort"

    def test_invalid_sort_falls_back_to_consistency(self, db):
        """Defensive: an unknown sort_by value falls back to 'consistency'
        rather than raising — protects against frontend bugs / typos."""
        now = datetime.now(timezone.utc)
        self._insert_session(db, 100, "alice", now)
        for d in range(3):
            self._insert_session(db, 100, "bob", now - timedelta(days=d))
        rows = db.get_server_leaderboard(guild_id=100, sort_by='garbage')
        # Should behave like 'consistency' — bob (3 streams) ranks above alice (1)
        assert rows[0]["streamer_name"] == "bob"

    def test_global_leaderboard_three_sorts(self, db):
        """Same three sort modes work on global leaderboard."""
        # Insert directly into global_stream_events
        conn = db.get_connection()
        now = datetime.now(timezone.utc)
        # Set up three streamers with different metrics
        # alice: 5 short streams (high count, low hours)
        for i in range(5):
            day = (now - timedelta(days=i)).strftime('%Y-%m-%d')
            conn.execute(
                "INSERT INTO global_stream_events "
                "(streamer_name, stream_date, went_live_at, ended_at) "
                "VALUES (?, ?, ?, ?)",
                ("alice", day, f"{day} 12:00:00", f"{day} 12:30:00")  # 30min each
            )
        # bob: 1 long 10-hour stream (low count, high hours, high longest)
        day = now.strftime('%Y-%m-%d')
        conn.execute(
            "INSERT INTO global_stream_events "
            "(streamer_name, stream_date, went_live_at, ended_at) "
            "VALUES (?, ?, ?, ?)",
            ("bob", day, f"{day} 02:00:00", f"{day} 12:00:00")
        )
        # carol: 3 streams totalling 9 hours (mid count, mid hours)
        for i in range(3):
            day = (now - timedelta(days=i+5)).strftime('%Y-%m-%d')
            conn.execute(
                "INSERT INTO global_stream_events "
                "(streamer_name, stream_date, went_live_at, ended_at) "
                "VALUES (?, ?, ?, ?)",
                ("carol", day, f"{day} 10:00:00", f"{day} 13:00:00")  # 3h each
            )
        conn.commit()
        conn.close()

        # Consistency: alice wins (5 streams)
        rows = db.get_global_leaderboard(sort_by='consistency')
        assert rows[0]["streamer_name"] == "alice"

        # Hours: bob wins (10h)
        rows = db.get_global_leaderboard(sort_by='hours')
        assert rows[0]["streamer_name"] == "bob"
        assert 9.9 <= rows[0]["hours_streamed"] <= 10.1

        # Longest: bob wins again (10h single session)
        rows = db.get_global_leaderboard(sort_by='longest')
        assert rows[0]["streamer_name"] == "bob"


class TestGetStreamEvents:
    """Tests for the dev-only event log query (db.get_stream_events)."""

    def _insert(self, db, streamer, went_live, ended_at=None):
        conn = db.get_connection()
        if ended_at:
            conn.execute(
                "INSERT INTO global_stream_events (streamer_name, stream_date, went_live_at, ended_at) "
                "VALUES (?, date(?), ?, ?)",
                (streamer.lower(), went_live, went_live, ended_at)
            )
        else:
            conn.execute(
                "INSERT INTO global_stream_events (streamer_name, stream_date, went_live_at, ended_at) "
                "VALUES (?, date(?), ?, NULL)",
                (streamer.lower(), went_live, went_live)
            )
        conn.commit()
        conn.close()

    def test_returns_recent_rows_with_hours_and_status(self, db):
        from datetime import datetime as _dt, timedelta as _td
        now = _dt.now(timezone.utc)
        # Closed stream 3 hours long
        s1 = (now - _td(hours=5)).strftime('%Y-%m-%d %H:%M:%S')
        e1 = (now - _td(hours=2)).strftime('%Y-%m-%d %H:%M:%S')
        self._insert(db, "alice", s1, e1)
        # Currently live (within 48h)
        s2 = (now - _td(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
        self._insert(db, "alice", s2)

        rows = db.get_stream_events()
        assert len(rows) == 2
        # Newest first
        assert rows[0]['went_live_at'] == s2
        assert rows[0]['status'] == 'live'
        assert rows[0]['hours'] is None
        assert rows[1]['status'] == 'closed'
        assert 2.9 <= rows[1]['hours'] <= 3.1

    def test_orphan_status_for_old_nulls(self, db):
        from datetime import datetime as _dt, timedelta as _td
        # >48h old NULL row → orphan
        s1 = (_dt.now(timezone.utc) - _td(hours=60)).strftime('%Y-%m-%d %H:%M:%S')
        self._insert(db, "alice", s1)
        rows = db.get_stream_events()
        assert len(rows) == 1
        assert rows[0]['status'] == 'orphan'

    def test_filter_by_streamer_substring(self, db):
        from datetime import datetime as _dt
        now = _dt.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        self._insert(db, "alice", now)
        self._insert(db, "bob", now)
        self._insert(db, "alicewonderland", now)

        rows = db.get_stream_events(streamer="alice")
        names = {r['streamer_name'] for r in rows}
        assert names == {"alice", "alicewonderland"}
        assert "bob" not in names

    def test_filter_by_streamer_is_case_insensitive(self, db):
        from datetime import datetime as _dt
        now = _dt.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        self._insert(db, "alice", now)
        rows = db.get_stream_events(streamer="ALICE")
        assert len(rows) == 1

    def test_limit_caps_results(self, db):
        from datetime import datetime as _dt, timedelta as _td
        for i in range(10):
            t = (_dt.now(timezone.utc) - _td(hours=i)).strftime('%Y-%m-%d %H:%M:%S')
            self._insert(db, f"alice{i}", t)
        rows = db.get_stream_events(limit=5)
        assert len(rows) == 5

    def test_month_filter(self, db):
        # Insert rows for two different months
        self._insert(db, "alice", "2026-04-15 10:00:00", "2026-04-15 12:00:00")
        from datetime import datetime as _dt
        now_str = _dt.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        self._insert(db, "alice", now_str)

        # Filter by April only
        rows = db.get_stream_events(month="2026-04")
        assert len(rows) == 1
        assert "2026-04" in rows[0]['went_live_at']

    def test_default_scope_is_current_month(self, db):
        from datetime import datetime as _dt
        # Insert one from way in the past (different month)
        self._insert(db, "alice", "2026-01-15 10:00:00", "2026-01-15 12:00:00")
        # And one in current month
        now_str = _dt.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        self._insert(db, "alice", now_str)
        rows = db.get_stream_events()  # no month arg
        # Should only see the current-month one
        assert len(rows) == 1
        assert "2026-01" not in rows[0]['went_live_at']


class TestCleanupStreamEventsRetention:
    """cleanup_stream_events now keeps current + previous month (rolling 2-month window)."""

    def _insert(self, db, streamer, went_live):
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO global_stream_events (streamer_name, stream_date, went_live_at) "
            "VALUES (?, date(?), ?)",
            (streamer.lower(), went_live, went_live)
        )
        conn.execute(
            "INSERT INTO stream_events (guild_id, streamer_name, went_live_at) "
            "VALUES (?, ?, ?)",
            (100, streamer.lower(), went_live)
        )
        conn.commit()
        conn.close()

    def test_keeps_current_and_previous_month_deletes_older(self, db):
        from datetime import datetime as _dt, timedelta as _td
        now = _dt.now(timezone.utc)
        # Current month: keep
        self._insert(db, "alice", now.strftime('%Y-%m-%d %H:%M:%S'))
        # Previous month-ish: keep (~35 days ago is reliably "last month")
        prev = now - _td(days=35)
        # If prev wraps before the 1st of this month, it's last month. Force first day of last month
        # Use 'now -1 month' equivalent computed in python
        if now.month == 1:
            prev = now.replace(year=now.year - 1, month=12, day=15)
        else:
            prev = now.replace(month=now.month - 1, day=15)
        self._insert(db, "bob", prev.strftime('%Y-%m-%d %H:%M:%S'))
        # Way older — 6 months ago: delete
        old_month = now.month - 6
        old_year = now.year
        while old_month < 1:
            old_month += 12
            old_year -= 1
        ancient = now.replace(year=old_year, month=old_month, day=15)
        self._insert(db, "carol", ancient.strftime('%Y-%m-%d %H:%M:%S'))

        db.cleanup_stream_events()

        conn = db.get_connection()
        names = {r[0] for r in conn.execute(
            "SELECT streamer_name FROM global_stream_events"
        ).fetchall()}
        conn.close()
        assert "alice" in names, "Current-month row must be kept"
        assert "bob" in names, "Previous-month row must be kept"
        assert "carol" not in names, "6-month-old row must be deleted"
