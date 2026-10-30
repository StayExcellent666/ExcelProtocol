from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .database import Database
from .scoring import (
    JOIN_POINTS,
    MESSAGE_SCORE_COOLDOWN_SECONDS,
    score_delta,
)


CONSENT_VERSION = "hypesquad-v1"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str | datetime | None) -> datetime | None:
    if value is None:
        return value
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def schedule_timezone(name: str):
    if str(name).upper() == "UTC":
        return timezone.utc
    return ZoneInfo(str(name))


class HypeError(Exception):
    status = 400
    code = "invalid_request"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(HypeError):
    status = 404
    code = "not_found"


class Conflict(HypeError):
    status = 409
    code = "conflict"


class Forbidden(HypeError):
    status = 403
    code = "forbidden"


class HypeSquadService:
    def __init__(self, db: Database, *, chooser=None):
        self.db = db
        self._chooser = chooser or secrets.choice
        now = iso(utcnow())
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO network_schedule
                   (singleton, enabled, weekday, hour, timezone, cooldown_days, updated_at)
                   VALUES (1, 0, 5, 19, 'UTC', 42, ?)""",
                (now,),
            )

    # ── Guilds and members ──────────────────────────────────────────────

    def configure_guild(
        self,
        guild_id: str,
        *,
        enabled: bool,
        announcement_channel_id: str | None = None,
        leaderboard_channel_id: str | None = None,
        auto_weekly: bool = True,
    ) -> dict:
        guild_id = str(guild_id)
        announcement_channel_id = (
            str(announcement_channel_id) if announcement_channel_id else None
        )
        leaderboard_channel_id = (
            str(leaderboard_channel_id) if leaderboard_channel_id else None
        )
        if enabled and not (announcement_channel_id and leaderboard_channel_id):
            raise HypeError(
                "Opted-in servers need announcement and leaderboard channels"
            )
        now = iso(utcnow())
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO guild_settings
                   (guild_id, enabled, announcement_channel_id,
                    leaderboard_channel_id, auto_weekly, joined_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                     enabled=excluded.enabled,
                     announcement_channel_id=excluded.announcement_channel_id,
                     leaderboard_channel_id=excluded.leaderboard_channel_id,
                     auto_weekly=excluded.auto_weekly,
                     updated_at=excluded.updated_at""",
                (
                    guild_id,
                    int(enabled),
                    announcement_channel_id,
                    leaderboard_channel_id,
                    int(auto_weekly),
                    now,
                    now,
                ),
            )
        return self.get_guild(guild_id)

    def get_guild(self, guild_id: str) -> dict:
        row = self.db.fetchone(
            "SELECT * FROM guild_settings WHERE guild_id = ?", (str(guild_id),)
        )
        if not row:
            raise NotFound("Hypesquad is not configured for this server")
        row["enabled"] = bool(row["enabled"])
        row["auto_weekly"] = bool(row["auto_weekly"])
        return row

    def link_member(
        self,
        discord_user_id: str,
        twitch_user_id: str,
        twitch_login: str,
        twitch_display_name: str | None = None,
    ) -> dict:
        discord_user_id = str(discord_user_id)
        twitch_user_id = str(twitch_user_id)
        twitch_login = str(twitch_login).strip().lower()
        if not (discord_user_id and twitch_user_id and twitch_login):
            raise HypeError("Discord and Twitch identities are required")
        now = iso(utcnow())
        with self.db.transaction() as connection:
            owner = connection.execute(
                "SELECT discord_user_id FROM members WHERE twitch_user_id = ?",
                (twitch_user_id,),
            ).fetchone()
            if owner and str(owner["discord_user_id"]) != discord_user_id:
                raise Conflict("That Twitch account is already linked to another member")
            existing = connection.execute(
                "SELECT twitch_user_id FROM members WHERE discord_user_id = ?",
                (discord_user_id,),
            ).fetchone()
            if existing and str(existing["twitch_user_id"]) != twitch_user_id:
                raise Conflict("Unlink the current Twitch account before linking another")
            connection.execute(
                """INSERT INTO members
                   (discord_user_id, twitch_user_id, twitch_login,
                    twitch_display_name, opted_in, consent_version,
                    status, linked_at, updated_at)
                   VALUES (?, ?, ?, ?, 0, ?, 'active', ?, ?)
                   ON CONFLICT(discord_user_id) DO UPDATE SET
                     twitch_login=excluded.twitch_login,
                     twitch_display_name=excluded.twitch_display_name,
                     consent_version=excluded.consent_version,
                     status='active', updated_at=excluded.updated_at""",
                (
                    discord_user_id,
                    twitch_user_id,
                    twitch_login,
                    twitch_display_name or twitch_login,
                    CONSENT_VERSION,
                    now,
                    now,
                ),
            )
        return self.get_member(discord_user_id)

    def join_member(self, discord_user_id: str, guild_id: str) -> dict:
        discord_user_id, guild_id = str(discord_user_id), str(guild_id)
        now = iso(utcnow())
        with self.db.transaction() as connection:
            guild = connection.execute(
                "SELECT enabled FROM guild_settings WHERE guild_id = ?", (guild_id,)
            ).fetchone()
            if not guild or not guild["enabled"]:
                raise Forbidden("This Discord server has not opted into Hypesquad")
            member = connection.execute(
                "SELECT 1 FROM members WHERE discord_user_id = ?",
                (discord_user_id,),
            ).fetchone()
            if not member:
                raise NotFound("Link a Twitch account before joining Hypesquad")
            connection.execute(
                """INSERT INTO member_guilds
                   (discord_user_id, guild_id, active, joined_at, updated_at)
                   VALUES (?, ?, 1, ?, ?)
                   ON CONFLICT(discord_user_id, guild_id) DO UPDATE SET
                     active=1, updated_at=excluded.updated_at""",
                (discord_user_id, guild_id, now, now),
            )
            connection.execute(
                "UPDATE members SET opted_in = 1, updated_at = ? WHERE discord_user_id = ?",
                (now, discord_user_id),
            )
        return self.get_member(discord_user_id)

    def leave_member(self, discord_user_id: str, guild_id: str) -> dict:
        discord_user_id, guild_id = str(discord_user_id), str(guild_id)
        now = iso(utcnow())
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE member_guilds SET active = 0, updated_at = ?
                   WHERE discord_user_id = ? AND guild_id = ?""",
                (now, discord_user_id, guild_id),
            )
            remaining = connection.execute(
                """SELECT 1 FROM member_guilds mg
                   JOIN guild_settings gs ON gs.guild_id = mg.guild_id
                   WHERE mg.discord_user_id = ? AND mg.active = 1
                     AND gs.enabled = 1 LIMIT 1""",
                (discord_user_id,),
            ).fetchone()
            if not remaining:
                connection.execute(
                    "UPDATE members SET opted_in = 0, updated_at = ? WHERE discord_user_id = ?",
                    (now, discord_user_id),
                )
        return self.get_member(discord_user_id)

    def unlink_member(self, discord_user_id: str) -> None:
        discord_user_id = str(discord_user_id)
        anonymous = f"deleted:{uuid.uuid4().hex}"
        with self.db.transaction() as connection:
            member = connection.execute(
                "SELECT twitch_user_id FROM members WHERE discord_user_id = ?",
                (discord_user_id,),
            ).fetchone()
            if not member:
                return
            twitch_user_id = str(member["twitch_user_id"])
            connection.execute(
                """UPDATE participants SET discord_user_id = ?, twitch_user_id = ?,
                   twitch_login = 'deleted-member', twitch_display_name = 'Deleted member'
                   WHERE discord_user_id = ?""",
                (anonymous, anonymous, discord_user_id),
            )
            connection.execute(
                """UPDATE events SET target_discord_user_id = ?,
                   target_twitch_user_id = ?, target_twitch_login = 'deleted-member'
                   WHERE target_discord_user_id = ?""",
                (anonymous, anonymous, discord_user_id),
            )
            connection.execute(
                "DELETE FROM activity_receipts WHERE twitch_user_id = ?",
                (twitch_user_id,),
            )
            connection.execute(
                "DELETE FROM activity_minutes WHERE twitch_user_id = ?",
                (twitch_user_id,),
            )
            connection.execute(
                "DELETE FROM members WHERE discord_user_id = ?", (discord_user_id,)
            )

    def get_member(self, discord_user_id: str) -> dict:
        row = self.db.fetchone(
            """SELECT discord_user_id, twitch_user_id, twitch_login,
                      twitch_display_name, opted_in, consent_version, status,
                      linked_at, updated_at, last_selected_at, cooldown_until,
                      selection_count
               FROM members WHERE discord_user_id = ?""",
            (str(discord_user_id),),
        )
        if not row:
            raise NotFound("Hypesquad member was not found")
        row["opted_in"] = bool(row["opted_in"])
        row["guilds"] = self.db.fetchall(
            "SELECT guild_id FROM member_guilds WHERE discord_user_id = ? AND active = 1",
            (str(discord_user_id),),
        )
        return row

    # ── Link state ──────────────────────────────────────────────────────

    def create_link_state(
        self, discord_user_id: str, guild_id: str, *, ttl_seconds: int = 600
    ) -> str:
        guild = self.get_guild(guild_id)
        if not guild["enabled"]:
            raise Forbidden("This Discord server has not opted into Hypesquad")
        raw = secrets.token_urlsafe(32)
        state_hash = hashlib.sha256(raw.encode()).hexdigest()
        expires = iso(utcnow() + timedelta(seconds=ttl_seconds))
        with self.db.transaction() as connection:
            connection.execute(
                "DELETE FROM oauth_states WHERE expires_at < ? OR consumed_at IS NOT NULL",
                (iso(utcnow()),),
            )
            connection.execute(
                """INSERT INTO oauth_states
                   (state_hash, discord_user_id, guild_id, expires_at)
                   VALUES (?, ?, ?, ?)""",
                (state_hash, str(discord_user_id), str(guild_id), expires),
            )
        return raw

    def consume_link_state(self, raw_state: str) -> dict:
        state_hash = hashlib.sha256(str(raw_state).encode()).hexdigest()
        now = iso(utcnow())
        with self.db.transaction() as connection:
            row = connection.execute(
                """SELECT discord_user_id, guild_id, expires_at, consumed_at
                   FROM oauth_states WHERE state_hash = ?""",
                (state_hash,),
            ).fetchone()
            if not row or row["consumed_at"] or row["expires_at"] < now:
                raise Forbidden("The Twitch link is invalid or expired")
            connection.execute(
                "UPDATE oauth_states SET consumed_at = ? WHERE state_hash = ?",
                (now, state_hash),
            )
            return dict(row)

    # ── Selection and events ────────────────────────────────────────────

    def _eligible_members(
        self, now: datetime | None = None, *, auto_weekly_only: bool = False
    ) -> list[dict]:
        now_iso = iso(now or utcnow())
        auto_clause = "AND gs.auto_weekly = 1" if auto_weekly_only else ""
        return self.db.fetchall(
            f"""SELECT DISTINCT m.* FROM members m
               JOIN member_guilds mg ON mg.discord_user_id = m.discord_user_id
               JOIN guild_settings gs ON gs.guild_id = mg.guild_id
               WHERE m.opted_in = 1 AND m.status = 'active'
                 AND mg.active = 1 AND gs.enabled = 1
                 {auto_clause}
                 AND (m.cooldown_until IS NULL OR m.cooldown_until <= ?)
               ORDER BY m.selection_count ASC,
                        CASE WHEN m.last_selected_at IS NULL THEN 0 ELSE 1 END,
                        m.last_selected_at ASC""",
            (now_iso,),
        )

    def eligible_twitch_user_ids(self, now: datetime | None = None) -> list[str]:
        return [
            row["twitch_user_id"]
            for row in self._eligible_members(now, auto_weekly_only=True)
        ]

    def _create_event(
        self,
        member: dict,
        *,
        source: str,
        source_guild_id: str | None,
        selection_slot: str | None,
        cooldown_days: int,
        selected_at: datetime,
    ) -> dict:
        event_id = uuid.uuid4().hex
        selected_iso = iso(selected_at)
        cooldown_until = iso(selected_at + timedelta(days=cooldown_days))
        with self.db.transaction() as connection:
            weekly_clause = "AND auto_weekly = 1" if source == "weekly" else ""
            targets = connection.execute(
                f"""SELECT guild_id, announcement_channel_id, leaderboard_channel_id
                   FROM guild_settings
                   WHERE enabled = 1 AND announcement_channel_id IS NOT NULL
                     AND leaderboard_channel_id IS NOT NULL {weekly_clause}"""
            ).fetchall()
            if not targets:
                raise Conflict("No opted-in Discord servers can receive a Hyperaid")
            try:
                connection.execute(
                    """INSERT INTO events
                       (id, target_discord_user_id, target_twitch_user_id,
                        target_twitch_login, source, source_guild_id,
                        selection_slot, status, selected_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting_live', ?)""",
                    (
                        event_id,
                        member["discord_user_id"],
                        member["twitch_user_id"],
                        member["twitch_login"],
                        source,
                        source_guild_id,
                        selection_slot,
                        selected_iso,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                message = str(exc).lower()
                if "selection_slot" in message:
                    raise Conflict("This weekly selection slot was already processed") from exc
                raise Conflict("Another Hyperaid is already waiting or active") from exc
            connection.executemany(
                """INSERT INTO event_guilds
                   (event_id, guild_id, announcement_channel_id,
                    leaderboard_channel_id) VALUES (?, ?, ?, ?)""",
                [
                    (
                        event_id,
                        row["guild_id"],
                        row["announcement_channel_id"],
                        row["leaderboard_channel_id"],
                    )
                    for row in targets
                ],
            )
            connection.execute(
                """UPDATE members SET last_selected_at = ?, cooldown_until = ?,
                   selection_count = selection_count + 1, updated_at = ?
                   WHERE discord_user_id = ?""",
                (selected_iso, cooldown_until, selected_iso, member["discord_user_id"]),
            )
            self._insert_outbox(
                connection,
                "hyperaid.selected",
                event_id,
                {
                    "event_id": event_id,
                    "target_twitch_login": member["twitch_login"],
                    "target_discord_user_id": member["discord_user_id"],
                    "source": source,
                },
                selected_iso,
            )
        return self.get_event(event_id)

    def select_manual(
        self, discord_user_id: str, source_guild_id: str, *, now: datetime | None = None
    ) -> dict:
        guild = self.get_guild(source_guild_id)
        if not guild["enabled"]:
            raise Forbidden("This Discord server has not opted into Hypesquad")
        selected_at = now or utcnow()
        member = next(
            (
                candidate
                for candidate in self._eligible_members(selected_at)
                if candidate["discord_user_id"] == str(discord_user_id)
            ),
            None,
        )
        if not member:
            raise Conflict("That member is not currently eligible for a Hyperaid")
        schedule = self.get_schedule()
        return self._create_event(
            member,
            source="manual",
            source_guild_id=str(source_guild_id),
            selection_slot=None,
            cooldown_days=schedule["cooldown_days"],
            selected_at=selected_at,
        )

    def select_weekly(
        self,
        live_twitch_user_ids: set[str],
        *,
        selection_slot: str,
        now: datetime | None = None,
    ) -> dict | None:
        selected_at = now or utcnow()
        live_ids = {str(value) for value in live_twitch_user_ids}
        candidates = [
            row
            for row in self._eligible_members(selected_at, auto_weekly_only=True)
            if row["twitch_user_id"] in live_ids
        ]
        if not candidates:
            return None
        minimum_count = min(row["selection_count"] for row in candidates)
        fairest = [row for row in candidates if row["selection_count"] == minimum_count]
        member = self._chooser(fairest)
        schedule = self.get_schedule()
        event = self._create_event(
            member,
            source="weekly",
            source_guild_id=None,
            selection_slot=selection_slot,
            cooldown_days=schedule["cooldown_days"],
            selected_at=selected_at,
        )
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE network_schedule SET last_slot = ?, updated_at = ? WHERE singleton = 1",
                (selection_slot, iso(selected_at)),
            )
        return event

    def mark_live(self, event_id: str, *, now: datetime | None = None) -> dict:
        started = iso(now or utcnow())
        with self.db.transaction() as connection:
            event = connection.execute(
                "SELECT * FROM events WHERE id = ?", (str(event_id),)
            ).fetchone()
            if not event:
                raise NotFound("Hyperaid was not found")
            if event["status"] == "finished":
                raise Conflict("That Hyperaid already ended")
            if event["status"] != "active":
                connection.execute(
                    """UPDATE events SET status='active', started_at=?,
                       last_live_seen_at=?, offline_checks=0 WHERE id=?""",
                    (started, started, str(event_id)),
                )
                targets = [
                    dict(row)
                    for row in connection.execute(
                        """SELECT guild_id, announcement_channel_id
                           FROM event_guilds WHERE event_id = ?""",
                        (str(event_id),),
                    ).fetchall()
                ]
                self._insert_outbox(
                    connection,
                    "hyperaid.started",
                    str(event_id),
                    {
                        "event_id": str(event_id),
                        "target_twitch_login": event["target_twitch_login"],
                        "guild_targets": targets,
                    },
                    started,
                )
        return self.get_event(event_id)

    def join_event(
        self, event_id: str, discord_user_id: str, source_guild_id: str,
        *, now: datetime | None = None,
    ) -> dict:
        joined_at = iso(now or utcnow())
        with self.db.transaction() as connection:
            event = connection.execute(
                "SELECT * FROM events WHERE id = ?", (str(event_id),)
            ).fetchone()
            if not event or event["status"] != "active":
                raise Conflict("This Hyperaid is not currently accepting participants")
            target = connection.execute(
                "SELECT 1 FROM event_guilds WHERE event_id = ? AND guild_id = ?",
                (str(event_id), str(source_guild_id)),
            ).fetchone()
            if not target:
                raise Forbidden("This server is not participating in that Hyperaid")
            member = connection.execute(
                """SELECT m.* FROM members m
                   JOIN member_guilds mg ON mg.discord_user_id = m.discord_user_id
                   JOIN guild_settings gs ON gs.guild_id = mg.guild_id
                   WHERE m.discord_user_id = ? AND m.opted_in = 1
                     AND m.status = 'active' AND mg.guild_id = ?
                     AND mg.active = 1 AND gs.enabled = 1""",
                (str(discord_user_id), str(source_guild_id)),
            ).fetchone()
            if not member:
                raise Forbidden("Join Hypesquad in this server before joining a Hyperaid")
            if member["twitch_user_id"] == event["target_twitch_user_id"]:
                raise Conflict("The featured streamer cannot join their own Hyperaid")
            connection.execute(
                """INSERT OR IGNORE INTO participants
                   (event_id, discord_user_id, twitch_user_id, twitch_login,
                    twitch_display_name, source_guild_id, joined_at, score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(event_id),
                    str(discord_user_id),
                    member["twitch_user_id"],
                    member["twitch_login"],
                    member["twitch_display_name"],
                    str(source_guild_id),
                    joined_at,
                    JOIN_POINTS,
                ),
            )
        return self.get_participant(event_id, discord_user_id)

    def record_activity(
        self,
        event_id: str,
        twitch_user_id: str,
        message_id: str,
        occurred_at: str | datetime | None = None,
    ) -> dict | None:
        occurred = parse_time(occurred_at) or utcnow()
        occurred_iso = iso(occurred)
        minute = occurred.replace(second=0, microsecond=0)
        minute_iso = iso(minute)
        with self.db.transaction() as connection:
            event = connection.execute(
                "SELECT status FROM events WHERE id = ?", (str(event_id),)
            ).fetchone()
            if not event or event["status"] != "active":
                return None
            participant = connection.execute(
                """SELECT * FROM participants
                   WHERE event_id = ? AND twitch_user_id = ?""",
                (str(event_id), str(twitch_user_id)),
            ).fetchone()
            if not participant:
                return None
            receipt = connection.execute(
                """INSERT OR IGNORE INTO activity_receipts
                   (event_id, message_id, twitch_user_id, occurred_at)
                   VALUES (?, ?, ?, ?)""",
                (str(event_id), str(message_id), str(twitch_user_id), occurred_iso),
            )
            if receipt.rowcount == 0:
                return dict(participant)
            last_scored = parse_time(participant["last_scored_at"])
            scored_message = (
                last_scored is None
                or (occurred - last_scored).total_seconds()
                >= MESSAGE_SCORE_COOLDOWN_SECONDS
            )
            minute_insert = connection.execute(
                """INSERT OR IGNORE INTO activity_minutes
                   (event_id, twitch_user_id, minute_start) VALUES (?, ?, ?)""",
                (str(event_id), str(twitch_user_id), minute_iso),
            )
            new_minute = minute_insert.rowcount == 1
            delta = score_delta(
                new_active_minute=new_minute, scored_message=scored_message
            )
            connection.execute(
                """UPDATE participants SET
                     message_count = message_count + 1,
                     scored_message_count = scored_message_count + ?,
                     active_minutes = active_minutes + ?,
                     score = score + ?,
                     last_scored_at = CASE WHEN ? THEN ? ELSE last_scored_at END
                   WHERE event_id = ? AND twitch_user_id = ?""",
                (
                    int(scored_message),
                    int(new_minute),
                    delta,
                    int(scored_message),
                    occurred_iso,
                    str(event_id),
                    str(twitch_user_id),
                ),
            )
        return self.get_participant_by_twitch(event_id, twitch_user_id)

    def record_live_status(
        self, event_id: str, is_live: bool, *, now: datetime | None = None
    ) -> dict:
        checked = iso(now or utcnow())
        event = self.get_event(event_id)
        if event["status"] == "waiting_live" and is_live:
            return self.mark_live(event_id, now=parse_time(checked))
        if event["status"] != "active":
            return event
        if is_live:
            with self.db.transaction() as connection:
                connection.execute(
                    """UPDATE events SET last_live_seen_at = ?, offline_checks = 0
                       WHERE id = ?""",
                    (checked, str(event_id)),
                )
            return self.get_event(event_id)
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE events SET offline_checks = offline_checks + 1 WHERE id = ?",
                (str(event_id),),
            )
            row = connection.execute(
                "SELECT offline_checks FROM events WHERE id = ?", (str(event_id),)
            ).fetchone()
        if row and row["offline_checks"] >= 2:
            return self.finish_event(event_id, now=parse_time(checked))
        return self.get_event(event_id)

    def finish_event(self, event_id: str, *, now: datetime | None = None) -> dict:
        ended = iso(now or utcnow())
        with self.db.transaction() as connection:
            event = connection.execute(
                "SELECT * FROM events WHERE id = ?", (str(event_id),)
            ).fetchone()
            if not event:
                raise NotFound("Hyperaid was not found")
            if event["status"] == "finished":
                return self.get_event(event_id)
            connection.execute(
                "UPDATE events SET status='finished', ended_at=? WHERE id=?",
                (ended, str(event_id)),
            )
            guild_rows = connection.execute(
                """SELECT eg.guild_id, eg.leaderboard_channel_id
                   FROM event_guilds eg JOIN guild_settings gs ON gs.guild_id=eg.guild_id
                   WHERE eg.event_id=? AND gs.enabled=1""",
                (str(event_id),),
            ).fetchall()
            leaderboards = []
            for guild in guild_rows:
                rows = [
                    dict(row)
                    for row in connection.execute(
                        """SELECT twitch_login, twitch_display_name, score,
                                  active_minutes, message_count, scored_message_count
                           FROM participants
                           WHERE event_id=? AND source_guild_id=?
                           ORDER BY score DESC, active_minutes DESC,
                                    scored_message_count DESC, joined_at ASC
                           LIMIT 25""",
                        (str(event_id), guild["guild_id"]),
                    ).fetchall()
                ]
                for rank, row in enumerate(rows, start=1):
                    row["rank"] = rank
                leaderboards.append(
                    {
                        "guild_id": guild["guild_id"],
                        "channel_id": guild["leaderboard_channel_id"],
                        "rows": rows,
                    }
                )
            global_rows = [
                dict(row)
                for row in connection.execute(
                    """SELECT twitch_login, twitch_display_name, score,
                              active_minutes, message_count, scored_message_count,
                              source_guild_id
                       FROM participants WHERE event_id=?
                       ORDER BY score DESC, active_minutes DESC,
                                scored_message_count DESC, joined_at ASC
                       LIMIT 50""",
                    (str(event_id),),
                ).fetchall()
            ]
            for rank, row in enumerate(global_rows, start=1):
                row["rank"] = rank
            self._insert_outbox(
                connection,
                "hyperaid.finished",
                str(event_id),
                {
                    "event_id": str(event_id),
                    "target_twitch_login": event["target_twitch_login"],
                    "guild_leaderboards": leaderboards,
                    "global_leaderboard": global_rows,
                },
                ended,
            )
            # Message contents are never stored. Idempotency receipts and
            # minute buckets are no longer needed once aggregates are final.
            connection.execute(
                "DELETE FROM activity_receipts WHERE event_id = ?", (str(event_id),)
            )
            connection.execute(
                "DELETE FROM activity_minutes WHERE event_id = ?", (str(event_id),)
            )
        return self.get_event(event_id)

    def cancel_event(self, event_id: str, *, now: datetime | None = None) -> dict:
        cancelled_at = iso(now or utcnow())
        with self.db.transaction() as connection:
            event = connection.execute(
                "SELECT * FROM events WHERE id = ?", (str(event_id),)
            ).fetchone()
            if not event:
                raise NotFound("Hyperaid was not found")
            if event["status"] in {"finished", "cancelled"}:
                return self.get_event(event_id)
            connection.execute(
                "UPDATE events SET status='cancelled', ended_at=? WHERE id=?",
                (cancelled_at, str(event_id)),
            )
            self._insert_outbox(
                connection,
                "hyperaid.cancelled",
                str(event_id),
                {"event_id": str(event_id)},
                cancelled_at,
            )
        return self.get_event(event_id)

    # ── Scheduling ──────────────────────────────────────────────────────

    def configure_schedule(
        self,
        *,
        enabled: bool,
        weekday: int,
        hour: int,
        timezone_name: str,
        cooldown_days: int = 42,
    ) -> dict:
        if not 0 <= int(weekday) <= 6 or not 0 <= int(hour) <= 23:
            raise HypeError("weekday must be 0-6 and hour must be 0-23")
        if not 1 <= int(cooldown_days) <= 365:
            raise HypeError("cooldown_days must be between 1 and 365")
        try:
            schedule_timezone(str(timezone_name))
        except ZoneInfoNotFoundError as exc:
            raise HypeError("Unknown schedule timezone") from exc
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE network_schedule SET enabled=?, weekday=?, hour=?,
                   timezone=?, cooldown_days=?, updated_at=? WHERE singleton=1""",
                (
                    int(enabled), int(weekday), int(hour), str(timezone_name),
                    int(cooldown_days), iso(utcnow()),
                ),
            )
        return self.get_schedule()

    def get_schedule(self) -> dict:
        row = self.db.fetchone("SELECT * FROM network_schedule WHERE singleton=1")
        row["enabled"] = bool(row["enabled"])
        return row

    def due_selection_slot(self, now: datetime | None = None) -> str | None:
        schedule = self.get_schedule()
        if not schedule["enabled"]:
            return None
        current = (now or utcnow()).astimezone(schedule_timezone(schedule["timezone"]))
        if current.weekday() != schedule["weekday"] or current.hour != schedule["hour"]:
            return None
        slot = f"{current.date().isoformat()}T{current.hour:02d}@{schedule['timezone']}"
        return None if slot == schedule.get("last_slot") else slot

    def tracked_events(self) -> list[dict]:
        return self.db.fetchall(
            """SELECT * FROM events
               WHERE status IN ('waiting_live', 'active') ORDER BY selected_at"""
        )

    # ── Read models and outbox ──────────────────────────────────────────

    def get_event(self, event_id: str) -> dict:
        row = self.db.fetchone("SELECT * FROM events WHERE id = ?", (str(event_id),))
        if not row:
            raise NotFound("Hyperaid was not found")
        row["guild_targets"] = self.db.fetchall(
            """SELECT guild_id, announcement_channel_id, leaderboard_channel_id,
                      announcement_message_id, leaderboard_message_id
               FROM event_guilds WHERE event_id = ? ORDER BY guild_id""",
            (str(event_id),),
        )
        row["participant_count"] = self.db.fetchone(
            "SELECT COUNT(*) AS count FROM participants WHERE event_id = ?",
            (str(event_id),),
        )["count"]
        return row

    def get_participant(self, event_id: str, discord_user_id: str) -> dict:
        row = self.db.fetchone(
            "SELECT * FROM participants WHERE event_id=? AND discord_user_id=?",
            (str(event_id), str(discord_user_id)),
        )
        if not row:
            raise NotFound("Hyperaid participant was not found")
        return row

    def get_participant_by_twitch(self, event_id: str, twitch_user_id: str) -> dict:
        row = self.db.fetchone(
            "SELECT * FROM participants WHERE event_id=? AND twitch_user_id=?",
            (str(event_id), str(twitch_user_id)),
        )
        if not row:
            raise NotFound("Hyperaid participant was not found")
        return row

    def get_leaderboards(self, event_id: str) -> dict:
        event = self.get_event(event_id)
        guilds = {}
        for target in event["guild_targets"]:
            guilds[target["guild_id"]] = self.db.fetchall(
                """SELECT twitch_login, twitch_display_name, score,
                          active_minutes, message_count, scored_message_count
                   FROM participants WHERE event_id=? AND source_guild_id=?
                   ORDER BY score DESC, active_minutes DESC,
                            scored_message_count DESC, joined_at ASC""",
                (str(event_id), target["guild_id"]),
            )
        return {"event_id": str(event_id), "guilds": guilds}

    def pending_outbox(self, *, after_id: int = 0, limit: int = 100) -> list[dict]:
        rows = self.db.fetchall(
            """SELECT id, event_type, aggregate_id, payload_json, created_at
               FROM outbox WHERE delivered_at IS NULL AND id > ?
               ORDER BY id LIMIT ?""",
            (max(0, int(after_id)), min(250, max(1, int(limit)))),
        )
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
        return rows

    def acknowledge_outbox(self, outbox_id: int) -> None:
        with self.db.transaction() as connection:
            updated = connection.execute(
                "UPDATE outbox SET delivered_at=? WHERE id=? AND delivered_at IS NULL",
                (iso(utcnow()), int(outbox_id)),
            )
            if updated.rowcount == 0:
                raise NotFound("Outbox message was not found or was already acknowledged")

    @staticmethod
    def _insert_outbox(
        connection,
        event_type: str,
        aggregate_id: str,
        payload: dict,
        created_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO outbox
               (event_type, aggregate_id, payload_json, created_at)
               VALUES (?, ?, ?, ?)""",
            (
                event_type,
                str(aggregate_id),
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                created_at,
            ),
        )

