from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    db_path: str = "./hypesquad.db"
    host: str = "127.0.0.1"
    port: int = 8091
    service_token: str = ""
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    twitch_redirect_uri: str = "http://127.0.0.1:8091/oauth/twitch/callback"
    public_base_url: str = "http://127.0.0.1:8091"
    scheduler_enabled: bool = False
    poll_seconds: int = 60

    @classmethod
    def from_env(cls) -> "Config":
        enabled = os.getenv("HYPESQUAD_SCHEDULER_ENABLED", "false").lower()
        config = cls(
            db_path=os.getenv("HYPESQUAD_DB_PATH", "./hypesquad.db"),
            host=os.getenv("HYPESQUAD_HOST", "127.0.0.1"),
            port=int(os.getenv("HYPESQUAD_PORT", "8091")),
            service_token=os.getenv("HYPESQUAD_SERVICE_TOKEN", ""),
            twitch_client_id=os.getenv("HYPESQUAD_TWITCH_CLIENT_ID", ""),
            twitch_client_secret=os.getenv("HYPESQUAD_TWITCH_CLIENT_SECRET", ""),
            twitch_redirect_uri=os.getenv(
                "HYPESQUAD_TWITCH_REDIRECT_URI",
                "http://127.0.0.1:8091/oauth/twitch/callback",
            ),
            public_base_url=os.getenv(
                "HYPESQUAD_PUBLIC_BASE_URL", "http://127.0.0.1:8091"
            ).rstrip("/"),
            scheduler_enabled=enabled in {"1", "true", "yes", "on"},
            poll_seconds=max(15, int(os.getenv("HYPESQUAD_POLL_SECONDS", "60"))),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if len(self.service_token) < 24:
            raise RuntimeError("HYPESQUAD_SERVICE_TOKEN must be at least 24 characters")
        if self.scheduler_enabled and not (
            self.twitch_client_id and self.twitch_client_secret
        ):
            raise RuntimeError(
                "The Hypesquad scheduler requires Twitch client credentials"
            )

