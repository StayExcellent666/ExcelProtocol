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
            
            await channel.send(embed=embed, view=view)
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

# Run the bot
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
