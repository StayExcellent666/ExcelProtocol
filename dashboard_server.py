"""
ExcelProtocol Dashboard Backend
================================
aiohttp server that runs alongside your Discord bot in the same Fly.io app.
Reads from the same SQLite DB at /data/twitch_bot.db.
Enriches data with Discord + Twitch API calls.
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
TWITCH_CLIENT_ID      = os.getenv("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET  = os.getenv("TWITCH_CLIENT_SECRET", "")
BOT_OWNER_ID          = os.getenv("BOT_OWNER_ID", "")
DEV_TOKEN             = os.getenv("DEV_TOKEN", "")
PORT                  = int(os.getenv("DASHBOARD_PORT", 8080))
DISCORD_API           = "https://discord.com/api/v10"

# ── DB Helper ─────────────────────────────────────────────────────────────────
async def db_fetch(query: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

async def db_execute(query: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(query, params)
        await db.commit()

# ── Discord API Helper ────────────────────────────────────────────────────────
_discord_cache: dict = {}

async def discord_get(path: str, token: str = None, use_bot: bool = True) -> dict:
    cache_key = path
    if cache_key in _discord_cache:
        return _discord_cache[cache_key]
    t = token or DISCORD_TOKEN
    prefix = "Bot" if use_bot else "Bearer"
    async with http_client.ClientSession() as session:
        async with session.get(
            f"{DISCORD_API}{path}",
            headers={"Authorization": f"{prefix} {t}"}
        ) as resp:
            data = await resp.json()
            if resp.status == 200:
                _discord_cache[cache_key] = data
            return data

async def get_channel_name(channel_id: str) -> str:
    try:
        data = await discord_get(f"/channels/{channel_id}")
        return f"#{data.get('name', channel_id)}"
    except Exception:
        return channel_id

async def get_guild_roles(guild_id: str) -> dict:
    """Returns {role_id: {name, color}} for all roles in a guild."""
    try:
        roles = await discord_get(f"/guilds/{guild_id}/roles")
        return {str(r["id"]): {"name": r["name"], "color": r["color"]} for r in roles}
    except Exception:
        return {}

async def get_guild_info(guild_id: str) -> dict:
    try:
        return await discord_get(f"/guilds/{guild_id}?with_counts=true")
    except Exception:
        return {}

async def get_guild_channels(guild_id: str) -> list:
    """Return list of text channels for a guild: [{id, name, position}]"""
    # Don't cache channels — they can change
    try:
        async with http_client.ClientSession() as session:
            async with session.get(
                f"{DISCORD_API}/guilds/{guild_id}/channels",
                headers={"Authorization": f"Bot {DISCORD_TOKEN}"}
            ) as resp:
                if resp.status != 200:
                    return []
                channels = await resp.json()
                # Type 0 = text channel, type 4 = category
                text = [
                    {"id": str(c["id"]), "name": c["name"], "position": c.get("position", 0), "parent_id": str(c.get("parent_id") or "")}
                    for c in channels if c.get("type") == 0
                ]
                return sorted(text, key=lambda c: c["position"])
    except Exception:
        return []

# ── Twitch API Helper ─────────────────────────────────────────────────────────
_twitch_token: dict = {"token": None, "expires_at": None}
_twitch_cache: dict = {}

async def get_twitch_token() -> str:
    now = datetime.utcnow()
    if _twitch_token["token"] and _twitch_token["expires_at"] and now < _twitch_token["expires_at"] - timedelta(seconds=60):
        return _twitch_token["token"]
    async with http_client.ClientSession() as session:
        async with session.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "client_credentials",
            }
        ) as resp:
            data = await resp.json()
            _twitch_token["token"] = data["access_token"]
            _twitch_token["expires_at"] = now + timedelta(seconds=data["expires_in"])
            return _twitch_token["token"]

async def get_twitch_users(usernames: list) -> dict:
    """Returns {username_lower: {display_name, profile_image_url, description}}"""
    if not usernames:
        return {}
    missing = [u for u in usernames if u.lower() not in _twitch_cache]
    if missing:
        try:
            token = await get_twitch_token()
            params = [("login", u.lower()) for u in missing]
            async with http_client.ClientSession() as session:
                async with session.get(
                    "https://api.twitch.tv/helix/users",
                    headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"},
                    params=params,
                ) as resp:
                    data = await resp.json()
                    for u in data.get("data", []):
                        _twitch_cache[u["login"].lower()] = {
                            "display_name":      u.get("display_name", u["login"]),
                            "profile_image_url": u.get("profile_image_url", ""),
                            "description":       u.get("description", ""),
                            "broadcaster_type":  u.get("broadcaster_type", ""),
                        }
        except Exception:
            pass
    return {u: _twitch_cache.get(u.lower(), {}) for u in usernames}

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
        "avatar":   user.get("avatar"),
        "guilds":   managed,
    }
    raise web.HTTPFound(f"/app/?token={session_token}")

async def auth_me(request):
    session = request["session"]
    if session.get("dev"):
        rows = await db_fetch("SELECT DISTINCT guild_id FROM monitored_streamers")
        guilds = []
        for r in rows:
            info = await get_guild_info(str(r["guild_id"]))
            guilds.append({
                "id":   str(r["guild_id"]),
                "name": info.get("name", str(r["guild_id"])),
                "icon": info.get("icon"),
                "approximate_member_count": info.get("approximate_member_count"),
            })
        return web.json_response({"username": "Dev", "avatar": None, "guilds": guilds})
    session_guilds = session.get("guilds", [])
    enriched = []
    for g in session_guilds:
        info = await get_guild_info(g["id"])
        enriched.append({
            **g,
            "approximate_member_count": info.get("approximate_member_count"),
        })
    return web.json_response({
        "user_id":  session["user_id"],
        "username": session["username"],
        "avatar":   session.get("avatar"),
        "guilds":   enriched,
    })

# ── Guilds ────────────────────────────────────────────────────────────────────
async def get_guilds(request):
    session = request["session"]
    if not session.get("dev") and "guilds" in session:
        return web.json_response(session["guilds"])
    rows = await db_fetch("SELECT DISTINCT guild_id FROM monitored_streamers")
    guilds = []
    for r in rows:
        info = await get_guild_info(str(r["guild_id"]))
        guilds.append({
            "id":   str(r["guild_id"]),
            "name": info.get("name", str(r["guild_id"])),
            "icon": info.get("icon"),
        })
    return web.json_response(guilds)

# ── Guild Summary ─────────────────────────────────────────────────────────────
async def get_guild_summary(request):
    guild_id = request.match_info["guild_id"]
    cutoff   = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    streamers_raw = await db_fetch(
        "SELECT id, guild_id, streamer_name AS twitch_username, channel_id, custom_channel_id FROM monitored_streamers WHERE guild_id = ?",
        (guild_id,)
    )
    reaction_roles_raw = await db_fetch(
        "SELECT message_id, guild_id, channel_id, title, type, only_add, max_roles, roles_json FROM reaction_roles WHERE guild_id = ?",
        (guild_id,)
    )
    notif_log = await db_fetch(
        """SELECT guild_id, streamer_name AS twitch_username, channel_id, status AS event, sent_at AS timestamp
           FROM notification_log WHERE guild_id = ? AND sent_at >= ?
           ORDER BY sent_at DESC LIMIT 100""",
        (guild_id, cutoff),
    )

    # Enrich streamers with Twitch data + channel names
    usernames = [s["twitch_username"] for s in streamers_raw]
    twitch_data = await get_twitch_users(usernames)
    eff_channel_ids = list({str(s.get("custom_channel_id") or s["channel_id"]) for s in streamers_raw})
    for rr in reaction_roles_raw:
        eff_channel_ids.append(str(rr["channel_id"]))

    channel_names = {}
    for cid in set(eff_channel_ids):
        channel_names[cid] = await get_channel_name(cid)

    streamers = []
    for s in streamers_raw:
        tw = twitch_data.get(s["twitch_username"].lower(), {})
        eff_ch = str(s.get("custom_channel_id") or s["channel_id"])
        streamers.append({
            **s,
            "display_name":      tw.get("display_name", s["twitch_username"]),
            "profile_image_url": tw.get("profile_image_url", ""),
            "description":       tw.get("description", ""),
            "channel_name":      channel_names.get(str(s["channel_id"]), str(s["channel_id"])),
            "effective_channel_name": channel_names.get(eff_ch, eff_ch),
        })

    # Enrich reaction roles with role names + colors from Discord
    guild_roles = await get_guild_roles(guild_id)
    reaction_roles = []
    for rr in reaction_roles_raw:
        try:
            roles = json.loads(rr.get("roles_json", "[]"))
        except Exception:
            roles = []
        enriched_roles = []
        for r in roles:
            role_id = str(r.get("role_id", ""))
            role_info = guild_roles.get(role_id, {})
            enriched_roles.append({
                **r,
                "role_name":  role_info.get("name", role_id),
                "role_color": role_info.get("color", 0),
            })
        reaction_roles.append({
            **rr,
            "roles":        enriched_roles,
            "channel_name": channel_names.get(str(rr["channel_id"]), str(rr["channel_id"])),
        })

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
        "SELECT id, guild_id, streamer_name AS twitch_username, channel_id, custom_channel_id FROM monitored_streamers WHERE guild_id = ?",
        (guild_id,)
    )
    usernames = [r["twitch_username"] for r in rows]
    twitch_data = await get_twitch_users(usernames)
    result = []
    for r in rows:
        tw = twitch_data.get(r["twitch_username"].lower(), {})
        eff_ch = str(r.get("custom_channel_id") or r["channel_id"])
        ch_name = await get_channel_name(eff_ch)
        result.append({
            **r,
            "display_name":      tw.get("display_name", r["twitch_username"]),
            "profile_image_url": tw.get("profile_image_url", ""),
            "channel_name":      ch_name,
        })
    return web.json_response(result)

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
    guild_roles = await get_guild_roles(guild_id)
    result = []
    for rr in rows:
        try:
            roles = json.loads(rr.get("roles_json", "[]"))
        except Exception:
            roles = []
        enriched = []
        for r in roles:
            role_id  = str(r.get("role_id", ""))
            role_info = guild_roles.get(role_id, {})
            enriched.append({**r, "role_name": role_info.get("name", role_id), "role_color": role_info.get("color", 0)})
        ch_name = await get_channel_name(str(rr["channel_id"]))
        result.append({**rr, "roles": enriched, "channel_name": ch_name})
    return web.json_response(result)

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
    cutoff   = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    rows = await db_fetch(
        """SELECT guild_id, streamer_name AS twitch_username, channel_id, status AS event, sent_at AS timestamp
           FROM notification_log WHERE guild_id = ? AND sent_at >= ?
           ORDER BY sent_at DESC LIMIT 100""",
        (guild_id, cutoff),
    )
    usernames = list({r["twitch_username"] for r in rows})
    twitch_data = await get_twitch_users(usernames)
    for r in rows:
        tw = twitch_data.get(r["twitch_username"].lower(), {})
        r["profile_image_url"] = tw.get("profile_image_url", "")
        r["display_name"]      = tw.get("display_name", r["twitch_username"])
        r["channel_name"]      = await get_channel_name(str(r["channel_id"]))
    return web.json_response(rows)

# ── Guild Channels ───────────────────────────────────────────────────────────
async def get_channels(request):
    guild_id = request.match_info["guild_id"]
    channels = await get_guild_channels(guild_id)
    # Also get the default notification channel from server_settings
    rows = await db_fetch("SELECT notification_channel_id FROM server_settings WHERE guild_id = ?", (guild_id,))
    default_channel_id = str(rows[0]["notification_channel_id"]) if rows else None
    return web.json_response({"channels": channels, "default_channel_id": default_channel_id})

# ── Edit Streamer ─────────────────────────────────────────────────────────────
async def edit_streamer(request):
    guild_id = request.match_info["guild_id"]
    username = request.match_info["username"]
    body = await request.json()
    channel_id = body.get("channel_id")
    if not channel_id:
        raise web.HTTPBadRequest(reason="channel_id is required")
    await db_execute(
        "UPDATE monitored_streamers SET custom_channel_id = ? WHERE guild_id = ? AND streamer_name = ?",
        (channel_id, guild_id, username.lower()),
    )
    return web.json_response({"ok": True})

# ── Edit Reaction Role Panel ──────────────────────────────────────────────────
async def edit_reaction_role(request):
    guild_id   = request.match_info["guild_id"]
    message_id = request.match_info["role_id"]
    body = await request.json()
    fields = []
    params = []
    if "title" in body:
        fields.append("title = ?"); params.append(body["title"])
    if "type" in body:
        fields.append("type = ?"); params.append(body["type"])
    if "only_add" in body:
        fields.append("only_add = ?"); params.append(1 if body["only_add"] else 0)
    if "max_roles" in body:
        fields.append("max_roles = ?"); params.append(body["max_roles"])
    if not fields:
        raise web.HTTPBadRequest(reason="Nothing to update")
    params += [guild_id, message_id]
    await db_execute(
        f"UPDATE reaction_roles SET {', '.join(fields)} WHERE guild_id = ? AND message_id = ?",
        tuple(params),
    )
    return web.json_response({"ok": True})

# ── Suggestions ──────────────────────────────────────────────────────────────
async def post_suggestion(request):
    """Receive a suggestion from the dashboard and DM it to the bot owner."""
    session = request["session"]
    body = await request.json()
    text = body.get("text", "").strip()

    if not text:
        raise web.HTTPBadRequest(reason="Suggestion text is required")
    if len(text) > 1000:
        raise web.HTTPBadRequest(reason="Suggestion must be under 1000 characters")
    if not BOT_OWNER_ID or not DISCORD_TOKEN:
        raise web.HTTPInternalServerError(reason="BOT_OWNER_ID or DISCORD_TOKEN not configured")

    sender = "Dev (dashboard)" if session.get("dev") else session.get("username", "Unknown")
    sender_id = None if session.get("dev") else session.get("user_id")

    async with http_client.ClientSession() as s:
        dm_resp = await s.post(
            f"{DISCORD_API}/users/@me/channels",
            headers={"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"},
            json={"recipient_id": BOT_OWNER_ID},
        )
        dm_data = await dm_resp.json()
        dm_channel_id = dm_data.get("id")
        if not dm_channel_id:
            raise web.HTTPInternalServerError(reason="Failed to open DM channel")

        embed = {
            "title": "\U0001f4a1 New Dashboard Suggestion",
            "description": text,
            "color": 0x5865F2,
            "fields": [
                {"name": "From", "value": f"{sender}{f' (`{sender_id}`)' if sender_id else ''}", "inline": True},
                {"name": "Via",  "value": "ExcelProtocol Dashboard", "inline": True},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "excelprotocol.fly.dev/app"},
        }
        msg_resp = await s.post(
            f"{DISCORD_API}/channels/{dm_channel_id}/messages",
            headers={"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"},
            json={"embeds": [embed]},
        )
        if msg_resp.status not in (200, 201):
            raise web.HTTPInternalServerError(reason="Failed to send DM")

    return web.json_response({"ok": True})

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

    app.router.add_get("/api/guild/{guild_id}/channels",                   get_channels)
    app.router.add_get("/api/guild/{guild_id}/notiflog", get_notif_log)
    app.router.add_patch("/api/guild/{guild_id}/streamers/{username}",      edit_streamer)
    app.router.add_patch("/api/guild/{guild_id}/reaction-roles/{role_id}",  edit_reaction_role)
    app.router.add_get("/api/commands",                  get_commands)
    app.router.add_post("/api/suggest",                    post_suggestion)

    dist_path = os.path.join(os.path.dirname(__file__), "dashboard", "dist")
    if os.path.exists(dist_path):
        async def serve_index(request):
            return web.FileResponse(os.path.join(dist_path, "index.html"))
        app.router.add_get("/app",  serve_index)
        app.router.add_get("/app/", serve_index)
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
