"""
ExcelProtocol Dashboard Backend
================================
aiohttp server that runs alongside your Discord bot in the same Fly.io app.
Reads from the same SQLite DB at /data/twitch_bot.db.
"""

import os
import json
import secrets
import aiosqlite
import aiohttp as http_client
from datetime import datetime, timedelta, timezone

from aiohttp import web
from aiohttp_cors import setup as cors_setup, ResourceOptions
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH               = os.getenv("DB_PATH", "/data/twitch_bot.db")
DISCORD_TOKEN         = os.getenv("DISCORD_TOKEN", "")
DISCORD_CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI  = os.getenv("DISCORD_REDIRECT_URI", "")
DEV_TOKEN             = os.getenv("DEV_TOKEN", "")
PORT                  = int(os.getenv("DASHBOARD_PORT", 8080))
DISCORD_API           = "https://discord.com/api/v10"

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

# ── Discord API Helper ────────────────────────────────────────────────────────
async def discord_get(path: str, token: str, use_bot: bool = False):
    prefix = "Bot" if use_bot else "Bearer"
    async with http_client.ClientSession() as session:
        async with session.get(
            f"{DISCORD_API}{path}",
            headers={"Authorization": f"{prefix} {token}"}
        ) as resp:
            return await resp.json()

# ── Session Store ─────────────────────────────────────────────────────────────
_sessions: dict = {}

def get_session(request: web.Request) -> dict | None:
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if DEV_TOKEN and token == DEV_TOKEN:
        return {"dev": True}
    return _sessions.get(token)

# ── Auth Middleware ───────────────────────────────────────────────────────────
@web.middleware
async def auth_middleware(request: web.Request, handler):
    public = ("/health", "/auth/login", "/auth/callback")
    if request.path in public or request.path.startswith("/app"):
        return await handler(request)
    session = get_session(request)
    if not session:
        raise web.HTTPUnauthorized(reason="Invalid or missing token")
    request["session"] = session
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
    code = request.rel_url.query.get("code")
    if not code:
        raise web.HTTPBadRequest(reason="Missing code")

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
            raise web.HTTPInternalServerError(reason="Failed to get access token")

        headers = {"Authorization": f"Bearer {access_token}"}
        user_resp   = await session.get(f"{DISCORD_API}/users/@me",        headers=headers)
        guilds_resp = await session.get(f"{DISCORD_API}/users/@me/guilds", headers=headers)
        user   = await user_resp.json()
        guilds = await guilds_resp.json()

    managed = [
        {"id": g["id"], "name": g["name"], "icon": g.get("icon")}
        for g in guilds
        if int(g.get("permissions", 0)) & 0x20
    ]

    session_token = secrets.token_hex(32)
    _sessions[session_token] = {
        "user_id":  user["id"],
        "username": user["username"],
        "guilds":   managed,
    }

    raise web.HTTPFound(f"/app/?token={session_token}")

async def auth_me(request):
    session = request["session"]
    if session.get("dev"):
        rows = await db_fetch("SELECT DISTINCT guild_id FROM monitored_streamers")
        guilds = []
        for r in rows:
            name = await get_guild_name(str(r["guild_id"]))
            guilds.append({"id": str(r["guild_id"]), "name": name})
        return web.json_response({"username": "Dev", "guilds": guilds})
    return web.json_response({"username": session["username"], "guilds": session["guilds"]})

# ── Guild name lookup via bot token ───────────────────────────────────────────
_guild_name_cache: dict = {}

async def get_guild_name(guild_id: str) -> str:
    if guild_id in _guild_name_cache:
        return _guild_name_cache[guild_id]
    try:
        data = await discord_get(f"/guilds/{guild_id}", DISCORD_TOKEN, use_bot=True)
        name = data.get("name", guild_id)
        _guild_name_cache[guild_id] = name
        return name
    except Exception:
        return guild_id

# ── Guilds ────────────────────────────────────────────────────────────────────
async def get_guilds(request):
    session = request["session"]
    if not session.get("dev") and "guilds" in session:
        return web.json_response(session["guilds"])
    rows = await db_fetch("SELECT DISTINCT guild_id FROM monitored_streamers")
    guilds = []
    for r in rows:
        gid = str(r["guild_id"])
        name = await get_guild_name(gid)
        guilds.append({"id": gid, "name": name})
    return web.json_response(guilds)

# ── Guild Summary ─────────────────────────────────────────────────────────────
async def get_guild_summary(request):
    guild_id = request.match_info["guild_id"]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    streamers = await db_fetch(
        "SELECT id, guild_id, streamer_name AS twitch_username, channel_id, custom_channel_id FROM monitored_streamers WHERE guild_id = ?",
        (guild_id,)
    )
    reaction_roles = await db_fetch(
        "SELECT message_id, guild_id, channel_id, title, type, only_add, max_roles, roles_json FROM reaction_roles WHERE guild_id = ?",
        (guild_id,)
    )
    for rr in reaction_roles:
        try:
            rr["roles"] = json.loads(rr.get("roles_json", "[]"))
        except Exception:
            rr["roles"] = []

    notif_log = await db_fetch(
        """SELECT guild_id, streamer_name AS twitch_username,
                  channel_id, status AS event, sent_at AS timestamp
           FROM notification_log
           WHERE guild_id = ? AND sent_at >= ?
           ORDER BY sent_at DESC LIMIT 100""",
        (guild_id, cutoff),
    )
    return web.json_response({
        "streamers": streamers, "reaction_roles": reaction_roles,
        "notif_log": notif_log, "commands": COMMANDS,
    })

# ── Streamers ─────────────────────────────────────────────────────────────────
async def get_streamers(request):
    guild_id = request.match_info["guild_id"]
    rows = await db_fetch(
        "SELECT id, guild_id, streamer_name AS twitch_username, channel_id, custom_channel_id FROM monitored_streamers WHERE guild_id = ?",
        (guild_id,)
    )
    return web.json_response(rows)

async def add_streamer(request):
    guild_id = request.match_info["guild_id"]
    body = await request.json()
    twitch_username = body.get("twitch_username", "").lower().strip()
    channel_id      = body.get("channel_id")
    if not twitch_username or not channel_id:
        raise web.HTTPBadRequest(reason="twitch_username and channel_id are required")
    try:
        await db_execute(
            "INSERT INTO monitored_streamers (guild_id, streamer_name, channel_id) VALUES (?, ?, ?)",
            (guild_id, twitch_username, channel_id),
        )
    except Exception as e:
        if "UNIQUE" in str(e):
            raise web.HTTPConflict(reason="Streamer already tracked")
        raise
    return web.json_response({"ok": True})

async def delete_streamer(request):
    guild_id = request.match_info["guild_id"]
    username = request.match_info["username"]
    await db_execute(
        "DELETE FROM monitored_streamers WHERE guild_id = ? AND streamer_name = ?",
        (guild_id, username.lower()),
    )
    return web.json_response({"ok": True})

# ── Reaction Roles ────────────────────────────────────────────────────────────
async def get_reaction_roles(request):
    guild_id = request.match_info["guild_id"]
    rows = await db_fetch(
        "SELECT message_id, guild_id, channel_id, title, type, only_add, max_roles, roles_json FROM reaction_roles WHERE guild_id = ?",
        (guild_id,)
    )
    for r in rows:
        try:
            r["roles"] = json.loads(r.get("roles_json", "[]"))
        except Exception:
            r["roles"] = []
    return web.json_response(rows)

async def delete_reaction_role(request):
    guild_id   = request.match_info["guild_id"]
    message_id = request.match_info["role_id"]
    await db_execute(
        "DELETE FROM reaction_roles WHERE guild_id = ? AND message_id = ?",
        (guild_id, message_id),
    )
    return web.json_response({"ok": True})

# ── Notification Log ──────────────────────────────────────────────────────────
async def get_notif_log(request):
    guild_id = request.match_info["guild_id"]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    rows = await db_fetch(
        """SELECT guild_id, streamer_name AS twitch_username,
                  channel_id, status AS event, sent_at AS timestamp
           FROM notification_log
           WHERE guild_id = ? AND sent_at >= ?
           ORDER BY sent_at DESC LIMIT 100""",
        (guild_id, cutoff),
    )
    return web.json_response(rows)

# ── Commands ──────────────────────────────────────────────────────────────────
COMMANDS = [
    {"name": "notiflog",       "description": "View notification audit log",               "usage": "/notiflog",              "category": "Moderation"},
    {"name": "repostlive",     "description": "Repost a live notification for a streamer", "usage": "/repostlive [username]", "category": "Streaming"},
    {"name": "cmd",            "description": "Manage custom Twitch chat commands",         "usage": "/cmd",                   "category": "Twitch"},
    {"name": "addstreamer",    "description": "Add a Twitch streamer to track",             "usage": "/addstreamer [u] [ch]",  "category": "Streaming"},
    {"name": "removestreamer", "description": "Remove a tracked streamer",                  "usage": "/removestreamer [u]",    "category": "Streaming"},
    {"name": "streamers",      "description": "List all tracked streamers",                 "usage": "/streamers",             "category": "Streaming"},
    {"name": "leaderboard",    "description": "View top streamers this month",              "usage": "/leaderboard",           "category": "Streaming"},
    {"name": "setcolor",       "description": "Set the embed colour for this server",       "usage": "/setcolor [color]",      "category": "Moderation"},
    {"name": "birthday",       "description": "Set your birthday",                          "usage": "/birthday",              "category": "Fun"},
    {"name": "birthdaylist",   "description": "List all birthdays in this server",          "usage": "/birthdaylist",          "category": "Fun"},
]

async def get_commands(request):
    return web.json_response(COMMANDS)

# ── App Factory ───────────────────────────────────────────────────────────────
def create_dashboard_app():
    app = web.Application(middlewares=[auth_middleware])

    app.router.add_get("/health",        health)
    app.router.add_get("/auth/login",    auth_login)
    app.router.add_get("/auth/callback", auth_callback)
    app.router.add_get("/api/me",        auth_me)
    app.router.add_get("/api/guilds",    get_guilds)
    app.router.add_get("/api/guild/{guild_id}", get_guild_summary)

    app.router.add_get   ("/api/guild/{guild_id}/streamers",              get_streamers)
    app.router.add_post  ("/api/guild/{guild_id}/streamers",              add_streamer)
    app.router.add_delete("/api/guild/{guild_id}/streamers/{username}",   delete_streamer)

    app.router.add_get   ("/api/guild/{guild_id}/reaction-roles",         get_reaction_roles)
    app.router.add_delete("/api/guild/{guild_id}/reaction-roles/{role_id}", delete_reaction_role)

    app.router.add_get("/api/guild/{guild_id}/notiflog", get_notif_log)
    app.router.add_get("/api/commands",                  get_commands)

    dist_path = os.path.join(os.path.dirname(__file__), "dashboard", "dist")
    if os.path.exists(dist_path):
        async def serve_index(request):
            return web.FileResponse(os.path.join(dist_path, "index.html"))
        app.router.add_get("/app",   serve_index)
        app.router.add_get("/app/",  serve_index)
        app.router.add_static("/app/assets", path=os.path.join(dist_path, "assets"), name="frontend_assets")

    cors = cors_setup(app, defaults={
        "*": ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*",
                             allow_methods=["GET", "POST", "DELETE", "PATCH", "OPTIONS"])
    })
    for route in list(app.router.routes()):
        try:
            cors.add(route)
        except Exception:
            pass

    return app
