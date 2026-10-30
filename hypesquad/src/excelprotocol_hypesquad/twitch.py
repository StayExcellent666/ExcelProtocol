from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import aiohttp


class TwitchError(RuntimeError):
    pass


class TwitchClient:
    """Minimal Twitch OAuth/Helix client.

    Member access tokens are used only long enough to prove account ownership
    and are never returned to or persisted by the Hypesquad service.
    """

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._app_token: str | None = None
        self._app_token_expires_at: datetime | None = None

    def authorization_url(self, state: str) -> str:
        return "https://id.twitch.tv/oauth2/authorize?" + urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                # OIDC proves identity without requesting access to email,
                # follows, subscriptions, chat, or broadcaster settings.
                "scope": "openid",
                "state": state,
                "force_verify": "true",
            }
        )

    async def exchange_identity(self, code: str) -> dict:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://id.twitch.tv/oauth2/token",
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": self.redirect_uri,
                },
            ) as response:
                payload = await response.json(content_type=None)
                if response.status != 200 or not payload.get("access_token"):
                    raise TwitchError("Twitch account verification failed")
                access_token = payload["access_token"]
            async with session.get(
                "https://id.twitch.tv/oauth2/validate",
                headers={"Authorization": f"OAuth {access_token}"},
            ) as response:
                identity = await response.json(content_type=None)
                if response.status != 200 or not identity.get("user_id"):
                    raise TwitchError("Twitch returned an invalid identity")
            # Best effort: the verification token is unnecessary after linking.
            try:
                async with session.post(
                    "https://id.twitch.tv/oauth2/revoke",
                    data={"client_id": self.client_id, "token": access_token},
                ):
                    pass
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
        return {
            "user_id": str(identity["user_id"]),
            "login": str(identity["login"]).lower(),
            "display_name": str(identity.get("login") or ""),
        }

    async def _get_app_token(self) -> str:
        now = datetime.now(timezone.utc)
        if (
            self._app_token
            and self._app_token_expires_at
            and now < self._app_token_expires_at - timedelta(minutes=2)
        ):
            return self._app_token
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://id.twitch.tv/oauth2/token",
                params={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
            ) as response:
                payload = await response.json(content_type=None)
                if response.status != 200 or not payload.get("access_token"):
                    raise TwitchError("Could not obtain a Twitch application token")
        self._app_token = payload["access_token"]
        self._app_token_expires_at = now + timedelta(
            seconds=int(payload.get("expires_in") or 3600)
        )
        return self._app_token

    async def get_live_user_ids(self, user_ids: list[str]) -> set[str]:
        unique = list(dict.fromkeys(str(value) for value in user_ids if value))
        if not unique:
            return set()
        token = await self._get_app_token()
        live: set[str] = set()
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for offset in range(0, len(unique), 100):
                batch = unique[offset : offset + 100]
                params = [("user_id", user_id) for user_id in batch]
                async with session.get(
                    "https://api.twitch.tv/helix/streams",
                    params=params,
                    headers={
                        "Client-ID": self.client_id,
                        "Authorization": f"Bearer {token}",
                    },
                ) as response:
                    payload = await response.json(content_type=None)
                    if response.status != 200:
                        raise TwitchError("Twitch live-status lookup failed")
                    live.update(str(row["user_id"]) for row in payload.get("data", []))
        return live

