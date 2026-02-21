import aiohttp
import logging
from datetime import datetime, timedelta
from config import TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET

logger = logging.getLogger(__name__)

class TwitchAPI:
    def __init__(self):
        self.client_id = TWITCH_CLIENT_ID
        self.client_secret = TWITCH_CLIENT_SECRET
        self.access_token = None
        self.token_expires_at = None
        self.base_url = "https://api.twitch.tv/helix"
        self._session = None

    async def get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        """Close the aiohttp session"""
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_access_token(self) -> str:
        """Get or refresh the app access token"""
        now = datetime.utcnow()

        # Return cached token if still valid (with 60s buffer)
        if self.access_token and self.token_expires_at and now < self.token_expires_at - timedelta(seconds=60):
            return self.access_token

        logger.info("Fetching new Twitch access token...")
        session = await self.get_session()

        async with session.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials"
            }
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"Failed to get Twitch token: {resp.status} - {text}")

            data = await resp.json()
            self.access_token = data["access_token"]
            self.token_expires_at = now + timedelta(seconds=data["expires_in"])
            logger.info("Successfully obtained Twitch access token")
            return self.access_token

    async def _headers(self) -> dict:
        """Build auth headers for Twitch API requests"""
        token = await self.get_access_token()
        return {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {token}"
        }

    async def get_live_streams(self, usernames: list) -> list:
        """
        Check which streamers from the list are currently live.
        Returns list of stream objects for live streamers.
        Handles batches of up to 100.
        """
        if not usernames:
            return []

        session = await self.get_session()
        headers = await self._headers()

        # Build query params - up to 100 user_login per request
        params = [("user_login", name.lower()) for name in usernames[:100]]

        try:
            async with session.get(
                f"{self.base_url}/streams",
                headers=headers,
                params=params
            ) as resp:
                if resp.status == 401:
                    # Token expired, clear and retry once
                    self.access_token = None
                    headers = await self._headers()
                    async with session.get(
                        f"{self.base_url}/streams",
                        headers=headers,
                        params=params
                    ) as retry_resp:
                        data = await retry_resp.json()
                elif resp.status != 200:
                    logger.error(f"Twitch streams API error: {resp.status}")
                    return []
                else:
                    data = await resp.json()

            streams = data.get("data", [])

            # Enrich each stream with the streamer's profile image
            if streams:
                user_ids = [s["user_id"] for s in streams]
                profile_images = await self._get_profile_images(user_ids)
                for stream in streams:
                    stream["profile_image_url"] = profile_images.get(stream["user_id"], "")

            return streams

        except Exception as e:
            logger.error(f"Error fetching live streams: {e}", exc_info=True)
            return []

    async def _get_profile_images(self, user_ids: list) -> dict:
        """Fetch profile image URLs for a list of user IDs. Returns {user_id: url}"""
        if not user_ids:
            return {}

        session = await self.get_session()
        headers = await self._headers()
        params = [("id", uid) for uid in user_ids[:100]]

        try:
            async with session.get(
                f"{self.base_url}/users",
                headers=headers,
                params=params
            ) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
                return {u["id"]: u["profile_image_url"] for u in data.get("data", [])}
        except Exception as e:
            logger.error(f"Error fetching profile images: {e}")
            return {}

    async def get_user(self, username: str) -> dict | None:
        """
        Get user info for a single Twitch username.
        Returns user dict or None if not found.
        """
        session = await self.get_session()
        headers = await self._headers()

        try:
            async with session.get(
                f"{self.base_url}/users",
                headers=headers,
                params={"login": username.lower()}
            ) as resp:
                if resp.status != 200:
                    logger.error(f"Twitch users API error: {resp.status}")
                    return None
                data = await resp.json()
                users = data.get("data", [])
                return users[0] if users else None

        except Exception as e:
            logger.error(f"Error fetching user {username}: {e}", exc_info=True)
            return None

    async def get_user_by_id(self, user_id: str) -> dict | None:
        """Get user info by Twitch user ID"""
        session = await self.get_session()
        headers = await self._headers()

        try:
            async with session.get(
                f"{self.base_url}/users",
                headers=headers,
                params={"id": user_id}
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                users = data.get("data", [])
                return users[0] if users else None
        except Exception as e:
            logger.error(f"Error fetching user by ID {user_id}: {e}")
            return None

    async def get_channel_info(self, broadcaster_id: str) -> dict | None:
        """
        Get channel info (current game, title, etc.) for a broadcaster.
        Used for shoutouts and stream info commands.
        """
        session = await self.get_session()
        headers = await self._headers()

        try:
            async with session.get(
                f"{self.base_url}/channels",
                headers=headers,
                params={"broadcaster_id": broadcaster_id}
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                channels = data.get("data", [])
                return channels[0] if channels else None
        except Exception as e:
            logger.error(f"Error fetching channel info for {broadcaster_id}: {e}")
            return None

    async def get_last_stream_info(self, user_login: str) -> dict | None:
        """
        Get info about a streamer's most recent broadcast.
        Returns dict with game, title, date - or None.
        Used for shoutout messages.
        """
        # First get user ID
        user = await self.get_user(user_login)
        if not user:
            return None

        # Get channel info (has last broadcast game/title)
        channel = await self.get_channel_info(user["id"])
        if not channel:
            return None

        # Get recent videos to find last stream date
        session = await self.get_session()
        headers = await self._headers()
        last_streamed_at = None

        try:
            async with session.get(
                f"{self.base_url}/videos",
                headers=headers,
                params={
                    "user_id": user["id"],
                    "type": "archive",
                    "first": 1
                }
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    videos = data.get("data", [])
                    if videos:
                        last_streamed_at = videos[0].get("created_at")
        except Exception as e:
            logger.error(f"Error fetching videos for {user_login}: {e}")

        return {
            "user": user,
            "game_name": channel.get("game_name", "Unknown"),
            "title": channel.get("title", ""),
            "last_streamed_at": last_streamed_at,
            "profile_image_url": user.get("profile_image_url", ""),
            "broadcaster_language": channel.get("broadcaster_language", ""),
            "description": user.get("description", "")
        }

    async def get_stream_uptime(self, user_login: str) -> str | None:
        """
        Get the current stream uptime for a live streamer.
        Returns a formatted string like '2h 35m' or None if offline.
        """
        streams = await self.get_live_streams([user_login])
        if not streams:
            return None

        stream = streams[0]
        started_at_str = stream.get("started_at")
        if not started_at_str:
            return None

        started_at = datetime.strptime(started_at_str, "%Y-%m-%dT%H:%M:%SZ")
        delta = datetime.utcnow() - started_at

        total_seconds = int(delta.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60

        if hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"

    async def get_viewer_count(self, user_login: str) -> int | None:
        """Get current viewer count for a live streamer"""
        streams = await self.get_live_streams([user_login])
        if not streams:
            return None
        return streams[0].get("viewer_count", 0)
