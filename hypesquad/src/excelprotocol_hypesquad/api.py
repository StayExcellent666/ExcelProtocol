from __future__ import annotations

import asyncio
import hmac
import logging
from datetime import datetime

from aiohttp import web

from .config import Config
from .database import Database
from .scheduler import HypeScheduler
from .service import HypeError, HypeSquadService
from .twitch import TwitchClient, TwitchError


logger = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 64 * 1024


def _service(request: web.Request) -> HypeSquadService:
    return request.app["service"]


async def _json(request: web.Request) -> dict:
    if request.content_length is not None and request.content_length > MAX_REQUEST_BYTES:
        raise web.HTTPRequestEntityTooLarge(
            max_size=MAX_REQUEST_BYTES, actual_size=request.content_length
        )
    try:
        value = await request.json()
    except Exception as exc:
        raise HypeError("A JSON object is required") from exc
    if not isinstance(value, dict):
        raise HypeError("A JSON object is required")
    return value


def _boolean(body: dict, key: str, default: bool) -> bool:
    value = body.get(key, default)
    if type(value) is not bool:
        raise HypeError(f"{key} must be a boolean")
    return value


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        response = await handler(request)
    except HypeError as exc:
        response = web.json_response(
            {"error": exc.code, "message": exc.message}, status=exc.status
        )
    except TwitchError as exc:
        response = web.json_response(
            {"error": "twitch_unavailable", "message": str(exc)}, status=502
        )
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@web.middleware
async def service_auth_middleware(request: web.Request, handler):
    if request.path in {"/health", "/oauth/twitch/callback"}:
        return await handler(request)
    expected = request.app["config"].service_token
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise web.HTTPUnauthorized(reason="Invalid service token")
    return await handler(request)


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "excelprotocol-hypesquad"})


async def put_guild(request: web.Request) -> web.Response:
    body = await _json(request)
    guild = _service(request).configure_guild(
        request.match_info["guild_id"],
        enabled=_boolean(body, "enabled", False),
        announcement_channel_id=body.get("announcement_channel_id"),
        leaderboard_channel_id=body.get("leaderboard_channel_id"),
        auto_weekly=_boolean(body, "auto_weekly", True),
    )
    return web.json_response(guild)


async def get_guild(request: web.Request) -> web.Response:
    return web.json_response(_service(request).get_guild(request.match_info["guild_id"]))


async def start_link(request: web.Request) -> web.Response:
    body = await _json(request)
    discord_user_id = str(body.get("discord_user_id") or "")
    guild_id = str(body.get("guild_id") or "")
    if not discord_user_id or not guild_id:
        raise HypeError("discord_user_id and guild_id are required")
    state = _service(request).create_link_state(discord_user_id, guild_id)
    return web.json_response(
        {
            "authorization_url": request.app["twitch"].authorization_url(state),
            "expires_in_seconds": 600,
        }
    )


async def twitch_callback(request: web.Request) -> web.Response:
    code = str(request.rel_url.query.get("code") or "")
    state = str(request.rel_url.query.get("state") or "")
    if not code or not state:
        raise HypeError("Twitch did not return a valid authorization")
    pending = _service(request).consume_link_state(state)
    identity = await request.app["twitch"].exchange_identity(code)
    _service(request).link_member(
        pending["discord_user_id"],
        identity["user_id"],
        identity["login"],
        identity.get("display_name"),
    )
    _service(request).join_member(pending["discord_user_id"], pending["guild_id"])
    return web.Response(
        text="""<!doctype html><meta charset="utf-8">
<title>Hypesquad linked</title>
<body style="background:#071116;color:#ecffff;font:18px system-ui;padding:48px">
<h1 style="color:#00f5d4">You joined the ExcelProtocol Hypesquad.</h1>
<p>Your Twitch identity was verified. You can close this window and return to Discord.</p>
</body>""",
        content_type="text/html",
    )


async def get_member(request: web.Request) -> web.Response:
    return web.json_response(
        _service(request).get_member(request.match_info["discord_user_id"])
    )


async def leave_member(request: web.Request) -> web.Response:
    body = await _json(request)
    member = _service(request).leave_member(
        request.match_info["discord_user_id"], str(body.get("guild_id") or "")
    )
    return web.json_response(member)


async def unlink_member(request: web.Request) -> web.Response:
    _service(request).unlink_member(request.match_info["discord_user_id"])
    return web.json_response({"ok": True})


async def create_manual_event(request: web.Request) -> web.Response:
    body = await _json(request)
    event = _service(request).select_manual(
        str(body.get("discord_user_id") or ""),
        str(body.get("source_guild_id") or ""),
    )
    return web.json_response(event, status=201)


async def get_event(request: web.Request) -> web.Response:
    return web.json_response(_service(request).get_event(request.match_info["event_id"]))


async def mark_event_live(request: web.Request) -> web.Response:
    return web.json_response(_service(request).mark_live(request.match_info["event_id"]))


async def join_event(request: web.Request) -> web.Response:
    body = await _json(request)
    participant = _service(request).join_event(
        request.match_info["event_id"],
        str(body.get("discord_user_id") or ""),
        str(body.get("guild_id") or ""),
    )
    return web.json_response(participant, status=201)


async def record_activity(request: web.Request) -> web.Response:
    body = await _json(request)
    twitch_user_id = str(body.get("twitch_user_id") or "")
    message_id = str(body.get("message_id") or "")
    if not twitch_user_id or not message_id:
        raise HypeError("twitch_user_id and message_id are required")
    participant = _service(request).record_activity(
        request.match_info["event_id"],
        twitch_user_id,
        message_id,
        body.get("occurred_at"),
    )
    return web.json_response({"counted": participant is not None, "participant": participant})


async def finish_event(request: web.Request) -> web.Response:
    return web.json_response(_service(request).finish_event(request.match_info["event_id"]))


async def cancel_event(request: web.Request) -> web.Response:
    return web.json_response(_service(request).cancel_event(request.match_info["event_id"]))


async def get_leaderboards(request: web.Request) -> web.Response:
    return web.json_response(
        _service(request).get_leaderboards(request.match_info["event_id"])
    )


async def put_schedule(request: web.Request) -> web.Response:
    body = await _json(request)
    schedule = _service(request).configure_schedule(
        enabled=_boolean(body, "enabled", False),
        weekday=int(body.get("weekday", 5)),
        hour=int(body.get("hour", 19)),
        timezone_name=str(body.get("timezone") or "UTC"),
        cooldown_days=int(body.get("cooldown_days", 42)),
    )
    return web.json_response(schedule)


async def get_schedule(request: web.Request) -> web.Response:
    return web.json_response(_service(request).get_schedule())


async def tick_schedule(request: web.Request) -> web.Response:
    body = await _json(request)
    raw_now = body.get("now")
    now = datetime.fromisoformat(raw_now.replace("Z", "+00:00")) if raw_now else None
    await request.app["scheduler"].tick(now)
    return web.json_response({"ok": True})


async def get_outbox(request: web.Request) -> web.Response:
    query = request.rel_url.query
    messages = _service(request).pending_outbox(
        after_id=int(query.get("after_id", 0)), limit=int(query.get("limit", 100))
    )
    return web.json_response({"messages": messages})


async def acknowledge_outbox(request: web.Request) -> web.Response:
    _service(request).acknowledge_outbox(int(request.match_info["outbox_id"]))
    return web.json_response({"ok": True})


def create_app(config: Config, *, service: HypeSquadService | None = None) -> web.Application:
    db = Database(config.db_path)
    domain = service or HypeSquadService(db)
    twitch = TwitchClient(
        config.twitch_client_id,
        config.twitch_client_secret,
        config.twitch_redirect_uri,
    )
    scheduler = HypeScheduler(domain, twitch, poll_seconds=config.poll_seconds)
    app = web.Application(
        middlewares=[error_middleware, service_auth_middleware],
        client_max_size=MAX_REQUEST_BYTES,
    )
    app["config"] = config
    app["service"] = domain
    app["twitch"] = twitch
    app["scheduler"] = scheduler

    app.router.add_get("/health", health)
    app.router.add_put("/v1/guilds/{guild_id}", put_guild)
    app.router.add_get("/v1/guilds/{guild_id}", get_guild)
    app.router.add_post("/v1/members/link/start", start_link)
    app.router.add_get("/oauth/twitch/callback", twitch_callback)
    app.router.add_get("/v1/members/{discord_user_id}", get_member)
    app.router.add_post("/v1/members/{discord_user_id}/leave", leave_member)
    app.router.add_delete("/v1/members/{discord_user_id}", unlink_member)
    app.router.add_post("/v1/events/manual", create_manual_event)
    app.router.add_get("/v1/events/{event_id}", get_event)
    app.router.add_post("/v1/events/{event_id}/live", mark_event_live)
    app.router.add_post("/v1/events/{event_id}/join", join_event)
    app.router.add_post("/v1/events/{event_id}/activity", record_activity)
    app.router.add_post("/v1/events/{event_id}/finish", finish_event)
    app.router.add_post("/v1/events/{event_id}/cancel", cancel_event)
    app.router.add_get("/v1/events/{event_id}/leaderboards", get_leaderboards)
    app.router.add_put("/v1/schedule", put_schedule)
    app.router.add_get("/v1/schedule", get_schedule)
    app.router.add_post("/v1/schedule/tick", tick_schedule)
    app.router.add_get("/v1/outbox", get_outbox)
    app.router.add_post("/v1/outbox/{outbox_id}/ack", acknowledge_outbox)

    async def start_background(_app):
        if config.scheduler_enabled:
            scheduler.start()

    async def stop_background(_app):
        await scheduler.stop()

    app.on_startup.append(start_background)
    app.on_cleanup.append(stop_background)
    return app

