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
        
        # Table for monitored streamers
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS monitored_streamers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                streamer_name TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, streamer_name)
            )
        ''')
        
        # Index for faster lookups
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_guild_id 
            ON monitored_streamers(guild_id)
        ''')
        
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_streamer_name 
            ON monitored_streamers(streamer_name)
        ''')
        
        conn.commit()
        conn.close()
        logger.info(f"Database initialized at {self.db_path}")
    
    def add_streamer(self, guild_id: int, streamer_name: str, channel_id: int) -> bool:
        """
        Add a streamer to monitor for a guild
        Returns True if added, False if already exists
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                INSERT INTO monitored_streamers (guild_id, streamer_name, channel_id)
                VALUES (?, ?, ?)
            ''', (guild_id, streamer_name.lower(), channel_id))
            
            conn.commit()
            logger.info(f"Added streamer {streamer_name} for guild {guild_id}")
            return True
        
        except sqlite3.IntegrityError:
            # Already exists
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
        
        conn.commit()
        conn.close()
        logger.info(f"Set notification channel for guild {guild_id} to {channel_id}")
    
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
        return row[0] if row and row[0] else 0x9146FF
    
    def cleanup_guild(self, guild_id: int):
        """Remove all data for a guild (called when bot is removed from server)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM monitored_streamers WHERE guild_id = ?', (guild_id,))
        cursor.execute('DELETE FROM server_settings WHERE guild_id = ?', (guild_id,))
        
        conn.commit()
        conn.close()
        logger.info(f"Cleaned up data for guild {guild_id}")
