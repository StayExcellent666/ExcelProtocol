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
from config import DISCORD_TOKEN, CHECK_INTERVAL_SECONDS, BOT_OWNER_ID, LOG_CHANNEL_ID
from config import TWITCH_BOT_USERNAME, TWITCH_BOT_TOKEN

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
        intents.members = True  # Required for display names in birthdaylist and member lookups
        
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
        # Load Twitch chat bot — wrapped in try/except so any failure here
        # never takes down the Discord bot
        self.twitch_chat_bot = None
        if TWITCH_BOT_USERNAME and TWITCH_BOT_TOKEN:
            try:
                from twitch_bot import TwitchChatBot
                import twitch_chat_cog

                registered_channels = [r['twitch_channel'] for r in self.db.get_all_twitch_channels()]

                bot_user = await self.twitch.get_user(TWITCH_BOT_USERNAME)
                if not bot_user:
                    logger.error(f'Could not fetch Twitch user info for {TWITCH_BOT_USERNAME} - Twitch chat bot disabled')
                else:
                    bot_id = bot_user['id']
                    self.twitch_chat_bot = TwitchChatBot(
                        token=TWITCH_BOT_TOKEN,
                        initial_channels=registered_channels,
                        db=self.db,
                        twitch_api=self.twitch
                    )
                    await twitch_chat_cog.setup(self, self.twitch_chat_bot)
                    asyncio.create_task(self.twitch_chat_bot.start())
                    logger.info("Twitch chat bot started")
            except Exception as e:
                logger.error(f"Twitch chat bot failed to start: {e} - Discord bot continuing normally")
                self.twitch_chat_bot = None
        else:
            logger.info("Twitch chat bot not configured (TWITCH_BOT_USERNAME / TWITCH_BOT_TOKEN not set)")

        # Load reaction roles
        try:
            import reaction_roles
            await reaction_roles.setup(self)
            logger.info("Reaction roles loaded")
        except Exception as e:
            logger.error(f"Reaction roles failed to load: {e} - continuing normally")

        # Load setchannel and birthday cogs
        try:
            import setchannel_cog
            await setchannel_cog.setup(self)
            logger.info("SetChannel cog loaded")
        except Exception as e:
            logger.error(f"SetChannel cog failed to load: {e} - continuing normally")

        try:
            import birthday_cog
            await birthday_cog.setup(self)
            logger.info("Birthday cog loaded")
        except Exception as e:
            logger.error(f"Birthday cog failed to load: {e} - continuing normally")

        await self.tree.sync()
        logger.info("Command tree synced")
    
    async def on_ready(self):
        """Called when bot successfully connects to Discord"""
        logger.info(f'Logged in as {self.user} (ID: {self.user.id})')
        logger.info('------')

        # Bug 2 fix: re-populate live_streamers from the DB so that streamers who
        # were already notified before a restart are not double-notified, and so
        # their stored message IDs can still be deleted when they go offline.
        try:
            conn = self.db.get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT streamer_name FROM notification_messages")
            rows = cursor.fetchall()
            conn.close()
            for (name,) in rows:
                self.live_streamers.add(name.lower())
            if rows:
                logger.info(f"Restored {len(rows)} active streamer(s) from notification_messages into live_streamers")
        except Exception as e:
            logger.error(f"Failed to restore live_streamers from DB on startup: {e}")
        
        # Start the polling loop
        if not self.check_streams.is_running():
            self.check_streams.start()
            logger.info("Stream checking loop started")
        
        # Start the cleanup loop
        if not self.cleanup_channels.is_running():
            self.cleanup_channels.start()
            logger.info("Channel cleanup loop started")
        
        # Start monthly leaderboard cleanup loop
        if not self.monthly_leaderboard_cleanup.is_running():
            self.monthly_leaderboard_cleanup.start()
            logger.info("Monthly leaderboard cleanup loop started")

        # Start status rotation loop
        if not self.rotate_status.is_running():
            self.rotate_status.start()
            logger.info("Status rotation loop started")

        # Start broadcaster token refresh loop
        if not self.refresh_broadcaster_tokens.is_running():
            self.refresh_broadcaster_tokens.start()
            logger.info("Broadcaster token refresh loop started")

        # Start permission check loop
        if not self.check_permissions.is_running():
            self.check_permissions.start()
            logger.info("Permission check loop started")

        # Start stat channel update loop
        if not self.update_stat_channels.is_running():
            self.update_stat_channels.start()
            logger.info("Stat channel update loop started")

        # Start milestone check loop (separate from EventSub)
        if not self.check_milestones.is_running():
            self.check_milestones.start()
            logger.info("Milestone check loop started")

        guild_count = len(self.guilds)
        await self.log_to_channel(
            "🤖", "Bot Started",
            f"ExcelProtocol is online.\n**Servers:** {guild_count}",
            color=0x00CC66
        )

        # Sync EventSub subscriptions on startup (async so it doesn't block ready)
        asyncio.create_task(self._initial_eventsub_sync())
    
    async def _register_eventsub_for_user(self, user_id: str, user_login: str):
        """Register stream.online and stream.offline EventSub for a single user."""
        callback_url, secret = await self._eventsub_config()
        success = True
        for event_type in ("stream.online", "stream.offline"):
            result = await self.twitch.register_stream_subscription(
                user_id, event_type, callback_url, secret
            )
            if result and not result.get("already_exists"):
                logger.info(f"Registered {event_type} EventSub for {user_login}")
            elif result and result.get("already_exists"):
                logger.debug(f"{event_type} EventSub already exists for {user_login}")
            else:
                success = False
                logger.warning(f"Failed to register {event_type} EventSub for {user_login}")
                await self.log_to_channel(
                    "⚠️", "EventSub Registration Failed",
                    f"Could not register `{event_type}` for **{user_login}**.\nStream notifications may not work.",
                    color=0xFF6B35
                )

    async def _initial_eventsub_sync(self):
        """Run EventSub sync on startup with a small delay to let things settle."""
        await asyncio.sleep(5)
        try:
            await self._sync_eventsub_subscriptions(alert_on_mismatch=False)
            await self.log_to_channel(
                "📡", "EventSub Subscriptions Synced",
                "Stream online/offline subscriptions registered for all monitored streamers.",
                color=0x00CC66
            )
        except Exception as e:
            logger.error(f"Initial EventSub sync failed: {e}", exc_info=True)
            await self.log_to_channel(
                "❌", "EventSub Initial Sync Failed",
                f"`{type(e).__name__}: {str(e)[:300]}`\n\nStream notifications may not work until the next sync attempt (30 min).",
                color=0xFF4444
            )

    async def close(self):
        """Called when bot is shutting down cleanly."""
        try:
            await self.log_to_channel(
                "🔴", "Bot Shutting Down",
                "ExcelProtocol is going offline.",
                color=0xFF4444
            )
            await asyncio.sleep(1)
        except Exception:
            pass
        # Clean up Twitch API session
        try:
            await self.twitch.close()
        except Exception as e:
            logger.error(f"Error closing Twitch API session: {e}")
        # Clean up Twitch chat bot
        try:
            if hasattr(self, 'twitch_chat_bot') and self.twitch_chat_bot:
                await self.twitch_chat_bot.close()
        except Exception as e:
            logger.error(f"Error closing Twitch chat bot: {e}")
        await super().close()

    async def on_guild_remove(self, guild):
        """Called when bot is removed from a server - clean up data"""
        logger.info(f"Bot removed from guild: {guild.name} (ID: {guild.id})")
        self.db.cleanup_guild(guild.id)
        logger.info(f"Cleaned up all data for guild {guild.id}")
    
    # ── EventSub Stream Notifications ────────────────────────────────────────

    @tasks.loop(minutes=30)
    async def check_streams(self):
        """Keep EventSub subscriptions healthy — re-register any missing ones every 30 min."""
        try:
            await self._sync_eventsub_subscriptions()
        except Exception as e:
            logger.error(f"Error in EventSub sync loop: {e}", exc_info=True)
            await self.log_to_channel(
                "⚠️", "EventSub Sync Error",
                f"**Error syncing EventSub subscriptions**\n`{type(e).__name__}: {str(e)[:300]}`",
                color=0xFF6B35
            )

    @check_streams.before_loop
    async def before_check_streams(self):
        """Wait until bot is ready before starting the loop"""
        await self.wait_until_ready()

    async def _eventsub_config(self):
        """Return (callback_url, secret) for EventSub."""
        base = os.getenv("DASHBOARD_BASE_URL", "https://excelprotocol.fly.dev")
        secret = os.getenv("EVENTSUB_SECRET")
        if not secret:
            raise RuntimeError("EVENTSUB_SECRET env var is not set — cannot register EventSub subscriptions safely")
        return f"{base}/api/eventsub/callback", secret

    async def _sync_eventsub_subscriptions(self, alert_on_mismatch: bool = True):
        """Register stream.online / stream.offline subscriptions for all monitored streamers."""
        # Prevent concurrent syncs (startup + loop overlap on boot)
        if getattr(self, '_eventsub_syncing', False):
            logger.debug("EventSub sync already in progress, skipping")
            return
        self._eventsub_syncing = True
        try:
            await self.__do_eventsub_sync(alert_on_mismatch)
        finally:
            self._eventsub_syncing = False

    async def __do_eventsub_sync(self, alert_on_mismatch: bool = True):
        """Internal EventSub sync logic."""
        callback_url, secret = await self._eventsub_config()

        # Get all unique streamers from DB with guild info
        streamers = self.db.get_all_streamers()
        unique_logins = list({s['streamer_name'].lower() for s in streamers})
        if not unique_logins:
            return

        # Build login → guild ids+names lookup for error reporting
        login_guilds: dict[str, list[str]] = {}
        login_guild_ids: dict[str, list[int]] = {}
        for s in streamers:
            login = s['streamer_name'].lower()
            guild = self.get_guild(s['guild_id'])
            guild_name = guild.name if guild else str(s['guild_id'])
            login_guilds.setdefault(login, []).append(guild_name)
            login_guild_ids.setdefault(login, []).append(s['guild_id'])

        # Clear stale unresolvable records — will repopulate fresh below
        self.db.clear_unresolvable_streamers()

        # Get existing subscriptions so we don't double-register
        existing = await self.twitch.get_subscriptions()
        existing_keys = set()
        for sub in existing:
            if sub.get("type") in ("stream.online", "stream.offline"):
                uid = sub.get("condition", {}).get("broadcaster_user_id", "")
                existing_keys.add((sub["type"], uid))

        registered = 0
        failed = 0
        failed_names = []
        unresolvable = []

        for i in range(0, len(unique_logins), 100):
            batch = unique_logins[i:i+100]
            session = await self.twitch.get_session()
            headers = await self.twitch._headers()
            params = [("login", login) for login in batch]
            try:
                async with session.get(
                    "https://api.twitch.tv/helix/users",
                    headers=headers, params=params
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"Failed to resolve user IDs for EventSub batch: {resp.status}")
                        continue
                    data = await resp.json()
                    users = data.get("data", [])
            except Exception as e:
                logger.error(f"Error resolving user IDs for EventSub: {e}")
                continue

            resolved_logins = {u["login"].lower() for u in users}
            for login in batch:
                if login not in resolved_logins:
                    guilds = login_guilds.get(login, [])
                    guild_str = ", ".join(set(guilds))
                    unresolvable.append(f"{login} ({guild_str})")
                    logger.warning(f"EventSub: no Twitch user for '{login}' in [{guild_str}] — banned/deleted/renamed?")
                    # Persist to DB so dashboard can show warning per guild
                    for gid in login_guild_ids.get(login, []):
                        self.db.add_unresolvable_streamer(login, gid)

            for user in users:
                uid = user["id"]
                for event_type in ("stream.online", "stream.offline"):
                    if (event_type, uid) in existing_keys:
                        continue
                    result = await self.twitch.register_stream_subscription(
                        uid, event_type, callback_url, secret
                    )
                    if result and not result.get("already_exists"):
                        registered += 1
                    elif not result:
                        failed += 1
                        failed_names.append(f"{user['login']} ({event_type})")
                        logger.warning(f"Failed to register {event_type} for {user['login']} ({uid})")

        if registered or failed:
            logger.info(f"EventSub sync: registered {registered} new, {failed} failed, {len(unresolvable)} unresolvable")

        if alert_on_mismatch and (failed or unresolvable):
            lines = []
            if failed:
                lines.append(f"⚠️ **{failed}** registration failure(s):\n" + "\n".join(f"• {n}" for n in failed_names[:10]))
            if unresolvable:
                lines.append(f"👻 **{len(unresolvable)}** unresolvable account(s) — likely banned, deleted or renamed:\n" + "\n".join(f"• {n}" for n in unresolvable[:10]))
            await self.log_to_channel(
                "📡", "EventSub Sync Issues",
                "\n\n".join(lines),
                color=0xFF6B35
            )

        if alert_on_mismatch:
            resolvable_count = len(unique_logins) - len(unresolvable)
            expected = resolvable_count * 2
            actual = len([s for s in existing if s.get("type") in ("stream.online", "stream.offline")]) + registered
            missing = expected - actual
            threshold = max(20, int(expected * 0.10))
            if missing > threshold:
                await self.log_to_channel(
                    "🚨", "EventSub Subscription Mismatch",
                    f"Expected **{expected}** subscriptions ({resolvable_count} active streamers × 2) but only **{actual}** registered.\n"
                    f"**{missing}** missing — Twitch may have revoked subscriptions.",
                    color=0xFF4444
                )
                await self.send_owner_alert(
                    "EventSub Mismatch",
                    f"**{missing} stream subscriptions missing!**\n\nExpected {expected}, found {actual}.\n"
                    f"Twitch may have revoked subscriptions. Next 30-min sync will attempt to re-register."
                )

    async def handle_stream_online(self, user_login: str, user_id: str):
        """Called by the dashboard webhook when a stream.online event is received."""
        try:
            logger.info(f"EventSub stream.online: {user_login}")
            self.live_streamers.add(user_login.lower())

            # Fetch full stream data
            stream = await self.twitch.get_stream_info_by_user_id(user_id)
            if not stream:
                logger.warning(f"stream.online for {user_login} but no stream data returned (may be very new)")
                # Retry once after a short delay
                await asyncio.sleep(10)
                stream = await self.twitch.get_stream_info_by_user_id(user_id)
                if not stream:
                    logger.error(f"Still no stream data for {user_login} after retry — skipping notification")
                    return

            # Wait for thumbnail
            logger.info(f"Waiting 15s for {user_login} thumbnail...")
            await asyncio.sleep(15)

            # Re-fetch after wait to get fresh thumbnail URL
            stream = await self.twitch.get_stream_info_by_user_id(user_id) or stream

            # Find all servers monitoring this streamer
            streamers = self.db.get_all_streamers()
            monitoring_servers = [
                s for s in streamers
                if s['streamer_name'].lower() == user_login.lower()
            ]

            for server_data in monitoring_servers:
                await self.send_notification(server_data, stream)

            await self.log_to_channel(
                "🟢", "Stream Online (EventSub)",
                f"**{stream.get('user_name', user_login)}** went live — notified {len(monitoring_servers)} server(s).",
                color=0x00CC66
            )

        except Exception as e:
            logger.error(f"Error handling stream.online for {user_login}: {e}", exc_info=True)
            await self.log_to_channel(
                "❌", "Stream Online Error",
                f"**Error handling stream.online for `{user_login}`**\n`{type(e).__name__}: {str(e)[:300]}`",
                color=0xFF4444
            )

    async def handle_stream_offline(self, user_login: str):
        """Called by the dashboard webhook when a stream.offline event is received."""
        try:
            logger.info(f"EventSub stream.offline: {user_login}")
            name_lower = user_login.lower()
            if name_lower in self.live_streamers:
                self.live_streamers.discard(name_lower)

            # Clear milestones
            streamers = self.db.get_all_streamers()
            for s in streamers:
                if s['streamer_name'].lower() == name_lower:
                    self.db.clear_milestones_for_streamer(s['guild_id'], name_lower)

            await self.delete_offline_notifications(user_login)

            await self.log_to_channel(
                "🔴", "Stream Offline (EventSub)",
                f"**{user_login}** went offline — notifications cleaned up.",
                color=0xFF4444
            )

        except Exception as e:
            logger.error(f"Error handling stream.offline for {user_login}: {e}", exc_info=True)
            await self.log_to_channel(
                "❌", "Stream Offline Error",
                f"**Error handling stream.offline for `{user_login}`**\n`{type(e).__name__}: {str(e)[:300]}`",
                color=0xFF4444
            )

    # ── Milestone Check (lightweight poll — EventSub doesn't cover this) ──────

    @tasks.loop(minutes=5)
    async def check_milestones(self):
        """Check 5h/10h stream milestones for currently live streamers."""
        try:
            if not self.live_streamers:
                return
            streamers = self.db.get_all_streamers()
            live_list = list(self.live_streamers)
            for i in range(0, len(live_list), 100):
                batch = live_list[i:i+100]
                live_streams = await self.twitch.get_live_streams(batch)
                for stream in live_streams:
                    streamer_name = stream['user_login']
                    stream_start = datetime.strptime(stream['started_at'], '%Y-%m-%dT%H:%M:%SZ')
                    hours_live = (datetime.utcnow() - stream_start).total_seconds() / 3600
                    for milestone_hours, description in [
                        (5, f"⏱️ **{stream['user_name']}** has been live for **5 HOURS!** They're not stopping anytime soon!"),
                        (10, f"💀 **{stream['user_name']}** has been live for **10 HOURS STRAIGHT.** Send help. 👀"),
                    ]:
                        if hours_live >= milestone_hours:
                            monitoring_servers = [
                                s for s in streamers
                                if s['streamer_name'].lower() == streamer_name.lower()
                            ]
                            for server_data in monitoring_servers:
                                guild_id = server_data['guild_id']
                                if not self.db.get_milestone_notifications(guild_id):
                                    continue
                                if self.db.has_milestone_been_sent(guild_id, streamer_name, milestone_hours):
                                    continue
                                channel_id = server_data.get('custom_channel_id') or server_data['channel_id']
                                channel = self.get_channel(channel_id)
                                if not channel:
                                    continue
                                try:
                                    embed_color = self.db.get_embed_color(guild_id)
                                    embed = discord.Embed(
                                        description=description,
                                        color=embed_color,
                                        timestamp=datetime.utcnow()
                                    )
                                    embed.set_author(
                                        name=stream['user_name'],
                                        url=f"https://twitch.tv/{stream['user_login']}",
                                        icon_url=stream.get('profile_image_url', '')
                                    )
                                    embed.add_field(name="Game", value=stream['game_name'] or "No category", inline=True)
                                    embed.add_field(name="Viewers", value=f"{stream['viewer_count']:,}", inline=True)
                                    thumbnail_url = stream['thumbnail_url'].replace('{width}', '440').replace('{height}', '248')
                                    embed.set_image(url=thumbnail_url)
                                    embed.set_footer(text="Twitch", icon_url="https://static.twitchcdn.net/assets/favicon-32-e29e246c157142c94346.png")
                                    view = discord.ui.View()
                                    view.add_item(discord.ui.Button(
                                        label="Watch Stream",
                                        url=f"https://twitch.tv/{stream['user_login']}",
                                        style=discord.ButtonStyle.link,
                                        emoji="🔴"
                                    ))
                                    await channel.send(embed=embed, view=view)
                                    self.db.record_milestone_sent(guild_id, streamer_name, milestone_hours)
                                    logger.info(f"Sent {milestone_hours}h milestone for {streamer_name} in guild {guild_id}")
                                except Exception as e:
                                    logger.error(f"Error sending milestone notification: {e}")
        except Exception as e:
            logger.error(f"Error in milestone check: {e}", exc_info=True)

    @check_milestones.before_loop
    async def before_check_milestones(self):
        await self.wait_until_ready()

    async def alert_permission_issue(self, guild: discord.Guild, channel_id: int, issue: str):
        """DM the guild owner and bot owner when a permission issue is detected."""
        guild_owner = guild.owner
        owner_notified = False

        # Rate limit guild owner DM to once per hour per guild, same as send_owner_alert
        error_key = f"perm_issue:{guild.id}"
        current_time = datetime.utcnow()
        if error_key in self.error_alerts_sent:
            time_diff = (current_time - self.error_alerts_sent[error_key]).total_seconds()
            if time_diff < self.alert_cooldown:
                logger.debug(f"Skipping guild owner DM for {guild.name} — cooldown active")
                return
        self.error_alerts_sent[error_key] = current_time

        admin_embed = discord.Embed(
            title="⚠️ ExcelProtocol Permission Issue",
            description=(
                f"ExcelProtocol is having trouble sending notifications in **{guild.name}**.\n\n"
                f"**Issue:** {issue}\n\n"
                f"**Channel:** <#{channel_id}>\n\n"
                f"Please make sure ExcelProtocol has **Send Messages** and **Embed Links** "
                f"permissions in that channel, then use `/setchannel` to reconfirm it.\n\n"
                f"If you need help, contact the bot owner on Discord: `stayexcellent`\n\n"
                f"⏱️ *This alert is sent at most once per hour to avoid spam.*"
            ),
            color=0xFF0000
        )
        admin_embed.set_footer(text="ExcelProtocol — Notification System")

        # DM guild owner
        if guild_owner:
            try:
                await guild_owner.send(embed=admin_embed)
                logger.info(f"Sent permission issue DM to guild owner {guild_owner} in {guild.name}")
                owner_notified = True
            except discord.Forbidden:
                logger.warning(f"Could not DM guild owner {guild_owner} in {guild.name} — DMs disabled")
                owner_notified = False
            except Exception as e:
                logger.error(f"Error DMing guild owner: {e}")
                owner_notified = False

        # DM bot owner — include whether guild owner was reached
        owner_note = (
            "Guild owner has been notified automatically."
            if owner_notified else
            "⚠️ Could not DM guild owner (DMs disabled) — you may need to reach out manually."
        )

        await self.send_owner_alert(
            "Permission Issue",
            f"**Bot cannot send notifications!**\n\n"
            f"**Server:** {guild.name} (`{guild.id}`)\n"
            f"**Channel:** <#{channel_id}>\n"
            f"**Issue:** {issue}\n\n"
            f"{owner_note}",
            guild_id=guild.id
        )
        await self.log_to_channel(
            "❌", "Notification Failed — Permission Issue",
            f"**Server:** {guild.name}\n**Channel:** <#{channel_id}>\n**Issue:** {issue}",
            color=0xFF0000
        )

    async def send_notification(self, server_data, stream):
        """Send a notification embed to the configured channel"""
        try:
            guild = self.get_guild(server_data['guild_id'])
            # Bug 3 fix: honour custom_channel_id so the stored channel_id matches
            # where the message is actually sent (mirrors repostlive behaviour).
            effective_channel_id = server_data.get('custom_channel_id') or server_data['channel_id']
            channel = self.get_channel(effective_channel_id)

            if not channel:
                logger.warning(f"Channel {effective_channel_id} not found")
                if guild:
                    await self.alert_permission_issue(
                        guild,
                        effective_channel_id,
                        "Notification channel not found — it may have been deleted."
                    )
                self.db.log_notification(server_data['guild_id'], stream['user_login'], effective_channel_id, 'failed')
                return

            # Check permissions before attempting to send
            perms = channel.permissions_for(guild.me)
            missing = []
            if not perms.send_messages:
                missing.append("Send Messages")
            if not perms.embed_links:
                missing.append("Embed Links")

            if missing:
                logger.warning(f"Missing permissions in #{channel.name} ({guild.name}): {', '.join(missing)}")
                await self.alert_permission_issue(
                    guild,
                    channel.id,
                    f"Missing permissions: **{', '.join(missing)}**"
                )
                self.db.log_notification(server_data['guild_id'], stream['user_login'], effective_channel_id, 'failed')
                return

            # Bug 4 fix: DB-level dedup to prevent duplicate notifications during
            # rolling deploys where multiple instances briefly run simultaneously,
            # each with an empty live_streamers set.
            if self.db.recent_notification_exists(server_data['guild_id'], stream['user_login']):
                logger.info(f"Skipping duplicate notification for {stream['user_login']} in guild {server_data['guild_id']} (sent within last 10 minutes)")
                return

            # Resolve ping role (if configured)
            ping_role_id = self.db.get_ping_role(server_data['guild_id'])
            ping_content = f"<@&{ping_role_id}>" if ping_role_id else None

            # Get custom color for this server (or default)
            embed_color = self.db.get_embed_color(server_data['guild_id'])
            
            # Create embed notification
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
            message = await channel.send(content=ping_content, embed=embed, view=view)
            logger.info(f"Sent notification for {stream['user_name']} to {channel.guild.name}")
            
            # Bug 1 fix: always save the message ID regardless of auto-delete setting.
            # The flag is checked at delete time in delete_offline_notifications, not here.
            self.db.save_notification_message(
                server_data['guild_id'],
                stream['user_login'],
                effective_channel_id,
                message.id
            )
            
            # Log stream event for leaderboard
            self.db.log_stream_event(server_data['guild_id'], stream['user_login'])

            # Log notification for history
            self.db.log_notification(server_data['guild_id'], stream['user_login'], effective_channel_id, 'sent')

        except discord.Forbidden:
            logger.error(f"Forbidden sending notification in guild {server_data['guild_id']}")
            guild = self.get_guild(server_data['guild_id'])
            if guild:
                await self.alert_permission_issue(
                    guild,
                    effective_channel_id,
                    "Bot was denied permission to send messages (Forbidden error)."
                )
            await self.log_to_channel(
                "🚫", "Notification Blocked — Missing Permissions",
                f"**Guild:** `{server_data['guild_id']}`\n"
                f"**Streamer:** {stream['user_name']}\n"
                f"**Channel:** <#{effective_channel_id}>\n\n"
                f"Bot was denied permission to send the live notification.",
                color=0xFF4444
            )
            try:
                self.db.log_notification(server_data['guild_id'], stream['user_login'], server_data.get('custom_channel_id') or server_data.get('channel_id', 0), 'failed')
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Error sending notification: {e}", exc_info=True)
            await self.log_to_channel(
                "❌", "Notification Failed",
                f"**Guild:** `{server_data['guild_id']}`\n"
                f"**Streamer:** {stream.get('user_name', 'unknown')}\n"
                f"`{type(e).__name__}: {str(e)[:300]}`",
                color=0xFF4444
            )
            try:
                self.db.log_notification(server_data['guild_id'], stream['user_login'], server_data.get('custom_channel_id') or server_data.get('channel_id', 0), 'failed')
            except Exception:
                pass
    
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
            await self.log_to_channel(
                "❌", "Auto-Delete Error",
                f"**Streamer:** {streamer_name}\n`{type(e).__name__}: {str(e)[:300]}`",
                color=0xFF4444
            )
    
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

    async def log_to_channel(self, emoji: str, title: str, description: str, color: int = 0x9146FF):
        """Send a log message to the hardcoded log channel."""
        if not LOG_CHANNEL_ID:
            return
        try:
            channel = self.get_channel(LOG_CHANNEL_ID)
            if not channel:
                channel = await self.fetch_channel(LOG_CHANNEL_ID)
            if not channel:
                return
            embed = discord.Embed(
                title=f"{emoji} {title}",
                description=description,
                color=color,
                timestamp=datetime.utcnow()
            )
            embed.set_footer(text="ExcelProtocol Log")
            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"Failed to send to log channel: {e}")
    
    @tasks.loop(hours=1)
    async def cleanup_channels(self):
        """Periodically clean up configured channels"""
        try:
            configs = self.db.get_all_cleanup_configs()
            
            if not configs:
                logger.debug("No cleanup configs to process")
                return
            
            logger.debug(f"Running cleanup for {len(configs)} channel(s)...")
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
            logger.debug(f"Cleanup complete: {total_deleted} messages deleted")
        
        except Exception as e:
            logger.error(f"Error in cleanup loop: {e}", exc_info=True)
            
            # Send alert for cleanup failures
            await self.send_owner_alert(
                "Cleanup Failed",
                f"**Error in channel cleanup loop!**\n\n"
                f"Error: `{str(e)[:200]}`\n\n"
                f"Automatic message cleanup may not be working."
            )
            await self.log_to_channel(
                "❌", "Channel Cleanup Error",
                f"**Error in cleanup loop**\n`{type(e).__name__}: {str(e)[:300]}`",
                color=0xFF4444
            )
    
    @cleanup_channels.before_loop
    async def before_cleanup_channels(self):
        """Wait until bot is ready before starting cleanup loop"""
        await self.wait_until_ready()

    @tasks.loop(hours=24)
    async def monthly_leaderboard_cleanup(self):
        """Check daily if it is the first of the month and clean old stream events"""
        try:
            now = datetime.utcnow()
            if now.day == 1:
                deleted = self.db.cleanup_stream_events()
                logger.info(f"Monthly leaderboard reset: deleted {deleted} old stream events")
            # Trim notification log daily (keep 30 days)
            self.db.trim_notification_log(days=30)
        except Exception as e:
            logger.error(f"Error in monthly leaderboard cleanup: {e}", exc_info=True)

    @monthly_leaderboard_cleanup.before_loop
    async def before_monthly_leaderboard_cleanup(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=20)
    async def rotate_status(self):
        """Rotate bot status messages"""
        statuses = [
            discord.Activity(
                type=discord.ActivityType.watching,
                name="Dashboard available now!"
            ),
            discord.Activity(
                type=discord.ActivityType.listening,
                name="Twitch stream alerts 📡"
            ),
            discord.Game(name="across multiple servers 🎮"),
        ]
        current = self.rotate_status.current_loop % len(statuses)
        await self.change_presence(activity=statuses[current])

    @rotate_status.before_loop
    async def before_rotate_status(self):
        await self.wait_until_ready()

    @tasks.loop(hours=3)
    async def refresh_broadcaster_tokens(self):
        """Proactively refresh all broadcaster OAuth tokens every 3 hours."""
        import aiohttp
        from datetime import timezone
        tokens = self.db.get_all_broadcaster_tokens()
        if not tokens:
            return
        refreshed = 0
        for t in tokens:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        "https://id.twitch.tv/oauth2/token",
                        data={
                            "grant_type": "refresh_token",
                            "refresh_token": t["refresh_token"],
                            "client_id": self.twitch.client_id,
                            "client_secret": self.twitch.client_secret,
                        }
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            from datetime import datetime, timedelta
                            expires_at = (datetime.utcnow() + timedelta(seconds=data["expires_in"])).isoformat()
                            self.db.set_broadcaster_token(
                                t["guild_id"], t["twitch_user_id"], t["twitch_login"],
                                data["access_token"], data.get("refresh_token", t["refresh_token"]), expires_at
                            )
                            refreshed += 1
                        else:
                            logger.warning(f"Failed to refresh broadcaster token for guild {t['guild_id']}: {resp.status}")
                            await self.log_to_channel(
                                "🔑", "Broadcaster Token Refresh Failed",
                                f"Failed to refresh Twitch token for guild `{t['guild_id']}` "
                                f"(Twitch: **{t['twitch_login']}**)\n"
                                f"HTTP status: `{resp.status}`\n"
                                f"Their channel rewards overlay may stop working until they reconnect.",
                                color=0xFF6B35
                            )
            except Exception as e:
                logger.error(f"Error refreshing broadcaster token for guild {t['guild_id']}: {e}")
                await self.log_to_channel(
                    "🔑", "Broadcaster Token Refresh Error",
                    f"Exception refreshing token for guild `{t['guild_id']}` "
                    f"(Twitch: **{t['twitch_login']}**)\n`{type(e).__name__}: {e}`",
                    color=0xFF6B35
                )
        if refreshed:
            logger.info(f"Refreshed {refreshed} broadcaster token(s)")

    @refresh_broadcaster_tokens.before_loop
    async def before_refresh_broadcaster_tokens(self):
        await self.wait_until_ready()

    # ── Permission Check Loop ─────────────────────────────────────────────────

    REQUIRED_PERMS = {
        'view_channel':    'View Channel',
        'send_messages':   'Send Messages',
        'embed_links':     'Embed Links',
        'manage_messages': 'Manage Messages',
    }
    # Checked once at guild level, not per-channel
    GUILD_PERMS = {
        'manage_roles': 'Manage Roles (server-wide)',
    }

    @tasks.loop(minutes=10)
    async def check_permissions(self):
        """Periodically check bot permissions in all configured notification channels."""
        try:
            for guild in self.guilds:
                await self._check_guild_permissions(guild)
        except Exception as e:
            logger.error(f"Error in permission check loop: {e}", exc_info=True)
            await self.log_to_channel(
                "❌", "Permission Check Error",
                f"**Error in permission check loop**\n`{type(e).__name__}: {str(e)[:300]}`",
                color=0xFF4444
            )

    @check_permissions.before_loop
    async def before_check_permissions(self):
        await self.wait_until_ready()

    async def _check_guild_permissions(self, guild):
        """Check permissions for every notification channel in a guild and update the DB."""
        try:
            streamers = self.db.get_server_streamers(guild.id)
            if not streamers:
                return

            # Check guild-level permissions once (e.g. manage_roles)
            guild_perms = guild.me.guild_permissions
            guild_missing = [
                label for attr, label in self.GUILD_PERMS.items()
                if not getattr(guild_perms, attr, True)
            ]

            # Collect all unique channel IDs that need checking
            channel_ids = set()
            for s in streamers:
                channel_ids.add(s.get('custom_channel_id') or s['channel_id'])

            for channel_id in channel_ids:
                channel = self.get_channel(channel_id)
                if not channel:
                    continue
                perms = channel.permissions_for(guild.me)
                missing = [
                    label for attr, label in self.REQUIRED_PERMS.items()
                    if not getattr(perms, attr, True)
                ] + guild_missing

                if missing:
                    self.db.upsert_permission_issue(guild.id, channel_id, missing)
                    logger.warning(f"Permission issues in #{channel.name} ({guild.name}): {missing}")
                else:
                    self.db.clear_permission_issue(guild.id, channel_id)
        except Exception as e:
            logger.error(f"Error checking permissions for guild {guild.id}: {e}")

    # ── Stat Channel Update Loop ──────────────────────────────────────────────

    @tasks.loop(minutes=15)
    async def update_stat_channels(self):
        """Update voice channel names with live member counts every 15 minutes."""
        try:
            configs = self.db.get_all_stat_channels()
            if not configs:
                return
            for cfg in configs:
                try:
                    guild = self.get_guild(cfg['guild_id'])
                    if not guild:
                        continue
                    channel = guild.get_channel(cfg['channel_id'])
                    if not channel:
                        continue
                    count = guild.member_count
                    new_name = cfg['format'].replace('{count}', f'{count:,}')
                    # Only update if the name actually changed to avoid wasting the rate limit
                    if channel.name != new_name:
                        await channel.edit(name=new_name, reason="ExcelProtocol stat update")
                        self.db.update_stat_channel_timestamp(cfg['guild_id'], cfg['channel_id'])
                        logger.info(f"Updated stat channel '{new_name}' in {guild.name}")
                except discord.Forbidden:
                    logger.warning(f"Missing permission to edit stat channel {cfg['channel_id']} in guild {cfg['guild_id']}")
                    await self.log_to_channel(
                        "🚫", "Stat Channel Update Blocked",
                        f"**Guild:** `{cfg['guild_id']}`\n**Channel:** <#{cfg['channel_id']}>\nMissing permission to edit channel name.",
                        color=0xFF6B35
                    )
                except Exception as e:
                    logger.error(f"Error updating stat channel {cfg['channel_id']}: {e}")
                    await self.log_to_channel(
                        "❌", "Stat Channel Update Error",
                        f"**Guild:** `{cfg['guild_id']}`\n**Channel:** <#{cfg['channel_id']}>\n`{type(e).__name__}: {str(e)[:300]}`",
                        color=0xFF4444
                    )
        except Exception as e:
            logger.error(f"Error in update_stat_channels loop: {e}", exc_info=True)

    @update_stat_channels.before_loop
    async def before_update_stat_channels(self):
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
            now = discord.utils.utcnow()
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

def sanitise_streamer_name(raw: str) -> str:
    """Strip URLs and whitespace from a streamer input, returning just the username.
    Handles inputs like 'https://twitch.tv/username', 'twitch.tv/username', '@username'."""
    name = raw.strip()
    # Strip full URL forms
    for prefix in ("https://www.twitch.tv/", "http://www.twitch.tv/",
                   "https://twitch.tv/", "http://twitch.tv/", "twitch.tv/"):
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
            break
    # Strip leading @ 
    name = name.lstrip("@")
    # Strip any trailing slashes or query strings
    name = name.split("/")[0].split("?")[0].strip()
    return name.lower()

# Slash Commands

@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="addstreamer", description="Add a Twitch streamer to monitor")
@app_commands.describe(
    streamer="Twitch username to monitor",
    channel="Optional: post notifications to this channel instead of the default"
)
async def add_streamer(interaction: discord.Interaction, streamer: str, channel: discord.TextChannel = None):
    """Add a streamer to monitor in this server"""
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ You need 'Manage Server' permission to use this command.",
            ephemeral=True
        )
        return

    # Determine which channel to use
    if channel:
        channel_id = channel.id
    else:
        channel_id = bot.db.get_notification_channel(interaction.guild_id)
        if not channel_id:
            channel_id = interaction.channel_id
            bot.db.set_notification_channel(interaction.guild_id, channel_id)

    # Check streamer limit
    limit = bot.db.get_streamer_limit(interaction.guild_id)
    count = bot.db.get_streamer_count(interaction.guild_id)
    if count >= limit:
        await interaction.response.send_message(
            f"❌ This server has reached its streamer limit ({count}/{limit}). Contact the bot owner to increase your limit.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    streamer = sanitise_streamer_name(streamer)
    user_info = await bot.twitch.get_user(streamer)

    if not user_info:
        await interaction.followup.send(
            f"❌ Twitch user '{streamer}' not found. Please check the spelling.",
            ephemeral=True
        )
        return

    success = bot.db.add_streamer(interaction.guild_id, user_info['login'], channel_id, custom_channel_id=channel.id if channel else None)

    if success:
        custom = " (custom channel)" if channel else ""
        await interaction.followup.send(
            f"✅ Now monitoring **{user_info['display_name']}** (twitch.tv/{user_info['login']})\nNotifications will be sent to <#{channel_id}>{custom}",
            ephemeral=True
        )
        await bot.log_to_channel(
            "➕", "Streamer Added",
            f"**{user_info['display_name']}** added in **{interaction.guild.name}**\n"
            f"Channel: <#{channel_id}>{custom}\nBy: {interaction.user} (`{interaction.user.id}`)"
        )
        # Register EventSub subscriptions for this streamer if not already done
        asyncio.create_task(bot._register_eventsub_for_user(user_info['id'], user_info['login']))
    else:
        await interaction.followup.send(
            f"ℹ️ Already monitoring **{user_info['display_name']}** in this server.",
            ephemeral=True
        )


@app_commands.default_permissions(manage_guild=True)
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
        await bot.log_to_channel(
            "➖", "Streamer Removed",
            f"**{streamer}** removed in **{interaction.guild.name}**\nBy: {interaction.user} (`{interaction.user.id}`)",
            color=0xFF6600
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
    streamer_links = []
    for s in streamers:
        line = f"• [{s['streamer_name']}](https://twitch.tv/{s['streamer_name']})"
        if s.get('custom_channel_id'):
            line += f" → <#{s['custom_channel_id']}>"
        streamer_links.append(line)
    
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
        color=bot.db.get_embed_color(interaction.guild_id)
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
        color=bot.db.get_embed_color(interaction.guild_id),
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
    
    embed.set_footer(text="ExcelProtocol")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


@app_commands.default_permissions(manage_guild=True)
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
        color=bot.db.get_embed_color(interaction.guild_id),  # Use server custom color
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


@app_commands.default_permissions(manage_guild=True)
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


@app_commands.default_permissions(manage_guild=True)
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
    
    await bot.log_to_channel(
        "🎨", "Embed Color Changed",
        f"**Server:** {interaction.guild.name}\n**New Color:** `#{color.upper()}`\nBy: {interaction.user} (`{interaction.user.id}`)"
    )
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


@app_commands.default_permissions(manage_guild=True)
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

    await bot.log_to_channel(
        "🎨", "Embed Color Reset",
        f"**Server:** {interaction.guild.name} reset to default purple\nBy: {interaction.user} (`{interaction.user.id}`)"
    )
    
    embed = discord.Embed(
        title="✅ Color Reset",
        description="Stream notifications will now use the default Twitch purple.",
        color=0x9146FF
    )
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


@app_commands.default_permissions(manage_guild=True)
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

    await bot.log_to_channel(
        "🗑️" if enabled else "📌", f"Auto-Delete {'Enabled' if enabled else 'Disabled'}",
        f"**Server:** {interaction.guild.name}\nBy: {interaction.user} (`{interaction.user.id}`)"
    )
    
    # Create response embed
    embed = discord.Embed(
        title="🗑️ Auto-Delete Enabled" if enabled else "📌 Auto-Delete Disabled",
        description=(
            "Notifications will be **automatically deleted** when streams end." if enabled else
            "Notifications will **stay in the channel** after streams end."
        ),
        color=bot.db.get_embed_color(interaction.guild_id)
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


@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="milestonetoggle", description="Toggle milestone notifications at 5 and 10 hours of streaming")
@app_commands.describe(enabled="Enable or disable milestone notifications")
async def milestone_toggle(interaction: discord.Interaction, enabled: bool):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ You need 'Manage Server' permission.", ephemeral=True)
        return

    bot.db.set_milestone_notifications(interaction.guild_id, enabled)

    embed = discord.Embed(
        title="⏱️ Milestone Notifications Enabled" if enabled else "⏱️ Milestone Notifications Disabled",
        description=(
            "The bot will send a notification when a streamer hits **5 hours** and **10 hours** live."
            if enabled else
            "No milestone notifications will be sent."
        ),
        color=bot.db.get_embed_color(interaction.guild_id)
    )
    if enabled:
        embed.add_field(
            name="Milestones",
            value=(
                "⏱️ **5 hours** — *They're not stopping anytime soon!*\n"
                "💀 **10 hours** — *Send help. 👀*"
            ),
            inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)
    await bot.log_to_channel(
        "⏱️", f"Milestone Notifications {'Enabled' if enabled else 'Disabled'}",
        f"**Server:** {interaction.guild.name}\nBy: {interaction.user} (`{interaction.user.id}`)"
    )


@app_commands.default_permissions(manage_guild=True)
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


@app_commands.default_permissions(manage_guild=True)
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


@app_commands.default_permissions(manage_guild=True)
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


@app_commands.default_permissions(administrator=True)
@bot.tree.command(name="tip", description="Support ExcelProtocol's development")
async def tip(interaction: discord.Interaction):
    embed = discord.Embed(
        title="☕ Support ExcelProtocol",
        description=(
            "If you're enjoying ExcelProtocol, consider leaving a tip!\n\n"
            "Every contribution helps keep the bot running and supports future development. "
            "It means a lot! 💜"
        ),
        color=0xFF5E5B
    )
    embed.set_footer(text="Thank you for your support! — stayexcellent")

    view = discord.ui.View()
    view.add_item(discord.ui.Button(
        label="Support on Ko-fi ☕",
        url="https://ko-fi.com/stayexcellent",
        style=discord.ButtonStyle.link
    ))

    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="help", description="Learn how to use ExcelProtocol")
async def help_command(interaction: discord.Interaction):
    embed_color = bot.db.get_embed_color(interaction.guild_id)

    pages = [
        discord.Embed(
            title="📖 ExcelProtocol — Getting Started",
            description=(
                "Welcome to ExcelProtocol! Here's a quick overview of what the bot can do.\n\n"
                "Use the buttons below to navigate through the setup guide.\n\n"
                "**Features:**\n"
                "📺 Twitch stream notifications\n"
                "🎂 Birthday announcements\n"
                "🔘 Reaction roles\n"
                "🟣 Twitch chat bot with custom commands\n"
                "🏆 Stream leaderboards\n"
                "🗑️ Auto channel cleanup"
            ),
            color=embed_color
        ),
        discord.Embed(
            title="📺 Stream Notifications — Setup",
            description=(
                "**Step 1 — Set your notification channel:**\n"
                "`/setchannel` → Select **Stream Notifications** → Pick a channel\n\n"
                "**Step 2 — Add streamers to monitor:**\n"
                "`/addstreamer streamer:ninja`\n\n"
                "Optionally post to a specific channel per streamer:\n"
                "`/addstreamer streamer:ninja channel:#ninja-notifs`\n\n"
                "**Other commands:**\n"
                "`/streamers` — List monitored streamers\n"
                "`/removestreamer streamer:ninja` — Stop monitoring\n"
                "`/live` — See who is live right now\n"
                "`/repostlive` — Re-send notifications for live streamers"
            ),
            color=embed_color
        ),
        discord.Embed(
            title="🎨 Notification Settings",
            description=(
                "**Change embed color:**\n"
                "`/setcolor color:9146FF` — Any 6-digit hex code\n"
                "`/resetcolor` — Reset to default Twitch purple\n\n"
                "**Auto-delete when stream ends:**\n"
                "`/autodelete enabled:True`\n"
                "Notifications are removed automatically when the streamer goes offline.\n\n"
                "**Milestone notifications:**\n"
                "`/milestonetoggle enabled:True`\n"
                "Sends a notification at 5 hours and 10 hours of streaming."
            ),
            color=embed_color
        ),
        discord.Embed(
            title="🔘 Reaction Roles — Setup",
            description=(
                "**Create a panel:**\n"
                "`/rr create` — Opens a setup popup\n\n"
                "**Add roles to your panel:**\n"
                "`/rr addrole label:Gaming role:@Gamer emoji:🎮`\n\n"
                "**Post the panel:**\n"
                "`/rr publish` — Sends the embed to the current channel\n\n"
                "**Manage panels:**\n"
                "`/rr edit message_id:123456` — Edit an existing panel\n"
                "`/rr delete message_id:123456` — Delete a panel\n"
                "`/rr sort message_id:123456` — Sort roles alphabetically\n"
                "`/rr list` — See all panels in this server\n"
                "`/rr cancel` — Cancel your current session"
            ),
            color=embed_color
        ),
        discord.Embed(
            title="🟣 Twitch Chat Bot — Setup",
            description=(
                "**Link your Twitch channel:**\n"
                "`/twitchset channel:yourchannel`\n\n"
                "**Add or edit a custom command:**\n"
                "`/cmd` — Opens a dropdown to pick an existing command or create new\n\n"
                "**Remove a command:**\n"
                "`/cmdremove` — Dropdown to pick which command to delete\n\n"
                "**View commands:**\n"
                "`/cmdlist` — List all custom commands\n"
                "`/twitchstatus` — Show linked channel info\n\n"
                "**Response variables:** `$user` `$game` `$uptime` `$viewers` `$count` `$channel`"
            ),
            color=embed_color
        ),
        discord.Embed(
            title="🟣 Built-in Twitch Chat Commands",
            description=(
                "These commands are always active in your Twitch chat once linked:\n\n"
                "`!commands` — Lists all active commands\n"
                "`!uptime` — How long the stream has been live\n"
                "`!game` — Current game\n"
                "`!title` — Stream title\n"
                "`!viewers` — Current viewer count\n"
                "`!so @username` — Shoutout another streamer *(mods only)*"
            ),
            color=embed_color
        ),
        discord.Embed(
            title="🎂 Birthdays — Setup",
            description=(
                "**Set birthday announcement channel:**\n"
                "`/setchannel` → Select **Birthday Announcements** → Pick a channel\n\n"
                "**Set your birthday:**\n"
                "`/birthday` — Opens a popup to enter your date\n\n"
                "**Mods can manage anyone's birthday:**\n"
                "`/birthday user:@username`\n"
                "`/birthdayremove user:@username`\n\n"
                "**View all birthdays:**\n"
                "`/birthdaylist` *(mods/admins only)*"
            ),
            color=embed_color
        ),
        discord.Embed(
            title="⚙️ Permissions Required",
            description=(
                "ExcelProtocol needs the following permissions to work correctly:\n\n"
                "✅ **Send Messages** — To post notifications\n"
                "✅ **Embed Links** — To send rich embeds\n"
                "✅ **Manage Messages** — For auto-delete and cleanup\n"
                "✅ **Read Message History** — For channel cleanup\n"
                "✅ **Add Reactions** — For reaction roles\n"
                "✅ **View Channels** — To see channels\n\n"
                "Need help? Contact the bot owner: `stayexcellent`"
            ),
            color=embed_color
        ),
    ]

    # Add page numbers to footers
    total = len(pages)
    for i, page in enumerate(pages):
        page.set_footer(text=f"Page {i + 1} of {total} • ExcelProtocol Help")

    class HelpView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.page = 0

        def update_buttons(self):
            self.prev_btn.disabled = self.page == 0
            self.next_btn.disabled = self.page == total - 1

        @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
        async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.page -= 1
            self.update_buttons()
            await interaction.response.edit_message(embed=pages[self.page], view=self)

        @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
        async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.page += 1
            self.update_buttons()
            await interaction.response.edit_message(embed=pages[self.page], view=self)

    view = HelpView()
    view.prev_btn.disabled = True  # Start on page 1, disable back button
    await interaction.response.send_message(embed=pages[0], view=view, ephemeral=True)


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


@app_commands.default_permissions(administrator=True)
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


@app_commands.default_permissions(manage_guild=True)
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
    streamer = sanitise_streamer_name(streamer)
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
        ping_role_id = bot.db.get_ping_role(interaction.guild_id)
        ping_content = f"<@&{ping_role_id}>" if ping_role_id else None
        message = await channel.send(content=ping_content, embed=embed, view=view)

        # Always save message ID (auto-delete checks flag at delete time)
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


@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="repostlive", description="Re-send notifications for all currently live monitored streamers")
async def repost_live(interaction: discord.Interaction):
    """Check all monitored streamers, send notifications for any currently live that haven't been notified yet."""
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ You need 'Manage Server' permission to use this command.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    streamers = bot.db.get_server_streamers(interaction.guild_id)
    if not streamers:
        await interaction.followup.send("📋 No streamers are being monitored in this server.", ephemeral=True)
        return

    streamer_names = [s['streamer_name'] for s in streamers]
    live_streams = await bot.twitch.get_live_streams(streamer_names)

    if not live_streams:
        await interaction.followup.send("📴 None of your monitored streamers are currently live.", ephemeral=True)
        return

    # Get all server streamers as a lookup
    streamer_lookup = {s['streamer_name'].lower(): s for s in streamers}

    sent = []
    skipped = []

    for stream in live_streams:
        streamer_name = stream['user_login'].lower()
        server_data = streamer_lookup.get(streamer_name)
        if not server_data:
            continue

        # Build server_data in the format send_notification expects
        notif_data = {
            'guild_id': interaction.guild_id,
            'streamer_name': streamer_name,
            'channel_id': server_data.get('custom_channel_id') or server_data['channel_id'],
        }

        # Fetch profile image
        user_info = await bot.twitch.get_user(streamer_name)
        if user_info:
            stream['profile_image_url'] = user_info.get('profile_image_url', '')

        await bot.send_notification(notif_data, stream)

        # Also mark as live so polling loop doesn't double notify
        bot.live_streamers.add(streamer_name)
        sent.append(stream['user_name'])

    if sent:
        await interaction.followup.send(
            f"✅ Re-sent notifications for **{len(sent)}** live streamer(s): {', '.join(sent)}",
            ephemeral=True
        )
    else:
        await interaction.followup.send(
            "ℹ️ No notifications were sent — check channel permissions with `/notiflog`.",
            ephemeral=True
        )


@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="notiflog", description="Check notification history for a streamer")
@app_commands.describe(
    streamer="Twitch username to check",
    limit="Number of entries to show (default 10, max 25)"
)
async def notif_log(interaction: discord.Interaction, streamer: str, limit: int = 10):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("❌ You need 'Manage Server' permission.", ephemeral=True)
        return

    limit = max(1, min(limit, 25))
    streamer = streamer.lower().strip().lstrip("@")
    logs = bot.db.get_notification_log(interaction.guild_id, streamer, limit)

    embed_color = bot.db.get_embed_color(interaction.guild_id)
    embed = discord.Embed(title=f"📋 Notification Log — {streamer}", color=embed_color)

    if not logs:
        embed.description = f"No notification history found for **{streamer}** in the last 30 days."
    else:
        lines_out = []
        for entry in logs:
            channel = bot.get_channel(entry['channel_id'])
            channel_str = f"<#{entry['channel_id']}>" if channel else f"`#{entry['channel_id']}`"
            status_emoji = "✅" if entry['status'] == 'sent' else "❌"
            lines_out.append(f"{status_emoji} `{entry['sent_at']}` → {channel_str}")
        embed.description = "\n".join(lines_out)
        embed.set_footer(text=f"Showing last {len(logs)} entries | 30 day retention")

    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="leaderboard", description="Top streamers in this server this month")
async def leaderboard(interaction: discord.Interaction):
    """Show the monthly leaderboard for this server"""
    rows = bot.db.get_server_leaderboard(interaction.guild_id, limit=10)
    
    now = datetime.utcnow()
    month_name = now.strftime("%B %Y")
    
    embed = discord.Embed(
        title=f"🏆 Streamer Leaderboard — {month_name}",
        description=f"Most active streamers tracked in **{interaction.guild.name}** this month",
        color=bot.db.get_embed_color(interaction.guild_id)
    )
    
    if not rows:
        embed.add_field(name="No data yet", value="Stream events will appear here once monitored streamers go live this month.", inline=False)
    else:
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(rows):
            medal = medals[i] if i < 3 else f"{i+1}."
            streams = row["stream_count"]
            name = row["streamer_name"]
            lines.append(f"{medal} [{name}](https://twitch.tv/{name}) — {streams} stream{'s' if streams != 1 else ''}")
        embed.add_field(name="Rankings", value="\n".join(lines), inline=False)
    
    embed.set_footer(text="Resets on the 1st of each month")
    await interaction.response.send_message(embed=embed)



@app_commands.default_permissions(administrator=True)
@bot.tree.command(name="globalleaderboard", description="[Owner only] Top streamers across all servers this month")
async def global_leaderboard(interaction: discord.Interaction):
    """Owner-only global leaderboard across all servers"""
    if interaction.user.id != BOT_OWNER_ID:
        await interaction.response.send_message(
            "❌ This command is restricted to the bot owner.",
            ephemeral=True
        )
        return

    rows = bot.db.get_global_leaderboard(limit=15)
    
    now = datetime.utcnow()
    month_name = now.strftime("%B %Y")
    
    embed = discord.Embed(
        title=f"🌍 Global Leaderboard — {month_name}",
        description="Most active streamers across all servers this month",
        color=0x9146FF
    )
    
    if not rows:
        embed.add_field(name="No data yet", value="No stream events recorded this month yet.", inline=False)
    else:
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(rows):
            medal = medals[i] if i < 3 else f"{i+1}."
            name = row["streamer_name"]
            streams = row["total_streams"]
            servers = row["server_count"]
            lines.append(f"{medal} [{name}](https://twitch.tv/{name}) — {streams} stream{'s' if streams != 1 else ''} across {servers} server{'s' if servers != 1 else ''}")

        # Split into fields if over 1024 char limit
        current_field = []
        current_length = 0
        field_num = 1
        for line in lines:
            line_length = len(line) + 1
            if current_length + line_length > 1000 and current_field:
                embed.add_field(name="Rankings" if field_num == 1 else f"Rankings (continued {field_num})", value="\n".join(current_field), inline=False)
                current_field = [line]
                current_length = line_length
                field_num += 1
            else:
                current_field.append(line)
                current_length += line_length
        if current_field:
            embed.add_field(name="Rankings" if field_num == 1 else f"Rankings (continued {field_num})", value="\n".join(current_field), inline=False)
    
    embed.set_footer(text="Resets on the 1st of each month")
    await interaction.response.send_message(embed=embed, ephemeral=True)



@app_commands.default_permissions(administrator=True)
@bot.tree.command(name="dbstats", description="[Owner only] View database statistics")
async def db_stats(interaction: discord.Interaction):
    """Owner-only: Show a summary of what is stored in the database"""
    if interaction.user.id != BOT_OWNER_ID:
        await interaction.response.send_message(
            "❌ This command is restricted to the bot owner.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    conn = bot.db.get_connection()
    cursor = conn.cursor()

    # Core tables
    cursor.execute("SELECT COUNT(DISTINCT guild_id) FROM server_settings")
    servers_configured = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(DISTINCT guild_id) FROM monitored_streamers")
    servers_with_streamers = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM monitored_streamers")
    total_streamer_rows = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(DISTINCT streamer_name) FROM monitored_streamers")
    unique_streamers = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM notification_messages")
    saved_notif_messages = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM cleanup_configs")
    cleanup_configs = cursor.fetchone()[0]

    # Leaderboard tables
    cursor.execute("SELECT COUNT(*) FROM stream_events WHERE strftime('%Y-%m', went_live_at) = strftime('%Y-%m', 'now')")
    stream_events_this_month = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM global_stream_events WHERE strftime('%Y-%m', went_live_at) = strftime('%Y-%m', 'now')")
    global_events_this_month = cursor.fetchone()[0]

    # Twitch chat bot tables
    cursor.execute("SELECT COUNT(*) FROM twitch_channels")
    twitch_channels = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM twitch_commands")
    twitch_commands = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(DISTINCT twitch_channel) FROM twitch_commands")
    channels_with_commands = cursor.fetchone()[0]

    # Top 5 most monitored streamers
    cursor.execute('''
        SELECT streamer_name, COUNT(DISTINCT guild_id) as server_count
        FROM monitored_streamers
        GROUP BY streamer_name
        ORDER BY server_count DESC
        LIMIT 5
    ''')
    top_streamers = cursor.fetchall()

    conn.close()

    now = datetime.utcnow()
    month_name = now.strftime("%B %Y")

    embed = discord.Embed(
        title="🗄️ Database Stats",
        description=f"Live snapshot of what is stored — {now.strftime('%d %b %Y %H:%M')} UTC",
        color=0x9146FF
    )

    embed.add_field(
        name="📊 Servers",
        value=(
            f"Servers configured: **{servers_configured}**\nServers with streamers: **{servers_with_streamers}**\nTwitch chat channels linked: **{twitch_channels}**"
        ),
        inline=True
    )

    embed.add_field(
        name="📺 Streamers",
        value=(
            f"Total monitoring rows: **{total_streamer_rows}**\nUnique streamers: **{unique_streamers}**\nSaved notif messages: **{saved_notif_messages}**"
        ),
        inline=True
    )

    embed.add_field(
        name="🤖 Twitch Chat Bot",
        value=(
            f"Custom commands total: **{twitch_commands}**\nChannels with commands: **{channels_with_commands}**\nCleanup configs: **{cleanup_configs}**"
        ),
        inline=True
    )

    embed.add_field(
        name=f"🏆 Leaderboard ({month_name})",
        value=(
            f"Server stream events: **{stream_events_this_month}**\nGlobal unique sessions: **{global_events_this_month}**"
        ),
        inline=True
    )

    if top_streamers:
        top_lines = [f"• **{r[0]}** — {r[1]} server{'s' if r[1] != 1 else ''}" for r in top_streamers]
        embed.add_field(
            name="🔥 Most Tracked Streamers",
            value="\n".join(top_lines),
            inline=False
        )

    await interaction.followup.send(embed=embed, ephemeral=True)


# Run the bot + dashboard together
async def main():
    # Start the dashboard HTTP server (reads from the same DB the bot uses)
    try:
        from aiohttp import web
        from dashboard_server import create_dashboard_app
        dashboard_app = create_dashboard_app(bot=bot)
        import logging as _logging
        # Custom access log format — method, path, status, size only
        access_logger = _logging.getLogger("aiohttp.access")
        access_logger.setLevel(_logging.WARNING)
        access_log_format = '%a "%r" %s %b'
        runner = web.AppRunner(dashboard_app, access_log_format=access_log_format, access_log=access_logger)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", 8080)
        await site.start()
        logger.info("Dashboard server started on port 8080")
    except Exception as e:
        # Dashboard failing should never take down the bot
        logger.error(f"Dashboard failed to start: {e} — bot continuing normally")
        # Wait for bot to be ready before logging
        async def _log_dash_fail():
            await bot.wait_until_ready()
            await bot.log_to_channel(
                "🌐", "Dashboard Failed to Start",
                f"`{type(e).__name__}: {e}`\n\nThe web dashboard is unavailable. Bot commands still work normally.",
                color=0xFF4444
            )
        asyncio.create_task(_log_dash_fail())

    # Start the Discord bot (this blocks until the bot stops)
    await bot.start(DISCORD_TOKEN)

async def main_with_error_handling():
    import traceback
    import platform
    import sys
    try:
        await main()
    except (KeyboardInterrupt, SystemExit):
        pass  # Normal shutdown, don't log
    except Exception as e:
        tb = traceback.format_exc()
        # Trim to Discord's 4096 char limit
        tb_trimmed = tb[-3500:] if len(tb) > 3500 else tb
        error_msg = (
            f"**Bot crashed with unhandled exception**\n\n"
            f"**Error:** `{type(e).__name__}: {e}`\n\n"
            f"**Python:** {sys.version.split()[0]}\n"
            f"**Platform:** {platform.system()} {platform.release()}\n\n"
            f"```python\n{tb_trimmed}\n```"
        )
        # Try to send to log channel before dying
        try:
            if bot.is_ready():
                await bot.log_to_channel("💥", "Bot Crashed", error_msg, color=0xFF0000)
                await asyncio.sleep(2)  # Give Discord time to send the message
        except Exception as log_err:
            logger.error(f"Failed to log crash to Discord: {log_err}")
        logger.critical(f"Bot crashed: {tb}")
        raise

if __name__ == "__main__":
    asyncio.run(main_with_error_handling())
