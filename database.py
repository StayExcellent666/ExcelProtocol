# ExcelProtocol — Copyright (c) 2026 stayexcellent. All rights reserved.
# Proprietary software. Viewing permitted; use, copying, or self-hosting is not.
# Unauthorized use is a violation of the ExcelProtocol Proprietary License.
# EP-ORIGIN:database:stayexcellent:2026

import sqlite3
import logging
import os
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, db_path=None):
        # Use /data volume on Fly.io if available, otherwise local
        if db_path is None:
            if os.path.exists('/data'):
                db_path = '/data/twitch_bot.db'
            else:
                db_path = 'twitch_bot.db'
        
        self.db_path = db_path
        
        # Ensure directory exists
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        
        self.init_database()
    
    def get_connection(self):
        """Create a database connection with WAL mode and busy timeout."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn
    
    def init_database(self):
        """Initialize database tables"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Table for server settings
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS server_settings (
                guild_id INTEGER PRIMARY KEY,
                notification_channel_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Add embed_color column if it doesn't exist (migration)
        cursor.execute('''
            SELECT COUNT(*) FROM pragma_table_info('server_settings') 
            WHERE name='embed_color'
        ''')
        has_color_column = cursor.fetchone()[0] > 0
        
        if not has_color_column:
            cursor.execute('''
                ALTER TABLE server_settings 
                ADD COLUMN embed_color INTEGER DEFAULT 0x9146FF
            ''')
            logger.info("Added embed_color column to server_settings")
        
        # Add auto_delete_notifications column if it doesn't exist (migration)
        cursor.execute('''
            SELECT COUNT(*) FROM pragma_table_info('server_settings') 
            WHERE name='auto_delete_notifications'
        ''')
        has_auto_delete_column = cursor.fetchone()[0] > 0
        
        if not has_auto_delete_column:
            cursor.execute('''
                ALTER TABLE server_settings 
                ADD COLUMN auto_delete_notifications INTEGER DEFAULT 0
            ''')
            logger.info("Added auto_delete_notifications column to server_settings")
        
        # Create table for storing notification message IDs
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notification_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                streamer_name TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, streamer_name, message_id)
            )
        ''')
        
        # Index for faster lookups
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_notification_guild_streamer 
            ON notification_messages(guild_id, streamer_name)
        ''')
        
        # Create table for channel cleanup configurations
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cleanup_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                interval_hours INTEGER NOT NULL,
                keep_pinned INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, channel_id)
            )
        ''')
        
        # Index for faster lookups
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_cleanup_guild 
            ON cleanup_configs(guild_id)
        ''')
        
        # Table for monitored streamers
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS monitored_streamers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                streamer_name TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                custom_channel_id INTEGER DEFAULT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, streamer_name)
            )
        ''')
        
        # Migration: add custom_channel_id if it doesn't exist
        try:
            cursor.execute('''
                ALTER TABLE monitored_streamers ADD COLUMN custom_channel_id INTEGER DEFAULT NULL
            ''')
            conn.commit()
            logger.info("Migration: added custom_channel_id to monitored_streamers")
        except Exception:
            pass  # Column already exists

        # Migration: add twitch_user_id if it doesn't exist
        try:
            cursor.execute('''
                ALTER TABLE monitored_streamers ADD COLUMN twitch_user_id TEXT DEFAULT NULL
            ''')
            conn.commit()
            logger.info("Migration: added twitch_user_id to monitored_streamers")
        except Exception:
            pass  # Column already exists

        # Migration: add streamer_limit to server_settings
        cursor.execute('''
            SELECT COUNT(*) FROM pragma_table_info('server_settings')
            WHERE name='streamer_limit'
        ''')
        if cursor.fetchone()[0] == 0:
            cursor.execute('''
                ALTER TABLE server_settings
                ADD COLUMN streamer_limit INTEGER DEFAULT 75
            ''')
            conn.commit()
            logger.info("Migration: added streamer_limit to server_settings")

        # Migration: add command_limit to server_settings
        cursor.execute('SELECT COUNT(*) FROM pragma_table_info("server_settings") WHERE name="command_limit"')
        if cursor.fetchone()[0] == 0:
            cursor.execute('ALTER TABLE server_settings ADD COLUMN command_limit INTEGER DEFAULT 50')
            conn.commit()
            logger.info("Migration: added command_limit to server_settings")

        # Migration: add ping_role_id to server_settings
        cursor.execute('SELECT COUNT(*) FROM pragma_table_info("server_settings") WHERE name="ping_role_id"')
        if cursor.fetchone()[0] == 0:
            cursor.execute('ALTER TABLE server_settings ADD COLUMN ping_role_id INTEGER DEFAULT NULL')
            conn.commit()
            logger.info("Migration: added ping_role_id to server_settings")

        # Migration: add body_text to reaction_roles
        cursor.execute('SELECT COUNT(*) FROM pragma_table_info("reaction_roles") WHERE name="body_text"')
        if cursor.fetchone()[0] == 0:
            cursor.execute('ALTER TABLE reaction_roles ADD COLUMN body_text TEXT DEFAULT NULL')
            conn.commit()
            logger.info("Migration: added body_text to reaction_roles")

        # Index for faster lookups
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_guild_id 
            ON monitored_streamers(guild_id)
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_streamer_name 
            ON monitored_streamers(streamer_name)
        ''')
        
        # ----------------------------------------------------------------
        # Stream events for leaderboard (monthly, auto-cleaned)
        # ----------------------------------------------------------------

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stream_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                streamer_name TEXT NOT NULL,
                went_live_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_stream_events_guild
            ON stream_events(guild_id, streamer_name)
        ''')

        # Global stream events — one row per stream session regardless of server count
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS global_stream_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                streamer_name TEXT NOT NULL,
                stream_date TEXT NOT NULL DEFAULT (date('now')),
                went_live_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(streamer_name, stream_date)
            )
        ''')

        # ----------------------------------------------------------------
        # Twitch chat bot tables (new — existing tables untouched)
        # ----------------------------------------------------------------

        # Which Twitch channel each Discord guild is linked to
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS twitch_channels (
                guild_id INTEGER PRIMARY KEY,
                twitch_channel TEXT NOT NULL,
                linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Custom chat commands per Twitch channel
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS twitch_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                twitch_channel TEXT NOT NULL,
                command_name TEXT NOT NULL,
                response TEXT NOT NULL,
                permission TEXT DEFAULT "everyone",
                cooldown_seconds INTEGER DEFAULT 0,
                use_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(twitch_channel, command_name)
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_twitch_commands_channel
            ON twitch_commands(twitch_channel)
        ''')

        # Notification log (30 day retention)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS notification_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                streamer_name TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'sent',
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_notif_log_guild
            ON notification_log(guild_id, streamer_name)
        ''')

        # ----------------------------------------------------------------
        # Birthday tables
        # ----------------------------------------------------------------

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS birthdays (
                guild_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                day      INTEGER NOT NULL,
                month    INTEGER NOT NULL,
                year     INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS birthday_channels (
                guild_id   INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL
            )
        ''')

        # Add milestone_notifications column if it doesn't exist (migration)
        cursor.execute('''
            SELECT COUNT(*) FROM pragma_table_info('server_settings')
            WHERE name='milestone_notifications'
        ''')
        if cursor.fetchone()[0] == 0:
            cursor.execute('''
                ALTER TABLE server_settings
                ADD COLUMN milestone_notifications INTEGER DEFAULT 0
            ''')
            logger.info("Added milestone_notifications column to server_settings")

        # Table to track which milestones have been sent per stream session
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS milestone_sent (
                guild_id       INTEGER NOT NULL,
                streamer_name  TEXT NOT NULL,
                milestone_hours INTEGER NOT NULL,
                sent_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, streamer_name, milestone_hours)
            )
        ''')

        # Reaction roles panels
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reaction_roles (
                message_id  INTEGER PRIMARY KEY,
                guild_id    INTEGER NOT NULL,
                channel_id  INTEGER NOT NULL,
                title       TEXT NOT NULL,
                type        TEXT NOT NULL DEFAULT 'dropdown',
                only_add    INTEGER NOT NULL DEFAULT 0,
                max_roles   INTEGER,
                roles_json  TEXT NOT NULL DEFAULT '[]'
            )
        ''')

        # Broadcaster OAuth tokens (for channel rewards / EventSub)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS broadcaster_tokens (
                guild_id       INTEGER PRIMARY KEY,
                twitch_user_id TEXT NOT NULL,
                twitch_login   TEXT NOT NULL,
                access_token   TEXT NOT NULL,
                refresh_token  TEXT NOT NULL,
                expires_at     TEXT NOT NULL
            )
        ''')

        # Video triggers — links a Twitch reward_id to a video URL
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reward_triggers (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id   INTEGER NOT NULL,
                reward_id  TEXT NOT NULL,
                reward_title TEXT NOT NULL DEFAULT \'\',
                video_url  TEXT NOT NULL,
                volume     REAL NOT NULL DEFAULT 1.0,
                UNIQUE(guild_id, reward_id)
            )
        ''')

        # Permission issues — written by bot's periodic check, read by dashboard
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS permission_issues (
                guild_id   INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                missing    TEXT NOT NULL,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, channel_id)
            )
        ''')

        # Stat channels — voice channels whose names are updated with live member counts
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS stat_channels (
                guild_id    INTEGER NOT NULL,
                channel_id  INTEGER NOT NULL,
                format      TEXT NOT NULL DEFAULT 'Members: {count}',
                last_updated TIMESTAMP DEFAULT NULL,
                PRIMARY KEY (guild_id, channel_id)
            )
        ''')

        # Unresolvable streamers — accounts that Twitch no longer recognises (banned/deleted/renamed)
        # Cleared and repopulated on every EventSub sync
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS unresolvable_streamers (
                streamer_name TEXT NOT NULL,
                guild_id      INTEGER NOT NULL,
                detected_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (streamer_name, guild_id)
            )
        ''')

        # VC Creator — "Join to Create" voice channel feature
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vc_settings (
                guild_id           INTEGER PRIMARY KEY,
                trigger_channel_id INTEGER NOT NULL,
                name_template      TEXT NOT NULL DEFAULT '🔵 {username}''s VC',
                category_id        INTEGER DEFAULT NULL
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS active_vcs (
                channel_id  INTEGER PRIMARY KEY,
                guild_id    INTEGER NOT NULL,
                owner_id    INTEGER NOT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Safety settings — new account filter config per guild
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS safety_settings (
                guild_id            INTEGER PRIMARY KEY,
                enabled             INTEGER NOT NULL DEFAULT 0,
                min_account_age_days INTEGER NOT NULL DEFAULT 7,
                check_username_pattern INTEGER NOT NULL DEFAULT 1,
                check_no_avatar     INTEGER NOT NULL DEFAULT 1,
                action              TEXT NOT NULL DEFAULT 'kick',
                bypass_role_id      INTEGER DEFAULT NULL,
                dm_on_kick          INTEGER NOT NULL DEFAULT 1
            )
        ''')

        # Safety kick log — record of every kick/ban action
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS safety_kicks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                username    TEXT NOT NULL,
                reason      TEXT NOT NULL,
                action      TEXT NOT NULL DEFAULT 'kick',
                kicked_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.commit()
        conn.close()
        logger.info(f"Database initialized at {self.db_path}")

    # ----------------------------------------------------------------
    # Permission issues
    # ----------------------------------------------------------------

    def upsert_permission_issue(self, guild_id: int, channel_id: int, missing: list):
        """Insert or replace a permission issue record."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO permission_issues (guild_id, channel_id, missing, detected_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                missing = excluded.missing,
                detected_at = excluded.detected_at
        ''', (guild_id, channel_id, ','.join(missing)))
        conn.commit()
        conn.close()

    def clear_permission_issue(self, guild_id: int, channel_id: int):
        """Remove a resolved permission issue."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM permission_issues WHERE guild_id = ? AND channel_id = ?', (guild_id, channel_id))
        conn.commit()
        conn.close()

    def clear_all_permission_issues(self, guild_id: int):
        """Remove all permission issues for a guild (e.g. on bot removal)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM permission_issues WHERE guild_id = ?', (guild_id,))
        conn.commit()
        conn.close()

    def get_permission_issues(self, guild_id: int) -> list:
        """Return all current permission issues for a guild."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT channel_id, missing, detected_at FROM permission_issues WHERE guild_id = ? ORDER BY detected_at DESC',
            (guild_id,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [{'channel_id': r[0], 'missing': r[1].split(','), 'detected_at': r[2]} for r in rows]

    # ----------------------------------------------------------------
    # Stat channels
    # ----------------------------------------------------------------

    def set_stat_channel(self, guild_id: int, channel_id: int, fmt: str):
        """Add or update a stat channel for a guild."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO stat_channels (guild_id, channel_id, format)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET format = excluded.format
        ''', (guild_id, channel_id, fmt))
        conn.commit()
        conn.close()

    def remove_stat_channel(self, guild_id: int, channel_id: int):
        """Remove a stat channel."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM stat_channels WHERE guild_id = ? AND channel_id = ?', (guild_id, channel_id))
        conn.commit()
        conn.close()

    # ExcelProtocol — Copyright (c) 2026 stayexcellent. All rights reserved.
# Proprietary software. Viewing permitted; use, copying, or self-hosting is not.
# Unauthorized use is a violation of the ExcelProtocol Proprietary License.
# EP-ORIGIN:database:stayexcellent:2026

    def get_stat_channels(self, guild_id: int) -> list:
        """Get all stat channels for a guild."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT channel_id, format, last_updated FROM stat_channels WHERE guild_id = ?', (guild_id,))
        rows = cursor.fetchall()
        conn.close()
        return [{'channel_id': r[0], 'format': r[1], 'last_updated': r[2]} for r in rows]

    def get_all_stat_channels(self) -> list:
        """Get all stat channels across all guilds."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT guild_id, channel_id, format, last_updated FROM stat_channels')
        rows = cursor.fetchall()
        conn.close()
        return [{'guild_id': r[0], 'channel_id': r[1], 'format': r[2], 'last_updated': r[3]} for r in rows]

    def update_stat_channel_timestamp(self, guild_id: int, channel_id: int):
        """Record when a stat channel was last updated."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE stat_channels SET last_updated = CURRENT_TIMESTAMP WHERE guild_id = ? AND channel_id = ?',
            (guild_id, channel_id)
        )
        conn.commit()
        conn.close()

    # ----------------------------------------------------------------
    # Unresolvable streamers
    # ----------------------------------------------------------------

    def clear_unresolvable_streamers(self):
        """Wipe all unresolvable streamer records — called at start of each EventSub sync."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM unresolvable_streamers')
        conn.commit()
        conn.close()

    def add_unresolvable_streamer(self, streamer_name: str, guild_id: int):
        """Record a streamer that Twitch can no longer resolve."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO unresolvable_streamers (streamer_name, guild_id, detected_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(streamer_name, guild_id) DO UPDATE SET detected_at = CURRENT_TIMESTAMP
        ''', (streamer_name.lower(), guild_id))
        conn.commit()
        conn.close()

    def get_unresolvable_streamers(self, guild_id: int) -> list:
        """Return list of unresolvable streamer names for a guild."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT streamer_name, detected_at FROM unresolvable_streamers WHERE guild_id = ? ORDER BY streamer_name',
            (guild_id,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [{'streamer_name': r[0], 'detected_at': r[1]} for r in rows]

    def get_all_unresolvable_streamers(self) -> list:
        """Return all unresolvable streamers across all guilds."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT streamer_name, guild_id, detected_at FROM unresolvable_streamers ORDER BY streamer_name')
        rows = cursor.fetchall()
        conn.close()
        return [{'streamer_name': r[0], 'guild_id': r[1], 'detected_at': r[2]} for r in rows]

    # ── VC Creator ────────────────────────────────────────────────────────────

    def get_vc_settings(self, guild_id: int) -> dict | None:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT trigger_channel_id, name_template, category_id FROM vc_settings WHERE guild_id = ?', (guild_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        return {'trigger_channel_id': row[0], 'name_template': row[1], 'category_id': row[2]}

    def set_vc_settings(self, guild_id: int, trigger_channel_id: int, name_template: str = "🔵 {username}'s VC", category_id: int = None):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO vc_settings (guild_id, trigger_channel_id, name_template, category_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                trigger_channel_id = excluded.trigger_channel_id,
                name_template      = excluded.name_template,
                category_id        = excluded.category_id
        ''', (guild_id, trigger_channel_id, name_template, category_id))
        conn.commit()
        conn.close()

    def clear_vc_settings(self, guild_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM vc_settings WHERE guild_id = ?', (guild_id,))
        conn.commit()
        conn.close()

    def add_active_vc(self, channel_id: int, guild_id: int, owner_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO active_vcs (channel_id, guild_id, owner_id) VALUES (?, ?, ?)',
                       (channel_id, guild_id, owner_id))
        conn.commit()
        conn.close()

    def remove_active_vc(self, channel_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM active_vcs WHERE channel_id = ?', (channel_id,))
        conn.commit()
        conn.close()

    def get_active_vc_by_owner(self, guild_id: int, owner_id: int) -> dict | None:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT channel_id FROM active_vcs WHERE guild_id = ? AND owner_id = ?', (guild_id, owner_id))
        row = cursor.fetchone()
        conn.close()
        return {'channel_id': row[0]} if row else None

    def get_active_vc(self, channel_id: int) -> dict | None:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT guild_id, owner_id FROM active_vcs WHERE channel_id = ?', (channel_id,))
        row = cursor.fetchone()
        conn.close()
        return {'guild_id': row[0], 'owner_id': row[1]} if row else None

    def get_all_active_vcs(self) -> list:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT channel_id, guild_id, owner_id FROM active_vcs')
        rows = cursor.fetchall()
        conn.close()
        return [{'channel_id': r[0], 'guild_id': r[1], 'owner_id': r[2]} for r in rows]

    # ── Safety ────────────────────────────────────────────────────────────────

    def get_safety_settings(self, guild_id: int) -> dict | None:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM safety_settings WHERE guild_id = ?', (guild_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        keys = ['guild_id','enabled','min_account_age_days','check_username_pattern',
                'check_no_avatar','action','bypass_role_id','dm_on_kick']
        return dict(zip(keys, row))

    def set_safety_settings(self, guild_id: int, enabled: bool, min_account_age_days: int = 7,
                             check_username_pattern: bool = True, check_no_avatar: bool = True,
                             action: str = 'kick', bypass_role_id: int = None, dm_on_kick: bool = True):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO safety_settings
                (guild_id, enabled, min_account_age_days, check_username_pattern,
                 check_no_avatar, action, bypass_role_id, dm_on_kick)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                enabled=excluded.enabled,
                min_account_age_days=excluded.min_account_age_days,
                check_username_pattern=excluded.check_username_pattern,
                check_no_avatar=excluded.check_no_avatar,
                action=excluded.action,
                bypass_role_id=excluded.bypass_role_id,
                dm_on_kick=excluded.dm_on_kick
        ''', (guild_id, int(enabled), min_account_age_days, int(check_username_pattern),
              int(check_no_avatar), action, bypass_role_id, int(dm_on_kick)))
        conn.commit()
        conn.close()

    def log_safety_kick(self, guild_id: int, user_id: int, username: str, reason: str, action: str = 'kick'):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO safety_kicks (guild_id, user_id, username, reason, action) VALUES (?, ?, ?, ?, ?)',
            (guild_id, user_id, username, reason, action)
        )
        conn.commit()
        conn.close()

    def get_safety_kicks(self, guild_id: int, limit: int = 100) -> list:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''SELECT user_id, username, reason, action, kicked_at FROM safety_kicks
               WHERE guild_id = ? AND kicked_at >= datetime('now', '-7 days')
               ORDER BY kicked_at DESC LIMIT ?''',
            (guild_id, limit)
        )
        rows = cursor.fetchall()
        conn.close()
        return [{'user_id': str(r[0]), 'username': r[1], 'reason': r[2], 'action': r[3], 'kicked_at': r[4]} for r in rows]

    def add_streamer(self, guild_id: int, streamer_name: str, channel_id: int, custom_channel_id: int = None, twitch_user_id: str = None) -> bool:
        """
        Add a streamer to monitor for a guild.
        custom_channel_id: if set, this streamer always posts to this channel regardless of default.
        twitch_user_id: Twitch user ID — stored so subscriptions survive renames.
        Returns True if added, False if already exists
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO monitored_streamers (guild_id, streamer_name, channel_id, custom_channel_id, twitch_user_id)
                VALUES (?, ?, ?, ?, ?)
            ''', (guild_id, streamer_name.lower(), channel_id, custom_channel_id, twitch_user_id))
            
            conn.commit()
            logger.info(f"Added streamer {streamer_name} for guild {guild_id} (custom channel: {custom_channel_id})")
            return True
        
        except sqlite3.IntegrityError:
            logger.info(f"Streamer {streamer_name} already monitored in guild {guild_id}")
            return False
        
        finally:
            conn.close()
    
    def remove_streamer(self, guild_id: int, streamer_name: str) -> bool:
        """
        Remove a streamer from monitoring
        Returns True if removed, False if not found
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            DELETE FROM monitored_streamers
            WHERE guild_id = ? AND streamer_name = ?
        ''', (guild_id, streamer_name.lower()))
        
        removed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        
        if removed:
            logger.info(f"Removed streamer {streamer_name} from guild {guild_id}")
        
        return removed
    
    def get_server_streamers(self, guild_id: int) -> List[Dict]:
        """Get all streamers monitored by a specific server"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT streamer_name, channel_id, added_at, custom_channel_id, twitch_user_id
            FROM monitored_streamers
            WHERE guild_id = ?
            ORDER BY streamer_name
        ''', (guild_id,))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [
            {
                'streamer_name': row[0],
                'channel_id': row[1],
                'added_at': row[2],
                'custom_channel_id': row[3],
                'twitch_user_id': row[4],
            }
            for row in rows
        ]
    
    def get_all_streamers(self) -> List[Dict]:
        """Get all monitored streamers across all servers"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT guild_id, streamer_name, channel_id, custom_channel_id, twitch_user_id
            FROM monitored_streamers
            ORDER BY streamer_name
        ''')
        
        rows = cursor.fetchall()
        conn.close()
        
        return [
            {
                'guild_id': row[0],
                'streamer_name': row[1],
                'channel_id': row[2],
                'custom_channel_id': row[3],
                'twitch_user_id': row[4],
            }
            for row in rows
        ]

    def update_streamer_user_id(self, guild_id: int, streamer_name: str, twitch_user_id: str):
        """Store the Twitch user ID for a monitored streamer."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE monitored_streamers SET twitch_user_id = ? WHERE guild_id = ? AND streamer_name = ?',
            (twitch_user_id, guild_id, streamer_name.lower())
        )
        conn.commit()
        conn.close()

    def update_streamer_login(self, old_login: str, new_login: str):
        """Update streamer_name across all guilds when a Twitch user renames."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE monitored_streamers SET streamer_name = ? WHERE streamer_name = ?',
            (new_login.lower(), old_login.lower())
        )
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected

    def get_streamers_missing_user_id(self) -> List[Dict]:
        """Return all monitored_streamers rows that don't have a twitch_user_id yet."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT DISTINCT streamer_name FROM monitored_streamers
            WHERE twitch_user_id IS NULL
            ORDER BY streamer_name
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [{'streamer_name': r[0]} for r in rows]

    def get_all_streamers_with_ids(self) -> List[Dict]:
        """Return all unique (streamer_name, twitch_user_id) pairs that have an ID stored."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT DISTINCT streamer_name, twitch_user_id FROM monitored_streamers
            WHERE twitch_user_id IS NOT NULL
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [{'streamer_name': r[0], 'twitch_user_id': r[1]} for r in rows]
    
    def set_notification_channel(self, guild_id: int, channel_id: int):
        """Set or update the notification channel for a server"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO server_settings (guild_id, notification_channel_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id) 
            DO UPDATE SET notification_channel_id = ?
        ''', (guild_id, channel_id, channel_id))
        
        # Update only streamers without a custom channel
        cursor.execute('''
            UPDATE monitored_streamers
            SET channel_id = ?
            WHERE guild_id = ?
            AND custom_channel_id IS NULL
        ''', (channel_id, guild_id))
        
        updated_streamers = cursor.rowcount
        
        conn.commit()
        conn.close()
        logger.info(f"Set notification channel for guild {guild_id} to {channel_id} (updated {updated_streamers} streamers)")
    
    def get_notification_channel(self, guild_id: int) -> Optional[int]:
        """Get the notification channel for a server"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT notification_channel_id
            FROM server_settings
            WHERE guild_id = ?
        ''', (guild_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        return row[0] if row else None
    
    def set_embed_color(self, guild_id: int, color: int):
        """Set the embed color for a server (as hex integer)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO server_settings (guild_id, notification_channel_id, embed_color)
            VALUES (?, 0, ?)
            ON CONFLICT(guild_id) 
            DO UPDATE SET embed_color = ?
        ''', (guild_id, color, color))
        
        conn.commit()
        conn.close()
        logger.info(f"Set embed color for guild {guild_id} to {hex(color)}")
    
    def get_embed_color(self, guild_id: int) -> int:
        """Get the embed color for a server (returns hex integer)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT embed_color
            FROM server_settings
            WHERE guild_id = ?
        ''', (guild_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        # Return custom color or default Twitch purple
        return row[0] if row and row[0] else 0x00FFFF
    
    def set_auto_delete(self, guild_id: int, enabled: bool):
        """Enable or disable auto-delete for a server"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO server_settings (guild_id, notification_channel_id, auto_delete_notifications)
            VALUES (?, 0, ?)
            ON CONFLICT(guild_id) 
            DO UPDATE SET auto_delete_notifications = ?
        ''', (guild_id, 1 if enabled else 0, 1 if enabled else 0))
        
        conn.commit()
        conn.close()
        logger.info(f"Set auto-delete for guild {guild_id} to {enabled}")


    # ExcelProtocol — Copyright (c) 2026 stayexcellent. All rights reserved.
# Proprietary software. Viewing permitted; use, copying, or self-hosting is not.
# Unauthorized use is a violation of the ExcelProtocol Proprietary License.
# EP-ORIGIN:database:stayexcellent:2026

    
    def get_auto_delete(self, guild_id: int) -> bool:
        """Check if auto-delete is enabled for a server"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT auto_delete_notifications
            FROM server_settings
            WHERE guild_id = ?
        ''', (guild_id,))
        
        row = cursor.fetchone()
        conn.close()
        
        return bool(row[0]) if row else False

    def set_ping_role(self, guild_id: int, role_id: Optional[int]):
        """Set or clear the ping role for stream notifications."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO server_settings (guild_id, notification_channel_id, ping_role_id)
            VALUES (?, 0, ?)
            ON CONFLICT(guild_id)
            DO UPDATE SET ping_role_id = ?
        ''', (guild_id, role_id, role_id))
        conn.commit()
        conn.close()
        logger.info(f"Set ping_role_id for guild {guild_id} to {role_id}")

    def get_ping_role(self, guild_id: int) -> Optional[int]:
        """Get the ping role ID for stream notifications (None if not set)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT ping_role_id FROM server_settings WHERE guild_id = ?', (guild_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row and row[0] else None

    def set_milestone_notifications(self, guild_id: int, enabled: bool):
        """Enable or disable milestone notifications for a server"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO server_settings (guild_id, notification_channel_id, milestone_notifications)
            VALUES (?, 0, ?)
            ON CONFLICT(guild_id)
            DO UPDATE SET milestone_notifications = ?
        ''', (guild_id, 1 if enabled else 0, 1 if enabled else 0))
        conn.commit()
        conn.close()
        logger.info(f"Set milestone notifications for guild {guild_id} to {enabled}")

    def get_milestone_notifications(self, guild_id: int) -> bool:
        """Check if milestone notifications are enabled for a server"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT milestone_notifications FROM server_settings WHERE guild_id = ?
        ''', (guild_id,))
        row = cursor.fetchone()
        conn.close()
        return bool(row[0]) if row else False

    def has_milestone_been_sent(self, guild_id: int, streamer_name: str, milestone_hours: int) -> bool:
        """Check if a milestone notification has already been sent this stream session"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 1 FROM milestone_sent
            WHERE guild_id = ? AND streamer_name = ? AND milestone_hours = ?
        ''', (guild_id, streamer_name, milestone_hours))
        row = cursor.fetchone()
        conn.close()
        return row is not None

    def record_milestone_sent(self, guild_id: int, streamer_name: str, milestone_hours: int):
        """Record that a milestone notification was sent"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO milestone_sent (guild_id, streamer_name, milestone_hours)
            VALUES (?, ?, ?)
        ''', (guild_id, streamer_name, milestone_hours))
        conn.commit()
        conn.close()

    def clear_milestones_for_streamer(self, guild_id: int, streamer_name: str):
        """Clear milestone records when a streamer goes offline (reset for next stream)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM milestone_sent WHERE guild_id = ? AND streamer_name = ?
        ''', (guild_id, streamer_name))
        conn.commit()
        conn.close()

    def recent_notification_exists(self, guild_id: int, streamer_name: str, within_minutes: int = 10) -> bool:
        """Return True if a notification was already sent for this streamer+guild within the last N minutes.
        Used to deduplicate across bot restarts and brief multi-instance windows during deploys."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT 1 FROM notification_messages
            WHERE guild_id = ? AND streamer_name = ?
            AND sent_at >= datetime('now', ? || ' minutes')
            LIMIT 1
        ''', (guild_id, streamer_name.lower(), f'-{within_minutes}'))
        row = cursor.fetchone()
        conn.close()
        return row is not None

    def save_notification_message(self, guild_id: int, streamer_name: str, channel_id: int, message_id: int):
        """Save a notification message ID for later deletion"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO notification_messages (guild_id, streamer_name, channel_id, message_id)
                VALUES (?, ?, ?, ?)
            ''', (guild_id, streamer_name.lower(), channel_id, message_id))
            
            conn.commit()
            logger.debug(f"Saved notification message {message_id} for {streamer_name} in guild {guild_id}")
        except sqlite3.IntegrityError:
            logger.warning(f"Message {message_id} already saved")
        finally:
            conn.close()
    
    def get_notification_messages(self, guild_id: int, streamer_name: str) -> List[Dict]:
        """Get all notification messages for a streamer in a guild"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT channel_id, message_id
            FROM notification_messages
            WHERE guild_id = ? AND streamer_name = ?
        ''', (guild_id, streamer_name.lower()))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [{'channel_id': row[0], 'message_id': row[1]} for row in rows]
    
    def delete_notification_messages(self, guild_id: int, streamer_name: str):
        """Remove notification message records after deletion"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            DELETE FROM notification_messages
            WHERE guild_id = ? AND streamer_name = ?
        ''', (guild_id, streamer_name.lower()))
        
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        
        logger.debug(f"Deleted {deleted} notification records for {streamer_name} in guild {guild_id}")
    
    def add_cleanup_config(self, guild_id: int, channel_id: int, interval_hours: int, keep_pinned: bool = True) -> bool:
        """Add or update cleanup configuration for a channel"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO cleanup_configs (guild_id, channel_id, interval_hours, keep_pinned)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, channel_id)
                DO UPDATE SET interval_hours = ?, keep_pinned = ?
            ''', (guild_id, channel_id, interval_hours, 1 if keep_pinned else 0, interval_hours, 1 if keep_pinned else 0))
            
            conn.commit()
            logger.info(f"Added cleanup config for channel {channel_id} in guild {guild_id}: {interval_hours}h")
            return True
        except Exception as e:
            logger.error(f"Error adding cleanup config: {e}")
            return False
        finally:
            conn.close()
    
    def remove_cleanup_config(self, guild_id: int, channel_id: int) -> bool:
        """Remove cleanup configuration for a channel"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            DELETE FROM cleanup_configs
            WHERE guild_id = ? AND channel_id = ?
        ''', (guild_id, channel_id))
        
        removed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        
        if removed:
            logger.info(f"Removed cleanup config for channel {channel_id} in guild {guild_id}")
        
        return removed
    
    def get_guild_cleanup_configs(self, guild_id: int) -> List[Dict]:
        """Get all cleanup configurations for a guild"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT channel_id, interval_hours, keep_pinned, created_at
            FROM cleanup_configs
            WHERE guild_id = ?
            ORDER BY channel_id
        ''', (guild_id,))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [
            {
                'channel_id': row[0],
                'interval_hours': row[1],
                'keep_pinned': bool(row[2]),
                'created_at': row[3]
            }
            for row in rows
        ]
    
    def get_all_cleanup_configs(self) -> List[Dict]:
        """Get all cleanup configurations across all guilds"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT guild_id, channel_id, interval_hours, keep_pinned
            FROM cleanup_configs
            ORDER BY guild_id, channel_id
        ''')
        
        rows = cursor.fetchall()
        conn.close()
        
        return [
            {
                'guild_id': row[0],
                'channel_id': row[1],
                'interval_hours': row[2],
                'keep_pinned': bool(row[3])
            }
            for row in rows
        ]
    
    def get_cleanup_config(self, guild_id: int, channel_id: int) -> Optional[Dict]:
        """Get cleanup configuration for a specific channel"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT interval_hours, keep_pinned
            FROM cleanup_configs
            WHERE guild_id = ? AND channel_id = ?
        ''', (guild_id, channel_id))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                'interval_hours': row[0],
                'keep_pinned': bool(row[1])
            }
        return None
    
    # ------------------------------------------------------------------
    # Stream events (leaderboard)
    # ------------------------------------------------------------------

    def log_stream_event(self, guild_id: int, streamer_name: str):
        """Log a stream going live. Per-server for server leaderboard, deduplicated globally."""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Per-server event (used for server leaderboard)
        cursor.execute(
            "INSERT INTO stream_events (guild_id, streamer_name) VALUES (?, ?)",
            (guild_id, streamer_name.lower())
        )

        # Global event — one per stream session per day (UNIQUE constraint deduplicates)
        cursor.execute('''
            INSERT OR IGNORE INTO global_stream_events (streamer_name, stream_date)
            VALUES (?, date('now'))
        ''', (streamer_name.lower(),))

        conn.commit()
        conn.close()

    def get_server_leaderboard(self, guild_id: int, limit: int = 10) -> list:
        """Get top streamers for a server this month"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT streamer_name, COUNT(*) as stream_count
            FROM stream_events
            WHERE guild_id = ?
              AND strftime('%Y-%m', went_live_at) = strftime('%Y-%m', 'now')
            GROUP BY streamer_name
            ORDER BY stream_count DESC
            LIMIT ?
        ''', (guild_id, limit))
        rows = cursor.fetchall()
        conn.close()
        return [{'streamer_name': r[0], 'stream_count': r[1]} for r in rows]

    def get_global_leaderboard(self, limit: int = 15) -> list:
        """Get top streamers globally this month — counts unique stream sessions only"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT g.streamer_name,
                   COUNT(*) as total_streams,
                   (SELECT COUNT(DISTINCT s.guild_id)
                    FROM stream_events s
                    WHERE s.streamer_name = g.streamer_name
                    AND strftime('%Y-%m', s.went_live_at) = strftime('%Y-%m', 'now')
                   ) as server_count
            FROM global_stream_events g
            WHERE strftime('%Y-%m', g.went_live_at) = strftime('%Y-%m', 'now')
            GROUP BY g.streamer_name
            ORDER BY total_streams DESC
            LIMIT ?
        ''', (limit,))
        rows = cursor.fetchall()
        conn.close()
        return [{'streamer_name': r[0], 'total_streams': r[1], 'server_count': r[2]} for r in rows]

    def log_notification(self, guild_id: int, streamer_name: str, channel_id: int, status: str = 'sent'):
        """Log a notification attempt"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO notification_log (guild_id, streamer_name, channel_id, status)
            VALUES (?, ?, ?, ?)
        ''', (guild_id, streamer_name.lower(), channel_id, status))
        conn.commit()
        conn.close()

    def get_notification_log(self, guild_id: int, streamer_name: str, limit: int = 10) -> list:
        """Get recent notification log for a streamer in a guild"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT streamer_name, channel_id, status, sent_at
            FROM notification_log
            WHERE guild_id = ? AND streamer_name = ?
            ORDER BY sent_at DESC
            LIMIT ?
        ''', (guild_id, streamer_name.lower(), limit))
        rows = cursor.fetchall()
        conn.close()
        return [{'streamer_name': r[0], 'channel_id': r[1], 'status': r[2], 'sent_at': r[3]} for r in rows]

    def trim_notification_log(self, days: int = 30):
        """Delete notification log entries older than X days"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM notification_log
            WHERE sent_at < datetime('now', ? || ' days')
        ''', (f'-{days}',))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        if deleted:
            logger.info(f"Trimmed {deleted} old notification log entries")
        return deleted

    def cleanup_stream_events(self):
        """Delete all stream events from previous months"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            DELETE FROM stream_events
            WHERE strftime('%Y-%m', went_live_at) != strftime('%Y-%m', 'now')
        ''')
        cursor.execute('''
            DELETE FROM global_stream_events
            WHERE strftime('%Y-%m', went_live_at) != strftime('%Y-%m', 'now')
        ''')
        conn.commit()
        conn.close()
        logger.info("Cleaned up old stream events")
        return 0

    # ExcelProtocol — Copyright (c) 2026 stayexcellent. All rights reserved.
# Proprietary software. Viewing permitted; use, copying, or self-hosting is not.
# Unauthorized use is a violation of the ExcelProtocol Proprietary License.
# EP-ORIGIN:database:stayexcellent:2026

    
    # ------------------------------------------------------------------
    # Twitch channel linking
    # ------------------------------------------------------------------

    def set_twitch_channel(self, guild_id: int, twitch_channel: str):
        """Link a Discord guild to a Twitch channel"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO twitch_channels (guild_id, twitch_channel)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET twitch_channel = ?
        ''', (guild_id, twitch_channel.lower(), twitch_channel.lower()))
        conn.commit()
        conn.close()

    def get_twitch_channel(self, guild_id: int) -> Optional[Dict]:
        """Get the Twitch channel linked to a Discord guild"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT guild_id, twitch_channel FROM twitch_channels WHERE guild_id = ?',
            (guild_id,)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return {'guild_id': row[0], 'twitch_channel': row[1]}
        return None

    def remove_twitch_channel(self, guild_id: int):
        """Unlink a Discord guild from its Twitch channel"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM twitch_channels WHERE guild_id = ?', (guild_id,))
        conn.commit()
        conn.close()

    def get_all_twitch_channels(self) -> List[Dict]:
        """Get all linked Twitch channels (used on bot startup to rejoin them)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT guild_id, twitch_channel FROM twitch_channels')
        rows = cursor.fetchall()
        conn.close()
        return [{'guild_id': r[0], 'twitch_channel': r[1]} for r in rows]

    def get_guilds_for_twitch_channel(self, twitch_channel: str) -> List[Dict]:
        """Get all Discord guilds linked to a specific Twitch channel"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT guild_id, twitch_channel FROM twitch_channels WHERE twitch_channel = ?',
            (twitch_channel.lower(),)
        )
        rows = cursor.fetchall()
        conn.close()
        return [{'guild_id': r[0], 'twitch_channel': r[1]} for r in rows]

    # ------------------------------------------------------------------
    # Twitch custom commands
    # ------------------------------------------------------------------

    def add_twitch_command(
        self,
        twitch_channel: str,
        command_name: str,
        response: str,
        permission: str = "everyone",
        cooldown_seconds: int = 0
    ) -> bool:
        """Add or update a custom Twitch command. Returns True on success."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO twitch_commands
                    (twitch_channel, command_name, response, permission, cooldown_seconds)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(twitch_channel, command_name)
                DO UPDATE SET response = ?, permission = ?, cooldown_seconds = ?
            ''', (
                twitch_channel.lower(), command_name.lower(), response, permission, cooldown_seconds,
                response, permission, cooldown_seconds
            ))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error adding Twitch command: {e}")
            return False
        finally:
            conn.close()

    def remove_twitch_command(self, twitch_channel: str, command_name: str) -> bool:
        """Remove a custom command. Returns True if it existed."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'DELETE FROM twitch_commands WHERE twitch_channel = ? AND command_name = ?',
            (twitch_channel.lower(), command_name.lower())
        )
        removed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return removed

    def get_twitch_command(self, twitch_channel: str, command_name: str) -> Optional[Dict]:
        """Get a single command by name"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT command_name, response, permission, cooldown_seconds, use_count
            FROM twitch_commands
            WHERE twitch_channel = ? AND command_name = ?
        ''', (twitch_channel.lower(), command_name.lower()))
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                'command_name': row[0],
                'response': row[1],
                'permission': row[2],
                'cooldown_seconds': row[3],
                'use_count': row[4]
            }
        return None

    def get_twitch_commands(self, twitch_channel: str) -> List[Dict]:
        """Get all commands for a Twitch channel"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT command_name, response, permission, cooldown_seconds, use_count
            FROM twitch_commands
            WHERE twitch_channel = ?
            ORDER BY command_name
        ''', (twitch_channel.lower(),))
        rows = cursor.fetchall()
        conn.close()
        return [
            {
                'command_name': r[0],
                'response': r[1],
                'permission': r[2],
                'cooldown_seconds': r[3],
                'use_count': r[4]
            }
            for r in rows
        ]

    def increment_command_uses(self, twitch_channel: str, command_name: str):
        """Increment the use counter for a command"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE twitch_commands
            SET use_count = use_count + 1
            WHERE twitch_channel = ? AND command_name = ?
        ''', (twitch_channel.lower(), command_name.lower()))
        conn.commit()
        conn.close()

    def get_command_limit(self, guild_id: int) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT command_limit FROM server_settings WHERE guild_id = ?", (guild_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row and row[0] is not None else 50

    def set_command_limit(self, guild_id: int, limit: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO server_settings (guild_id, notification_channel_id, command_limit) VALUES (?, 0, ?) ON CONFLICT(guild_id) DO UPDATE SET command_limit = ?", (guild_id, limit, limit))
        conn.commit()
        conn.close()
        logger.info(f"Set command limit for guild {guild_id} to {limit}")

    def get_command_count(self, guild_id: int) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT twitch_channel FROM twitch_channels WHERE guild_id = ?", (guild_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return 0
        cursor.execute("SELECT COUNT(*) FROM twitch_commands WHERE twitch_channel = ?", (row[0],))
        count_row = cursor.fetchone()
        conn.close()
        return count_row[0] if count_row else 0

    def get_streamer_limit(self, guild_id: int) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT streamer_limit FROM server_settings WHERE guild_id = ?", (guild_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row and row[0] is not None else 75

    def set_streamer_limit(self, guild_id: int, limit: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO server_settings (guild_id, notification_channel_id, streamer_limit)
            VALUES (?, 0, ?)
            ON CONFLICT(guild_id) DO UPDATE SET streamer_limit = ?
        """, (guild_id, limit, limit))
        conn.commit()
        conn.close()
        logger.info(f"Set streamer limit for guild {guild_id} to {limit}")

    def get_streamer_count(self, guild_id: int) -> int:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM monitored_streamers WHERE guild_id = ?", (guild_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 0

    # ── Broadcaster tokens ──────────────────────────────────────────────────────

    def set_broadcaster_token(self, guild_id: int, twitch_user_id: str, twitch_login: str,
                               access_token: str, refresh_token: str, expires_at: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO broadcaster_tokens (guild_id, twitch_user_id, twitch_login, access_token, refresh_token, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                twitch_user_id = excluded.twitch_user_id,
                twitch_login   = excluded.twitch_login,
                access_token   = excluded.access_token,
                refresh_token  = excluded.refresh_token,
                expires_at     = excluded.expires_at
        """, (guild_id, twitch_user_id, twitch_login, access_token, refresh_token, expires_at))
        conn.commit()
        conn.close()

    def get_broadcaster_token(self, guild_id: int) -> Optional[Dict]:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM broadcaster_tokens WHERE guild_id = ?", (guild_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        return {"guild_id": row[0], "twitch_user_id": row[1], "twitch_login": row[2],
                "access_token": row[3], "refresh_token": row[4], "expires_at": row[5]}

    def get_all_broadcaster_tokens(self) -> List[Dict]:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM broadcaster_tokens")
        rows = cursor.fetchall()
        conn.close()
        return [{"guild_id": r[0], "twitch_user_id": r[1], "twitch_login": r[2],
                 "access_token": r[3], "refresh_token": r[4], "expires_at": r[5]} for r in rows]

    def delete_broadcaster_token(self, guild_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM broadcaster_tokens WHERE guild_id = ?", (guild_id,))
        conn.commit()
        conn.close()

    # ── Reward triggers ──────────────────────────────────────────────────────────

    def set_reward_trigger(self, guild_id: int, reward_id: str, reward_title: str, video_url: str, volume: float = 1.0):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO reward_triggers (guild_id, reward_id, reward_title, video_url, volume)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, reward_id) DO UPDATE SET
                reward_title = excluded.reward_title,
                video_url    = excluded.video_url,
                volume       = excluded.volume
        """, (guild_id, reward_id, reward_title, video_url, volume))
        conn.commit()
        conn.close()

    def get_reward_triggers(self, guild_id: int) -> List[Dict]:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT reward_id, reward_title, video_url, volume FROM reward_triggers WHERE guild_id = ?", (guild_id,))
        rows = cursor.fetchall()
        conn.close()
        return [{"reward_id": r[0], "reward_title": r[1], "video_url": r[2], "volume": r[3]} for r in rows]

    def get_reward_trigger(self, guild_id: int, reward_id: str) -> Optional[Dict]:
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT reward_id, reward_title, video_url, volume FROM reward_triggers WHERE guild_id = ? AND reward_id = ?", (guild_id, reward_id))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        return {"reward_id": row[0], "reward_title": row[1], "video_url": row[2], "volume": row[3]}

    def delete_reward_trigger(self, guild_id: int, reward_id: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM reward_triggers WHERE guild_id = ? AND reward_id = ?", (guild_id, reward_id))
        conn.commit()
        conn.close()

    def get_all_reward_triggers(self) -> List[Dict]:
        """Get all triggers across all guilds — used for EventSub routing."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT guild_id, reward_id, reward_title, video_url, volume FROM reward_triggers")
        rows = cursor.fetchall()
        conn.close()
        return [{"guild_id": r[0], "reward_id": r[1], "reward_title": r[2], "video_url": r[3], "volume": r[4]} for r in rows]

    def cleanup_guild(self, guild_id: int):
        """Remove all data for a guild (called when bot is removed from server)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM monitored_streamers WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM server_settings WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM notification_messages WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM cleanup_configs WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM twitch_channels WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM birthdays WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM birthday_channels WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM reaction_roles WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM broadcaster_tokens WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM reward_triggers WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM milestone_sent WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM stream_events WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM permission_issues WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM stat_channels WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM unresolvable_streamers WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM vc_settings WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM active_vcs WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM safety_settings WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM safety_kicks WHERE guild_id = ?', (guild_id,))
        
        conn.commit()
        conn.close()
        logger.info(f"Cleaned up data for guild {guild_id}")

    # ------------------------------------------------------------------
    # Birthday methods
    # ------------------------------------------------------------------

    def set_birthday(self, guild_id: int, user_id: int, day: int, month: int, year: int):
        """Set or update a user's birthday for a guild."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO birthdays (guild_id, user_id, day, month, year)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                day   = excluded.day,
                month = excluded.month,
                year  = excluded.year
        ''', (guild_id, user_id, day, month, year))
        conn.commit()
        conn.close()

    def remove_birthday(self, guild_id: int, user_id: int):
        """Remove a user's birthday for a guild."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM birthdays WHERE guild_id = ? AND user_id = ?', (guild_id, user_id))
        conn.commit()
        conn.close()

    def get_all_birthdays(self, guild_id: int) -> list:
        """Return all birthday entries for a guild."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, day, month, year FROM birthdays WHERE guild_id = ?', (guild_id,))
        rows = cursor.fetchall()
        conn.close()
        return [{'user_id': r[0], 'day': r[1], 'month': r[2], 'year': r[3]} for r in rows]

    def get_birthdays_on(self, guild_id: int, month: int, day: int) -> list:
        """Return all birthday entries for a specific day in a guild."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT user_id, day, month, year FROM birthdays WHERE guild_id = ? AND month = ? AND day = ?',
            (guild_id, month, day)
        )
        rows = cursor.fetchall()
        conn.close()
        return [{'user_id': r[0], 'day': r[1], 'month': r[2], 'year': r[3]} for r in rows]

    def set_birthday_channel(self, guild_id: int, channel_id: int):
        """Set the birthday announcement channel for a guild."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO birthday_channels (guild_id, channel_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id
        ''', (guild_id, channel_id))
        conn.commit()
        conn.close()

    def get_birthday_channel(self, guild_id: int):
        """Get the birthday announcement channel for a guild. Returns None if not set."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT channel_id FROM birthday_channels WHERE guild_id = ?', (guild_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    # ----------------------------------------------------------------
    # Reaction roles
    # ----------------------------------------------------------------

    def rr_save(self, message_id: int, guild_id: int, channel_id: int, title: str,
                rr_type: str, only_add: bool, max_roles, roles: list, body_text: str = None):
        """Save or update a reaction role panel."""
        import json
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO reaction_roles (message_id, guild_id, channel_id, title, type, only_add, max_roles, roles_json, body_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                guild_id   = excluded.guild_id,
                channel_id = excluded.channel_id,
                title      = excluded.title,
                type       = excluded.type,
                only_add   = excluded.only_add,
                max_roles  = excluded.max_roles,
                roles_json = excluded.roles_json,
                body_text  = excluded.body_text
        ''', (message_id, guild_id, channel_id, title, rr_type,
              1 if only_add else 0, max_roles, json.dumps(roles), body_text or None))
        conn.commit()
        conn.close()

    def rr_get(self, message_id: int) -> dict | None:
        """Get a reaction role panel by message ID."""
        import json
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM reaction_roles WHERE message_id = ?', (message_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        return {
            'message_id': row[0], 'guild_id': row[1], 'channel_id': row[2],
            'title': row[3], 'type': row[4], 'only_add': bool(row[5]),
            'max_roles': row[6], 'roles': json.loads(row[7]),
            'body_text': row[8] if len(row) > 8 else None,
        }

    def rr_get_all(self) -> list:
        """Get all reaction role panels (for restore on startup)."""
        import json
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM reaction_roles')
        rows = cursor.fetchall()
        conn.close()
        return [{
            'message_id': r[0], 'guild_id': r[1], 'channel_id': r[2],
            'title': r[3], 'type': r[4], 'only_add': bool(r[5]),
            'max_roles': r[6], 'roles': json.loads(r[7]),
            'body_text': r[8] if len(r) > 8 else None,
        } for r in rows]

    def rr_get_for_guild(self, guild_id: int) -> list:
        """Get all reaction role panels for a specific guild."""
        import json
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM reaction_roles WHERE guild_id = ?', (guild_id,))
        rows = cursor.fetchall()
        conn.close()
        return [{
            'message_id': r[0], 'guild_id': r[1], 'channel_id': r[2],
            'title': r[3], 'type': r[4], 'only_add': bool(r[5]),
            'max_roles': r[6], 'roles': json.loads(r[7]),
            'body_text': r[8] if len(r) > 8 else None,
        } for r in rows]

    def rr_delete(self, message_id: int):
        """Delete a reaction role panel."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM reaction_roles WHERE message_id = ?', (message_id,))
        conn.commit()
        conn.close()

    def rr_update_roles(self, message_id: int, roles: list):
        """Update just the roles list for a panel."""
        import json
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE reaction_roles SET roles_json = ? WHERE message_id = ?',
                       (json.dumps(roles), message_id))
        conn.commit()
        conn.close()


# ExcelProtocol — Copyright (c) 2026 stayexcellent. All rights reserved.
# Proprietary software. Viewing permitted; use, copying, or self-hosting is not.
# Unauthorized use is a violation of the ExcelProtocol Proprietary License.
# EP-ORIGIN:database:stayexcellent:2026
