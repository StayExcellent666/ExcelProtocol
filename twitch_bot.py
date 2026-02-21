import twitchio
from twitchio.ext import commands
import logging
import asyncio
from datetime import datetime
from database import Database
from twitch_api import TwitchAPI

logger = logging.getLogger(__name__)

COMMAND_LIMIT = 75  # Max custom commands per channel


class TwitchChatBot(commands.Bot):
    def __init__(self, token: str, initial_channels: list, db: Database, twitch_api: TwitchAPI):
        super().__init__(
            token=token,
            prefix="!",
            initial_channels=initial_channels or ["placeholder"]  # twitchio needs at least one
        )
        self.db = db
        self.twitch_api = twitch_api

        # Cooldown tracking: {channel: {command_name: last_used_timestamp}}
        self._cooldowns: dict[str, dict[str, datetime]] = {}

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def event_ready(self):
        logger.info(f"Twitch chat bot ready | Nick: {self.nick}")

        # Join any channels already registered in the DB that we didn't join at startup
        registered = self.db.get_all_twitch_channels()
        for row in registered:
            channel_name = row["twitch_channel"]
            if channel_name not in [c.name for c in self.connected_channels]:
                try:
                    await self.join_channels([channel_name])
                    logger.info(f"Joined Twitch channel: {channel_name}")
                except Exception as e:
                    logger.error(f"Failed to join {channel_name}: {e}")

    async def event_message(self, message):
        """Handle incoming chat messages"""
        # Ignore messages sent by the bot itself
        if message.echo:
            return

        if not message.content.startswith("!"):
            return

        # Split into command name and args
        parts = message.content.strip().split(maxsplit=1)
        command_name = parts[0].lower()  # e.g. "!lurk"
        args = parts[1] if len(parts) > 1 else ""

        channel_name = message.channel.name.lower()

        # Try built-in commands first
        handled = await self._handle_builtin(message, command_name, args, channel_name)
        if handled:
            return

        # Try custom commands from DB
        await self._handle_custom(message, command_name, args, channel_name)

    # ------------------------------------------------------------------
    # Built-in commands
    # ------------------------------------------------------------------

    async def _handle_builtin(self, message, command_name: str, args: str, channel_name: str) -> bool:
        """
        Handle built-in commands. Returns True if command was handled.
        """
        is_mod = (
            message.author.is_mod
            or message.author.name.lower() == channel_name  # broadcaster
        )

        # !so @USER or !so USER
        if command_name == "!so":
            if not is_mod:
                return True  # silently ignore non-mods
            target = args.lstrip("@").strip().lower()
            if not target:
                await message.channel.send(f"Usage: !so @username")
                return True
            await self._do_shoutout(message.channel, target)
            return True

        # !uptime
        if command_name == "!uptime":
            if not await self._check_cooldown(channel_name, "!uptime", 30):
                return True
            uptime = await self.twitch_api.get_stream_uptime(channel_name)
            if uptime:
                await message.channel.send(f"⏱️ {channel_name} has been live for {uptime}!")
            else:
                await message.channel.send(f"{channel_name} is not currently live.")
            return True

        # !game
        if command_name == "!game":
            if not await self._check_cooldown(channel_name, "!game", 30):
                return True
            user = await self.twitch_api.get_user(channel_name)
            if user:
                info = await self.twitch_api.get_channel_info(user["id"])
                game = info.get("game_name", "Unknown") if info else "Unknown"
                await message.channel.send(f"🎮 Currently playing: {game}")
            return True

        # !title
        if command_name == "!title":
            if not await self._check_cooldown(channel_name, "!title", 30):
                return True
            user = await self.twitch_api.get_user(channel_name)
            if user:
                info = await self.twitch_api.get_channel_info(user["id"])
                title = info.get("title", "No title set") if info else "No title set"
                await message.channel.send(f"📋 {title}")
            return True

        # !viewers
        if command_name == "!viewers":
            if not await self._check_cooldown(channel_name, "!viewers", 60):
                return True
            count = await self.twitch_api.get_viewer_count(channel_name)
            if count is not None:
                await message.channel.send(f"👀 Current viewers: {count:,}")
            else:
                await message.channel.send(f"{channel_name} is not currently live.")
            return True

        # !commands
        if command_name == "!commands":
            if not await self._check_cooldown(channel_name, "!commands", 60):
                return True
            custom_cmds = self.db.get_twitch_commands(channel_name)
            builtin = "!uptime !game !title !viewers !so !commands"
            if custom_cmds:
                names = " ".join(c["command_name"] for c in custom_cmds)
                await message.channel.send(f"📋 Commands: {builtin} | Custom: {names}")
            else:
                await message.channel.send(f"📋 Commands: {builtin}")
            return True

        return False  # not a built-in command

    async def _do_shoutout(self, channel, target_login: str):
        """Send a rich shoutout message for a streamer"""
        try:
            info = await self.twitch_api.get_last_stream_info(target_login)
            if not info:
                await channel.send(f"❌ Could not find Twitch user: {target_login}")
                return

            display_name = info["user"].get("display_name", target_login)
            game = info.get("game_name") or "something awesome"
            last_date = info.get("last_streamed_at")

            # Format last stream date
            if last_date:
                try:
                    dt = datetime.strptime(last_date, "%Y-%m-%dT%H:%M:%SZ")
                    date_str = dt.strftime("%b %d")
                except Exception:
                    date_str = None
            else:
                date_str = None

            if date_str:
                msg = (
                    f"🎉 Show some love to {display_name}! "
                    f"They were last seen streaming {game} on {date_str}. "
                    f"Give them a follow → twitch.tv/{target_login}"
                )
            else:
                msg = (
                    f"🎉 Show some love to {display_name}! "
                    f"They stream {game}. "
                    f"Give them a follow → twitch.tv/{target_login}"
                )

            await channel.send(msg)

        except Exception as e:
            logger.error(f"Error in shoutout for {target_login}: {e}", exc_info=True)
            await channel.send(f"❌ Failed to get shoutout info for {target_login}")

    # ------------------------------------------------------------------
    # Custom commands
    # ------------------------------------------------------------------

    async def _handle_custom(self, message, command_name: str, args: str, channel_name: str):
        """Handle a custom command from the database"""
        cmd = self.db.get_twitch_command(channel_name, command_name)
        if not cmd:
            return

        # Permission check
        permission = cmd.get("permission", "everyone")
        if not self._has_permission(message.author, message.channel.name, permission):
            return  # silently ignore

        # Cooldown check
        cooldown = cmd.get("cooldown_seconds", 0)
        if cooldown > 0:
            if not await self._check_cooldown(channel_name, command_name, cooldown):
                return

        # Update use count
        self.db.increment_command_uses(channel_name, command_name)

        # Build response — replace $variables
        response = self._replace_variables(
            cmd["response"],
            message.author.name,
            channel_name,
            cmd.get("use_count", 0) + 1
        )

        await message.channel.send(response)

    def _has_permission(self, author, channel_name: str, permission: str) -> bool:
        """Check if a user has the required permission level"""
        if permission == "everyone":
            return True
        if permission == "subscriber":
            return author.is_subscriber or author.is_mod or author.name.lower() == channel_name.lower()
        if permission == "mod":
            return author.is_mod or author.name.lower() == channel_name.lower()
        if permission == "broadcaster":
            return author.name.lower() == channel_name.lower()
        return True

    def _replace_variables(self, text: str, username: str, channel: str, count: int) -> str:
        """Replace $variable placeholders in a command response"""
        text = text.replace("$user", username)
        text = text.replace("$channel", channel)
        text = text.replace("$count", str(count))
        # $uptime, $game, $viewers are handled async — skip for now (see note below)
        return text

    async def _check_cooldown(self, channel: str, command: str, seconds: int) -> bool:
        """
        Returns True if command is off cooldown (allowed to run).
        Returns False if still on cooldown (skip it).
        """
        now = datetime.utcnow()
        channel_cooldowns = self._cooldowns.setdefault(channel, {})
        last_used = channel_cooldowns.get(command)

        if last_used:
            elapsed = (now - last_used).total_seconds()
            if elapsed < seconds:
                return False

        channel_cooldowns[command] = now
        return True

    # ------------------------------------------------------------------
    # Channel management (called from Discord bot)
    # ------------------------------------------------------------------

    async def join_channel(self, channel_name: str):
        """Dynamically join a new Twitch channel"""
        channel_name = channel_name.lower()
        if channel_name not in [c.name for c in self.connected_channels]:
            await self.join_channels([channel_name])
            logger.info(f"Dynamically joined Twitch channel: {channel_name}")

    async def leave_channel(self, channel_name: str):
        """Leave a Twitch channel"""
        channel_name = channel_name.lower()
        await self.part_channels([channel_name])
        logger.info(f"Left Twitch channel: {channel_name}")
