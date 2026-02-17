import discord
from discord import app_commands
from discord.ext import tasks
import asyncio
import logging
import psutil
import os
from datetime import datetime, timedelta
from database import Database
from twitch_api import TwitchAPI
from config import DISCORD_TOKEN, CHECK_INTERVAL_SECONDS, BOT_OWNER_ID

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class TwitchNotifierBot(discord.Client):
    def __init__(self):
        # Required intents for the bot
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.db = Database()
        self.twitch = TwitchAPI()
        
        # Track which streamers are currently live to avoid duplicate notifications
        self.live_streamers = set()
        
        # Track bot start time for uptime calculation
        self.start_time = datetime.utcnow()
        
        # Track cleanup statistics
        self.cleanup_stats = {'last_run': None, 'total_deleted': 0}
        
        # Track errors for DM alerts (rate limiting)
        self.error_alerts_sent = {}  # {error_key: timestamp}
        self.alert_cooldown = 3600  # Don't spam same error within 1 hour
    
    async def setup_hook(self):
        """Called when bot is starting up"""
        await self.tree.sync()
        logger.info("Command tree synced")
    
    async def on_ready(self):
        """Called when bot successfully connects to Discord"""
        logger.info(f'Logged in as {self.user} (ID: {self.user.id})')
        logger.info('------')
        
        # Start the polling loop
        if not self.check_streams.is_running():
            self.check_streams.start()
            logger.info("Stream checking loop started")
        
        # Start the cleanup loop
        if not self.cleanup_channels.is_running():
            self.cleanup_channels.start()
            logger.info("Channel cleanup loop started")
    
    async def on_guild_remove(self, guild):
        """Called when bot is removed from a server - clean up data"""
        logger.info(f"Bot removed from guild: {guild.name} (ID: {guild.id})")
        
        # Clean up all data for this guild
        self.db.cleanup_guild(guild.id)
        
        logger.info(f"Cleaned up all data for guild {guild.id}")
    
    @tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
    async def check_streams(self):
        """Periodically check Twitch for live streams"""
        try:
            # Get all monitored streamers from database
            streamers = self.db.get_all_streamers()
            
            if not streamers:
                logger.debug("No streamers to monitor")
                return
            
            # Get unique streamer names (remove duplicates across servers)
            unique_streamers = list(set(s['streamer_name'] for s in streamers))
            
            logger.info(f"Checking {len(unique_streamers)} streamers...")
            
            # Batch check streamers (Twitch API supports up to 100 per request)
            # Split into batches of 100 if needed
            for i in range(0, len(unique_streamers), 100):
                batch = unique_streamers[i:i+100]
                live_streams = await self.twitch.get_live_streams(batch)
                
                # Process each live stream
                for stream in live_streams:
                    streamer_name = stream['user_login']
                    
                    # Check if we already notified about this stream
                    if streamer_name in self.live_streamers:
                        continue
                    
                    # Check if stream just started (within the last 5 minutes)
                    # This prevents re-notifying about old streams after bot restart
                    stream_start = datetime.strptime(stream['started_at'], '%Y-%m-%dT%H:%M:%SZ')
                    time_since_start = datetime.utcnow() - stream_start
                    
                    # Only notify if stream started recently (within 5 minutes)
                    # This accounts for bot restarts and prevents spam
                    if time_since_start.total_seconds() > 300:  # 5 minutes = 300 seconds
                        logger.info(f"Skipping {streamer_name} - stream started {int(time_since_start.total_seconds()/60)} minutes ago")
                        self.live_streamers.add(streamer_name)  # Still mark as seen
                        continue
                    
                    # Mark as notified
                    self.live_streamers.add(streamer_name)
                    
                    # Find all servers monitoring this streamer
                    monitoring_servers = [
                        s for s in streamers 
                        if s['streamer_name'].lower() == streamer_name.lower()
                    ]
                    
                    # Send notification to each server
                    for server_data in monitoring_servers:
                        await self.send_notification(server_data, stream)
                
                # Remove streamers from live set if they went offline
                batch_lower = [s.lower() for s in batch]
                live_names = [s['user_login'].lower() for s in live_streams]
                
                for streamer in batch_lower:
                    if streamer in self.live_streamers and streamer not in live_names:
                        self.live_streamers.remove(streamer)
                        logger.info(f"{streamer} went offline")
                        
                        # Delete notification messages if auto-delete is enabled
                        await self.delete_offline_notifications(streamer)
        
        except Exception as e:
            logger.error(f"Error in check_streams loop: {e}", exc_info=True)
            
            # Send alert for critical stream checking failures
            await self.send_owner_alert(
                "Stream Check Failed",
                f"**Critical error in stream checking loop!**\n\n"
                f"Error: `{str(e)[:200]}`\n\n"
                f"This might mean Twitch API is down or there's a code bug.\n"
                f"Stream notifications may not be working."
            )
    
    @check_streams.before_loop
    async def before_check_streams(self):
        """Wait until bot is ready before starting the loop"""
        await self.wait_until_ready()
    
    async def send_notification(self, server_data, stream):
        """Send a notification embed to the configured channel"""
        try:
            channel = self.get_channel(server_data['channel_id'])
            
            if not channel:
                logger.warning(f"Channel {server_data['channel_id']} not found")
                
                # Send alert to owner
                await self.send_owner_alert(
                    "Channel Not Found",
                    f"**Notification channel not found!**\n\n"
                    f"Channel ID: `{server_data['channel_id']}`\n"
                    f"This usually means the channel was deleted.\n\n"
                    f"**Action needed:** Run `/setchannel` in the server to fix this.",
                    guild_id=server_data['guild_id']
                )
                return
            
            # Get custom color for this server (or default)
            embed_color = self.db.get_embed_color(server_data['guild_id'])
            
            # Create embed notification
            embed = discord.Embed(
                title=stream['title'],
                url=f"https://twitch.tv/{stream['user_login']}",
                description=f"**{stream['user_name']}** is now live!",
                color=embed_color,  # Use custom or default color
                timestamp=datetime.utcnow()
            )
            
            embed.set_author(
                name=stream['user_name'],
                url=f"https://twitch.tv/{stream['user_login']}",
                icon_url=stream.get('profile_image_url', '')
            )
            
            embed.add_field(
                name="Game",
                value=stream['game_name'] or "No category",
                inline=True
            )
            
            embed.add_field(
                name="Viewers",
                value=str(stream['viewer_count']),
                inline=True
            )
            
            # Use stream thumbnail
            thumbnail_url = stream['thumbnail_url'].replace('{width}', '440').replace('{height}', '248')
            embed.set_image(url=thumbnail_url)
            
            embed.set_footer(text="Twitch", icon_url="https://static.twitchcdn.net/assets/favicon-32-e29e246c157142c94346.png")
            
            # Create Watch Stream button
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="Watch Stream",
                url=f"https://twitch.tv/{stream['user_login']}",
                style=discord.ButtonStyle.link,
                emoji="🔴"
            ))
            
            # Send the notification
            message = await channel.send(embed=embed, view=view)
            logger.info(f"Sent notification for {stream['user_name']} to {channel.guild.name}")
            
            # Save message ID if auto-delete is enabled
            if self.db.get_auto_delete(server_data['guild_id']):
                self.db.save_notification_message(
                    server_data['guild_id'],
                    stream['user_login'],
                    server_data['channel_id'],
                    message.id
                )
        
        except Exception as e:
            logger.error(f"Error sending notification: {e}", exc_info=True)
    
    async def delete_offline_notifications(self, streamer_name: str):
        """Delete notification messages when streamer goes offline"""
        try:
            # Get all servers monitoring this streamer
            all_streamers = self.db.get_all_streamers()
            monitoring_servers = [
                s for s in all_streamers 
                if s['streamer_name'].lower() == streamer_name.lower()
            ]
            
            for server_data in monitoring_servers:
                guild_id = server_data['guild_id']
                
                # Check if auto-delete is enabled for this server
                if not self.db.get_auto_delete(guild_id):
                    continue
                
                # Get all notification messages for this streamer
                messages = self.db.get_notification_messages(guild_id, streamer_name)
                
                for msg_data in messages:
                    try:
                        channel = self.get_channel(msg_data['channel_id'])
                        if channel:
                            message = await channel.fetch_message(msg_data['message_id'])
                            await message.delete()
                            logger.info(f"Deleted notification {msg_data['message_id']} for {streamer_name}")
                    except discord.NotFound:
                        logger.warning(f"Message {msg_data['message_id']} not found (already deleted?)")
                    except discord.Forbidden:
                        logger.error(f"No permission to delete message {msg_data['message_id']}")
                    except Exception as e:
                        logger.error(f"Error deleting message: {e}")
                
                # Clean up database records
                self.db.delete_notification_messages(guild_id, streamer_name)
        
        except Exception as e:
            logger.error(f"Error in delete_offline_notifications: {e}", exc_info=True)
    
    async def send_owner_alert(self, error_type: str, details: str, guild_id: int = None):
        """Send DM alert to bot owner about critical errors"""
        try:
            # Check if we already sent this alert recently (rate limiting)
            error_key = f"{error_type}:{guild_id or 'global'}"
            current_time = datetime.utcnow()
            
            if error_key in self.error_alerts_sent:
                last_sent = self.error_alerts_sent[error_key]
                time_diff = (current_time - last_sent).total_seconds()
                
                if time_diff < self.alert_cooldown:
                    logger.debug(f"Skipping alert for {error_key} - cooldown active")
                    return
            
            # Get owner user
            if BOT_OWNER_ID == 0:
                logger.warning("BOT_OWNER_ID not set - cannot send alert DM")
                return
            
            owner = await self.fetch_user(BOT_OWNER_ID)
            if not owner:
                logger.error(f"Could not fetch owner user {BOT_OWNER_ID}")
                return
            
            # Get guild name if available
            guild_name = "Unknown"
            if guild_id:
                guild = self.get_guild(guild_id)
                if guild:
                    guild_name = guild.name
            
            # Create alert embed
            embed = discord.Embed(
                title=f"🚨 Bot Error Alert: {error_type}",
                description=details,
                color=0xFF0000,
                timestamp=current_time
            )
            
            if guild_id:
                embed.add_field(
                    name="Server",
                    value=f"{guild_name}\nID: `{guild_id}`",
                    inline=False
                )
            
            embed.set_footer(text="ExcelProtocol Error Monitor")
            
            # Send DM
            await owner.send(embed=embed)
            
            # Mark as sent
            self.error_alerts_sent[error_key] = current_time
            logger.info(f"Sent error alert to owner: {error_type}")
        
        except discord.Forbidden:
            logger.error("Cannot send DM to owner - DMs may be disabled")
        except Exception as e:
            logger.error(f"Error sending owner alert: {e}", exc_info=True)
    
    @tasks.loop(hours=1)
    async def cleanup_channels(self):
        """Periodically clean up configured channels"""
        try:
            configs = self.db.get_all_cleanup_configs()
            
            if not configs:
                logger.debug("No cleanup configs to process")
                return
            
            logger.info(f"Running cleanup for {len(configs)} channel(s)...")
            total_deleted = 0
            
            for config in configs:
                deleted = await self.cleanup_channel(
                    config['guild_id'],
                    config['channel_id'],
                    config['interval_hours'],
                    config['keep_pinned']
                )
                total_deleted += deleted
            
            self.cleanup_stats['last_run'] = datetime.utcnow()
            self.cleanup_stats['total_deleted'] += total_deleted
            logger.info(f"Cleanup complete: {total_deleted} messages deleted")
        
        except Exception as e:
            logger.error(f"Error in cleanup loop: {e}", exc_info=True)
            
            # Send alert for cleanup failures
            await self.send_owner_alert(
                "Cleanup Failed",
                f"**Error in channel cleanup loop!**\n\n"
                f"Error: `{str(e)[:200]}`\n\n"
                f"Automatic message cleanup may not be working."
            )
    
    @cleanup_channels.before_loop
    async def before_cleanup_channels(self):
        """Wait until bot is ready before starting cleanup loop"""
        await self.wait_until_ready()
    
    async def cleanup_channel(self, guild_id: int, channel_id: int, interval_hours: int, keep_pinned: bool) -> int:
        """Clean up old messages in a channel"""
        try:
            channel = self.get_channel(channel_id)
            
            if not channel:
                logger.warning(f"Channel {channel_id} not found for cleanup")
                return 0
            
            # Check bot permissions
            permissions = channel.permissions_for(channel.guild.me)
            if not permissions.manage_messages or not permissions.read_message_history:
                logger.error(f"Missing permissions for cleanup in channel {channel_id}")
                
                # Send alert to owner
                await self.send_owner_alert(
                    "Missing Permissions",
                    f"**Bot is missing permissions for channel cleanup!**\n\n"
                    f"Channel: {channel.mention}\n"
                    f"Server: {channel.guild.name}\n\n"
                    f"**Missing:** Manage Messages or Read Message History\n\n"
                    f"**Action needed:** Grant bot the 'Manage Messages' permission in this channel or server.",
                    guild_id=guild_id
                )
                return 0
            
            # Calculate cutoff time
            cutoff_time = datetime.utcnow() - timedelta(hours=interval_hours)
            
            # Fetch messages older than cutoff
            messages_to_delete = []
            async for message in channel.history(limit=1000, before=cutoff_time):
                # Skip if we want to keep pinned messages and this is pinned
                if keep_pinned and message.pinned:
                    continue
                messages_to_delete.append(message)
            
            if not messages_to_delete:
                logger.debug(f"No messages to delete in channel {channel_id}")
                return 0
            
            # Discord allows bulk delete for messages less than 14 days old
            deleted_count = 0
            now = datetime.utcnow()
            bulk_delete = [m for m in messages_to_delete if (now - m.created_at).days < 14]
            individual_delete = [m for m in messages_to_delete if (now - m.created_at).days >= 14]
            
            # Bulk delete (100 at a time)
            for i in range(0, len(bulk_delete), 100):
                batch = bulk_delete[i:i+100]
                await channel.delete_messages(batch)
                deleted_count += len(batch)
            
            # Individual delete for old messages
            for message in individual_delete:
                try:
                    await message.delete()
                    deleted_count += 1
                except discord.NotFound:
                    pass
                except Exception as e:
                    logger.error(f"Error deleting message: {e}")
            
            if deleted_count > 0:
                logger.info(f"Deleted {deleted_count} messages from {channel.name} (guild {guild_id})")
            
            return deleted_count
        
        except discord.Forbidden:
            logger.error(f"No permission to delete messages in channel {channel_id}")
            return 0
        except Exception as e:
            logger.error(f"Error cleaning up channel {channel_id}: {e}", exc_info=True)
            return 0

# Initialize bot
bot = TwitchNotifierBot()

# Slash Commands
@bot.tree.command(name="addstreamer", description="Add a Twitch streamer to monitor")
@app_commands.describe(streamer="Twitch username to monitor")
async def add_streamer(interaction: discord.Interaction, streamer: str):
    """Add a streamer to monitor in this server"""
    # Check if user has manage guild permission
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ You need 'Manage Server' permission to use this command.",
            ephemeral=True
        )
        return
    
    # Get the notification channel (use current channel if not set)
    channel_id = bot.db.get_notification_channel(interaction.guild_id)
    if not channel_id:
        channel_id = interaction.channel_id
        bot.db.set_notification_channel(interaction.guild_id, channel_id)
    
    # Verify streamer exists on Twitch
    await interaction.response.defer(ephemeral=True)
    
    user_info = await bot.twitch.get_user(streamer)
    
    if not user_info:
        await interaction.followup.send(
            f"❌ Twitch user '{streamer}' not found. Please check the spelling.",
            ephemeral=True
        )
        return
    
    # Add to database
    success = bot.db.add_streamer(interaction.guild_id, user_info['login'], channel_id)
    
    if success:
        await interaction.followup.send(
            f"✅ Now monitoring **{user_info['display_name']}** (twitch.tv/{user_info['login']})\n"
            f"Notifications will be sent to <#{channel_id}>",
            ephemeral=True
        )
    else:
        await interaction.followup.send(
            f"ℹ️ Already monitoring **{user_info['display_name']}** in this server.",
            ephemeral=True
        )

@bot.tree.command(name="removestreamer", description="Stop monitoring a Twitch streamer")
@app_commands.describe(streamer="Twitch username to stop monitoring")
async def remove_streamer(interaction: discord.Interaction, streamer: str):
    """Remove a streamer from monitoring"""
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ You need 'Manage Server' permission to use this command.",
            ephemeral=True
        )
        return
    
    success = bot.db.remove_streamer(interaction.guild_id, streamer)
    
    if success:
        await interaction.response.send_message(
            f"✅ No longer monitoring **{streamer}**",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"❌ Not currently monitoring **{streamer}** in this server.",
            ephemeral=True
        )

@bot.tree.command(name="streamers", description="List all monitored streamers in this server")
async def list_streamers(interaction: discord.Interaction):
    """Show all streamers being monitored in this server"""
    streamers = bot.db.get_server_streamers(interaction.guild_id)
    
    if not streamers:
        await interaction.response.send_message(
            "📋 No streamers are currently being monitored in this server.\n"
            "Use `/addstreamer` to add one!",
            ephemeral=True
        )
        return
    
    # Create embed
    embed_color = bot.db.get_embed_color(interaction.guild_id)
    embed = discord.Embed(
        title="📺 Monitored Streamers",
        description=f"Watching {len(streamers)} streamer(s) in this server",
        color=embed_color
    )
    
    channel_id = bot.db.get_notification_channel(interaction.guild_id)
    if channel_id:
        embed.add_field(
            name="Notification Channel",
            value=f"<#{channel_id}>",
            inline=False
        )
    
    # Split streamers into chunks to avoid 1024 character limit per field
    streamer_links = [f"• [{s['streamer_name']}](https://twitch.tv/{s['streamer_name']})" for s in streamers]
    
    # Build fields with max 1000 characters each (safe margin)
    current_field = []
    current_length = 0
    field_num = 1
    
    for link in streamer_links:
        link_length = len(link) + 1  # +1 for newline
        
        if current_length + link_length > 1000 and current_field:
            # Add current field and start a new one
            field_name = "Streamers" if field_num == 1 else f"Streamers (continued {field_num})"
            embed.add_field(
                name=field_name,
                value="\n".join(current_field),
                inline=False
            )
            current_field = [link]
            current_length = link_length
            field_num += 1
        else:
            current_field.append(link)
            current_length += link_length
    
    # Add the last field
    if current_field:
        field_name = "Streamers" if field_num == 1 else f"Streamers (continued {field_num})"
        embed.add_field(
            name=field_name,
            value="\n".join(current_field),
            inline=False
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="setchannel", description="Set the channel for stream notifications")
@app_commands.describe(channel="Channel where notifications will be sent")
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    """Set the notification channel for this server"""
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ You need 'Manage Server' permission to use this command.",
            ephemeral=True
        )
        return
    
    bot.db.set_notification_channel(interaction.guild_id, channel.id)
    
    await interaction.response.send_message(
        f"✅ Stream notifications will now be sent to {channel.mention}",
        ephemeral=True
    )

@bot.tree.command(name="live", description="Check which monitored streamers are currently live")
async def check_live(interaction: discord.Interaction):
    """Manually check which streamers are live"""
    await interaction.response.defer(ephemeral=True)
    
    streamers = bot.db.get_server_streamers(interaction.guild_id)
    
    if not streamers:
        await interaction.followup.send(
            "📋 No streamers are currently being monitored in this server.",
            ephemeral=True
        )
        return
    
    # Get live status
    streamer_names = [s['streamer_name'] for s in streamers]
    live_streams = await bot.twitch.get_live_streams(streamer_names)
    
    if not live_streams:
        await interaction.followup.send(
            "📴 None of your monitored streamers are currently live.",
            ephemeral=True
        )
        return
    
    # Create embed
    embed = discord.Embed(
        title="🔴 Currently Live",
        description=f"{len(live_streams)} streamer(s) live now",
        color=0xFF0000
    )
    
    for stream in live_streams:
        embed.add_field(
            name=f"{stream['user_name']}",
            value=f"**{stream['title']}**\n"
                  f"Playing: {stream['game_name'] or 'No category'}\n"
                  f"Viewers: {stream['viewer_count']}\n"
                  f"[Watch Now](https://twitch.tv/{stream['user_login']})",
            inline=False
        )
    
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="stats", description="Show bot statistics and resource usage")
async def bot_stats(interaction: discord.Interaction):
    """Display bot statistics including memory usage and uptime"""
    # Get process info
    process = psutil.Process(os.getpid())
    memory_info = process.memory_info()
    
    # Memory usage in MB
    memory_mb = memory_info.rss / 1024 / 1024
    memory_percent = (memory_mb / 256) * 100  # Percentage of 256MB
    
    # CPU usage
    cpu_percent = process.cpu_percent(interval=0.1)
    
    # Uptime
    uptime = datetime.utcnow() - bot.start_time
    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    # Database stats
    all_streamers = bot.db.get_all_streamers()
    unique_streamers = len(set(s['streamer_name'] for s in all_streamers))
    total_servers = len(set(s['guild_id'] for s in all_streamers))
    
    # Currently live streamers
    currently_live = len(bot.live_streamers)
    
    # Create embed
    embed = discord.Embed(
        title="📊 Bot Statistics",
        color=0x9146FF,
        timestamp=datetime.utcnow()
    )
    
    # Memory bar visualization
    memory_bar_length = 10
    filled = int((memory_percent / 100) * memory_bar_length)
    memory_bar = "█" * filled + "░" * (memory_bar_length - filled)
    
    embed.add_field(
        name="💾 Memory Usage",
        value=f"`{memory_bar}` {memory_mb:.1f} MB / 256 MB ({memory_percent:.1f}%)",
        inline=False
    )
    
    embed.add_field(
        name="⚡ CPU Usage",
        value=f"{cpu_percent:.1f}%",
        inline=True
    )
    
    embed.add_field(
        name="⏱️ Uptime",
        value=f"{days}d {hours}h {minutes}m {seconds}s",
        inline=True
    )
    
    embed.add_field(
        name="📺 Monitoring",
        value=f"{unique_streamers} unique streamer(s)\n{total_servers} server(s)",
        inline=True
    )
    
    embed.add_field(
        name="🔴 Currently Live",
        value=f"{currently_live} streamer(s)",
        inline=True
    )
    
    embed.add_field(
        name="🔄 Check Interval",
        value=f"Every {CHECK_INTERVAL_SECONDS} seconds",
        inline=True
    )
    
    embed.add_field(
        name="📡 Latency",
        value=f"{round(bot.latency * 1000)}ms",
        inline=True
    )
    
    embed.set_footer(text="Twitch Notifier Bot")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="testnotification", description="Send a test stream notification to see what it looks like")
async def test_notification(interaction: discord.Interaction):
    """Send a test notification to preview the embed design"""
    # Check if user has manage guild permission
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ You need 'Manage Server' permission to use this command.",
            ephemeral=True
        )
        return
    
    # Get the notification channel
    channel_id = bot.db.get_notification_channel(interaction.guild_id)
    if not channel_id:
        channel_id = interaction.channel_id
    
    channel = bot.get_channel(channel_id)
    
    if not channel:
        await interaction.response.send_message(
            "❌ Notification channel not found. Use `/setchannel` first.",
            ephemeral=True
        )
        return
    
    # Create a fake stream data object
    fake_stream = {
        'user_name': 'TestStreamer',
        'user_login': 'teststreamer',
        'title': 'This is a test notification! Playing some epic games 🎮',
        'game_name': 'Valorant',
        'viewer_count': 1337,
        'thumbnail_url': 'https://static-cdn.jtvnw.net/previews-ttv/live_user_teststreamer-{width}x{height}.jpg',
        'profile_image_url': 'https://static-cdn.jtvnw.net/jtv_user_pictures/default_profile_image-300x300.png'
    }
    
    # Create the same embed as real notifications
    embed = discord.Embed(
        title=fake_stream['title'],
        url=f"https://twitch.tv/{fake_stream['user_login']}",
        description=f"**{fake_stream['user_name']}** is now live!",
        color=0x9146FF,  # Twitch purple
        timestamp=datetime.utcnow()
    )
    
    embed.set_author(
        name=fake_stream['user_name'],
        url=f"https://twitch.tv/{fake_stream['user_login']}",
        icon_url=fake_stream.get('profile_image_url', '')
    )
    
    embed.add_field(
        name="Game",
        value=fake_stream['game_name'] or "No category",
        inline=True
    )
    
    embed.add_field(
        name="Viewers",
        value=str(fake_stream['viewer_count']),
        inline=True
    )
    
    # Use stream thumbnail
    thumbnail_url = fake_stream['thumbnail_url'].replace('{width}', '440').replace('{height}', '248')
    embed.set_image(url=thumbnail_url)
    
    # Get custom color for this server
    test_color = bot.db.get_embed_color(interaction.guild_id)
    embed.color = test_color
    
    embed.set_footer(
        text="🧪 TEST NOTIFICATION - This is a preview",
        icon_url="https://static.twitchcdn.net/assets/favicon-32-e29e246c157142c94346.png"
    )
    
    # Create Watch Stream button
    view = discord.ui.View()
    view.add_item(discord.ui.Button(
        label="Watch Stream",
        url=f"https://twitch.tv/{fake_stream['user_login']}",
        style=discord.ButtonStyle.link,
        emoji="🔴"
    ))
    
    try:
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message(
            f"✅ Test notification sent to {channel.mention}!",
            ephemeral=True
        )
    except Exception as e:
        await interaction.response.send_message(
            f"❌ Failed to send test notification: {str(e)}",
            ephemeral=True
        )

@bot.tree.command(name="importfile", description="Import multiple streamers from a text file")
@app_commands.describe(file="Text file with one streamer name per line")
async def import_file(interaction: discord.Interaction, file: discord.Attachment):
    """Import streamers from a text file (one per line)"""
    # Check permissions
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ You need 'Manage Server' permission to use this command.",
            ephemeral=True
        )
        return
    
    # Check if it's a text file
    if not file.filename.endswith('.txt'):
        await interaction.response.send_message(
            "❌ Please upload a .txt file with one streamer name per line.",
            ephemeral=True
        )
        return
    
    # Defer response since this might take a while
    await interaction.response.defer(ephemeral=True)
    
    try:
        # Download and read the file
        file_content = await file.read()
        text = file_content.decode('utf-8')
        
        # Split by lines and clean up
        streamer_names = [
            line.strip().lower() 
            for line in text.split('\n') 
            if line.strip() and not line.strip().startswith('#')
        ]
        
        if not streamer_names:
            await interaction.followup.send(
                "❌ No streamer names found in the file.",
                ephemeral=True
            )
            return
        
        # Get notification channel
        channel_id = bot.db.get_notification_channel(interaction.guild_id)
        if not channel_id:
            channel_id = interaction.channel_id
            bot.db.set_notification_channel(interaction.guild_id, channel_id)
        
        # Track results
        successful = []
        failed = []
        already_added = []
        
        # Process each streamer
        for streamer_name in streamer_names:
            # Verify streamer exists on Twitch
            user_info = await bot.twitch.get_user(streamer_name)
            
            if not user_info:
                failed.append(streamer_name)
                continue
            
            # Try to add to database
            success = bot.db.add_streamer(
                interaction.guild_id, 
                user_info['login'], 
                channel_id
            )
            
            if success:
                successful.append(user_info['display_name'])
            else:
                already_added.append(user_info['display_name'])
        
        # Build response message
        response_parts = []
        
        if successful:
            response_parts.append(
                f"✅ **Successfully added {len(successful)} streamer(s):**\n" +
                ", ".join(successful)
            )
        
        if already_added:
            response_parts.append(
                f"ℹ️ **Already monitoring {len(already_added)} streamer(s):**\n" +
                ", ".join(already_added)
            )
        
        if failed:
            response_parts.append(
                f"❌ **Failed to add {len(failed)} streamer(s)** (not found on Twitch):\n" +
                ", ".join(failed)
            )
        
        # Add summary
        summary = f"\n📊 **Summary:** {len(successful)} added, {len(already_added)} already existed, {len(failed)} failed"
        response_parts.append(summary)
        
        # Send response
        final_response = "\n\n".join(response_parts)
        
        # Discord has a 2000 character limit, so truncate if needed
        if len(final_response) > 1900:
            final_response = final_response[:1900] + "\n\n... (response truncated)"
        
        await interaction.followup.send(final_response, ephemeral=True)
        
    except UnicodeDecodeError:
        await interaction.followup.send(
            "❌ Could not read file. Please make sure it's a plain text (.txt) file.",
            ephemeral=True
        )
    except Exception as e:
        logger.error(f"Error importing streamers from file: {e}", exc_info=True)
        await interaction.followup.send(
            f"❌ An error occurred while importing: {str(e)}",
            ephemeral=True
        )

@bot.tree.command(name="color", description="Set the embed color for stream notifications")
@app_commands.describe(color="Hex color code (e.g., #9146FF, #FF0000, #00FF00)")
async def set_color(interaction: discord.Interaction, color: str):
    """Set custom embed color for notifications"""
    # Check permissions
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ You need 'Manage Server' permission to use this command.",
            ephemeral=True
        )
        return
    
    # Clean up the color input
    color = color.strip()
    if color.startswith('#'):
        color = color[1:]
    
    # Validate hex color
    if len(color) != 6:
        await interaction.response.send_message(
            "❌ Invalid color format. Please use 6-digit hex code (e.g., `#9146FF` or `9146FF`)",
            ephemeral=True
        )
        return
    
    try:
        # Convert hex string to integer
        color_int = int(color, 16)
    except ValueError:
        await interaction.response.send_message(
            "❌ Invalid hex color. Use only 0-9 and A-F characters (e.g., `#9146FF`)",
            ephemeral=True
        )
        return
    
    # Save to database
    bot.db.set_embed_color(interaction.guild_id, color_int)
    
    # Create preview embed
    preview_embed = discord.Embed(
        title="Color Updated!",
        description=f"Stream notifications will now use this color.",
        color=color_int
    )
    
    preview_embed.add_field(
        name="Hex Code",
        value=f"`#{color.upper()}`",
        inline=True
    )
    
    preview_embed.add_field(
        name="Preview",
        value="This is how your notifications will look!",
        inline=True
    )
    
    await interaction.response.send_message(embed=preview_embed, ephemeral=True)

@bot.tree.command(name="resetcolor", description="Reset embed color to default Twitch purple")
async def reset_color(interaction: discord.Interaction):
    """Reset notification color to default"""
    # Check permissions
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ You need 'Manage Server' permission to use this command.",
            ephemeral=True
        )
        return
    
    # Reset to Twitch purple
    bot.db.set_embed_color(interaction.guild_id, 0x9146FF)
    
    embed = discord.Embed(
        title="✅ Color Reset",
        description="Stream notifications will now use the default Twitch purple.",
        color=0x9146FF
    )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="autodelete", description="Toggle auto-deletion of notifications when streams end")
@app_commands.describe(enabled="Enable or disable auto-delete")
async def auto_delete(interaction: discord.Interaction, enabled: bool):
    """Toggle automatic deletion of notifications when streamers go offline"""
    # Check permissions
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ You need 'Manage Server' permission to use this command.",
            ephemeral=True
        )
        return
    
    # Save setting
    bot.db.set_auto_delete(interaction.guild_id, enabled)
    
    # Create response embed
    embed = discord.Embed(
        title="🗑️ Auto-Delete Enabled" if enabled else "📌 Auto-Delete Disabled",
        description=(
            "Notifications will be **automatically deleted** when streams end." if enabled else
            "Notifications will **stay in the channel** after streams end."
        ),
        color=0x00FF00 if enabled else 0xFF0000
    )
    
    if enabled:
        embed.add_field(
            name="How it works",
            value="• Stream goes live → Notification sent\n"
                  "• Stream ends → Notification deleted\n"
                  "• Keeps your channel clean!",
            inline=False
        )
    else:
        embed.add_field(
            name="How it works",
            value="• Notifications stay in the channel permanently\n"
                  "• Useful for keeping a history of streams",
            inline=False
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="cleanupset", description="Configure automatic message cleanup for a channel")
@app_commands.describe(
    channel="Channel to clean up",
    hours="Delete messages older than this many hours (minimum 12)",
    keep_pinned="Keep pinned messages (default: Yes)"
)
async def cleanup_set(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    hours: int,
    keep_pinned: bool = True
):
    """Set up automatic cleanup for a channel"""
    # Check permissions
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ You need 'Manage Server' permission to use this command.",
            ephemeral=True
        )
        return
    
    # Validate hours
    if hours < 12:
        await interaction.response.send_message(
            "❌ Interval must be at least 12 hours.",
            ephemeral=True
        )
        return
    
    # Check bot permissions in the channel
    permissions = channel.permissions_for(channel.guild.me)
    if not permissions.manage_messages or not permissions.read_message_history:
        await interaction.response.send_message(
            f"❌ I don't have the required permissions in {channel.mention}!\n"
            f"I need: **Manage Messages** and **Read Message History**",
            ephemeral=True
        )
        return
    
    # Save config
    success = bot.db.add_cleanup_config(
        interaction.guild_id,
        channel.id,
        hours,
        keep_pinned
    )
    
    if success:
        days = hours // 24
        await interaction.response.send_message(
            f"✅ **Cleanup configured for {channel.mention}**\n\n"
            f"• **Interval:** {hours} hours ({days} day{'s' if days != 1 else ''})\n"
            f"• **Keep pinned:** {'Yes' if keep_pinned else 'No'}\n\n"
            f"Messages older than {hours} hours will be deleted automatically every hour.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "❌ Failed to save configuration. Please try again.",
            ephemeral=True
        )

@bot.tree.command(name="cleanuplist", description="List all configured cleanup channels")
async def cleanup_list(interaction: discord.Interaction):
    """Show all cleanup configurations for this server"""
    configs = bot.db.get_guild_cleanup_configs(interaction.guild_id)
    
    if not configs:
        await interaction.response.send_message(
            "📋 No cleanup configurations found for this server.\n"
            "Use `/cleanupset` to set one up!",
            ephemeral=True
        )
        return
    
    embed = discord.Embed(
        title="🗑️ Configured Cleanup Channels",
        description=f"Auto-cleanup is active in {len(configs)} channel(s)",
        color=0x00FF00
    )
    
    for config in configs:
        channel_obj = bot.get_channel(config['channel_id'])
        channel_name = channel_obj.mention if channel_obj else f"Unknown Channel ({config['channel_id']})"
        hours = config['interval_hours']
        days = hours // 24
        
        embed.add_field(
            name=channel_name,
            value=f"• **Interval:** {hours}h ({days} day{'s' if days != 1 else ''})\n"
                  f"• **Keep pinned:** {'Yes' if config['keep_pinned'] else 'No'}",
            inline=False
        )
    
    if bot.cleanup_stats['last_run']:
        embed.set_footer(text=f"Last cleanup: {bot.cleanup_stats['last_run'].strftime('%Y-%m-%d %H:%M UTC')}")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="cleanupremove", description="Remove cleanup configuration from a channel")
@app_commands.describe(channel="Channel to remove cleanup from")
async def cleanup_remove(interaction: discord.Interaction, channel: discord.TextChannel):
    """Remove cleanup config"""
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ You need 'Manage Server' permission to use this command.",
            ephemeral=True
        )
        return
    
    success = bot.db.remove_cleanup_config(interaction.guild_id, channel.id)
    
    if success:
        await interaction.response.send_message(
            f"✅ Removed cleanup configuration for {channel.mention}",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"❌ No cleanup configuration found for {channel.mention}",
            ephemeral=True
        )

@bot.tree.command(name="cleanuptest", description="Preview what would be deleted (doesn't actually delete)")
@app_commands.describe(channel="Channel to test cleanup on")
async def cleanup_test(interaction: discord.Interaction, channel: discord.TextChannel):
    """Test cleanup without actually deleting"""
    config = bot.db.get_cleanup_config(interaction.guild_id, channel.id)
    
    if not config:
        await interaction.response.send_message(
            f"❌ No cleanup configuration found for {channel.mention}\n"
            f"Use `/cleanupset` first.",
            ephemeral=True
        )
        return
    
    await interaction.response.defer(ephemeral=True)
    
    # Count messages that would be deleted
    cutoff_time = datetime.utcnow() - timedelta(hours=config['interval_hours'])
    count = 0
    
    try:
        async for message in channel.history(limit=1000, before=cutoff_time):
            if config['keep_pinned'] and message.pinned:
                continue
            count += 1
    except discord.Forbidden:
        await interaction.followup.send(
            f"❌ I don't have permission to read message history in {channel.mention}",
            ephemeral=True
        )
        return
    
    hours = config['interval_hours']
    days = hours // 24
    
    await interaction.followup.send(
        f"🧪 **Test Results for {channel.mention}**\n\n"
        f"**Messages that would be deleted:** {count}\n"
        f"(Checked last 1000 messages older than {hours} hours / {days} day{'s' if days != 1 else ''})\n\n"
        f"**Keep pinned messages:** {'Yes' if config['keep_pinned'] else 'No'}\n\n"
        f"ℹ️ This is a preview only - no messages were deleted.",
        ephemeral=True
    )

@bot.tree.command(name="botinfo", description="Show bot statistics and server information")
async def bot_info(interaction: discord.Interaction):
    """Display bot stats including server count and configurations"""
    # Owner-only command
    if interaction.user.id != BOT_OWNER_ID:
        await interaction.response.send_message(
            "❌ This command is restricted to the bot owner.",
            ephemeral=True
        )
        return
    
    # Get all servers bot is in
    guild_count = len(bot.guilds)
    
    # Get total streamers being monitored
    all_streamers = bot.db.get_all_streamers()
    unique_streamers = len(set(s['streamer_name'] for s in all_streamers))
    total_configs = len(all_streamers)
    
    # Get cleanup configs
    cleanup_configs = bot.db.get_all_cleanup_configs()
    
    # Create embed
    embed = discord.Embed(
        title="🤖 Bot Information",
        color=0x9146FF
    )
    
    embed.add_field(
        name="📊 Servers",
        value=f"{guild_count} server{'s' if guild_count != 1 else ''}",
        inline=True
    )
    
    embed.add_field(
        name="📺 Unique Streamers",
        value=f"{unique_streamers} being monitored",
        inline=True
    )
    
    embed.add_field(
        name="🔔 Total Configs",
        value=f"{total_configs} across all servers",
        inline=True
    )
    
    embed.add_field(
        name="🗑️ Cleanup Channels",
        value=f"{len(cleanup_configs)} configured",
        inline=True
    )
    
    embed.add_field(
        name="💾 Memory Usage",
        value=f"{round(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024, 1)} MB",
        inline=True
    )
    
    embed.add_field(
        name="⏱️ Uptime",
        value=f"{(datetime.utcnow() - bot.start_time).days} days",
        inline=True
    )
    
    # List servers
    server_list = "\n".join([f"• {guild.name} ({guild.id})" for guild in bot.guilds])
    if len(server_list) > 1024:
        server_list = server_list[:1020] + "..."
    
    embed.add_field(
        name="🏠 Servers",
        value=server_list or "None",
        inline=False
    )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="serverdetails", description="Show detailed info for a specific server")
@app_commands.describe(server_id="Server ID to check (leave empty for current server)")
async def server_details(interaction: discord.Interaction, server_id: str = None):
    """Show streamers and configs for a specific server"""
    # Owner-only command
    if interaction.user.id != BOT_OWNER_ID:
        await interaction.response.send_message(
            "❌ This command is restricted to the bot owner.",
            ephemeral=True
        )
        return
    
    # Use current server if no ID provided
    guild_id = int(server_id) if server_id else interaction.guild_id
    
    # Get guild info
    guild = bot.get_guild(guild_id)
    if not guild:
        await interaction.response.send_message(
            f"❌ Server with ID {guild_id} not found or bot is not in that server.",
            ephemeral=True
        )
        return
    
    # Get streamers for this guild
    streamers = bot.db.get_server_streamers(guild_id)
    cleanup_configs = bot.db.get_guild_cleanup_configs(guild_id)
    
    embed = discord.Embed(
        title=f"📋 Server Details: {guild.name}",
        description=f"Server ID: `{guild_id}`",
        color=0x9146FF
    )
    
    # Notification channel
    notif_channel_id = bot.db.get_notification_channel(guild_id)
    notif_channel = bot.get_channel(notif_channel_id) if notif_channel_id else None
    
    embed.add_field(
        name="🔔 Notification Channel",
        value=notif_channel.mention if notif_channel else "Not set",
        inline=False
    )
    
    # Streamers
    if streamers:
        streamer_list = "\n".join([f"• {s['streamer_name']}" for s in streamers[:20]])
        if len(streamers) > 20:
            streamer_list += f"\n... and {len(streamers) - 20} more"
        
        embed.add_field(
            name=f"📺 Monitored Streamers ({len(streamers)})",
            value=streamer_list,
            inline=False
        )
    else:
        embed.add_field(
            name="📺 Monitored Streamers",
            value="None",
            inline=False
        )
    
    # Cleanup configs
    if cleanup_configs:
        cleanup_list = []
        for config in cleanup_configs[:5]:
            channel = bot.get_channel(config['channel_id'])
            channel_name = channel.mention if channel else f"Unknown ({config['channel_id']})"
            cleanup_list.append(f"• {channel_name}: {config['interval_hours']}h")
        
        if len(cleanup_configs) > 5:
            cleanup_list.append(f"... and {len(cleanup_configs) - 5} more")
        
        embed.add_field(
            name=f"🗑️ Cleanup Channels ({len(cleanup_configs)})",
            value="\n".join(cleanup_list),
            inline=False
        )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="manualnotif", description="Manually send a stream notification")
@app_commands.describe(
    streamer="Twitch streamer username",
    channel="Channel to send notification to (optional, uses notification channel if not specified)"
)
async def manual_notif(
    interaction: discord.Interaction,
    streamer: str,
    channel: discord.TextChannel = None
):
    """Manually send a notification for a streamer"""
    # Check permissions
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ You need 'Manage Server' permission to use this command.",
            ephemeral=True
        )
        return
    
    await interaction.response.defer(ephemeral=True)
    
    # Get streamer info from Twitch
    user_info = await bot.twitch.get_user(streamer)
    
    if not user_info:
        await interaction.followup.send(
            f"❌ Streamer '{streamer}' not found on Twitch.",
            ephemeral=True
        )
        return
    
    # Get stream info
    streams = await bot.twitch.get_live_streams([user_info['login']])
    
    if not streams:
        await interaction.followup.send(
            f"ℹ️ {user_info['display_name']} is not currently live.\n"
            f"Sending notification anyway with placeholder data...",
            ephemeral=True
        )
        
        # Create fake stream data
        stream = {
            'user_name': user_info['display_name'],
            'user_login': user_info['login'],
            'title': 'Live Stream',
            'game_name': 'Just Chatting',
            'viewer_count': 0,
            'thumbnail_url': f"https://static-cdn.jtvnw.net/previews-ttv/live_user_{user_info['login']}-{{width}}x{{height}}.jpg",
            'profile_image_url': user_info.get('profile_image_url', '')
        }
    else:
        stream = streams[0]
    
    # Use specified channel or notification channel
    if not channel:
        channel_id = bot.db.get_notification_channel(interaction.guild_id)
        if channel_id:
            channel = bot.get_channel(channel_id)
    
    if not channel:
        await interaction.followup.send(
            "❌ Please specify a channel or set a notification channel with `/setchannel`",
            ephemeral=True
        )
        return
    
    # Get custom color
    embed_color = bot.db.get_embed_color(interaction.guild_id)
    
    # Create embed
    embed = discord.Embed(
        title=stream['title'],
        url=f"https://twitch.tv/{stream['user_login']}",
        description=f"**{stream['user_name']}** is now live!",
        color=embed_color,
        timestamp=datetime.utcnow()
    )
    
    embed.set_author(
        name=stream['user_name'],
        url=f"https://twitch.tv/{stream['user_login']}",
        icon_url=stream.get('profile_image_url', '')
    )
    
    embed.add_field(
        name="Game",
        value=stream['game_name'] or "No category",
        inline=True
    )
    
    embed.add_field(
        name="Viewers",
        value=str(stream['viewer_count']),
        inline=True
    )
    
    thumbnail_url = stream['thumbnail_url'].replace('{width}', '440').replace('{height}', '248')
    embed.set_image(url=thumbnail_url)
    
    embed.set_footer(
        text="Twitch",
        icon_url="https://static.twitchcdn.net/assets/favicon-32-e29e246c157142c94346.png"
    )
    
    # Add Watch Stream button
    view = discord.ui.View()
    view.add_item(discord.ui.Button(
        label="Watch Stream",
        url=f"https://twitch.tv/{stream['user_login']}",
        style=discord.ButtonStyle.link,
        emoji="🔴"
    ))
    
    # Send notification
    try:
        message = await channel.send(embed=embed, view=view)
        
        # Save for auto-delete if enabled
        if bot.db.get_auto_delete(interaction.guild_id):
            bot.db.save_notification_message(
                interaction.guild_id,
                stream['user_login'],
                channel.id,
                message.id
            )
        
        await interaction.followup.send(
            f"✅ Manual notification sent for {stream['user_name']} to {channel.mention}",
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(
            f"❌ Failed to send notification: {str(e)}",
            ephemeral=True
        )

# Run the bot
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
