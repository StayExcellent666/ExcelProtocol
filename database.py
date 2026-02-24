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
        """Create a database connection"""
        return sqlite3.connect(self.db_path)
    
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

        conn.commit()
        conn.close()
        logger.info(f"Database initialized at {self.db_path}")
    
    def add_streamer(self, guild_id: int, streamer_name: str, channel_id: int, custom_channel_id: int = None) -> bool:
        """
        Add a streamer to monitor for a guild.
        custom_channel_id: if set, this streamer always posts to this channel regardless of default.
        Returns True if added, False if already exists
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO monitored_streamers (guild_id, streamer_name, channel_id, custom_channel_id)
                VALUES (?, ?, ?, ?)
            ''', (guild_id, streamer_name.lower(), channel_id, custom_channel_id))
            
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
            SELECT streamer_name, channel_id, added_at
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
                'added_at': row[2]
            }
            for row in rows
        ]
    
    def get_all_streamers(self) -> List[Dict]:
        """Get all monitored streamers across all servers"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT guild_id, streamer_name, channel_id
            FROM monitored_streamers
            ORDER BY streamer_name
        ''')
        
        rows = cursor.fetchall()
        conn.close()
        
        return [
            {
                'guild_id': row[0],
                'streamer_name': row[1],
                'channel_id': row[2]
            }
            for row in rows
        ]
    
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
            logger.info(f"Saved notification message {message_id} for {streamer_name} in guild {guild_id}")
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
        
        logger.info(f"Deleted {deleted} notification records for {streamer_name} in guild {guild_id}")
    
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

    def cleanup_guild(self, guild_id: int):
        """Remove all data for a guild (called when bot is removed from server)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM monitored_streamers WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM server_settings WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM notification_messages WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM cleanup_configs WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM twitch_channels WHERE guild_id = ?', (guild_id,))
        
        conn.commit()
        conn.close()
        logger.info(f"Cleaned up data for guild {guild_id}")
