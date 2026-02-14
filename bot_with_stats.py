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
from config import DISCORD_TOKEN, CHECK_INTERVAL_SECONDS

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
        
        except Exception as e:
            logger.error(f"Error in check_streams loop: {e}", exc_info=True)
    
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
                return
            
            # Create embed notification
            embed = discord.Embed(
                title=stream['title'],
                url=f"https://twitch.tv/{stream['user_login']}",
                description=f"**{stream['user_name']}** is now live!",
                color=0x9146FF,  # Twitch purple
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
            
            await channel.send(embed=embed)
            logger.info(f"Sent notification for {stream['user_name']} to {channel.guild.name}")
        
        except Exception as e:
            logger.error(f"Error sending notification: {e}", exc_info=True)

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
    embed = discord.Embed(
        title="📺 Monitored Streamers",
        description=f"Watching {len(streamers)} streamer(s) in this server",
        color=0x9146FF
    )
    
    channel_id = bot.db.get_notification_channel(interaction.guild_id)
    if channel_id:
        embed.add_field(
            name="Notification Channel",
            value=f"<#{channel_id}>",
            inline=False
        )
    
    streamer_list = "\n".join([f"• [{s['streamer_name']}](https://twitch.tv/{s['streamer_name']})" for s in streamers])
    embed.add_field(
        name="Streamers",
        value=streamer_list,
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

# Run the bot
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
