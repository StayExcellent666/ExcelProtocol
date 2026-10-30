from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .service import HypeSquadService
from .twitch import TwitchClient, TwitchError


logger = logging.getLogger(__name__)


class HypeScheduler:
    """Restart-safe Twitch polling worker for selection and stream completion."""

    def __init__(
        self, service: HypeSquadService, twitch: TwitchClient, *, poll_seconds: int = 60
    ):
        self.service = service
        self.twitch = twitch
        self.poll_seconds = max(15, int(poll_seconds))
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if not self._task or self._task.done():
            self._task = asyncio.create_task(self.run(), name="hypesquad-scheduler")

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Hypesquad scheduler tick failed")
            await asyncio.sleep(self.poll_seconds)

    async def tick(self, now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        slot = self.service.due_selection_slot(current)
        tracked = self.service.tracked_events()
        lookup_ids = {row["target_twitch_user_id"] for row in tracked}
        eligible_ids: list[str] = []
        if slot and not tracked:
            eligible_ids = self.service.eligible_twitch_user_ids(current)
            lookup_ids.update(eligible_ids)
        if not lookup_ids:
            return
        try:
            live_ids = await self.twitch.get_live_user_ids(sorted(lookup_ids))
        except TwitchError:
            logger.warning("Skipping Hypesquad tick because Twitch is unavailable")
            return

        if slot and not tracked:
            event = self.service.select_weekly(live_ids, selection_slot=slot, now=current)
            if event:
                self.service.mark_live(event["id"], now=current)

        for event in tracked:
            self.service.record_live_status(
                event["id"], event["target_twitch_user_id"] in live_ids, now=current
            )

