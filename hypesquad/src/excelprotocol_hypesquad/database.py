from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    announcement_channel_id TEXT,
    leaderboard_channel_id TEXT,
    auto_weekly INTEGER NOT NULL DEFAULT 1 CHECK (auto_weekly IN (0, 1)),
    joined_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    discord_user_id TEXT PRIMARY KEY,
    twitch_user_id TEXT NOT NULL UNIQUE,
    twitch_login TEXT NOT NULL,
    twitch_display_name TEXT NOT NULL,
    opted_in INTEGER NOT NULL DEFAULT 0 CHECK (opted_in IN (0, 1)),
    consent_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    linked_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_selected_at TEXT,
    cooldown_until TEXT,
    selection_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS member_guilds (
    discord_user_id TEXT NOT NULL REFERENCES members(discord_user_id) ON DELETE CASCADE,
    guild_id TEXT NOT NULL REFERENCES guild_settings(guild_id) ON DELETE CASCADE,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    joined_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (discord_user_id, guild_id)
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash TEXT PRIMARY KEY,
    discord_user_id TEXT NOT NULL,
    guild_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS network_schedule (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    weekday INTEGER NOT NULL DEFAULT 5 CHECK (weekday BETWEEN 0 AND 6),
    hour INTEGER NOT NULL DEFAULT 19 CHECK (hour BETWEEN 0 AND 23),
    timezone TEXT NOT NULL DEFAULT 'UTC',
    cooldown_days INTEGER NOT NULL DEFAULT 42 CHECK (cooldown_days BETWEEN 1 AND 365),
    last_slot TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    target_discord_user_id TEXT NOT NULL,
    target_twitch_user_id TEXT NOT NULL,
    target_twitch_login TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('manual', 'weekly')),
    source_guild_id TEXT,
    selection_slot TEXT UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('waiting_live', 'active', 'finished', 'cancelled')),
    selected_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    last_live_seen_at TEXT,
    offline_checks INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS event_guilds (
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    guild_id TEXT NOT NULL,
    announcement_channel_id TEXT NOT NULL,
    leaderboard_channel_id TEXT NOT NULL,
    announcement_message_id TEXT,
    leaderboard_message_id TEXT,
    PRIMARY KEY (event_id, guild_id)
);

CREATE TABLE IF NOT EXISTS participants (
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    discord_user_id TEXT NOT NULL,
    twitch_user_id TEXT NOT NULL,
    twitch_login TEXT NOT NULL,
    twitch_display_name TEXT NOT NULL,
    source_guild_id TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    scored_message_count INTEGER NOT NULL DEFAULT 0,
    active_minutes INTEGER NOT NULL DEFAULT 0,
    score INTEGER NOT NULL DEFAULT 10,
    last_scored_at TEXT,
    PRIMARY KEY (event_id, discord_user_id),
    UNIQUE (event_id, twitch_user_id)
);

CREATE TABLE IF NOT EXISTS activity_receipts (
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    message_id TEXT NOT NULL,
    twitch_user_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    PRIMARY KEY (event_id, message_id)
);

CREATE TABLE IF NOT EXISTS activity_minutes (
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    twitch_user_id TEXT NOT NULL,
    minute_start TEXT NOT NULL,
    PRIMARY KEY (event_id, twitch_user_id, minute_start)
);

CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_members_eligible
    ON members(opted_in, status, cooldown_until, selection_count);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_open_hyperaid
    ON events((1)) WHERE status IN ('waiting_live', 'active');
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(delivered_at, id);
"""


class Database:
    """Small standalone SQLite boundary with explicit transactions."""

    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.commit()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    @contextmanager
    def transaction(self):
        with self._write_lock:
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def fetchone(self, query: str, params=()):
        with self.connect() as connection:
            row = connection.execute(query, params).fetchone()
            return dict(row) if row else None

    def fetchall(self, query: str, params=()):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

