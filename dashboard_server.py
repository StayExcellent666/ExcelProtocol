"""
ExcelProtocol Dashboard Backend
================================
aiohttp server that runs alongside your Discord bot in the same Fly.io app.
Reads from the same SQLite DB at /data/twitch_bot.db.

This file sits in the root of your repo next to main.py (or however your bot
is structured). It gets started by main.py — see the comment at the bottom.
"""

import os
import aiosqlite
from datetime import datetime, timedelta, timezone

from aiohttp import web
from aiohttp_cors import setup as cors_setup, ResourceOptions
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
# DB path matches your fly.toml mount: /data/twitch_bot.db
DB_PATH               = os.getenv("DB_PATH", "/data/twitch_bot.db")
DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI  = os.getenv("DISCORD_REDIRECT_URI", "")
DEV_TOKEN             = os.getenv("DEV_TOKEN", "")   # Set this in Fly.io Secrets
PORT                  = int(os.getenv("DASHBOARD_PORT", 8080))

DISCORD_API = "https://discord.com/api/v10"

# ── DB Helper ─────────────────────────────────────────────────────────────────
async def db_fetch(query: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

async def db_execute(query: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(query, params)
        await db.commit()

# ── Auth Middleware ───────────────────────────────────────────────────────────
@web.middleware
async def auth_middleware(request: web.Request, handler):
    # Public routes — no token needed
    if request.path in ("/health", "/auth/login", "/auth/callback") or request.path.startswith("/app"):
        return await handler(request)

    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not DEV_TOKEN or token != DEV_TOKEN:
        raise web.HTTPUnauthorized(reason="Invalid or missing token")

    return await handler(request)

# ── Health ────────────────────────────────────────────────────────────────────
async def health(request):
    return web.json_response({"status": "ok", "bot": "ExcelProtocol"})

# ── OAuth2 ────────────────────────────────────────────────────────────────────
async def auth_login(request):
    url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify+guilds"
    )
    raise web.HTTPFound(url)

async def auth_callback(request):
    import aiohttp as http_client

    code = request.rel_url.query.get("code")
    if not code:
        raise web.HTTPBadRequest(reason="Missing code param")

    async with http_client.ClientSession() as session:
        token_resp = await session.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": DISCORD_REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token_data = await token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise web.HTTPInternalServerError(reason="Failed to get Discord access token")

        headers = {"Authorization": f"Bearer {access_token}"}
        user_resp   = await session.get(f"{DISCORD_API}/users/@me",        headers=headers)
        guilds_resp = await session.get(f"{DISCORD_API}/users/@me/guilds", headers=headers)
        user   = await user_resp.json()
        guilds = await guilds_resp.json()

    # Only show guilds where the user has Manage Server (bit 0x20)
    managed = [
        {"id": g["id"], "name": g["name"], "icon": g.get("icon")}
        for g in guilds
        if int(g.get("permissions", 0)) & 0x20
    ]

    # TODO: Replace this with a real session/JWT once you wire up the frontend login flow
    return web.json_response({
        "user":   {"id": user["id"], "username": user["username"]},
        "guilds": managed,
        "token":  access_token,
    })

# ── Guilds ────────────────────────────────────────────────────────────────────
async def get_guilds(request):
    rows = await db_fetch("SELECT DISTINCT guild_id FROM monitored_streamers")
    return web.json_response([{"id": r["guild_id"]} for r in rows])

# ── Guild summary (single call for all tabs) ──────────────────────────────────
async def get_guild_summary(request):
    guild_id = request.match_info["guild_id"]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    streamers      = await db_fetch(
        "SELECT id, guild_id, streamer_name AS twitch_username, channel_id FROM monitored_streamers WHERE guild_id = ?",
        (guild_id,)
    )
    reaction_roles = await db_fetch(
        "SELECT message_id, guild_id, channel_id, title, type, only_add, max_roles, roles_json FROM reaction_roles WHERE guild_id = ?",
        (guild_id,)
    )
    notif_log      = await db_fetch(
        """SELECT guild_id, streamer_name AS twitch_username,
                  channel_id, status AS event,
                  sent_at AS timestamp
           FROM notification_log
           WHERE guild_id = ? AND sent_at >= ?
           ORDER BY sent_at DESC LIMIT 100""",
        (guild_id, cutoff),
    )

    return web.json_response({
        "streamers":      streamers,
        "reaction_roles": reaction_roles,
        "notif_log":      notif_log,
        "commands":       COMMANDS,
    })

# ── Streamers ─────────────────────────────────────────────────────────────────
async def get_streamers(request):
    guild_id = request.match_info["guild_id"]
    rows = await db_fetch(
        "SELECT id, guild_id, streamer_name AS twitch_username, channel_id FROM monitored_streamers WHERE guild_id = ?",
        (guild_id,)
    )
    return web.json_response(rows)

async def add_streamer(request):
    guild_id = request.match_info["guild_id"]
    body = await request.json()
    await db_execute(
        "INSERT INTO monitored_streamers (guild_id, streamer_name, channel_id) VALUES (?, ?, ?)",
        (guild_id, body["twitch_username"], body["channel_id"]),
    )
    return web.json_response({"ok": True})

async def delete_streamer(request):
    guild_id = request.match_info["guild_id"]
    username = request.match_info["username"]
    await db_execute(
        "DELETE FROM monitored_streamers WHERE guild_id = ? AND streamer_name = ?",
        (guild_id, username),
    )
    return web.json_response({"ok": True})

# ── Reaction Roles ────────────────────────────────────────────────────────────
async def get_reaction_roles(request):
    guild_id = request.match_info["guild_id"]
    rows = await db_fetch(
        "SELECT message_id, guild_id, channel_id, title, type, only_add, max_roles, roles_json FROM reaction_roles WHERE guild_id = ?",
        (guild_id,)
    )
    return web.json_response(rows)

async def delete_reaction_role(request):
    guild_id = request.match_info["guild_id"]
    role_id  = request.match_info["role_id"]
    await db_execute(
        "DELETE FROM reaction_roles WHERE guild_id = ? AND message_id = ?",
        (guild_id, role_id),
    )
    return web.json_response({"ok": True})

# ── Notification Log ──────────────────────────────────────────────────────────
async def get_notif_log(request):
    guild_id = request.match_info["guild_id"]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    rows = await db_fetch(
        """SELECT guild_id, streamer_name AS twitch_username,
                  channel_id, status AS event,
                  sent_at AS timestamp
           FROM notification_log
           WHERE guild_id = ? AND sent_at >= ?
           ORDER BY sent_at DESC LIMIT 100""",
        (guild_id, cutoff),
    )
    return web.json_response(rows)

# ── Commands (static) ─────────────────────────────────────────────────────────
COMMANDS = [
    {"name": "notiflog",       "description": "View notification audit log",               "usage": "/notiflog",              "category": "Moderation"},
    {"name": "repostlive",     "description": "Repost a live notification for a streamer", "usage": "/repostlive [username]", "category": "Streaming"},
    {"name": "cmd",            "description": "Manage custom Twitch chat commands",         "usage": "/cmd",                   "category": "Twitch"},
    {"name": "addstreamer",    "description": "Add a Twitch streamer to track",             "usage": "/addstreamer [u] [ch]",  "category": "Streaming"},
    {"name": "removestreamer", "description": "Remove a tracked streamer",                  "usage": "/removestreamer [u]",    "category": "Streaming"},
    {"name": "streamers",      "description": "List all tracked streamers",                 "usage": "/streamers",             "category": "Streaming"},
]

async def get_commands(request):
    return web.json_response(COMMANDS)

# ── App Factory ───────────────────────────────────────────────────────────────
def create_dashboard_app():
    app = web.Application(middlewares=[auth_middleware])

    app.router.add_get("/health",                                           health)
    app.router.add_get("/auth/login",                                       auth_login)
    app.router.add_get("/auth/callback",                                    auth_callback)

    app.router.add_get("/api/guilds",                                       get_guilds)
    app.router.add_get("/api/guild/{guild_id}",                             get_guild_summary)

    app.router.add_get   ("/api/guild/{guild_id}/streamers",                get_streamers)
    app.router.add_post  ("/api/guild/{guild_id}/streamers",                add_streamer)
    app.router.add_delete("/api/guild/{guild_id}/streamers/{username}",     delete_streamer)

    app.router.add_get   ("/api/guild/{guild_id}/reaction-roles",           get_reaction_roles)
    app.router.add_delete("/api/guild/{guild_id}/reaction-roles/{role_id}", delete_reaction_role)

    app.router.add_get("/api/guild/{guild_id}/notiflog",                    get_notif_log)
    app.router.add_get("/api/commands",                                     get_commands)

    # Serve the built React frontend from dashboard/dist/
    dist_path = os.path.join(os.path.dirname(__file__), "dashboard", "dist")
    if os.path.exists(dist_path):
        app.router.add_static("/app", path=dist_path, name="frontend")

    cors = cors_setup(app, defaults={
        "*": ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods=["GET", "POST", "DELETE", "PATCH", "OPTIONS"],
        )
    })
    for route in list(app.router.routes()):
        try:
            cors.add(route)
        except Exception:
            pass

    return app


# ── How to wire this into your existing main.py ───────────────────────────────
#
# Add these lines to your main.py so the dashboard starts alongside the bot:
#
#   from aiohttp import web
#   from dashboard_server import create_dashboard_app
#
#   async def start_dashboard():
#       app = create_dashboard_app()
#       runner = web.AppRunner(app)
#       await runner.setup()
#       site = web.TCPSite(runner, "0.0.0.0", 8080)
#       await site.start()
#       print("Dashboard running on port 8080")
#
# Then call start_dashboard() before or alongside your bot.start() call.
# If your bot uses asyncio.run(main()), just await start_dashboard() inside main().
