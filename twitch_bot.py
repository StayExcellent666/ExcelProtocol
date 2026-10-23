import asyncio
import aiohttp
from datetime import datetime
from twitchio.ext import commands 
import logging
import re
import time
from database import Database
from twitch_api import TwitchAPI
from utils import utcnow, parse_twitch_iso
from config import TWITCH_CLIENT_ID
from twitch_command_variables import render_custom_command

logger = logging.getLogger(__name__)

COMMAND_LIMIT = 75
MASS_SHOUTOUT_EXPLICIT_LIMIT = 10
MASS_SHOUTOUT_ALL_LIMIT = 25
MASS_SHOUTOUT_DELAY_SECONDS = 2.25
MASS_SHOUTOUT_EXCLUDED_LOGINS = {
    "nightbot", "streamelements", "streamlabs", "moobot", "fossabot",
    "sery_bot", "wizebot", "soundalerts", "botrixoficial", "own3d",
}


class TwitchChatBot(commands.Bot):
    def __init__(self, token: str, initial_channels: list, db: Database,
                 twitch_api: TwitchAPI, bot_id: str = None,
                 token_refresher=None, lifecycle_callback=None,
                 channel_live_checker=None):
        super().__init__(
            token=token,
            prefix="!",
            initial_channels=initial_channels or ["#placeholder"],
        )
        self.db = db
        self.twitch_api = twitch_api
        self.bot_id = str(bot_id or "")
        self._user_access_token = self._normalise_user_token(token)
        self._token_refresher = token_refresher
        self._lifecycle_callback = lifecycle_callback
        self._channel_live_checker = channel_live_checker
        self._cooldowns: dict[str, dict[str, datetime]] = {}
        self._broadcaster_ids: dict[str, str] = {}
        self._helix_send_failures = 0
        self._helix_send_disabled_until = 0.0
        self._mass_shoutout_tasks: dict[str, asyncio.Task] = {}
        self._timed_message_task: asyncio.Task | None = None

    def _report_lifecycle(self, event: str, detail: str | None = None):
        callback = self._lifecycle_callback
        if callback is None:
            return
        try:
            callback(event, detail)
        except Exception:
            logger.exception("Twitch chat lifecycle callback failed for %s", event)

    @staticmethod
    def _normalise_user_token(token: str) -> str:
        token = str(token or "").strip()
        return token[6:] if token.lower().startswith("oauth:") else token

    def update_user_token(self, token: str):
        """Publish a rotated user token to every TwitchIO token consumer."""
        token = self._normalise_user_token(token)
        if not token:
            raise ValueError("Twitch user access token cannot be empty")
        self._user_access_token = token
        connection = getattr(self, "_connection", None)
        if connection is not None:
            connection._token = token
        http = getattr(self, "_http", None)
        if http is not None:
            http.token = token

    @staticmethod
    def _is_oauth_failure(reason: str | None) -> bool:
        reason = str(reason or "").lower()
        return "http 401" in reason or "oauth token" in reason or "unauthorized" in reason

    def reset_helix_delivery(self, bot_id: str | None = None):
        """Close the fallback circuit after a successful bot authorization."""
        if bot_id:
            self.bot_id = str(bot_id)
        self._helix_send_failures = 0
        self._helix_send_disabled_until = 0.0

    async def _broadcaster_id_for(self, channel_name: str) -> str | None:
        channel_name = str(channel_name or "").strip().lstrip("#").lower()
        if not channel_name:
            return None
        cached = self._broadcaster_ids.get(channel_name)
        if cached:
            return cached
        user = await self.twitch_api.get_user(channel_name)
        broadcaster_id = str(user.get("id") or "") if user else ""
        if broadcaster_id:
            self._broadcaster_ids[channel_name] = broadcaster_id
            return broadcaster_id
        return None

    async def send_chat(self, channel_name: str, text: str, fallback_channel=None) -> str:
        """Prefer Twitch's badged Helix sender and transparently fall back to IRC.

        The small circuit breaker prevents a Twitch API outage or missing grant
        from adding a timeout to every command response. The next message after
        one minute probes Helix again automatically.
        """
        channel_name = str(channel_name or "").strip().lstrip("#").lower()
        text = str(text or "")[:500]
        if not channel_name or not text:
            raise ValueError("A Twitch channel and message are required")

        loop = asyncio.get_running_loop()
        now = loop.time()
        if self.bot_id and now >= self._helix_send_disabled_until:
            broadcaster_id = await self._broadcaster_id_for(channel_name)
            if broadcaster_id:
                sent, reason = await self.twitch_api.send_chat_message(
                    broadcaster_id, self.bot_id, text,
                )
                if sent:
                    self._helix_send_failures = 0
                    self._helix_send_disabled_until = 0.0
                    return "helix"

                self._helix_send_failures += 1
                logger.warning(
                    "Twitch Helix chat send failed in @%s; using IRC fallback: %s",
                    channel_name, reason,
                )
                if self._helix_send_failures >= 3:
                    self._helix_send_disabled_until = now + 60
                    logger.warning("Twitch Helix chat fallback circuit opened for 60 seconds")

        channel = fallback_channel
        if channel is None:
            channel = next(
                (
                    item for item in (self.connected_channels or [])
                    if str(getattr(item, "name", "") or "").lower() == channel_name
                ),
                None,
            )
        if channel is None:
            raise RuntimeError(f"Twitch chat bot is not joined to @{channel_name}")
        await channel.send(text)
        return "irc"

    async def _send_chat(self, channel, text: str) -> str:
        return await self.send_chat(getattr(channel, "name", ""), text, channel)

    async def _send_announcement(self, channel, text: str,
                                 color: str = "purple") -> bool:
        """Try a highlighted announcement, allowing callers to fall back."""
        channel_name = str(getattr(channel, "name", "") or "").lower()
        try:
            broadcaster_id = await self._broadcaster_id_for(channel_name)
            token = self._user_access_token
            if not broadcaster_id or not self.bot_id or not token:
                return False
            sent, reason = await self.twitch_api.send_chat_announcement(
                broadcaster_id, self.bot_id, token, text, color,
            )
            if not sent and self._is_oauth_failure(reason) and self._token_refresher:
                logger.info(
                    "Twitch rejected the announcement token in @%s; refreshing and retrying once",
                    channel_name,
                )
                refreshed_token = await self._token_refresher()
                self.update_user_token(refreshed_token)
                sent, reason = await self.twitch_api.send_chat_announcement(
                    broadcaster_id, self.bot_id, self._user_access_token,
                    text, color,
                )
        except Exception as exc:
            sent, reason = False, f"{type(exc).__name__}: {exc}"
        if not sent:
            logger.warning(
                "Twitch announcement failed in @%s; using normal chat: %s",
                channel_name, reason,
            )
        return sent

    def _connected_channel_for(self, channel_name: str):
        wanted = str(channel_name or "").strip().lstrip("#").lower()
        return next(
            (
                channel for channel in (self.connected_channels or [])
                if str(getattr(channel, "name", "") or "").lower() == wanted
            ),
            None,
        )

    async def _deliver_timed_message(self, timer: dict) -> bool:
        channel_name = str(timer.get("twitch_channel") or "").strip().lower()
        channel = self._connected_channel_for(channel_name)
        if channel is None:
            return False
        if timer.get("only_while_live"):
            checker = self._channel_live_checker
            if checker is None or not checker(channel_name):
                return False

        text = str(timer.get("message") or "").strip()[:500]
        if not text:
            return False
        if timer.get("delivery_mode") == "announcement":
            if not await self._send_announcement(channel, text, color="purple"):
                await self._send_chat(channel, text)
        else:
            await self._send_chat(channel, text)
        logger.info(
            "Sent Twitch timeline %s in @%s (slot +%sm)",
            timer.get("delivery_mode", "chat"), channel_name,
            (int(timer.get("slot_index", 0)) + 1) * 5,
        )
        return True

    async def _run_timed_messages(self):
        """Evaluate every channel once on the shared five-minute heartbeat."""
        try:
            while True:
                now = time.time()
                next_heartbeat = (int(now // 300) + 1) * 300
                await asyncio.sleep(max(0.1, next_heartbeat - now))
                heartbeat_tick = int(time.time() // 300)
                due = await asyncio.to_thread(
                    self.db.get_twitch_timeline_messages_for_tick,
                    heartbeat_tick,
                )
                for timer in due:
                    try:
                        if await self._deliver_timed_message(timer):
                            await asyncio.sleep(1.2)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "Timed Twitch message %s failed: %s",
                            timer.get("id"), exc,
                        )
        except asyncio.CancelledError:
            raise

    async def close(self):
        task = self._timed_message_task
        self._timed_message_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await super().close()

    def _connected_channel_names(self) -> set[str]:
        """Return usable joined-channel names, ignoring TwitchIO reconnect gaps."""
        names = set()
        for channel in self.connected_channels:
            name = getattr(channel, "name", None)
            if name:
                names.add(str(name).lower())
        return names

    @staticmethod
    def _registered_channel_names(rows) -> set[str]:
        """Normalize and deduplicate the channels that must be restored."""
        names = set()
        for row in rows:
            try:
                name = str(row["twitch_channel"] or "").strip().lstrip("#").lower()
            except (KeyError, TypeError):
                continue
            if name:
                names.add(name)
        return names

    async def event_ready(self):
        logger.info(f"Twitch chat bot ready | Nick: {self.nick}")
        self._report_lifecycle("ready")
        await asyncio.sleep(3)
        connected_names = self._connected_channel_names()
        registered_names = self._registered_channel_names(self.db.get_all_twitch_channels())
        for channel_name in sorted(registered_names):
            if channel_name not in connected_names:
                for attempt in range(3):
                    try:
                        await self.join_channels([channel_name])
                        connected_names.add(channel_name)
                        logger.info(f"Joined Twitch channel: {channel_name}")
                        break
                    except KeyError:
                        # twitchio internal race condition — join likely succeeded, verify
                        await asyncio.sleep(1)
                        if channel_name in self._connected_channel_names():
                            connected_names.add(channel_name)
                            logger.info(f"Joined Twitch channel: {channel_name} (KeyError suppressed)")
                            break
                        elif attempt < 2:
                            await asyncio.sleep(4)
                        else:
                            logger.error(f"Failed to join {channel_name} after 3 attempts")
                    except Exception as e:
                        if attempt < 2:
                            logger.debug(f"Join attempt {attempt+1} failed for {channel_name}, retrying...")
                            await asyncio.sleep(5)
                        else:
                            logger.error(f"Failed to join {channel_name} after 3 attempts: {e}")
                await asyncio.sleep(0.5)
        if self._timed_message_task is None or self._timed_message_task.done():
            self._timed_message_task = asyncio.create_task(
                self._run_timed_messages(), name="twitch-timed-messages",
            )

    async def event_raw_data(self, data: str):
        # Twitch sends periodic PING frames even in quiet channels. Recording
        # all inbound IRC traffic gives the supervisor a reliable liveness
        # signal without generating any additional Twitch requests.
        self._report_lifecycle("activity")

    async def event_reconnect(self):
        self._report_lifecycle("reconnect")
        logger.warning("Twitch requested an IRC reconnect")

    async def event_channel_join_failure(self, channel: str):
        self._report_lifecycle("join_failure", f"Unable to join @{channel}")
        logger.warning("Twitch chat could not join @%s", channel)

    async def event_error(self, error: Exception, data=None):
        detail = f"{type(error).__name__}: {error}"
        self._report_lifecycle("error", detail)
        logger.error(
            "Twitch chat event error: %s", detail,
            exc_info=(type(error), error, error.__traceback__),
        )

    async def event_message(self, message):
        report_lifecycle = getattr(self, "_report_lifecycle", None)
        if report_lifecycle:
            report_lifecycle("activity")
        if message.echo:
            return

        channel_name = str(getattr(message.channel, "name", "") or "").lower()
        author_login = str(getattr(message.author, "name", "") or "").lower()
        # Chat activity sounds are intentionally fire-and-forget: an OBS client
        # or database hiccup must never delay normal chat-command handling.
        # Broadcaster messages are excluded so the streamer does not trigger
        # their own notification while replying to chat.
        if channel_name and author_login != channel_name:
            asyncio.create_task(self._notify_chat_activity(channel_name))

        if not message.content or not message.content.startswith("!"):
            return

        parts = message.content.strip().split(maxsplit=1)
        command_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        # An active ExcelFortuna chat-command giveaway gets first refusal on
        # its configured command. Outside that narrow case, existing built-in
        # and custom command behaviour is unchanged.
        try:
            from dashboard_server import _fortuna_record_chat_entry
            author_login = str(getattr(message.author, "name", "") or "").lower()
            author_id = str(getattr(message.author, "id", "") or author_login)
            display_name = str(
                getattr(message.author, "display_name", "") or author_login
            )
            if await _fortuna_record_chat_entry(
                channel_name, command_name, str(getattr(message, "id", "") or ""),
                author_id, author_login, display_name, message.channel,
            ):
                return
        except Exception as e:
            logger.error(f"Fortuna chat entry failed for {channel_name}: {e}", exc_info=True)

        handled = await self._handle_builtin(message, command_name, args, channel_name)
        if handled:
            return
        await self._handle_custom(message, command_name, args, channel_name)

    async def _notify_chat_activity(self, channel_name: str):
        try:
            from dashboard_server import push_chat_notification_to_overlay
            await push_chat_notification_to_overlay(channel_name)
        except Exception as e:
            logger.debug("Chat notification sound failed for %s: %s", channel_name, e)

    async def _handle_builtin(self, message, command_name: str, args: str, channel_name: str) -> bool:
        is_mod = message.author.is_mod or message.author.name.lower() == channel_name

        configurable = {"!uptime", "!game", "!title", "!viewers", "!so", "!mso", "!commands"}
        enabled_check = getattr(self.db, "is_builtin_command_enabled", None)
        if command_name in configurable and enabled_check and not enabled_check(channel_name, command_name):
            return True

        if command_name == "!so":
            if not is_mod:
                return True
            target = args.lstrip("@").strip().lower()
            if not target:
                await self._send_chat(message.channel, "Usage: !so @username")
                return True
            await self._do_shoutout(message.channel, target)
            return True

        if command_name == "!mso":
            if not is_mod:
                return True
            if not await self._check_cooldown(channel_name, "!mso", 30):
                return True
            await self._start_mass_shoutout(message.channel, args, channel_name)
            return True

        if command_name == "!stop":
            if not is_mod:
                return True
            if not self.db.is_play_enabled(channel_name):
                return True
            try:
                await self._delete_msg(channel_name, message.id)
            except Exception as e:
                logger.debug(f"_delete_msg failed (non-fatal): {e}")
            try:
                from dashboard_server import push_stop_to_overlay
                await push_stop_to_overlay(channel_name)
                await self._send_chat(message.channel, "⏹ 𝗦𝘁𝗼𝗽𝗽𝗲𝗱")
            except Exception as e:
                logger.error(f"!stop overlay push failed for {channel_name}: {e}")
            return True

        if command_name == "!skip":
            if not is_mod:
                return True
            if not self.db.is_play_enabled(channel_name):
                return True
            try:
                await self._delete_msg(channel_name, message.id)
            except Exception as e:
                logger.debug(f"_delete_msg failed (non-fatal): {e}")
            try:
                from dashboard_server import push_skip_to_overlay
                pushed = await push_skip_to_overlay(channel_name)
                if pushed:
                    await self._send_chat(message.channel, "⏭ 𝗦𝗸𝗶𝗽𝗽𝗲𝗱")
                else:
                    await self._send_chat(message.channel, "❌ No OBS overlay connected.")
            except Exception as e:
                logger.error(f"!skip overlay push failed for {channel_name}: {e}")
            return True

        if command_name == "!play":
            if not is_mod:
                return True
            if not self.db.is_play_enabled(channel_name):
                return True
            url = args.strip()
            if not url:
                await self._send_chat(message.channel, "Usage: !play <youtube_url>")
                return True
            try:
                await self._delete_msg(channel_name, message.id)
            except Exception as e:
                logger.debug(f"_delete_msg failed (non-fatal): {e}")
            try:
                from dashboard_server import push_play_to_overlay
                pushed = await push_play_to_overlay(channel_name, url, message.author.name)
                if pushed:
                    await self._send_chat(message.channel, "▶ 𝗤𝘂𝗲𝘂𝗲𝗱 • PogChamp")
                else:
                    await self._send_chat(message.channel, "❌ No OBS overlay connected — make sure the browser source is open.")
            except Exception as e:
                logger.error(f"!play overlay push failed for {channel_name}: {e}")
                await self._send_chat(message.channel, "❌ Could not push to overlay.")
            return True

        if command_name == "!uptime":
            if not await self._check_cooldown(channel_name, "!uptime", 30):
                return True
            uptime = await self.twitch_api.get_stream_uptime(channel_name)
            if uptime:
                await self._send_chat(message.channel, f"⏱️ 𝗨𝗽𝘁𝗶𝗺𝗲 • {uptime}")
            else:
                await self._send_chat(message.channel, f"⏱️ 𝗦𝘁𝗮𝘁𝘂𝘀 • {channel_name} is not currently live.")
            return True

        if command_name == "!game":
            if not await self._check_cooldown(channel_name, "!game", 30):
                return True
            user = await self.twitch_api.get_user(channel_name)
            if user:
                info = await self.twitch_api.get_channel_info(user["id"])
                game = info.get("game_name", "Unknown") if info else "Unknown"
                await self._send_chat(message.channel, f"🎮 𝗖𝗮𝘁𝗲𝗴𝗼𝗿𝘆 • {game}")
            return True

        if command_name == "!title":
            if not await self._check_cooldown(channel_name, "!title", 30):
                return True
            user = await self.twitch_api.get_user(channel_name)
            if user:
                info = await self.twitch_api.get_channel_info(user["id"])
                title = info.get("title", "No title set") if info else "No title set"
                await self._send_chat(message.channel, f"📺 𝗧𝗶𝘁𝗹𝗲 • {title}")
            return True

        if command_name == "!viewers":
            if not await self._check_cooldown(channel_name, "!viewers", 60):
                return True
            count = await self.twitch_api.get_viewer_count(channel_name)
            if count is not None:
                await self._send_chat(message.channel, f"👀 𝗩𝗶𝗲𝘄𝗲𝗿𝘀 • {count:,}")
            else:
                await self._send_chat(message.channel, f"👀 𝗦𝘁𝗮𝘁𝘂𝘀 • {channel_name} is not currently live.")
            return True

        if command_name == "!clip":
            config = self.db.get_clip_config(channel_name)
            if not config:
                return False
            if not await self._check_cooldown(
                channel_name, "!clip", int(config.get("cooldown", 60))
            ):
                return True
            await self._create_clip(message, config)
            return True

        if command_name == "!commands":
            if not await self._check_cooldown(channel_name, "!commands", 60):
                return True
            custom_cmds = self.db.get_twitch_commands(channel_name)
            settings_getter = getattr(self.db, "get_builtin_command_settings", None)
            settings = settings_getter(channel_name) if settings_getter else {
                name: True for name in ("!uptime", "!game", "!title", "!viewers", "!so", "!mso", "!commands")
            }
            builtin = " ".join(name for name, enabled in settings.items() if enabled)
            if self.db.get_clip_config(channel_name):
                builtin = f"{builtin} !clip".strip()
            if self.db.is_play_enabled(channel_name):
                builtin = f"{builtin} !play !stop !skip".strip()
            if custom_cmds:
                names = " ".join(c["command_name"] for c in custom_cmds)
                await self._send_chat(message.channel, f"⌨️ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱𝘀 • {builtin} | 𝗖𝘂𝘀𝘁𝗼𝗺 • {names}")
            else:
                await self._send_chat(message.channel, f"⌨️ 𝗖𝗼𝗺𝗺𝗮𝗻𝗱𝘀 • {builtin}")
            return True

        return False

    async def _create_clip(self, message, config: dict):
        """Create a Twitch clip with the connected broadcaster's OAuth token."""
        duration = max(5, min(60, int(config.get("duration", 45))))
        try:
            session = await self.twitch_api.get_session()
            async with session.post(
                "https://api.twitch.tv/helix/clips",
                headers={
                    "Authorization": f"Bearer {config['access_token']}",
                    "Client-Id": TWITCH_CLIENT_ID,
                },
                params={
                    "broadcaster_id": config["broadcaster_id"],
                    "duration": duration,
                },
            ) as resp:
                payload = await resp.json(content_type=None)
                if resp.status != 202:
                    detail = payload.get("message", f"HTTP {resp.status}") if isinstance(payload, dict) else f"HTTP {resp.status}"
                    logger.warning("!clip failed for %s: %s", message.channel.name, detail)
                    if resp.status in (401, 403):
                        await self._send_chat(message.channel, "❌ Clip permission is missing. A server manager needs to reconnect Twitch in the ExcelProtocol dashboard.")
                    else:
                        await self._send_chat(message.channel, "❌ Twitch could not create that clip right now.")
                    return

            clips = payload.get("data", []) if isinstance(payload, dict) else []
            clip_id = clips[0].get("id") if clips else None
            if not clip_id:
                raise RuntimeError("Twitch returned no clip ID")
            await self._send_chat(
                message.channel,
                f"🎬 𝗖𝗹𝗶𝗽 • @{message.author.name} clipped the last {duration} seconds: "
                f"https://clips.twitch.tv/{clip_id}"
            )
        except Exception as e:
            logger.error("!clip creation failed for %s: %s", message.channel.name, e, exc_info=True)
            await self._send_chat(message.channel, "❌ Twitch could not create that clip right now.")

    @staticmethod
    def _parse_mass_shoutout_targets(args: str) -> list[str]:
        """Normalize a comma/space-separated list of Twitch logins."""
        targets = []
        seen = set()
        for raw in re.split(r"[\s,]+", str(args or "").strip()):
            login = raw.strip().lstrip("@").lower()
            if not login or not re.fullmatch(r"[a-z0-9_]{1,25}", login):
                continue
            if login not in seen:
                seen.add(login)
                targets.append(login)
        return targets

    async def _get_mass_shoutout_chatters(self, channel_name: str) -> list[str]:
        broadcaster_id = await self._broadcaster_id_for(channel_name)
        if not broadcaster_id or not self.bot_id:
            raise RuntimeError("Could not resolve broadcaster or bot identity")

        async def fetch():
            return await self.twitch_api.get_chatters(
                broadcaster_id,
                self.bot_id,
                self._user_access_token,
                max_results=1000,
            )

        try:
            chatters = await fetch()
        except RuntimeError as exc:
            if self._is_oauth_failure(str(exc)) and self._token_refresher:
                refreshed_token = await self._token_refresher()
                self.update_user_token(refreshed_token)
                chatters = await fetch()
            else:
                raise

        try:
            bot_login = str(self.nick or "").lower()
        except (AttributeError, RuntimeError):
            bot_login = ""
        excluded = set(MASS_SHOUTOUT_EXCLUDED_LOGINS)
        excluded.update({channel_name.lower(), bot_login})
        targets = []
        seen = set()
        for chatter in chatters:
            if str(chatter.get("user_id") or "") == str(self.bot_id):
                continue
            login = str(chatter.get("user_login") or "").strip().lower()
            if not login or login in excluded or login in seen:
                continue
            seen.add(login)
            targets.append(login)
        return targets

    async def _start_mass_shoutout(self, channel, args: str, channel_name: str):
        tasks = getattr(self, "_mass_shoutout_tasks", None)
        if tasks is None:
            tasks = self._mass_shoutout_tasks = {}
        active = tasks.get(channel_name)
        if active and not active.done():
            await self._send_chat(channel, "📣 A mass shoutout is already running in this channel.")
            return

        requested = str(args or "").strip()
        if not requested:
            await self._send_chat(channel, "Usage: !mso @name1 @name2, or !mso all")
            return

        all_chatters = requested.lower() == "all"
        if all_chatters:
            try:
                targets = await self._get_mass_shoutout_chatters(channel_name)
            except Exception as exc:
                logger.warning("!mso all could not read chatters in @%s: %s", channel_name, exc)
                await self._send_chat(
                    channel,
                    "❌ !mso all needs Chatters access. Make sure the bot is modded, then reconnect the Twitch Bot from Health Check once.",
                )
                return
            if not targets:
                await self._send_chat(channel, "📣 No eligible chatters found to shout out.")
                return
            total = len(targets)
            targets = targets[:MASS_SHOUTOUT_ALL_LIMIT]
            limit_note = (
                f" • first {len(targets)} of {total} (safety limit)"
                if total > len(targets) else ""
            )
        else:
            targets = self._parse_mass_shoutout_targets(requested)
            if not targets:
                await self._send_chat(channel, "Usage: !mso @name1 @name2, or !mso all")
                return
            if len(targets) > MASS_SHOUTOUT_EXPLICIT_LIMIT:
                await self._send_chat(
                    channel,
                    f"❌ Add at most {MASS_SHOUTOUT_EXPLICIT_LIMIT} names per !mso command.",
                )
                return
            limit_note = ""

        await self._send_chat(
            channel,
            f"📣 𝗠𝗔𝗦𝗦 𝗦𝗛𝗢𝗨𝗧𝗢𝗨𝗧 • Queued {len(targets)} creator(s){limit_note}",
        )
        task = asyncio.create_task(
            self._run_mass_shoutout(channel, channel_name, targets),
            name=f"mass-shoutout-{channel_name}",
        )
        tasks[channel_name] = task

    async def _run_mass_shoutout(self, channel, channel_name: str,
                                 targets: list[str]):
        sent = 0
        failed = 0
        try:
            for index, target in enumerate(targets):
                if await self._do_shoutout(channel, target, report_errors=False):
                    sent += 1
                else:
                    failed += 1
                if index < len(targets) - 1:
                    await asyncio.sleep(MASS_SHOUTOUT_DELAY_SECONDS)
            result = f"✅ 𝗠𝗔𝗦𝗦 𝗦𝗛𝗢𝗨𝗧𝗢𝗨𝗧 • {sent} sent"
            if failed:
                result += f" • {failed} skipped"
            await self._send_chat(channel, result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Mass shoutout failed in @%s: %s", channel_name, exc, exc_info=True)
            try:
                await self._send_chat(channel, "❌ Mass shoutout stopped because Twitch returned an error.")
            except Exception:
                pass
        finally:
            tasks = getattr(self, "_mass_shoutout_tasks", {})
            if tasks.get(channel_name) is asyncio.current_task():
                tasks.pop(channel_name, None)

    async def _do_shoutout(self, channel, target_login: str,
                           report_errors: bool = True) -> bool:
        try:
            info = await self.twitch_api.get_last_stream_info(target_login)
            if not info:
                if report_errors:
                    await self._send_chat(channel, f"Could not find Twitch user: {target_login}")
                return False

            display_name = info["user"].get("display_name", target_login)
            game = info.get("game_name") or "something awesome"
            last_date = info.get("last_streamed_at")

            if last_date:
                try:
                    dt = parse_twitch_iso(last_date)
                    date_str = dt.strftime("%b %d")
                except Exception:
                    date_str = None
            else:
                date_str = None

            if date_str:
                msg = (
                    f"📣 𝗦𝗛𝗢𝗨𝗧𝗢𝗨𝗧 • Go show @{display_name} some love! "
                    f"🎮 {game} • Last live {date_str} • twitch.tv/{target_login}"
                )
            else:
                msg = (
                    f"📣 𝗦𝗛𝗢𝗨𝗧𝗢𝗨𝗧 • Go show @{display_name} some love! "
                    f"🎮 {game} • twitch.tv/{target_login}"
                )

            announced = await self._send_announcement(channel, msg, color="purple")
            if not announced:
                await self._send_chat(channel, msg)
            return True

        except Exception as e:
            logger.error(f"Error in shoutout for {target_login}: {e}", exc_info=True)
            if report_errors:
                await self._send_chat(channel, f"Failed to get shoutout info for {target_login}")
            return False

    async def _handle_custom(self, message, command_name: str, args: str, channel_name: str):
        cmd = self.db.get_twitch_command(channel_name, command_name)
        if not cmd:
            return

        permission = cmd.get("permission", "everyone")
        if not self._has_permission(message.author, channel_name, permission):
            return

        cooldown = cmd.get("cooldown_seconds", 0)
        if cooldown > 0:
            if not await self._check_cooldown(channel_name, command_name, cooldown):
                return

        count = self.db.increment_command_uses(channel_name, command_name)
        if count is None:
            count = cmd.get("use_count", 0) + 1
        username = str(getattr(message.author, "name", "") or "viewer")
        display_name = str(
            getattr(message.author, "display_name", "") or username
        )
        user_key = str(getattr(message.author, "id", "") or username).lower()
        user_counter = getattr(self.db, "increment_command_user_uses", None)
        user_count = (
            user_counter(channel_name, command_name, user_key, username)
            if user_counter else 1
        )
        response = self._replace_variables(
            cmd["response"],
            username,
            channel_name,
            count,
            args=args,
            display_name=display_name,
            user_count=user_count,
            command_name=command_name,
        )
        if response:
            await self._send_chat(message.channel, response)

    def _has_permission(self, author, channel_name: str, permission: str) -> bool:
        if permission == "everyone":
            return True
        if permission == "subscriber":
            return author.is_subscriber or author.is_mod or author.name.lower() == channel_name.lower()
        if permission == "mod":
            return author.is_mod or author.name.lower() == channel_name.lower()
        if permission == "broadcaster":
            return author.name.lower() == channel_name.lower()
        return True

    def _replace_variables(self, text: str, username: str, channel: str, count: int,
                           args: str = "", display_name: str = None,
                           user_count: int = 1, command_name: str = "") -> str:
        return render_custom_command(
            text, username, channel, count,
            args=args, display_name=display_name,
            user_count=user_count, command_name=command_name,
        )

    async def _check_cooldown(self, channel: str, command: str, seconds: int) -> bool:
        now = utcnow()
        channel_cooldowns = self._cooldowns.setdefault(channel, {})
        last_used = channel_cooldowns.get(command)
        if last_used and (now - last_used).total_seconds() < seconds:
            return False
        channel_cooldowns[command] = now
        return True


    async def _delete_msg(self, channel_name: str, message_id: str):
        """Delete a chat message using the broadcaster's OAuth token — broadcaster can delete any message."""
        try:
            # Look up guild_id from channel name
            all_channels = self.db.get_all_twitch_channels()
            guild_id = next((ch["guild_id"] for ch in all_channels
                             if ch["twitch_channel"].lower() == channel_name.lower()), None)
            if not guild_id:
                return

            # Get broadcaster token — has full delete permissions in own channel
            row = self.db.get_broadcaster_token(guild_id)
            if not row:
                return

            access_token   = row["access_token"]
            broadcaster_id = row["twitch_user_id"]

            async with aiohttp.ClientSession() as session:
                async with session.delete(
                    "https://api.twitch.tv/helix/moderation/chat",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Client-Id": TWITCH_CLIENT_ID,
                    },
                    params={
                        "broadcaster_id": broadcaster_id,
                        "moderator_id":   broadcaster_id,  # broadcaster is their own moderator
                        "message_id":     message_id,
                    }
                ) as resp:
                    if resp.status not in (200, 204):
                        text = await resp.text()
                        logger.error(f"Delete FAILED {resp.status}: {text}")
                    else:
                        logger.debug(f"Delete OK: {message_id}")
        except Exception as e:
            logger.error(f"Delete exception: {e}")


    async def _send_and_delete(self, channel, channel_name: str, text: str, delay: int = 3):
        """Send a response message — bot responses are not deleted (no API support)."""
        try:
            await self.send_chat(channel_name, text, channel)
        except Exception as e:
            logger.debug(f"send error: {e}")


    async def join_channel(self, channel_name: str):
        try:
            await self.join_channels([channel_name.lower()])
            logger.info(f"Dynamically joined Twitch channel: {channel_name}")
        except Exception as e:
            logger.error(f"Error joining channel {channel_name}: {e}")

    async def leave_channel(self, channel_name: str):
        try:
            await self.part_channels([channel_name.lower()])
            logger.info(f"Left Twitch channel: {channel_name}")
        except Exception as e:
            logger.error(f"Error leaving channel {channel_name}: {e}")
