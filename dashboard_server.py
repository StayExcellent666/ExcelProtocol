"""
ExcelProtocol Dashboard Backend
================================
aiohttp server that runs alongside your Discord bot in the same Fly.io app.
Reads from the same SQLite DB at /data/twitch_bot.db.
Enriches data with Discord + Twitch API calls.

"""

import os
import asyncio
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
TWITCH_REDIRECT_URI   = os.getenv("TWITCH_REDIRECT_URI", "https://excelprotocol.fly.dev/auth/twitch/callback")
TWITCH_API            = "https://api.twitch.tv/helix"

# WebSocket connections for overlays: {guild_id: set of ws}
_overlay_connections: dict = {}

async def push_play_to_overlay(twitch_channel: str, video_url: str, requester: str):
    """Push a !play event to all overlay WebSockets for guilds linked to this Twitch channel.
    Returns True if pushed to at least one overlay, False otherwise."""
    import json as _json
    if not _bot_ref:
        return False
    guilds = _bot_ref.db.get_guilds_for_twitch_channel(twitch_channel)
    if not guilds:
        return False
    payload = _json.dumps({"type": "play", "video_url": video_url, "volume": 1.0, "redeemer": requester})
    pushed = False
    for g in guilds:
        gid = str(g['guild_id'])
        conns = _overlay_connections.get(gid, set())
        dead = set()
        for ws in conns:
            try:
                await ws.send_str(payload)
                pushed = True
            except Exception:
                dead.add(ws)
        if dead:
            conns.difference_update(dead)
    return pushed

async def push_stop_to_overlay(twitch_channel: str):
    """Push a stop event to all overlay WebSockets for guilds linked to this Twitch channel."""
    import json as _json
    if not _bot_ref:
        return
    guilds = _bot_ref.db.get_guilds_for_twitch_channel(twitch_channel)
    payload = _json.dumps({"type": "stop"})
    for g in guilds:
        gid = str(g['guild_id'])
        conns = _overlay_connections.get(gid, set())
        dead = set()
        for ws in conns:
            try:
                await ws.send_str(payload)
            except Exception:
                dead.add(ws)
        if dead:
            conns.difference_update(dead)

async def push_skip_to_overlay(twitch_channel: str):
    """Skip the current video — overlay will play the next queued item if any."""
    import json as _json
    if not _bot_ref:
        return False
    guilds = _bot_ref.db.get_guilds_for_twitch_channel(twitch_channel)
    payload = _json.dumps({"type": "skip"})
    pushed = False
    for g in guilds:
        gid = str(g['guild_id'])
        conns = _overlay_connections.get(gid, set())
        dead = set()
        for ws in conns:
            try:
                await ws.send_str(payload)
                pushed = True
            except Exception:
                dead.add(ws)
        if dead:
            conns.difference_update(dead)
    return pushed

# EventSub message dedup: {message_id: received_at} — Twitch recommends deduping by message ID
_eventsub_seen: dict = {}

# Bot reference — set by create_dashboard_app() so we can reload views
_bot_ref = None

# ── DB Helper ─────────────────────────────────────────────────────────────────
async def db_fetch(query: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=30000")
        db.row_factory = aiosqlite.Row
        async with db.execute(query, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

async def db_execute(query: str, params: tuple = ()):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=30000")
        await db.execute(query, params)
        await db.commit()

# ── Shared HTTP Session ───────────────────────────────────────────────────────
_http_session: http_client.ClientSession | None = None

def get_http_session() -> http_client.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = http_client.ClientSession()
    return _http_session

# ── Discord API Helper ────────────────────────────────────────────────────────
_discord_cache: dict = {}

async def discord_get(path: str, token: str = None, use_bot: bool = True) -> dict:
    cache_key = path
    if cache_key in _discord_cache:
        return _discord_cache[cache_key]
    t = token or DISCORD_TOKEN
    prefix = "Bot" if use_bot else "Bearer"
    session = get_http_session()
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
    """Returns {role_id: {name, color}} for all roles in a guild. Not cached — roles can be renamed."""
    try:
        session = get_http_session()
        async with session.get(
            f"{DISCORD_API}/guilds/{guild_id}/roles",
            headers={"Authorization": f"Bot {DISCORD_TOKEN}"}
        ) as resp:
            if resp.status != 200:
                return {}
            roles = await resp.json()
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
    try:
        session = get_http_session()
        async with session.get(
            f"{DISCORD_API}/guilds/{guild_id}/channels",
            headers={"Authorization": f"Bot {DISCORD_TOKEN}"}
        ) as resp:
            if resp.status != 200:
                return []
            channels = await resp.json()
            text = [
                {"id": str(c["id"]), "name": c["name"], "position": c.get("position", 0), "parent_id": str(c.get("parent_id") or "")}
                for c in channels if c.get("type") == 0
            ]
            return sorted(text, key=lambda c: c["position"])
    except Exception:
        return []

async def get_guild_voice_channels(guild_id: str) -> list:
    """Return list of voice channels for a guild: [{id, name, position}]"""
    try:
        session = get_http_session()
        async with session.get(
            f"{DISCORD_API}/guilds/{guild_id}/channels",
            headers={"Authorization": f"Bot {DISCORD_TOKEN}"}
        ) as resp:
            if resp.status != 200:
                return []
            channels = await resp.json()
            voice = [
                {"id": str(c["id"]), "name": c["name"], "position": c.get("position", 0)}
                for c in channels if c.get("type") == 2
            ]
            return sorted(voice, key=lambda c: c["position"])
    except Exception:
        return []

# ── Twitch API Helper ─────────────────────────────────────────────────────────
_twitch_token: dict = {"token": None, "expires_at": None}
_twitch_cache: dict = {}

async def get_twitch_token() -> str:
    now = datetime.utcnow()
    if _twitch_token["token"] and _twitch_token["expires_at"] and now < _twitch_token["expires_at"] - timedelta(seconds=60):
        return _twitch_token["token"]
    session = get_http_session()
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
            session = get_http_session()
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
_SESSION_TTL_SECONDS = 86400 * 7  # 7 days

def _prune_sessions():
    """Remove sessions older than TTL. Called on each session lookup."""
    cutoff = datetime.now(timezone.utc).timestamp() - _SESSION_TTL_SECONDS
    stale = [k for k, v in list(_sessions.items()) if v.get("_created_at", 0) < cutoff]
    for k in stale:
        del _sessions[k]

def get_session(request: web.Request) -> dict | None:
    # Try cookie first (new secure method)
    token = request.cookies.get("ep_session", "")
    # Fall back to Authorization header for API clients
    if not token:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        return None
    # DEV_TOKEN is only for internal server use — never expose to browser clients
    if DEV_TOKEN and token == DEV_TOKEN:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return None
        return {"dev": True}
    _prune_sessions()
    return _sessions.get(token)

# ── Error Logging Middleware ──────────────────────────────────────────────────
@web.middleware
async def error_logging_middleware(request: web.Request, handler):
    """Catch unhandled 500s and log them to the bot's Discord log channel."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise  # Let normal HTTP errors (400, 401, 403, 404) pass through untouched
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        tb_trimmed = tb[-2000:] if len(tb) > 2000 else tb
        logger.error(f"Unhandled dashboard error on {request.method} {request.path}: {e}\n{tb}")
        if _bot_ref:
            try:
                await _bot_ref.log_to_channel(
                    "🌐", "Dashboard 500 Error",
                    f"**Route:** `{request.method} {request.path}`\n"
                    f"**Error:** `{type(e).__name__}: {str(e)[:200]}`\n\n"
                    f"```python\n{tb_trimmed}\n```",
                    color=0xFF4444
                )
            except Exception as log_err:
                logger.error(f"Failed to log dashboard error to Discord: {log_err}")
        raise web.HTTPInternalServerError(reason="Internal server error")

# ── Auth Middleware ───────────────────────────────────────────────────────────
def _session_can_access_guild(session: dict, guild_id: str) -> bool:
    """Check the session has access to the requested guild."""
    if session.get("dev"):
        return True  # Dev token has full access — only used server-side/internally
    guilds = session.get("guilds", [])
    return any(str(g["id"]) == str(guild_id) for g in guilds)

@web.middleware
async def auth_middleware(request: web.Request, handler):
    public = ("/health", "/", "/terms", "/privacy", "/auth/login", "/auth/callback", "/auth/logout", "/auth/twitch/callback", "/api/eventsub/callback")
    if request.path in public or request.path.startswith("/app") or request.path.startswith("/overlay") or request.path.startswith("/auth/twitch/login") or request.path.startswith("/companion"):
        return await handler(request)

    session = get_session(request)
    if not session:
        raise web.HTTPUnauthorized(reason="Invalid or missing token")

    # For guild-scoped routes, verify the session owns that guild
    guild_id = request.match_info.get("guild_id")
    if guild_id and not _session_can_access_guild(session, guild_id):
        raise web.HTTPForbidden(reason="You do not have access to this guild")

    request["session"] = session
    return await handler(request)

# ── Health ────────────────────────────────────────────────────────────────────
async def health(request):
    return web.json_response({"status": "ok", "bot": "ExcelProtocol"})

async def reload_rr_views(request=None):
    """Re-register all reaction role views with the bot. Called after dashboard creates/edits a panel."""
    if _bot_ref is None:
        return
    try:
        import reaction_roles
        await reaction_roles.restore_views(_bot_ref)
        logger.info("Reloaded reaction role views from dashboard trigger")
    except Exception as e:
        logger.error(f"Failed to reload reaction role views: {e}")

import logging
logger = logging.getLogger(__name__)

# ── OAuth2 ────────────────────────────────────────────────────────────────────
# In-memory state store for CSRF protection — {state: True}
_oauth_states: dict = {}

# Twitch OAuth state store — {state: {guild_id, session_token, expires_at}}
_twitch_oauth_states: dict = {}

async def auth_login(request):
    state = secrets.token_hex(16)
    _oauth_states[state] = datetime.now(timezone.utc).timestamp()
    # Clean up states older than 10 minutes
    cutoff = datetime.now(timezone.utc).timestamp() - 600
    stale = [k for k, v in list(_oauth_states.items()) if v < cutoff]
    for k in stale:
        del _oauth_states[k]
    url = (
        f"https://discord.com/api/oauth2/authorize"
        f"?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope=identify+guilds"
        f"&state={state}"
    )
    raise web.HTTPFound(url)

async def auth_callback(request):
    code  = request.rel_url.query.get("code")
    state = request.rel_url.query.get("state")
    if not code:
        raise web.HTTPBadRequest(reason="Missing code")
    # Validate CSRF state
    if not state or state not in _oauth_states:
        raise web.HTTPBadRequest(reason="Invalid or missing state — possible CSRF attempt")
    del _oauth_states[state]
    session = get_http_session()
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

    # Only include guilds where user has Manage Server AND the bot is present
    # Get bot's guild list from Discord API — more reliable than in-memory cache
    # which may not reflect recently joined guilds
    try:
        session = get_http_session()
        async with session.get(
            f"{DISCORD_API}/users/@me/guilds",
            headers={"Authorization": f"Bot {DISCORD_TOKEN}"}
        ) as resp:
            if resp.status == 200:
                bot_guilds = await resp.json()
                bot_guild_ids = {str(g["id"]) for g in bot_guilds}
            else:
                # Fall back to in-memory cache
                bot_guild_ids = {str(g.id) for g in _bot_ref.guilds} if _bot_ref else set()
    except Exception:
        bot_guild_ids = {str(g.id) for g in _bot_ref.guilds} if _bot_ref else set()
    managed = [
        {"id": g["id"], "name": g["name"], "icon": g.get("icon")}
        for g in guilds
        if (int(g.get("permissions", 0)) & 0x20  # Manage Guild
            or int(g.get("permissions", 0)) & 0x8   # Administrator
            or g.get("owner", False))               # Server owner
        and (not bot_guild_ids or g["id"] in bot_guild_ids)
    ]
    logger.info(f"Auth: user has {len(guilds)} guilds, {len(managed)} managed, bot in {len(bot_guild_ids)} guilds")
    session_token = secrets.token_hex(32)
    is_owner = BOT_OWNER_ID and str(user["id"]) == str(BOT_OWNER_ID)
    _sessions[session_token] = {
        "user_id":      user["id"],
        "username":     user["username"],
        "avatar":       user.get("avatar"),
        "guilds":       managed,
        "dev":          is_owner,
        "_created_at":  datetime.now(timezone.utc).timestamp(),
    }
    response = web.HTTPFound("/app/")
    response.set_cookie(
        "ep_session", session_token,
        httponly=True, samesite="Lax", secure=True, max_age=86400 * 7
    )
    raise response

async def auth_me(request):
    session = request["session"]
    if session.get("dev"):
        # Use the bot's actual guild list so all servers show up, even ones with no streamers yet
        guilds = []
        if _bot_ref:
            for g in _bot_ref.guilds:
                guilds.append({
                    "id":   str(g.id),
                    "name": g.name,
                    "icon": g.icon.key if g.icon else None,
                    "approximate_member_count": g.member_count,
                })
        else:
            # Fallback to DB if bot ref not available
            rows = await db_fetch("SELECT DISTINCT guild_id FROM monitored_streamers")
            for r in rows:
                info = await get_guild_info(str(r["guild_id"]))
                guilds.append({
                    "id":   str(r["guild_id"]),
                    "name": info.get("name", str(r["guild_id"])),
                    "icon": info.get("icon"),
                    "approximate_member_count": info.get("approximate_member_count"),
                })
        guilds.sort(key=lambda g: g["name"].lower())
        return web.json_response({
            "user_id":  session.get("user_id"),
            "username": session.get("username"),
            "avatar":   session.get("avatar"),
            "guilds":   guilds,
            "is_dev":   True,
        })
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
        "is_dev":   session.get("dev", False),
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
        "SELECT message_id, guild_id, channel_id, title, type, only_add, max_roles, roles_json, body_text FROM reaction_roles WHERE guild_id = ?",
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
            "message_id":   str(rr["message_id"]),
            "channel_id":   str(rr["channel_id"]),
            "guild_id":     str(rr["guild_id"]),
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
    import asyncio as _asyncio
    limit = await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.get_streamer_limit(int(guild_id))) if _bot_ref else 75
    count = len(result)
    return web.json_response({"streamers": result, "count": count, "limit": limit})

async def add_streamer(request):
    guild_id = request.match_info["guild_id"]
    body = await request.json()
    raw_username = body.get("twitch_username", "").strip()
    channel_id   = body.get("channel_id")

    # Strip URLs, @ signs, and trailing slashes so users can paste full Twitch URLs
    twitch_username = raw_username.lower()
    for prefix in ("https://www.twitch.tv/", "http://www.twitch.tv/",
                   "https://twitch.tv/", "http://twitch.tv/", "twitch.tv/"):
        if twitch_username.startswith(prefix):
            twitch_username = twitch_username[len(prefix):]
            break
    twitch_username = twitch_username.lstrip("@").split("/")[0].split("?")[0].strip()

    if not twitch_username or not channel_id:
        raise web.HTTPBadRequest(reason="twitch_username and channel_id are required")

    # Verify the Twitch account actually exists
    if _bot_ref:
        user_info = await _bot_ref.twitch.get_user(twitch_username)
        if not user_info:
            raise web.HTTPBadRequest(reason=f"Twitch user '{twitch_username}' not found. Check the spelling.")
        # Use the canonical login name from Twitch in case casing differs
        twitch_username = user_info["login"]

    # Check streamer limit — dev sessions are exempt
    session = request.get("session", {})
    if not session.get("dev") and _bot_ref:
        import asyncio as _asyncio
        limit = await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.get_streamer_limit(int(guild_id)))
        count = await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.get_streamer_count(int(guild_id)))
        if count >= limit:
            raise web.HTTPForbidden(reason=f"Streamer limit reached ({count}/{limit}). Contact the bot owner to increase your limit.")

    try:
        await db_execute(
            "INSERT INTO monitored_streamers (guild_id, streamer_name, channel_id, twitch_user_id) VALUES (?, ?, ?, ?)",
            (guild_id, twitch_username, channel_id, user_info["id"] if user_info else None),
        )
    except Exception as e:
        if "UNIQUE" in str(e):
            raise web.HTTPConflict(reason="Streamer already tracked")
        raise

    # Register EventSub for this streamer
    if _bot_ref and user_info:
        asyncio.create_task(_bot_ref._register_eventsub_for_user(user_info["id"], twitch_username))
        await _bot_ref.log_to_channel(
            "➕", "Streamer Added (Dashboard)",
            f"**{user_info.get('display_name', twitch_username)}** added to guild `{guild_id}`"
        )

    return web.json_response({"ok": True})

async def delete_streamer(request):
    guild_id = request.match_info["guild_id"]
    username = request.match_info["username"]
    # Sanitise in case a URL was stored — strip prefix so the DB lookup matches
    for prefix in ("https://www.twitch.tv/", "http://www.twitch.tv/",
                   "https://twitch.tv/", "http://twitch.tv/", "twitch.tv/"):
        if username.lower().startswith(prefix):
            username = username[len(prefix):]
            break
    username = username.lstrip("@").split("?")[0].strip()
    await db_execute(
        "DELETE FROM monitored_streamers WHERE guild_id = ? AND streamer_name = ?",
        (guild_id, username.lower()),
    )
    if _bot_ref:
        await _bot_ref.log_to_channel(
            "➖", "Streamer Removed (Dashboard)",
            f"**{username}** removed from guild `{guild_id}`"
        )
    return web.json_response({"ok": True})

# ── Reaction Roles ────────────────────────────────────────────────────────────
async def get_reaction_roles(request):
    guild_id = request.match_info["guild_id"]
    rows = await db_fetch(
        "SELECT message_id, guild_id, channel_id, title, type, only_add, max_roles, roles_json, body_text FROM reaction_roles WHERE guild_id = ?",
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
            enriched.append({**r, "role_id": str(r.get("role_id", "")), "role_name": role_info.get("name", role_id), "role_color": role_info.get("color", 0)})
        ch_name = await get_channel_name(str(rr["channel_id"]))
        # Stringify message_id and channel_id — JS loses precision on Discord snowflakes (64-bit ints)
        result.append({**rr, "message_id": str(rr["message_id"]), "channel_id": str(rr["channel_id"]), "guild_id": str(rr["guild_id"]), "roles": enriched, "channel_name": ch_name})
    return web.json_response(result)

async def delete_reaction_role(request):
    guild_id   = request.match_info["guild_id"]
    message_id = request.match_info["role_id"]

    # Get the entry first so we know which channel the message is in
    if _bot_ref:
        entry = _bot_ref.db.rr_get(int(message_id))
        if entry and str(entry["guild_id"]) == guild_id:
            try:
                guild = _bot_ref.get_guild(int(guild_id))
                if guild:
                    channel = guild.get_channel(entry["channel_id"])
                    if channel:
                        msg = await channel.fetch_message(int(message_id))
                        await msg.delete()
                        logger.info(f"Dashboard deleted RR message {message_id}")
            except Exception as e:
                logger.warning(f"Could not delete RR Discord message {message_id}: {e}")
            # Also delete via bot DB method so any internal state is cleared
            _bot_ref.db.rr_delete(int(message_id))
        else:
            await db_execute(
                "DELETE FROM reaction_roles WHERE guild_id = ? AND message_id = ?",
                (guild_id, message_id),
            )
    else:
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
    channels, voice_channels = await asyncio.gather(
        get_guild_channels(guild_id),
        get_guild_voice_channels(guild_id),
    )
    rows = await db_fetch("SELECT notification_channel_id FROM server_settings WHERE guild_id = ?", (guild_id,))
    default_channel_id = str(rows[0]["notification_channel_id"]) if rows else None
    return web.json_response({"channels": channels, "voice_channels": voice_channels, "default_channel_id": default_channel_id})

# ── Guild Emojis ─────────────────────────────────────────────────────────────
async def get_emojis(request):
    guild_id = request.match_info["guild_id"]
    try:
        session = get_http_session()
        async with session.get(
            f"{DISCORD_API}/guilds/{guild_id}/emojis",
            headers={"Authorization": f"Bot {DISCORD_TOKEN}"}
        ) as resp:
            if resp.status != 200:
                return web.json_response([])
            emojis = await resp.json()
            return web.json_response([
                {"id": str(e["id"]), "name": e["name"], "animated": e.get("animated", False)}
                for e in emojis if not e.get("managed")
            ])
    except Exception:
        return web.json_response([])

# ── Guild Roles List ──────────────────────────────────────────────────────────
async def get_roles_list(request):
    guild_id = request.match_info["guild_id"]
    try:
        session = get_http_session()
        async with session.get(
            f"{DISCORD_API}/guilds/{guild_id}/roles",
            headers={"Authorization": f"Bot {DISCORD_TOKEN}"}
        ) as resp:
            if resp.status != 200:
                return web.json_response([])
            roles = await resp.json()
            return web.json_response([
                {"id": str(r["id"]), "name": r["name"], "color": r["color"]}
                for r in roles if r["name"] != "@everyone"
            ])
    except Exception:
        return web.json_response([])

# ── Create Reaction Role Panel ────────────────────────────────────────────────
async def _resolve_role_id(guild_id: str, role_id: str, new_role_name: str = None, new_role_color: int = None) -> str:
    """If role_id is __create__, create the role in Discord and return the real ID."""
    if role_id != "__create__":
        return role_id
    if not new_role_name or not new_role_name.strip():
        raise web.HTTPBadRequest(reason="New role name is required when creating a role")
    role_payload = {"name": new_role_name.strip()}
    if new_role_color is not None:
        role_payload["color"] = int(new_role_color)
    session = get_http_session()
    resp = await session.post(
        f"{DISCORD_API}/guilds/{guild_id}/roles",
        headers={"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"},
        json=role_payload,
    )
    if resp.status not in (200, 201):
        err = await resp.text()
        raise web.HTTPInternalServerError(reason=f"Failed to create role: {err}")
    data = await resp.json()
    # Bust the roles cache so the new role shows up next time
    _discord_cache.pop(f"/guilds/{guild_id}/roles", None)
    return str(data["id"])

async def create_reaction_role(request):
    """
    Create a new reaction role panel and post it to Discord.
    The bot posts the embed+components to the channel, then saves to DB.
    """
    guild_id = request.match_info["guild_id"]
    body = await request.json()
    title      = body.get("title", "").strip()
    rr_type    = body.get("type", "dropdown")
    only_add   = body.get("only_add", False)
    max_roles  = body.get("max_roles")
    channel_id = body.get("channel_id")
    roles      = body.get("roles", [])
    body_text  = (body.get("body_text") or "").strip() or None

    if not title or not channel_id or not roles:
        raise web.HTTPBadRequest(reason="title, channel_id and roles are required")

    # Resolve any __create__ role IDs first
    for r in roles:
        r["role_id"] = await _resolve_role_id(guild_id, str(r.get("role_id", "")), r.get("new_role_name"))

    if _bot_ref is None:
        raise web.HTTPInternalServerError(reason="Bot not available — try again in a moment")

    import json as _json
    import reaction_roles as rr_module

    # Convert role_id strings to ints as reaction_roles.py expects
    for r in roles:
        r["role_id"] = int(r["role_id"])

    # Post a placeholder message first to get the message_id, then build the proper view
    guild = _bot_ref.get_guild(int(guild_id))
    if not guild:
        raise web.HTTPBadRequest(reason="Bot is not in that guild")

    channel = guild.get_channel(int(channel_id))
    if not channel:
        raise web.HTTPBadRequest(reason="Channel not found")

    embed_color = _bot_ref.db.get_embed_color(int(guild_id))

    import discord
    embed = discord.Embed(title=title, description=body_text, color=embed_color)

    # Build a temporary view to post (message_id=0), then re-edit with correct ID
    temp_entry = {
        "message_id": 0,
        "guild_id": int(guild_id),
        "channel_id": int(channel_id),
        "title": title,
        "type": rr_type,
        "only_add": only_add,
        "max_roles": max_roles,
        "roles": roles,
        "body_text": body_text,
    }
    temp_view = rr_module._build_view(temp_entry, _bot_ref)
    try:
        message = await channel.send(embed=embed, view=temp_view)
    except discord.Forbidden:
        raise web.HTTPForbidden(reason=f"Bot is missing permissions in #{channel.name} — check Send Messages and Embed Links.")

    # Now rebuild the view with the real message_id and edit
    real_entry = {**temp_entry, "message_id": message.id}
    real_view = rr_module._build_view(real_entry, _bot_ref)
    await message.edit(view=real_view)
    _bot_ref.add_view(real_view, message_id=message.id)

    # Save to DB using the bot's own rr_save so it's identical to slash command flow
    _bot_ref.db.rr_save(
        message_id=message.id,
        guild_id=int(guild_id),
        channel_id=int(channel_id),
        title=title,
        rr_type=rr_type,
        only_add=only_add,
        max_roles=max_roles,
        roles=roles,
        body_text=body_text,
    )

    logger.info(f"Dashboard created RR panel '{title}' in #{channel.name} (msg {message.id})")
    return web.json_response({"ok": True, "message_id": str(message.id)})

# ── Edit Streamer ─────────────────────────────────────────────────────────────
async def edit_streamer(request):
    guild_id = request.match_info["guild_id"]
    username = request.match_info["username"]
    for prefix in ("https://www.twitch.tv/", "http://www.twitch.tv/",
                   "https://twitch.tv/", "http://twitch.tv/", "twitch.tv/"):
        if username.lower().startswith(prefix):
            username = username[len(prefix):]
            break
    username = username.lstrip("@").split("?")[0].strip()
    body = await request.json()
    channel_id = body.get("channel_id")
    if not channel_id:
        raise web.HTTPBadRequest(reason="channel_id is required")
    await db_execute(
        "UPDATE monitored_streamers SET custom_channel_id = ? WHERE guild_id = ? AND streamer_name = ?",
        (channel_id, guild_id, username.lower()),
    )
    # Clear stale permission issues — next check will re-evaluate with new channel
    await db_execute("DELETE FROM permission_issues WHERE guild_id = ?", (guild_id,))
    return web.json_response({"ok": True})

# ── Edit Reaction Role Panel ──────────────────────────────────────────────────
async def edit_reaction_role(request):
    guild_id   = request.match_info["guild_id"]
    message_id = request.match_info["role_id"]
    body = await request.json()

    if _bot_ref is None:
        raise web.HTTPInternalServerError(reason="Bot not available")

    import reaction_roles as rr_module
    import discord

    # Get current entry from DB
    entry = _bot_ref.db.rr_get(int(message_id))
    if not entry or str(entry["guild_id"]) != guild_id:
        raise web.HTTPNotFound(reason="Panel not found")

    # Merge updates into the entry
    if "title" in body:     entry["title"]     = body["title"]
    if "type" in body:      entry["type"]      = body["type"]
    if "only_add" in body:  entry["only_add"]  = body["only_add"]
    if "max_roles" in body: entry["max_roles"] = body["max_roles"]
    if "body_text" in body: entry["body_text"] = (body["body_text"] or "").strip() or None
    if "roles" in body:
        # Resolve any __create__ role IDs first, then convert to int
        for r in body["roles"]:
            r["role_id"] = await _resolve_role_id(guild_id, str(r.get("role_id", "")), r.get("new_role_name"))
        entry["roles"] = [{**r, "role_id": int(r["role_id"])} for r in body["roles"]]

    # Save updated entry to DB
    _bot_ref.db.rr_save(
        message_id=int(message_id),
        guild_id=entry["guild_id"],
        channel_id=entry["channel_id"],
        title=entry["title"],
        rr_type=entry["type"],
        only_add=entry["only_add"],
        max_roles=entry["max_roles"],
        roles=entry["roles"],
        body_text=entry.get("body_text"),
    )

    # Edit the actual Discord message
    guild = _bot_ref.get_guild(int(guild_id))
    if guild:
        channel = guild.get_channel(entry["channel_id"])
        if channel:
            try:
                msg = await channel.fetch_message(int(message_id))
                embed_color = _bot_ref.db.get_embed_color(int(guild_id))
                embed = discord.Embed(title=entry["title"], description=entry.get("body_text") or None, color=embed_color)
                view = rr_module._build_view(entry, _bot_ref)
                await msg.edit(embed=embed, view=view)
                _bot_ref.add_view(view, message_id=int(message_id))
                logger.info(f"Dashboard edited RR panel {message_id}")
            except discord.NotFound:
                logger.warning(f"RR message {message_id} not found in Discord — DB updated only")
            except Exception as e:
                logger.error(f"Failed to edit RR Discord message: {e}")

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

    s = get_http_session()
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

# ── Support ──────────────────────────────────────────────────────────────────
async def post_support(request):
    """Receive a support message from the dashboard and DM it to the bot owner."""
    session = request["session"]
    body = await request.json()
    text = body.get("text", "").strip()
    guild_id = body.get("guild_id", "")

    if not text:
        raise web.HTTPBadRequest(reason="Message is required")
    if len(text) > 1000:
        raise web.HTTPBadRequest(reason="Message must be under 1000 characters")
    if not BOT_OWNER_ID or not DISCORD_TOKEN:
        raise web.HTTPInternalServerError(reason="BOT_OWNER_ID or DISCORD_TOKEN not configured")

    sender    = "Dev (dashboard)" if session.get("dev") else session.get("username", "Unknown")
    sender_id = None if session.get("dev") else session.get("user_id")

    # Get guild name if possible
    guild_name = guild_id
    if guild_id and _bot_ref:
        try:
            guild_obj = _bot_ref.get_guild(int(guild_id))
            if guild_obj:
                guild_name = guild_obj.name
        except Exception:
            pass

    s = get_http_session()
    dm_resp = await s.post(
        f"{DISCORD_API}/users/@me/channels",
        headers={"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"},
        json={"recipient_id": BOT_OWNER_ID},
    )
    dm_data = await dm_resp.json()
    dm_channel_id = dm_data.get("id")
    if not dm_channel_id:
        raise web.HTTPInternalServerError(reason="Failed to open DM channel")

    user_value = f"{sender}"
    if sender_id:
        user_value += f"\n`{sender_id}`"
        user_value += f"\n<@{sender_id}>"

    embed = {
        "title": "🎫 New Support Request",
        "description": text,
        "color": 0xFF6B6B,
        "fields": [
            {"name": "From",   "value": user_value, "inline": True},
            {"name": "Server", "value": f"{guild_name}\n`{guild_id}`" if guild_id else "Unknown", "inline": True},
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



# ── Birthdays ─────────────────────────────────────────────────────────────────
async def get_birthdays(request):
    guild_id = request.match_info["guild_id"]
    rows = await db_fetch(
        "SELECT user_id, day, month, year FROM birthdays WHERE guild_id = ? ORDER BY month, day",
        (guild_id,)
    )
    # Try to enrich with Discord usernames via bot
    result = []
    for r in rows:
        username = None
        if _bot_ref:
            try:
                guild = _bot_ref.get_guild(int(guild_id))
                if guild:
                    member = guild.get_member(int(r["user_id"]))
                    if member:
                        username = member.display_name
            except Exception:
                pass
        result.append({
            "user_id":  str(r["user_id"]),
            "username": username or str(r["user_id"]),
            "day":   r["day"],
            "month": r["month"],
            "year":  r["year"],
        })
    return web.json_response(result)

async def add_birthday(request):
    guild_id = request.match_info["guild_id"]
    body = await request.json()
    user_id = body.get("user_id")
    day     = body.get("day")
    month   = body.get("month")
    year    = body.get("year", 0)
    if not user_id or not day or not month:
        raise web.HTTPBadRequest(reason="user_id, day and month are required")
    if not (1 <= int(day) <= 31) or not (1 <= int(month) <= 12):
        raise web.HTTPBadRequest(reason="Invalid day or month")
    await db_execute(
        "INSERT INTO birthdays (guild_id, user_id, day, month, year) VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET day=excluded.day, month=excluded.month, year=excluded.year",
        (guild_id, int(user_id), int(day), int(month), int(year) if year else 0)
    )
    return web.json_response({"ok": True})

async def delete_birthday(request):
    guild_id = request.match_info["guild_id"]
    user_id  = request.match_info["user_id"]
    await db_execute(
        "DELETE FROM birthdays WHERE guild_id = ? AND user_id = ?",
        (guild_id, int(user_id))
    )
    return web.json_response({"ok": True})

async def get_guild_members(request):
    guild_id = request.match_info["guild_id"]
    if not _bot_ref:
        return web.json_response([])
    try:
        guild = _bot_ref.get_guild(int(guild_id))
        if not guild:
            return web.json_response([])
        members = [
            {"id": str(m.id), "username": m.display_name}
            for m in guild.members if not m.bot
        ]
        members.sort(key=lambda m: m["username"].lower())
        return web.json_response(members)
    except Exception as e:
        logger.error(f"Failed to get members: {e}")
        return web.json_response([])

# ── Server Settings ───────────────────────────────────────────────────────────
async def get_server_settings(request):
    guild_id = request.match_info["guild_id"]
    rows = await db_fetch(
        "SELECT notification_channel_id, embed_color, auto_delete_notifications, milestone_notifications, ping_role_id FROM server_settings WHERE guild_id = ?",
        (guild_id,)
    )
    bday = await db_fetch("SELECT channel_id FROM birthday_channels WHERE guild_id = ?", (guild_id,))
    s = rows[0] if rows else {}
    color_int = s.get("embed_color") or 0x00FFFF
    color_hex = f"#{color_int:06x}"
    return web.json_response({
        "notification_channel_id": str(s["notification_channel_id"]) if s.get("notification_channel_id") else None,
        "embed_color": color_hex,
        "auto_delete_notifications": bool(s.get("auto_delete_notifications", 0)),
        "milestone_notifications": bool(s.get("milestone_notifications", 0)),
        "birthday_channel_id": str(bday[0]["channel_id"]) if bday else None,
        "ping_role_id": str(s["ping_role_id"]) if s.get("ping_role_id") else None,
    })

async def patch_server_settings(request):
    guild_id = request.match_info["guild_id"]
    body = await request.json()

    if "notification_channel_id" in body:
        cid = int(body["notification_channel_id"])
        await db_execute(
            "INSERT INTO server_settings (guild_id, notification_channel_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET notification_channel_id = ?",
            (guild_id, cid, cid)
        )
        await db_execute(
            "UPDATE monitored_streamers SET channel_id = ? WHERE guild_id = ? AND custom_channel_id IS NULL",
            (cid, guild_id)
        )
        # Clear stale permission issues — next periodic check will re-evaluate current channels
        await db_execute("DELETE FROM permission_issues WHERE guild_id = ?", (guild_id,))

    if "embed_color" in body:
        hex_str = body["embed_color"].lstrip("#")
        color_int = int(hex_str, 16)
        await db_execute(
            "INSERT INTO server_settings (guild_id, notification_channel_id, embed_color) VALUES (?, 0, ?) ON CONFLICT(guild_id) DO UPDATE SET embed_color = ?",
            (guild_id, color_int, color_int)
        )

    if "auto_delete_notifications" in body:
        val = 1 if body["auto_delete_notifications"] else 0
        await db_execute(
            "INSERT INTO server_settings (guild_id, notification_channel_id, auto_delete_notifications) VALUES (?, 0, ?) ON CONFLICT(guild_id) DO UPDATE SET auto_delete_notifications = ?",
            (guild_id, val, val)
        )

    if "milestone_notifications" in body:
        val = 1 if body["milestone_notifications"] else 0
        await db_execute(
            "INSERT INTO server_settings (guild_id, notification_channel_id, milestone_notifications) VALUES (?, 0, ?) ON CONFLICT(guild_id) DO UPDATE SET milestone_notifications = ?",
            (guild_id, val, val)
        )

    if "birthday_channel_id" in body:
        cid = int(body["birthday_channel_id"])
        await db_execute(
            "INSERT INTO birthday_channels (guild_id, channel_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET channel_id = ?",
            (guild_id, cid, cid)
        )

    if "ping_role_id" in body:
        raw = body["ping_role_id"]
        if raw is None or raw == "":
            # Clear the ping role
            await db_execute(
                "INSERT INTO server_settings (guild_id, notification_channel_id, ping_role_id) VALUES (?, 0, NULL) ON CONFLICT(guild_id) DO UPDATE SET ping_role_id = NULL",
                (guild_id,)
            )
        else:
            # May be "__create__" with accompanying new_role_name / new_role_color
            resolved = await _resolve_role_id(
                guild_id,
                str(raw),
                body.get("new_role_name"),
                body.get("new_role_color"),
            )
            rid = int(resolved)
            await db_execute(
                "INSERT INTO server_settings (guild_id, notification_channel_id, ping_role_id) VALUES (?, 0, ?) ON CONFLICT(guild_id) DO UPDATE SET ping_role_id = ?",
                (guild_id, rid, rid)
            )

    return web.json_response({"ok": True})

# ── Cleanup Configs ───────────────────────────────────────────────────────────
async def get_cleanup_configs(request):
    guild_id = request.match_info["guild_id"]
    rows = await db_fetch(
        "SELECT channel_id, interval_hours, keep_pinned FROM cleanup_configs WHERE guild_id = ? ORDER BY channel_id",
        (guild_id,)
    )
    result = []
    for r in rows:
        name = await get_channel_name(str(r["channel_id"]))
        result.append({
            "channel_id": str(r["channel_id"]),
            "channel_name": name,
            "interval_hours": r["interval_hours"],
            "keep_pinned": bool(r["keep_pinned"]),
        })
    return web.json_response(result)

async def add_cleanup_config(request):
    guild_id = request.match_info["guild_id"]
    body = await request.json()
    channel_id = body.get("channel_id")
    interval_hours = body.get("interval_hours")
    keep_pinned = body.get("keep_pinned", True)
    if not channel_id or not interval_hours:
        raise web.HTTPBadRequest(reason="channel_id and interval_hours are required")
    await db_execute(
        "INSERT INTO cleanup_configs (guild_id, channel_id, interval_hours, keep_pinned) VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, channel_id) DO UPDATE SET interval_hours = ?, keep_pinned = ?",
        (guild_id, int(channel_id), int(interval_hours), 1 if keep_pinned else 0, int(interval_hours), 1 if keep_pinned else 0)
    )
    return web.json_response({"ok": True})

async def edit_cleanup_config(request):
    guild_id = request.match_info["guild_id"]
    channel_id = request.match_info["channel_id"]
    body = await request.json()
    interval_hours = body.get("interval_hours")
    keep_pinned = body.get("keep_pinned")
    if interval_hours is not None:
        await db_execute(
            "UPDATE cleanup_configs SET interval_hours = ? WHERE guild_id = ? AND channel_id = ?",
            (int(interval_hours), guild_id, int(channel_id))
        )
    if keep_pinned is not None:
        await db_execute(
            "UPDATE cleanup_configs SET keep_pinned = ? WHERE guild_id = ? AND channel_id = ?",
            (1 if keep_pinned else 0, guild_id, int(channel_id))
        )
    return web.json_response({"ok": True})

async def delete_cleanup_config(request):
    guild_id = request.match_info["guild_id"]
    channel_id = request.match_info["channel_id"]
    await db_execute(
        "DELETE FROM cleanup_configs WHERE guild_id = ? AND channel_id = ?",
        (guild_id, int(channel_id))
    )
    return web.json_response({"ok": True})



# ── Streamer Limit (dev only) ─────────────────────────────────────────────────
async def set_streamer_limit(request):
    session = request["session"]
    if not session.get("dev"):
        raise web.HTTPForbidden(reason="Dev access required")
    guild_id = request.match_info["guild_id"]
    body = await request.json()
    limit = body.get("limit")
    if limit is None or not isinstance(limit, int) or limit < 1:
        raise web.HTTPBadRequest(reason="limit must be a positive integer")
    if _bot_ref:
        import asyncio as _asyncio
        await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.set_streamer_limit(int(guild_id), limit))
    else:
        await db_execute(
            "INSERT INTO server_settings (guild_id, notification_channel_id, streamer_limit) VALUES (?, 0, ?) ON CONFLICT(guild_id) DO UPDATE SET streamer_limit = ?",
            (guild_id, limit, limit)
        )
    return web.json_response({"ok": True, "limit": limit})


# ── Twitch Commands ───────────────────────────────────────────────────────────
async def get_twitch_info(request):
    """Get linked twitch channel + all commands for a guild."""
    guild_id = request.match_info["guild_id"]
    import asyncio as _asyncio
    if not _bot_ref:
        return web.json_response({"linked": False, "channel": None, "commands": [], "count": 0, "limit": 50})

    row = await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.get_twitch_channel(int(guild_id)))

    # If no /twitchset channel, fall back to the broadcaster OAuth token login
    if not row:
        broadcaster_rows = await db_fetch(
            "SELECT twitch_login FROM broadcaster_tokens WHERE guild_id = ?", (guild_id,)
        )
        if broadcaster_rows:
            twitch_login = broadcaster_rows[0]["twitch_login"]
            # Auto-link the channel using the broadcaster login
            await _asyncio.get_event_loop().run_in_executor(
                None, lambda: _bot_ref.db.set_twitch_channel(int(guild_id), twitch_login)
            )
            row = {"twitch_channel": twitch_login}
        else:
            return web.json_response({"linked": False, "channel": None, "commands": [], "count": 0, "limit": 50, "can_link_via_oauth": True})

    channel = row["twitch_channel"]
    commands = await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.get_twitch_commands(channel))
    limit = await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.get_command_limit(int(guild_id)))

    # Check if bot is modded in the channel
    bot_is_modded = False
    BOT_TWITCH_LOGIN = "excelprotocol"
    try:
        broadcaster = await get_twitch_token()
        sess = get_http_session()
        # Get broadcaster user ID first
        async with sess.get(
            "https://api.twitch.tv/helix/users",
            headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {broadcaster}"},
            params={"login": channel}
        ) as resp:
            udata = await resp.json()
            users = udata.get("data", [])
            if users:
                broadcaster_id = users[0]["id"]
                # Check moderators list
                async with sess.get(
                    "https://api.twitch.tv/helix/moderation/moderators",
                    headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {broadcaster}"},
                    params={"broadcaster_id": broadcaster_id, "first": 100}
                ) as mresp:
                    if mresp.status == 200:
                        mdata = await mresp.json()
                        mod_logins = [m["user_login"].lower() for m in mdata.get("data", [])]
                        bot_is_modded = BOT_TWITCH_LOGIN in mod_logins
    except Exception as e:
        logger.warning(f"Could not check mod status for {channel}: {e}")

    return web.json_response({
        "linked": True,
        "channel": channel,
        "commands": commands,
        "count": len(commands),
        "limit": limit,
        "bot_is_modded": bot_is_modded,
        "play_enabled": row.get("play_enabled", False) if row else False,
        "overlay_volume": _bot_ref.db.get_overlay_volume(int(guild_id)) if _bot_ref else 100,
    })

async def set_play_enabled(request):
    """Toggle !play command on/off for a guild."""
    guild_id = request.match_info["guild_id"]
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    import asyncio as _asyncio
    if _bot_ref:
        await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.set_play_enabled(int(guild_id), enabled))
    else:
        await db_execute("UPDATE twitch_channels SET play_enabled = ? WHERE guild_id = ?", (int(enabled), guild_id))
    return web.json_response({"ok": True, "play_enabled": enabled})


async def set_overlay_volume(request):
    """Save overlay volume (0-100) for a guild and push live to connected WebSocket overlays."""
    guild_id = request.match_info["guild_id"]
    body = await request.json()
    volume = max(0, min(100, int(body.get("volume", 100))))
    import asyncio as _asyncio
    if _bot_ref:
        await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.set_overlay_volume(int(guild_id), volume))
    else:
        await db_execute("UPDATE twitch_channels SET overlay_volume = ? WHERE guild_id = ?", (volume, guild_id))
    # Push live to any connected overlay WebSockets
    payload = json.dumps({"type": "set_volume", "volume": volume})
    conns = _overlay_connections.get(str(guild_id), set())
    dead = set()
    for ws in conns:
        try:
            await ws.send_str(payload)
        except Exception:
            dead.add(ws)
    if dead:
        conns.difference_update(dead)
    return web.json_response({"ok": True, "volume": volume})


async def play_test_overlay(request):
    """Send a test video to the overlay so the user can verify volume in OBS."""
    guild_id = request.match_info["guild_id"]
    payload = json.dumps({"type": "play", "video_url": "https://www.youtube.com/watch?v=UKZzszc9AEI", "volume": 1.0, "redeemer": ""})
    conns = _overlay_connections.get(str(guild_id), set())
    dead = set()
    for ws in conns:
        try:
            await ws.send_str(payload)
        except Exception:
            dead.add(ws)
    if dead:
        conns.difference_update(dead)
    return web.json_response({"ok": True})


async def add_twitch_command(request):
    guild_id = request.match_info["guild_id"]
    body = await request.json()
    session = request.get("session", {})
    import asyncio as _asyncio

    if not _bot_ref:
        raise web.HTTPInternalServerError(reason="Bot not available")

    row = await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.get_twitch_channel(int(guild_id)))
    if not row:
        raise web.HTTPBadRequest(reason="No Twitch channel linked to this guild")

    channel = row["twitch_channel"]
    command_name = body.get("command_name", "").strip().lower()
    response = body.get("response", "").strip()
    permission = body.get("permission", "everyone")
    cooldown = int(body.get("cooldown_seconds", 0))

    if not command_name or not response:
        raise web.HTTPBadRequest(reason="command_name and response are required")
    if not command_name.startswith("!"):
        command_name = "!" + command_name
    if permission not in ("everyone", "subscriber", "mod", "broadcaster"):
        raise web.HTTPBadRequest(reason="Invalid permission level")

    # Check limit unless dev or editing existing
    existing = await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.get_twitch_command(channel, command_name))
    if not existing and not session.get("dev"):
        limit = await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.get_command_limit(int(guild_id)))
        count = await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.get_command_count(int(guild_id)))
        if count >= limit:
            raise web.HTTPForbidden(reason=f"Command limit reached ({count}/{limit}). Contact the bot owner to increase your limit.")

    success = await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.add_twitch_command(channel, command_name, response, permission, cooldown))
    if not success:
        raise web.HTTPInternalServerError(reason="Failed to save command")
    return web.json_response({"ok": True})

async def delete_twitch_command(request):
    guild_id = request.match_info["guild_id"]
    command_name = request.match_info["command_name"]
    import asyncio as _asyncio

    if not _bot_ref:
        raise web.HTTPInternalServerError(reason="Bot not available")

    row = await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.get_twitch_channel(int(guild_id)))
    if not row:
        raise web.HTTPNotFound(reason="No Twitch channel linked")

    await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.remove_twitch_command(row["twitch_channel"], command_name))
    return web.json_response({"ok": True})

async def set_command_limit(request):
    session = request["session"]
    if not session.get("dev"):
        raise web.HTTPForbidden(reason="Dev access required")
    guild_id = request.match_info["guild_id"]
    body = await request.json()
    limit = body.get("limit")
    if limit is None or not isinstance(limit, int) or limit < 1:
        raise web.HTTPBadRequest(reason="limit must be a positive integer")
    import asyncio as _asyncio
    if _bot_ref:
        await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.set_command_limit(int(guild_id), limit))
    else:
        await db_execute("INSERT INTO server_settings (guild_id, notification_channel_id, command_limit) VALUES (?, 0, ?) ON CONFLICT(guild_id) DO UPDATE SET command_limit = ?", (guild_id, limit, limit))
    return web.json_response({"ok": True, "limit": limit})


# ── Broadcaster OAuth ─────────────────────────────────────────────────────────
async def twitch_broadcaster_login(request):
    """Redirect streamer to Twitch OAuth — stores secure state mapped to guild+session."""
    guild_id = request.match_info["guild_id"]

    # Route is in public bypass so middleware didn't populate session — get it manually
    session = get_session(request)
    if not session:
        raise web.HTTPFound(f"/auth/login")
    if not _session_can_access_guild(session, guild_id):
        raise web.HTTPForbidden(reason="You do not have access to this guild")

    # Generate a random state and store guild_id + session cookie so callback can verify both
    state = secrets.token_hex(16)
    session_token = request.cookies.get("ep_session", "")
    _twitch_oauth_states[state] = {
        "guild_id":      guild_id,
        "session_token": session_token,
        "expires_at":    (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    }

    import urllib.parse
    params = urllib.parse.urlencode({
        "client_id":    TWITCH_CLIENT_ID,
        "redirect_uri": TWITCH_REDIRECT_URI,
        "response_type": "code",
        "scope":        "channel:read:redemptions channel:manage:redemptions",
        "state":        state,
        "force_verify": "true",
    })
    raise web.HTTPFound(f"https://id.twitch.tv/oauth2/authorize?{params}")

async def twitch_broadcaster_callback(request):
    """Handle Twitch OAuth callback — exchange code for tokens."""
    code  = request.rel_url.query.get("code")
    state = request.rel_url.query.get("state")
    if not code or not state:
        raise web.HTTPBadRequest(reason="Missing code or state")

    # Validate state and retrieve guild_id — prevents CSRF and account mixup
    state_data = _twitch_oauth_states.pop(state, None)
    if not state_data:
        raise web.HTTPBadRequest(reason="Invalid or expired state — please try connecting again")

    # Clean up expired states opportunistically
    now = datetime.now(timezone.utc)
    stale = [k for k, v in list(_twitch_oauth_states.items())
             if datetime.fromisoformat(v["expires_at"]) < now]
    for k in stale:
        del _twitch_oauth_states[k]

    # Verify the session cookie matches what initiated the flow
    session_token = request.cookies.get("ep_session", "")
    if state_data["session_token"] != session_token:
        raise web.HTTPForbidden(reason="Session mismatch — please log in again and retry")

    guild_id = state_data["guild_id"]

    # Verify the session still has access to this guild
    session = _sessions.get(session_token, {})
    if not _session_can_access_guild(session, guild_id):
        raise web.HTTPForbidden(reason="You no longer have access to this guild")

    sess = get_http_session()
    # Exchange code for tokens
    resp = await sess.post("https://id.twitch.tv/oauth2/token", data={
        "client_id":     TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "code":          code,
        "grant_type":    "authorization_code",
        "redirect_uri":  TWITCH_REDIRECT_URI,
    })
    if resp.status != 200:
        raise web.HTTPInternalServerError(reason="Failed to exchange code for token")
    token_data = await resp.json()
    access_token  = token_data["access_token"]
    refresh_token = token_data["refresh_token"]
    expires_at    = (datetime.now(timezone.utc) + timedelta(seconds=token_data["expires_in"])).isoformat()

    # Get Twitch user info
    user_resp = await sess.get(f"{TWITCH_API}/users",
        headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {access_token}"})
    user_data = await user_resp.json()
    user = user_data["data"][0] if user_data.get("data") else None
    if not user:
        raise web.HTTPInternalServerError(reason="Could not get Twitch user info")

    if _bot_ref:
        import asyncio as _asyncio
        await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.set_broadcaster_token(
            int(guild_id), user["id"], user["login"], access_token, refresh_token, expires_at
        ))
    else:
        await db_execute(
            "INSERT INTO broadcaster_tokens (guild_id, twitch_user_id, twitch_login, access_token, refresh_token, expires_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET twitch_user_id=excluded.twitch_user_id, twitch_login=excluded.twitch_login, access_token=excluded.access_token, refresh_token=excluded.refresh_token, expires_at=excluded.expires_at",
            (guild_id, user["id"], user["login"], access_token, refresh_token, expires_at)
        )

    # Register EventSub subscription for channel point redeems
    await _register_eventsub(user["id"])

    raise web.HTTPFound(f"/app/?twitch_connected=1")

async def twitch_broadcaster_disconnect(request):
    """Remove stored broadcaster token for a guild."""
    guild_id = request.match_info["guild_id"]
    if _bot_ref:
        import asyncio as _asyncio
        await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.delete_broadcaster_token(int(guild_id)))
    else:
        await db_execute("DELETE FROM broadcaster_tokens WHERE guild_id = ?", (guild_id,))
    return web.json_response({"ok": True})

async def _register_eventsub(broadcaster_user_id: str):
    """Register EventSub subscription for channel point redeems."""
    callback_url = f"{os.getenv('DASHBOARD_BASE_URL', 'https://excelprotocol.fly.dev')}/api/eventsub/callback"
    secret = os.getenv("EVENTSUB_SECRET")
    if not secret:
        logger.error("EVENTSUB_SECRET env var is not set — cannot register EventSub subscription")
        return
    try:
        sess = get_http_session()
        logger.info(f"Registering EventSub for broadcaster {broadcaster_user_id} with callback {callback_url}")
        app_token = await get_twitch_token()
        resp = await sess.post(
            f"{TWITCH_API}/eventsub/subscriptions",
            headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {app_token}", "Content-Type": "application/json"},
            json={
                "type": "channel.channel_points_custom_reward_redemption.add",
                "version": "1",
                "condition": {"broadcaster_user_id": broadcaster_user_id},
                "transport": {"method": "webhook", "callback": callback_url, "secret": secret},
            }
        )
        data = await resp.json()
        if resp.status == 409:
            logger.info(f"EventSub already subscribed for broadcaster {broadcaster_user_id}")
        elif resp.status in (200, 202):
            logger.info(f"EventSub registered for broadcaster {broadcaster_user_id}: {data}")
        else:
            logger.warning(f"EventSub registration failed for {broadcaster_user_id}: {resp.status} {data}")
    except Exception as e:
        logger.error(f"Error registering EventSub for {broadcaster_user_id}: {e}")

# ── EventSub Webhook ──────────────────────────────────────────────────────────
async def eventsub_callback(request):
    """Receive EventSub events from Twitch and push to overlay websockets."""
    import hmac, hashlib
    body = await request.read()
    secret = os.getenv("EVENTSUB_SECRET")
    if not secret:
        logger.error("EVENTSUB_SECRET env var not set — rejecting EventSub callback")
        raise web.HTTPInternalServerError(reason="Server misconfiguration")
    secret = secret.encode()

    # Verify signature
    msg_id        = request.headers.get("Twitch-Eventsub-Message-Id", "")
    msg_timestamp = request.headers.get("Twitch-Eventsub-Message-Timestamp", "")
    msg_signature = request.headers.get("Twitch-Eventsub-Message-Signature", "")
    hmac_msg = (msg_id + msg_timestamp + body.decode()).encode()
    expected = "sha256=" + hmac.new(secret, hmac_msg, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, msg_signature):
        raise web.HTTPForbidden(reason="Invalid signature")

    import json as _json
    data = _json.loads(body)
    msg_type = request.headers.get("Twitch-Eventsub-Message-Type", "")

    # Twitch sends a challenge to verify the webhook
    if msg_type == "webhook_callback_verification":
        return web.Response(text=data["challenge"], content_type="text/plain")

    # Deduplicate by message ID — Twitch may re-deliver the same event
    now = datetime.utcnow()
    if msg_id in _eventsub_seen:
        logger.debug(f"Dropping duplicate EventSub message {msg_id}")
        return web.Response(status=204)
    _eventsub_seen[msg_id] = now
    # Clean up entries older than 10 minutes
    cutoff = now.timestamp() - 600
    stale = [k for k, v in _eventsub_seen.items() if v.timestamp() < cutoff]
    for k in stale:
        del _eventsub_seen[k]

    if msg_type == "notification":
        sub_type = data.get("subscription", {}).get("type", "")
        event = data.get("event", {})

        if sub_type == "stream.online":
            user_login = event.get("broadcaster_user_login", "").lower()
            user_id    = event.get("broadcaster_user_id", "")
            logger.info(f"EventSub stream.online received for {user_login}")
            if _bot_ref:
                asyncio.create_task(_bot_ref.handle_stream_online(user_login, user_id))

        elif sub_type == "stream.offline":
            user_login = event.get("broadcaster_user_login", "").lower()
            logger.info(f"EventSub stream.offline received for {user_login}")
            if _bot_ref:
                asyncio.create_task(_bot_ref.handle_stream_offline(user_login))

        elif sub_type == "channel.channel_points_custom_reward_redemption.add":
            reward_id         = event.get("reward", {}).get("id")
            broadcaster_login = event.get("broadcaster_user_login", "").lower()
            redeemer          = event.get("user_name", "")

            # Find which guild this broadcaster belongs to
            rows = await db_fetch("SELECT guild_id FROM broadcaster_tokens WHERE twitch_login = ?", (broadcaster_login,))
            for row in rows:
                guild_id = str(row["guild_id"])
                trigger_rows = await db_fetch(
                    "SELECT video_url, volume FROM reward_triggers WHERE guild_id = ? AND reward_id = ?",
                    (guild_id, reward_id)
                )
                if trigger_rows:
                    trigger = trigger_rows[0]
                    import json as _json
                    dead = set()
                    # Send video trigger if set
                    if trigger["video_url"]:
                        payload = _json.dumps({
                            "type": "play",
                            "video_url": trigger["video_url"],
                            "volume": trigger["volume"],
                            "redeemer": redeemer,
                        })
                        for ws in _overlay_connections.get(guild_id, set()):
                            try:
                                await ws.send_str(payload)
                            except Exception:
                                dead.add(ws)
                    # Send hotkey trigger if set and broadcaster is live
                    if trigger.get("hotkey"):
                        broadcaster_login_check = broadcaster_login
                        is_live = _bot_ref and broadcaster_login_check in _bot_ref.live_streamers if _bot_ref else False
                        if is_live:
                            hotkey_payload = _json.dumps({
                                "type": "hotkey",
                                "hotkey_name": f"reward_{reward_id}",
                                "hotkey_keys": trigger["hotkey"],
                                "reward_title": trigger.get("reward_title", ""),
                                "redeemer": redeemer,
                            })
                            for ws in _overlay_connections.get(guild_id, set()):
                                try:
                                    await ws.send_str(hotkey_payload)
                                except Exception:
                                    dead.add(ws)
                    if dead:
                        _overlay_connections.get(guild_id, set()).difference_update(dead)

    return web.Response(status=204)

# ── Overlay WebSocket ─────────────────────────────────────────────────────────
async def overlay_ws(request):
    """WebSocket endpoint for OBS browser source overlays."""
    import asyncio as _asyncio
    guild_id = request.match_info["guild_id"]
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    _overlay_connections.setdefault(guild_id, set()).add(ws)
    # Send saved volume immediately on connect so OBS picks it up
    try:
        volume = _bot_ref.db.get_overlay_volume(int(guild_id)) if _bot_ref else 100
        await ws.send_str(json.dumps({"type": "set_volume", "volume": volume}))
    except Exception:
        pass
    try:
        async for msg in ws:
            pass  # overlay only receives, doesn't need to send back
    except _asyncio.CancelledError:
        pass
    except Exception as e:
        logger.debug(f"Overlay WS closed for guild {guild_id}: {e}")
    finally:
        _overlay_connections.get(guild_id, set()).discard(ws)
        if not ws.closed:
            await ws.close()
    return ws

# ── Overlay HTML Page ─────────────────────────────────────────────────────────
async def overlay_page(request):
    """Serve the OBS browser source overlay page."""
    guild_id = request.match_info["guild_id"]
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:transparent; overflow:hidden; width:100vw; height:100vh; }}
  #frame-wrap {{
    display:none;
    position:fixed;
    inset:0;
    align-items:center;
    justify-content:center;
    pointer-events:none;
  }}
  #video-container {{
    position:relative;
    width:100vw;
    height:100vh;
    display:flex;
    align-items:center;
    justify-content:center;
    flex-shrink:0;
  }}
  #player-sizer {{
    position:relative;
    /* Fit 16:9 as large as possible inside the container */
    width:min(100vw, calc(100vh * 16 / 9));
    height:min(100vh, calc(100vw * 9 / 16));
    pointer-events:none;
  }}
  #yt-player, #yt-player iframe {{ display:block; width:100%; height:100%; pointer-events:none; }}
  #bottom-overlay {{
    position:absolute;
    bottom:0; left:0; right:0;
    z-index:11;
    padding:32px 16px 10px 16px;
    background:linear-gradient(to top, rgba(0,0,0,0.75) 0%, transparent 100%);
    display:flex;
    flex-direction:column;
    gap:5px;
  }}
  #rdm {{
    font-family:sans-serif;
    font-size:16px;
    color:#fff;
    text-shadow:0 1px 6px #000;
    display:none;
    white-space:nowrap;
  }}
  #progress-wrap {{
    display:none;
    flex-direction:column;
    gap:3px;
  }}
  #progress-bar-bg {{
    width:100%; height:8px; border-radius:4px;
    background:rgba(255,255,255,0.2);
    overflow:hidden;
  }}
  #progress-bar-fill {{
    height:100%; width:0%; border-radius:4px;
    background:linear-gradient(90deg,#00f5d4,#9146ff);
    transition:width 0.5s linear;
  }}
  #progress-timer {{
    font-family:'JetBrains Mono',monospace,sans-serif;
    font-size:11px; color:rgba(255,255,255,0.7);
    text-align:right;
  }}
</style>
</head>
<body>
<div id="frame-wrap">
  <div id="video-container">
    <div id="player-sizer">
      <div id="yt-player"></div>
      <div id="bottom-overlay">
        <div id="rdm"></div>
        <div id="progress-wrap">
          <div id="progress-bar-bg"><div id="progress-bar-fill"></div></div>
          <div id="progress-timer">0:00</div>
        </div>
      </div>
    </div>
  </div>
</div>
<script src="https://www.youtube.com/iframe_api"></script>
<script>
const guildId = "{guild_id}";
const rdm = document.getElementById("rdm");
const frameWrap = document.getElementById("frame-wrap");
const progressWrap = document.getElementById("progress-wrap");
const progressFill = document.getElementById("progress-bar-fill");
const progressTimer = document.getElementById("progress-timer");
const queue = [];
let playing = false;
let player = null;
let ytReady = false;
let savedVolume = 100;
let progressInterval = null;


function formatTime(seconds) {{
  const s = Math.floor(seconds);
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}}

function startProgress() {{
  if (progressInterval) clearInterval(progressInterval);
  progressWrap.style.display = "flex";
  progressInterval = setInterval(() => {{
    if (!player || typeof player.getCurrentTime !== "function") return;
    const current = player.getCurrentTime();
    const duration = player.getDuration();
    if (!duration || duration <= 0) return;
    const remaining = Math.max(0, duration - current);
    const pct = Math.min(100, (current / duration) * 100);
    progressFill.style.width = pct + "%";
    progressTimer.textContent = formatTime(remaining);
  }}, 500);
}}

function stopProgress() {{
  if (progressInterval) clearInterval(progressInterval);
  progressInterval = null;
  progressWrap.style.display = "none";
  progressFill.style.width = "0%";
  progressTimer.textContent = "0:00";
}}

function onYouTubeIframeAPIReady() {{
  console.log("YouTube IFrame API ready");
  ytReady = true;
  processQueue();
}}

function extractVideoId(url) {{
  try {{
    const u = new URL(url);
    if (u.hostname.includes("youtu.be")) return u.pathname.slice(1).split("?")[0];
    if (u.pathname.includes("/shorts/")) return u.pathname.split("/shorts/")[1].split("?")[0];
    return u.searchParams.get("v") || null;
  }} catch {{ return null; }}
}}

const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
const ws = new WebSocket(wsProto + "//" + location.host + "/overlay/" + guildId + "/ws");

ws.onmessage = e => {{
  const msg = JSON.parse(e.data);
  console.log("Overlay received:", msg);
  if (msg.type === "set_volume") {{
    savedVolume = msg.volume;
    if (player) player.setVolume(msg.volume);
  }}
  if (msg.type === "play") {{ queue.push(msg); processQueue(); }}
  if (msg.type === "skip") {{
    if (player) {{ player.stopVideo(); }}
    frameWrap.style.display = "none";
    rdm.style.display = "none";
    stopProgress();
    playing = false;
    setTimeout(processQueue, 100);
  }}
  if (msg.type === "stop") {{
    queue.length = 0;
    if (player) {{ player.stopVideo(); }}
    frameWrap.style.display = "none";
    rdm.style.display = "none";
    stopProgress();
    playing = false;
  }}
}};

ws.onclose = () => {{ setTimeout(() => location.reload(), 3000); }};

function onPlayerStateChange(e) {{
  if (e.data === YT.PlayerState.PLAYING) {{
    startProgress();
  }}
  if (e.data === YT.PlayerState.ENDED && playing) {{
    frameWrap.style.display = "none";
    rdm.style.display = "none";
    stopProgress();
    playing = false;
    setTimeout(processQueue, 500);
  }}
}}

function processQueue() {{
  if (playing || queue.length === 0) return;
  if (!ytReady) {{
    console.log("ytReady not set yet, retrying in 500ms");
    setTimeout(processQueue, 500);
    return;
  }}
  const item = queue.shift();
  const videoId = extractVideoId(item.video_url);
  console.log("Playing videoId:", videoId, "ytReady:", ytReady);
  if (!videoId) {{ playing = false; setTimeout(processQueue, 500); return; }}
  playing = true;
  const volume = savedVolume;
  rdm.textContent = item.redeemer ? item.redeemer + " redeemed!" : "";
  rdm.style.display = item.redeemer ? "block" : "none";
  frameWrap.style.display = "flex";
  if (player) {{
    player.loadVideoById(videoId);
    player.setVolume(volume);
  }} else {{
    player = new YT.Player("yt-player", {{
      height: "100%", width: "100%",
      videoId: videoId,
      playerVars: {{ autoplay: 1, controls: 0, disablekb: 1, modestbranding: 1, rel: 0, iv_load_policy: 3 }},
      events: {{
        onReady: e => {{ e.target.setVolume(volume); e.target.playVideo(); }},
        onStateChange: onPlayerStateChange
      }}
    }});
  }}
}}
</script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

# ── Broadcaster Info + Rewards ────────────────────────────────────────────────
async def get_broadcaster_info(request):
    """Return connection status and channel rewards for a guild."""
    guild_id = request.match_info["guild_id"]
    rows = await db_fetch("SELECT twitch_login, twitch_user_id, access_token FROM broadcaster_tokens WHERE guild_id = ?", (guild_id,))
    if not rows:
        return web.json_response({"connected": False})

    token_row = rows[0]
    access_token = token_row["access_token"]
    twitch_login = token_row["twitch_login"]
    broadcaster_id = token_row["twitch_user_id"]

    # Fetch channel rewards from Twitch
    rewards = []
    try:
        sess = get_http_session()
        resp = await sess.get(
            f"{TWITCH_API}/channel_points/custom_rewards",
            headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {access_token}"},
            params={"broadcaster_id": broadcaster_id}
        )
        if resp.status == 200:
            data = await resp.json()
            # Also fetch app-managed IDs to mark which ones are editable
            mgmt_resp = await sess.get(
                f"{TWITCH_API}/channel_points/custom_rewards",
                headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {access_token}"},
                params={"broadcaster_id": broadcaster_id, "only_manageable_rewards": "true"}
            )
            manageable_ids = set()
            if mgmt_resp.status == 200:
                mgmt_data = await mgmt_resp.json()
                manageable_ids = {r["id"] for r in mgmt_data.get("data", [])}
            rewards = [{"id": r["id"], "title": r["title"], "cost": r["cost"],
                        "is_enabled": r["is_enabled"], "background_color": r.get("background_color", "#9146FF"),
                        "manageable": r["id"] in manageable_ids}
                       for r in data.get("data", [])]
        elif resp.status == 401:
            return web.json_response({"connected": False, "expired": True})
        elif resp.status == 403:
            # Not affiliate/partner
            rows2 = await db_fetch("SELECT twitch_login FROM broadcaster_tokens WHERE guild_id = ?", (guild_id,))
            return web.json_response({"connected": True, "not_affiliate": True, "twitch_login": rows2[0]["twitch_login"] if rows2 else "", "rewards": [], "triggers": [], "overlay_url": f"https://excelprotocol.fly.dev/overlay/{guild_id}", "overlay_volume": _bot_ref.db.get_overlay_volume(int(guild_id)) if _bot_ref else 100})
    except Exception as e:
        logger.error(f"Error fetching rewards for guild {guild_id}: {e}")

    # Get existing triggers
    triggers = await db_fetch("SELECT reward_id, reward_title, video_url, volume, hotkey FROM reward_triggers WHERE guild_id = ?", (guild_id,))

    return web.json_response({
        "connected": True,
        "twitch_login": twitch_login,
        "rewards": rewards,
        "triggers": triggers,
        "overlay_url": f"https://excelprotocol.fly.dev/overlay/{guild_id}",
        "overlay_volume": _bot_ref.db.get_overlay_volume(int(guild_id)) if _bot_ref else 100,
    })

async def upsert_reward_trigger(request):
    """Add or update a video trigger for a reward."""
    guild_id     = request.match_info["guild_id"]
    body         = await request.json()
    reward_id    = body.get("reward_id", "").strip()
    reward_title = body.get("reward_title", "").strip()
    video_url    = body.get("video_url", "").strip()
    volume       = float(body.get("volume", 1.0))
    hotkey       = body.get("hotkey", None)  # e.g. "ctrl+alt+1" or null to clear
    if not reward_id:
        raise web.HTTPBadRequest(reason="reward_id is required")
    await db_execute(
        "INSERT INTO reward_triggers (guild_id, reward_id, reward_title, video_url, volume, hotkey) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(guild_id, reward_id) DO UPDATE SET reward_title=excluded.reward_title, video_url=excluded.video_url, volume=excluded.volume, hotkey=excluded.hotkey",
        (guild_id, reward_id, reward_title, video_url, volume, hotkey)
    )
    return web.json_response({"ok": True})

async def delete_reward_trigger(request):
    guild_id  = request.match_info["guild_id"]
    reward_id = request.match_info["reward_id"]
    await db_execute("DELETE FROM reward_triggers WHERE guild_id = ? AND reward_id = ?", (guild_id, reward_id))
    return web.json_response({"ok": True})

async def create_reward(request):
    """Create a new channel point reward on Twitch."""
    guild_id = request.match_info["guild_id"]
    body = await request.json()
    rows = await db_fetch("SELECT access_token, twitch_user_id FROM broadcaster_tokens WHERE guild_id = ?", (guild_id,))
    if not rows:
        raise web.HTTPUnauthorized(reason="No Twitch account connected")
    access_token   = rows[0]["access_token"]
    broadcaster_id = rows[0]["twitch_user_id"]
    sess = get_http_session()
    resp = await sess.post(
        f"{TWITCH_API}/channel_points/custom_rewards",
        headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        params={"broadcaster_id": broadcaster_id},
        json={"title": body.get("title", "New Reward"), "cost": int(body.get("cost", 100)),
              "is_enabled": body.get("is_enabled", True)}
    )
    data = await resp.json()
    if resp.status not in (200, 201):
        raise web.HTTPBadRequest(reason=data.get("message", "Failed to create reward"))
    reward = data["data"][0]
    return web.json_response({"ok": True, "reward": {"id": reward["id"], "title": reward["title"], "cost": reward["cost"]}})

async def edit_reward(request):
    """Edit an existing channel point reward on Twitch."""
    guild_id  = request.match_info["guild_id"]
    reward_id = request.match_info["reward_id"]
    body = await request.json()
    rows = await db_fetch("SELECT access_token, twitch_user_id FROM broadcaster_tokens WHERE guild_id = ?", (guild_id,))
    if not rows:
        raise web.HTTPUnauthorized(reason="No Twitch account connected")
    access_token   = rows[0]["access_token"]
    broadcaster_id = rows[0]["twitch_user_id"]
    patch = {}
    if "title"      in body: patch["title"]      = body["title"]
    if "cost"       in body: patch["cost"]        = int(body["cost"])
    if "is_enabled" in body: patch["is_enabled"]  = body["is_enabled"]
    sess = get_http_session()
    resp = await sess.patch(
        f"{TWITCH_API}/channel_points/custom_rewards",
        headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        params={"broadcaster_id": broadcaster_id, "id": reward_id},
        json=patch
    )
    if resp.status not in (200, 204):
        data = await resp.json()
        raise web.HTTPBadRequest(reason=data.get("message", "Failed to edit reward"))
    return web.json_response({"ok": True})

async def delete_reward(request):
    """Delete a channel point reward from Twitch."""
    guild_id  = request.match_info["guild_id"]
    reward_id = request.match_info["reward_id"]
    rows = await db_fetch("SELECT access_token, twitch_user_id FROM broadcaster_tokens WHERE guild_id = ?", (guild_id,))
    if not rows:
        raise web.HTTPUnauthorized(reason="No Twitch account connected")
    access_token   = rows[0]["access_token"]
    broadcaster_id = rows[0]["twitch_user_id"]
    sess = get_http_session()
    resp = await sess.delete(
        f"{TWITCH_API}/channel_points/custom_rewards",
        headers={"Client-ID": TWITCH_CLIENT_ID, "Authorization": f"Bearer {access_token}"},
        params={"broadcaster_id": broadcaster_id, "id": reward_id}
    )
    if resp.status not in (200, 204):
        raise web.HTTPBadRequest(reason="Failed to delete reward")
    # Also remove trigger if exists
    await db_execute("DELETE FROM reward_triggers WHERE guild_id = ? AND reward_id = ?", (guild_id, reward_id))
    return web.json_response({"ok": True})

# ── Permission Issues ─────────────────────────────────────────────────────────
async def get_permission_issues(request):
    """Return current permission issues for a guild."""
    guild_id = request.match_info["guild_id"]
    rows = await db_fetch(
        "SELECT channel_id, missing, detected_at FROM permission_issues WHERE guild_id = ? ORDER BY detected_at DESC",
        (guild_id,)
    )
    result = []
    for r in rows:
        ch_name = await get_channel_name(str(r["channel_id"]))
        result.append({
            "channel_id":   str(r["channel_id"]),
            "channel_name": ch_name,
            "missing":      r["missing"].split(","),
            "detected_at":  r["detected_at"],
        })
    return web.json_response(result)

async def get_unresolvable_streamers(request):
    """Return streamers that Twitch can no longer resolve for this guild."""
    guild_id = request.match_info["guild_id"]
    rows = await db_fetch(
        "SELECT streamer_name, detected_at FROM unresolvable_streamers WHERE guild_id = ? ORDER BY streamer_name",
        (guild_id,)
    )
    return web.json_response([{"streamer_name": r["streamer_name"], "detected_at": r["detected_at"]} for r in rows])

async def recheck_permissions(request):
    """Trigger an immediate permission re-check for a guild via the bot."""
    guild_id = request.match_info["guild_id"]
    if _bot_ref is None:
        raise web.HTTPServiceUnavailable(reason="Bot not available")
    guild = _bot_ref.get_guild(int(guild_id))
    if not guild:
        raise web.HTTPNotFound(reason="Guild not found")
    import asyncio as _asyncio
    _asyncio.create_task(_bot_ref._check_guild_permissions(guild))
    return web.json_response({"ok": True})


async def fix_permissions(request):
    """Attempt to auto-fix missing channel permissions by adding overwrites."""
    guild_id = request.match_info["guild_id"]
    channel_id = int(request.match_info["channel_id"])

    if _bot_ref is None:
        raise web.HTTPServiceUnavailable(reason="Bot not available")
    guild = _bot_ref.get_guild(int(guild_id))
    if not guild:
        raise web.HTTPNotFound(reason="Guild not found")

    channel = guild.get_channel(channel_id)
    if not channel:
        return web.json_response({"ok": False, "message": "Channel not found."})

    guild_perms = guild.me.guild_permissions

    # Check if we have manage_roles or manage_channels — needed to set overwrites
    can_fix = guild_perms.manage_roles or guild_perms.manage_channels or guild_perms.administrator
    if not can_fix:
        return web.json_response({
            "ok": False,
            "can_fix": False,
            "message": (
                "ExcelProtocol doesn't have **Manage Roles** or **Manage Channels** "
                "so it can't fix this automatically.\n\n"
                "**To fix manually in Discord:**\n"
                "1. Go to your server **Settings → Channels**\n"
                f"2. Select the channel **#{channel.name}**\n"
                "3. Click **Permissions → + Add member or role**\n"
                "4. Select **ExcelProtocol**\n"
                "5. Enable: **View Channel**, **Send Messages**, **Embed Links**, **Manage Messages**\n"
                "6. Save and click **Re-check** to confirm."
            )
        })

    # Attempt to set channel overwrites
    import discord as _discord
    try:
        overwrite = channel.overwrites_for(guild.me)
        overwrite.view_channel    = True
        overwrite.send_messages   = True
        overwrite.embed_links     = True
        overwrite.manage_messages = True
        await channel.set_permissions(guild.me, overwrite=overwrite, reason="ExcelProtocol auto-fix permissions")

        # Trigger a re-check to clear the warning if it worked
        asyncio.create_task(_bot_ref._check_guild_permissions(guild))

        return web.json_response({
            "ok": True,
            "can_fix": True,
            "message": f"Permissions updated for #{channel.name}. Running a re-check now..."
        })
    except _discord.Forbidden:
        return web.json_response({
            "ok": False,
            "can_fix": False,
            "message": (
                "Fix failed — ExcelProtocol's role may be too low in the hierarchy to set permissions.\n\n"
                "**To fix manually in Discord:**\n"
                "1. Go to your server **Settings → Channels**\n"
                f"2. Select the channel **#{channel.name}**\n"
                "3. Click **Permissions → + Add member or role**\n"
                "4. Select **ExcelProtocol**\n"
                "5. Enable: **View Channel**, **Send Messages**, **Embed Links**, **Manage Messages**\n"
                "6. Save and click **Re-check** to confirm."
            )
        })
    except Exception as e:
        logger.error(f"Error auto-fixing permissions for channel {channel_id}: {e}")
        return web.json_response({"ok": False, "can_fix": False, "message": f"Unexpected error: {str(e)[:200]}"})


# ── Stat Channels ─────────────────────────────────────────────────────────────
# ── Safety ────────────────────────────────────────────────────────────────────
async def get_safety_settings(request):
    guild_id = request.match_info["guild_id"]
    import asyncio as _asyncio
    if _bot_ref:
        settings = await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.get_safety_settings(int(guild_id)))
    else:
        rows = await db_fetch("SELECT * FROM safety_settings WHERE guild_id = ?", (guild_id,))
        settings = rows[0] if rows else None
    if not settings:
        return web.json_response({"enabled": False, "min_account_age_days": 7, "check_username_pattern": True,
                                   "check_no_avatar": True, "action": "kick",
                                   "bypass_role_id": None, "dm_on_kick": True})
    return web.json_response({
        "enabled":               bool(settings["enabled"]),
        "min_account_age_days":  settings["min_account_age_days"],
        "check_username_pattern": bool(settings["check_username_pattern"]),
        "check_no_avatar":       bool(settings["check_no_avatar"]),
        "action":                settings["action"],
        "bypass_role_id":        str(settings["bypass_role_id"]) if settings["bypass_role_id"] else None,
        "dm_on_kick":            bool(settings["dm_on_kick"]),
    })

async def set_safety_settings(request):
    guild_id = request.match_info["guild_id"]
    body = await request.json()
    import asyncio as _asyncio
    if _bot_ref:
        await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.set_safety_settings(
            int(guild_id),
            enabled=bool(body.get("enabled", False)),
            min_account_age_days=int(body.get("min_account_age_days", 7)),
            check_username_pattern=bool(body.get("check_username_pattern", True)),
            check_no_avatar=bool(body.get("check_no_avatar", True)),
            action=body.get("action", "kick"),
            bypass_role_id=int(body["bypass_role_id"]) if body.get("bypass_role_id") else None,
            dm_on_kick=bool(body.get("dm_on_kick", True)),
        ))
    else:
        await db_execute('''
            INSERT INTO safety_settings
                (guild_id, enabled, min_account_age_days, check_username_pattern,
                 check_no_avatar, action, bypass_role_id, dm_on_kick)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                enabled=excluded.enabled, min_account_age_days=excluded.min_account_age_days,
                check_username_pattern=excluded.check_username_pattern, check_no_avatar=excluded.check_no_avatar,
                action=excluded.action, bypass_role_id=excluded.bypass_role_id, dm_on_kick=excluded.dm_on_kick
        ''', (guild_id, int(body.get("enabled", False)), int(body.get("min_account_age_days", 7)),
              int(body.get("check_username_pattern", True)), int(body.get("check_no_avatar", True)),
              body.get("action", "kick"),
              int(body["bypass_role_id"]) if body.get("bypass_role_id") else None,
              int(body.get("dm_on_kick", True))))
    return web.json_response({"ok": True})

async def get_safety_kicks(request):
    guild_id = request.match_info["guild_id"]
    import asyncio as _asyncio
    if _bot_ref:
        kicks = await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.get_safety_kicks(int(guild_id), limit=100))
    else:
        rows = await db_fetch(
            "SELECT user_id, username, reason, action, kicked_at FROM safety_kicks WHERE guild_id = ? ORDER BY kicked_at DESC LIMIT 100",
            (guild_id,)
        )
        kicks = [{"user_id": str(r["user_id"]), "username": r["username"], "reason": r["reason"],
                  "action": r["action"], "kicked_at": r["kicked_at"]} for r in rows]
    return web.json_response(kicks)


# ── VC Creator ────────────────────────────────────────────────────────────────
async def get_vc_settings(request):
    guild_id = request.match_info["guild_id"]
    rows = await db_fetch("SELECT trigger_channel_id, name_template, category_id FROM vc_settings WHERE guild_id = ?", (guild_id,))
    if not rows:
        return web.json_response({"enabled": False})
    r = rows[0]
    # Get channel name for display
    trigger_name = str(r["trigger_channel_id"])
    if _bot_ref:
        guild = _bot_ref.get_guild(int(guild_id))
        if guild:
            ch = guild.get_channel(r["trigger_channel_id"])
            if ch:
                trigger_name = ch.name
    return web.json_response({
        "enabled": True,
        "trigger_channel_id": str(r["trigger_channel_id"]),
        "trigger_channel_name": trigger_name,
        "name_template": r["name_template"],
        "category_id": str(r["category_id"]) if r["category_id"] else None,
    })

async def set_vc_settings(request):
    guild_id = request.match_info["guild_id"]
    body = await request.json()
    trigger_channel_id = body.get("trigger_channel_id")
    name_template      = body.get("name_template", "{username}'s VC").strip() or "{username}'s VC"
    if not trigger_channel_id:
        raise web.HTTPBadRequest(reason="trigger_channel_id is required")
    import asyncio as _asyncio
    if _bot_ref:
        await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.set_vc_settings(
            int(guild_id), int(trigger_channel_id), name_template
        ))
    else:
        await db_execute(
            "INSERT INTO vc_settings (guild_id, trigger_channel_id, name_template) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET trigger_channel_id=excluded.trigger_channel_id, name_template=excluded.name_template",
            (guild_id, trigger_channel_id, name_template)
        )
    return web.json_response({"ok": True})

async def delete_vc_settings(request):
    guild_id = request.match_info["guild_id"]
    await db_execute("DELETE FROM vc_settings WHERE guild_id = ?", (guild_id,))
    return web.json_response({"ok": True})


async def get_stat_channels(request):
    guild_id = request.match_info["guild_id"]
    rows = await db_fetch(
        "SELECT channel_id, format, last_updated FROM stat_channels WHERE guild_id = ?",
        (guild_id,)
    )
    result = []
    for r in rows:
        ch_name = str(r["channel_id"])
        if _bot_ref:
            guild = _bot_ref.get_guild(int(guild_id))
            if guild:
                ch = guild.get_channel(r["channel_id"])
                if ch:
                    ch_name = ch.name
        result.append({
            "channel_id":   str(r["channel_id"]),
            "channel_name": ch_name,
            "format":       r["format"],
            "last_updated": r["last_updated"],
        })
    return web.json_response(result)

async def set_stat_channel(request):
    guild_id = request.match_info["guild_id"]
    body = await request.json()
    channel_id = body.get("channel_id")
    fmt = body.get("format", "Members: {count}").strip()
    if not channel_id:
        raise web.HTTPBadRequest(reason="channel_id is required")
    if "{count}" not in fmt:
        raise web.HTTPBadRequest(reason="format must contain {count}")
    await db_execute(
        "INSERT INTO stat_channels (guild_id, channel_id, format) VALUES (?, ?, ?) ON CONFLICT(guild_id, channel_id) DO UPDATE SET format = excluded.format",
        (guild_id, int(channel_id), fmt)
    )
    # Trigger an immediate update via the bot
    if _bot_ref:
        import asyncio as _asyncio
        async def _immediate_update():
            try:
                guild = _bot_ref.get_guild(int(guild_id))
                if not guild:
                    return
                channel = guild.get_channel(int(channel_id))
                if not channel:
                    return
                new_name = fmt.replace('{count}', f'{guild.member_count:,}')
                await channel.edit(name=new_name, reason="ExcelProtocol stat update")
                await db_execute(
                    "UPDATE stat_channels SET last_updated = CURRENT_TIMESTAMP WHERE guild_id = ? AND channel_id = ?",
                    (guild_id, int(channel_id))
                )
            except Exception as e:
                logger.warning(f"Immediate stat update failed: {e}")
        _asyncio.create_task(_immediate_update())
    return web.json_response({"ok": True})

async def delete_stat_channel(request):
    guild_id   = request.match_info["guild_id"]
    channel_id = request.match_info["channel_id"]
    await db_execute(
        "DELETE FROM stat_channels WHERE guild_id = ? AND channel_id = ?",
        (guild_id, int(channel_id))
    )
    return web.json_response({"ok": True})

# ── Dev: Global Stats ─────────────────────────────────────────────────────────
async def get_global_stats(request):
    """Dev-only: global stats across all servers."""
    session = request["session"]
    if not session.get("dev"):
        raise web.HTTPForbidden(reason="Dev access required")

    servers        = await db_fetch("SELECT COUNT(DISTINCT guild_id) AS c FROM server_settings")
    streamer_rows  = await db_fetch("SELECT COUNT(*) AS c FROM monitored_streamers")
    unique_str     = await db_fetch("SELECT COUNT(DISTINCT streamer_name) AS c FROM monitored_streamers")
    notif_msgs     = await db_fetch("SELECT COUNT(*) AS c FROM notification_messages")
    last_24h       = await db_fetch("""
        SELECT COUNT(*) AS c FROM notification_log
        WHERE sent_at >= datetime('now', '-24 hours') AND status = 'sent'
    """)
    top_streamers  = await db_fetch("""
        SELECT streamer_name, COUNT(DISTINCT guild_id) AS server_count
        FROM monitored_streamers GROUP BY streamer_name
        ORDER BY server_count DESC LIMIT 15
    """)
    servers_by_count = await db_fetch("""
        SELECT guild_id, COUNT(*) AS streamer_count
        FROM monitored_streamers GROUP BY guild_id
        ORDER BY streamer_count DESC LIMIT 10
    """)
    # Enrich with guild names
    enriched_servers = []
    for r in servers_by_count:
        name = str(r["guild_id"])
        if _bot_ref:
            g = _bot_ref.get_guild(r["guild_id"])
            if g:
                name = g.name
        enriched_servers.append({"guild_id": str(r["guild_id"]), "name": name, "streamer_count": r["streamer_count"]})

    # Live streamers from bot memory
    live_count = len(_bot_ref.live_streamers) if _bot_ref else 0
    live_list  = sorted(_bot_ref.live_streamers) if _bot_ref else []

    # EventSub subscription count
    eventsub_count = 0
    try:
        if _bot_ref:
            subs = await _bot_ref.twitch.get_subscriptions()
            eventsub_count = len([s for s in subs if s.get("type") in ("stream.online", "stream.offline")])
    except Exception:
        pass

    return web.json_response({
        "total_servers":       servers[0]["c"] if servers else 0,
        "total_streamer_rows": streamer_rows[0]["c"] if streamer_rows else 0,
        "unique_streamers":    unique_str[0]["c"] if unique_str else 0,
        "active_notifications": notif_msgs[0]["c"] if notif_msgs else 0,
        "notifications_24h":   last_24h[0]["c"] if last_24h else 0,
        "live_count":          live_count,
        "live_streamers":      live_list,
        "eventsub_count":      eventsub_count,
        "top_streamers":       top_streamers,
        "servers_by_count":    enriched_servers,
    })


# ── Dev: DB Tools ─────────────────────────────────────────────────────────────
async def db_tools_status(request):
    """Dev-only: show orphaned records and fixable issues."""
    session = request["session"]
    if not session.get("dev"):
        raise web.HTTPForbidden(reason="Dev access required")

    # Orphaned notification_messages (streamer no longer monitored anywhere)
    orphaned_notifs = await db_fetch("""
        SELECT DISTINCT streamer_name FROM notification_messages
        WHERE streamer_name NOT IN (SELECT DISTINCT streamer_name FROM monitored_streamers)
    """)
    # Orphaned permission_issues (channel no longer a notification channel)
    orphaned_perms = await db_fetch("""
        SELECT guild_id, channel_id FROM permission_issues
        WHERE channel_id NOT IN (
            SELECT DISTINCT channel_id FROM monitored_streamers
            UNION SELECT DISTINCT custom_channel_id FROM monitored_streamers WHERE custom_channel_id IS NOT NULL
        )
    """)
    # Bad streamer names (contain /)
    bad_names = await db_fetch("""
        SELECT streamer_name, COUNT(DISTINCT guild_id) AS guild_count
        FROM monitored_streamers WHERE streamer_name LIKE '%/%'
        GROUP BY streamer_name
    """)
    # notification_log row count
    log_count = await db_fetch("SELECT COUNT(*) AS c FROM notification_log")
    # stat_channels
    stat_channels = await db_fetch("SELECT guild_id, channel_id, format, last_updated FROM stat_channels")

    return web.json_response({
        "orphaned_notification_messages": [r["streamer_name"] for r in orphaned_notifs],
        "orphaned_permission_issues": [{"guild_id": str(r["guild_id"]), "channel_id": str(r["channel_id"])} for r in orphaned_perms],
        "bad_streamer_names": [{"name": r["streamer_name"], "guild_count": r["guild_count"]} for r in bad_names],
        "notification_log_rows": log_count[0]["c"] if log_count else 0,
        "stat_channels": [{"guild_id": str(r["guild_id"]), "channel_id": str(r["channel_id"]), "format": r["format"], "last_updated": r["last_updated"]} for r in stat_channels],
    })


async def db_tools_action(request):
    """Dev-only: run a DB cleanup action."""
    session = request["session"]
    if not session.get("dev"):
        raise web.HTTPForbidden(reason="Dev access required")
    body = await request.json()
    action = body.get("action")

    if action == "clear_orphaned_notifications":
        await db_execute("""
            DELETE FROM notification_messages
            WHERE streamer_name NOT IN (SELECT DISTINCT streamer_name FROM monitored_streamers)
        """)
        return web.json_response({"ok": True, "message": "Orphaned notification_messages cleared."})

    elif action == "clear_orphaned_perms":
        await db_execute("""
            DELETE FROM permission_issues
            WHERE channel_id NOT IN (
                SELECT DISTINCT channel_id FROM monitored_streamers
                UNION SELECT DISTINCT custom_channel_id FROM monitored_streamers WHERE custom_channel_id IS NOT NULL
            )
        """)
        return web.json_response({"ok": True, "message": "Orphaned permission_issues cleared."})

    elif action == "fix_bad_streamer_names":
        rows = await db_fetch("SELECT rowid, streamer_name FROM monitored_streamers WHERE streamer_name LIKE '%/%'")
        fixed = 0
        for r in rows:
            raw = r["streamer_name"]
            clean = raw.split("/")[-1].split("?")[0].strip().lower()
            if clean and clean != raw:
                await db_execute("UPDATE monitored_streamers SET streamer_name = ? WHERE rowid = ?", (clean, r["rowid"]))
                fixed += 1
        return web.json_response({"ok": True, "message": f"Fixed {fixed} bad streamer name(s)."})

    elif action == "trim_notification_log":
        days = int(body.get("days", 30))
        await db_execute(f"DELETE FROM notification_log WHERE sent_at < datetime('now', '-{days} days')")
        return web.json_response({"ok": True, "message": f"Trimmed notification_log to last {days} days."})

    elif action == "clear_live_streamers":
        if _bot_ref:
            count = len(_bot_ref.live_streamers)
            _bot_ref.live_streamers.clear()
            return web.json_response({"ok": True, "message": f"Cleared {count} live_streamers from memory."})
        return web.json_response({"ok": False, "message": "Bot not available."})

    elif action == "sync_eventsub":
        if _bot_ref:
            asyncio.create_task(_bot_ref._sync_eventsub_subscriptions())
            return web.json_response({"ok": True, "message": "EventSub sync triggered."})
        return web.json_response({"ok": False, "message": "Bot not available."})

    raise web.HTTPBadRequest(reason=f"Unknown action: {action}")


# ── Legal Pages ───────────────────────────────────────────────────────────────
_LEGAL_CSS = """
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
         background: #0a0f16; color: #e2e8f0; line-height: 1.7; }
  .wrap { max-width: 780px; margin: 0 auto; padding: 48px 28px 80px; }
  .logo { font-size: 13px; font-weight: 700; color: #00f5d4; letter-spacing: 2px;
          text-transform: uppercase; margin-bottom: 8px; }
  h1.title { font-size: 36px; font-weight: 800; color: #fff; margin-bottom: 6px; }
  .subtitle { font-size: 13px; color: #64748b; margin-bottom: 48px; }
  h2 { font-size: 18px; font-weight: 700; color: #00f5d4; margin: 36px 0 10px;
       padding-bottom: 6px; border-bottom: 1px solid rgba(0,245,212,0.15); }
  p { margin-bottom: 14px; color: #cbd5e1; font-size: 15px; }
  ul { margin: 0 0 14px 24px; }
  li { color: #cbd5e1; font-size: 15px; margin-bottom: 6px; }
  .footer { margin-top: 60px; padding-top: 20px; border-top: 1px solid #1e293b;
            font-size: 12px; color: #475569; }
  a { color: #00f5d4; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .back { display: inline-block; margin-bottom: 32px; font-size: 13px;
          color: #64748b; }
  .back:hover { color: #00f5d4; }
"""

def _legal_html(title, subtitle, body_html):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} — ExcelProtocol</title>
  <style>{_LEGAL_CSS}</style>
</head>
<body>
  <div class="wrap">
    <div class="logo">ExcelProtocol</div>
    <h1 class="title">{title}</h1>
    <div class="subtitle">{subtitle}</div>
    {body_html}
    <div class="footer">
      ExcelProtocol is an independent project and is not affiliated with Discord Inc. or Twitch Interactive, Inc.<br>
      <a href="/terms">Terms of Service</a> &nbsp;·&nbsp; <a href="/privacy">Privacy Policy</a>
    </div>
  </div>
</body>
</html>"""


async def landing_page(request):
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ExcelProtocol — Twitch Stream Notifications for Discord</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@700;800;900&family=JetBrains+Mono:wght@400;500&family=Outfit:wght@300;400;500;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg:       #080c12;
      --bg2:      #0d1420;
      --cyan:     #00f5d4;
      --cyan2:    #00c4aa;
      --cyan-dim: rgba(0,245,212,0.08);
      --green:    #39d98a;
      --text:     #f0f4f8;
      --text2:    #94a3b8;
      --text3:    #4a5568;
      --border:   rgba(0,245,212,0.12);
      --purple:   #a78bfa;
    }
    * { margin:0; padding:0; box-sizing:border-box; }
    html { scroll-behavior:smooth; }
    body {
      font-family:'Outfit',sans-serif;
      background:var(--bg);
      color:var(--text);
      overflow-x:hidden;
    }

    /* ── Canvas background ── */
    #bg-canvas {
      position:fixed; inset:0; z-index:0; pointer-events:none;
      opacity:0.4;
    }

    /* ── Nav ── */
    nav {
      position:fixed; top:0; left:0; right:0; z-index:100;
      display:flex; align-items:center; justify-content:space-between;
      padding:0 40px; height:60px;
      background:rgba(8,12,18,0.85);
      backdrop-filter:blur(16px);
      border-bottom:1px solid var(--border);
    }
    .nav-logo {
      display:flex; align-items:center; gap:10px;
      font-family:'Orbitron',sans-serif; font-weight:800; font-size:17px;
      color:var(--text); text-decoration:none; letter-spacing:-0.3px;
    }
    .nav-logo img { width:30px; height:30px; border-radius:50%; border:1px solid var(--cyan); }
    .nav-links { display:flex; align-items:center; gap:8px; }
    .nav-link {
      padding:7px 16px; border-radius:8px; font-size:13px; font-weight:500;
      color:var(--text2); text-decoration:none; transition:color 0.2s;
      font-family:'Outfit',sans-serif;
    }
    .nav-link:hover { color:var(--text); }
    .nav-btn {
      padding:7px 18px; border-radius:8px; font-size:13px; font-weight:600;
      background:var(--cyan-dim); color:var(--cyan);
      border:1px solid rgba(0,245,212,0.3); text-decoration:none;
      transition:all 0.2s; font-family:'Outfit',sans-serif;
    }
    .nav-btn:hover { background:rgba(0,245,212,0.15); box-shadow:0 0 16px rgba(0,245,212,0.2); }

    /* ── Hero ── */
    .hero {
      position:relative; z-index:1;
      min-height:100vh;
      display:flex; flex-direction:column; align-items:center; justify-content:center;
      text-align:center; padding:80px 24px 60px;
    }
    .hero-badge {
      display:inline-flex; align-items:center; gap:7px;
      padding:5px 14px; border-radius:20px;
      background:rgba(57,217,138,0.08); border:1px solid rgba(57,217,138,0.25);
      font-size:11px; font-weight:600; color:var(--green);
      font-family:'JetBrains Mono',monospace; letter-spacing:1px;
      text-transform:uppercase; margin-bottom:28px;
      animation:fadeUp 0.6s ease both;
    }
    .pulse-dot {
      width:6px; height:6px; border-radius:50%; background:var(--green);
      animation:pulse 2s ease infinite;
      box-shadow:0 0 6px rgba(57,217,138,0.8);
    }
    @keyframes pulse {
      0%,100% { opacity:1; transform:scale(1); }
      50% { opacity:0.6; transform:scale(0.85); }
    }
    .hero-title {
      font-family:'Orbitron',sans-serif; font-weight:900;
      font-size:clamp(36px, 6vw, 72px);
      line-height:1.15; letter-spacing:0px;
      color:var(--text);
      animation:fadeUp 0.6s 0.1s ease both;
      margin-bottom:6px;
    }
    .hero-title span {
      background:linear-gradient(135deg, var(--cyan) 0%, var(--purple) 100%);
      -webkit-background-clip:text; -webkit-text-fill-color:transparent;
      background-clip:text;
    }
    .hero-sub {
      font-size:clamp(16px, 2.5vw, 20px); color:var(--text2); font-weight:300;
      max-width:540px; margin:20px auto 40px; line-height:1.6;
      animation:fadeUp 0.6s 0.2s ease both;
    }
    .hero-actions {
      display:flex; gap:12px; flex-wrap:wrap; justify-content:center;
      animation:fadeUp 0.6s 0.3s ease both;
    }
    .btn-primary {
      display:inline-flex; align-items:center; gap:8px;
      padding:14px 28px; border-radius:10px; font-size:15px; font-weight:700;
      background:var(--cyan); color:#080c12; text-decoration:none;
      font-family:'Outfit',sans-serif; letter-spacing:-0.2px;
      transition:all 0.2s; box-shadow:0 4px 24px rgba(0,245,212,0.3);
    }
    .btn-primary:hover { transform:translateY(-2px); box-shadow:0 8px 32px rgba(0,245,212,0.45); }
    .btn-secondary {
      display:inline-flex; align-items:center; gap:8px;
      padding:14px 28px; border-radius:10px; font-size:15px; font-weight:600;
      background:transparent; color:var(--text);
      border:1px solid rgba(255,255,255,0.15); text-decoration:none;
      font-family:'Outfit',sans-serif;
      transition:all 0.2s;
    }
    .btn-secondary:hover { background:rgba(255,255,255,0.05); border-color:rgba(255,255,255,0.25); }
    .hero-stats {
      display:flex; gap:40px; margin-top:64px; flex-wrap:wrap; justify-content:center;
      animation:fadeUp 0.6s 0.4s ease both;
    }
    .stat { text-align:center; }
    .stat-num {
      font-family:'Orbitron',sans-serif; font-weight:800; font-size:32px;
      color:var(--cyan); line-height:1;
    }
    .stat-label { font-size:12px; color:var(--text3); margin-top:4px; font-family:'JetBrains Mono',monospace; letter-spacing:0.5px; }

    /* ── Section ── */
    section {
      position:relative; z-index:1;
      padding:100px 24px;
    }
    .section-inner { max-width:1100px; margin:0 auto; }
    .section-label {
      font-family:'JetBrains Mono',monospace; font-size:10px; font-weight:500;
      color:var(--cyan); letter-spacing:3px; text-transform:uppercase;
      margin-bottom:12px;
    }
    .section-title {
      font-family:'Orbitron',sans-serif; font-weight:800;
      font-size:clamp(22px, 3vw, 34px); line-height:1.2;
      letter-spacing:0px; color:var(--text); margin-bottom:16px;
    }
    .section-sub { font-size:16px; color:var(--text2); max-width:520px; line-height:1.6; }

    /* ── Feature grid ── */
    .feature-grid {
      display:grid;
      grid-template-columns:repeat(auto-fill, minmax(300px, 1fr));
      gap:16px; margin-top:56px;
    }
    .feature-card {
      padding:28px; border-radius:14px;
      background:rgba(13,20,32,0.8);
      border:1px solid var(--border);
      transition:all 0.3s;
      position:relative; overflow:hidden;
    }
    .feature-card::before {
      content:''; position:absolute; inset:0;
      background:radial-gradient(circle at top left, rgba(0,245,212,0.05) 0%, transparent 60%);
      opacity:0; transition:opacity 0.3s;
    }
    .feature-card:hover { border-color:rgba(0,245,212,0.25); transform:translateY(-3px); }
    .feature-card:hover::before { opacity:1; }
    .feature-icon { font-size:26px; margin-bottom:14px; }
    .feature-title {
      font-family:'Orbitron',sans-serif; font-weight:700; font-size:16px;
      color:var(--text); margin-bottom:8px;
    }
    .feature-desc { font-size:14px; color:var(--text2); line-height:1.6; }
    .feature-tag {
      display:inline-block; margin-top:12px;
      padding:3px 8px; border-radius:4px; font-size:10px;
      font-family:'JetBrains Mono',monospace; font-weight:500;
      background:rgba(0,245,212,0.08); color:var(--cyan);
      border:1px solid rgba(0,245,212,0.15);
    }

    /* ── How it works ── */
    .steps { display:flex; flex-direction:column; gap:0; margin-top:56px; max-width:600px; }
    .step { display:flex; gap:24px; position:relative; padding-bottom:40px; }
    .step:last-child { padding-bottom:0; }
    .step-left { display:flex; flex-direction:column; align-items:center; }
    .step-num {
      width:40px; height:40px; border-radius:50%; flex-shrink:0;
      background:var(--cyan-dim); border:1px solid rgba(0,245,212,0.3);
      display:flex; align-items:center; justify-content:center;
      font-family:'JetBrains Mono',monospace; font-weight:700; font-size:13px;
      color:var(--cyan);
    }
    .step-line {
      width:1px; flex:1; background:linear-gradient(to bottom, rgba(0,245,212,0.2), transparent);
      margin-top:8px;
    }
    .step:last-child .step-line { display:none; }
    .step-content { padding-top:8px; }
    .step-title { font-family:'Orbitron',sans-serif; font-weight:700; font-size:16px; color:var(--text); margin-bottom:6px; }
    .step-desc { font-size:14px; color:var(--text2); line-height:1.6; }

    /* ── CTA ── */
    .cta-section {
      padding:100px 24px;
      position:relative; z-index:1;
    }
    .cta-inner {
      max-width:700px; margin:0 auto; text-align:center;
      padding:64px 40px;
      background:linear-gradient(135deg, rgba(0,245,212,0.06) 0%, rgba(167,139,250,0.06) 100%);
      border-radius:24px; border:1px solid rgba(0,245,212,0.15);
      position:relative; overflow:hidden;
    }
    .cta-inner::before {
      content:''; position:absolute; top:-60px; left:50%; transform:translateX(-50%);
      width:300px; height:300px; border-radius:50%;
      background:radial-gradient(circle, rgba(0,245,212,0.08) 0%, transparent 70%);
      pointer-events:none;
    }
    .cta-title {
      font-family:'Orbitron',sans-serif; font-weight:800;
      font-size:clamp(22px, 3vw, 34px); line-height:1.2;
      letter-spacing:0px; margin-bottom:14px;
    }
    .cta-sub { font-size:16px; color:var(--text2); margin-bottom:36px; line-height:1.6; }

    /* ── Footer ── */
    footer {
      position:relative; z-index:1;
      border-top:1px solid var(--border);
      padding:32px 40px;
      display:flex; align-items:center; justify-content:space-between;
      flex-wrap:wrap; gap:16px;
    }
    .footer-left { font-size:13px; color:var(--text3); font-family:'JetBrains Mono',monospace; }
    .footer-links { display:flex; gap:20px; }
    .footer-link { font-size:13px; color:var(--text3); text-decoration:none; transition:color 0.2s; }
    .footer-link:hover { color:var(--cyan); }

    @keyframes fadeUp {
      from { opacity:0; transform:translateY(20px); }
      to   { opacity:1; transform:translateY(0); }
    }

    /* ── Divider ── */
    .divider {
      height:1px; background:linear-gradient(to right, transparent, var(--border), transparent);
      max-width:1100px; margin:0 auto;
    }
  </style>
</head>
<body>

<canvas id="bg-canvas"></canvas>

<!-- Nav -->
<nav>
  <a class="nav-logo" href="/">
    <img src="data:image/png;base64,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" alt="ExcelProtocol">
    ExcelProtocol
  </a>
  <div class="nav-links">
    <a class="nav-link" href="#features">Features</a>
    <a class="nav-link" href="#how-it-works">How it works</a>
    <a class="nav-link" href="/terms">Terms</a>
    <a class="nav-link" href="/privacy">Privacy</a>
    <a class="nav-btn" href="/app/">Dashboard</a>
  </div>
</nav>

<!-- Hero -->
<section class="hero">
  <div class="hero-badge">
    <div class="pulse-dot"></div>
    Powered by Twitch EventSub — instant notifications
  </div>
  <h1 class="hero-title">Never miss a<br><span>live stream</span></h1>
  <p class="hero-sub">ExcelProtocol brings real-time Twitch stream notifications, channel point video triggers, and full server management to your Discord — with a web dashboard and zero local software required.</p>
  <div class="hero-actions">
    <a class="btn-primary" href="https://discord.com/oauth2/authorize?client_id=1472217050104729701&permissions=1497740488784&scope=bot+applications.commands">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M20.317 4.37a19.791 19.791 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057c.002.022.015.043.03.056a19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994a.076.076 0 0 0-.041-.106 13.107 13.107 0 0 1-1.872-.892.077.077 0 0 1-.008-.128 10.2 10.2 0 0 0 .372-.292.074.074 0 0 1 .077-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .078.01c.12.098.246.198.373.292a.077.077 0 0 1-.006.127 12.299 12.299 0 0 1-1.873.892.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028 19.839 19.839 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03z"/></svg>
      Add to Discord
    </a>
    <a class="btn-secondary" href="/app/">
      Open Dashboard →
    </a>
  </div>
  <div class="hero-stats">
    <div class="stat">
      <div class="stat-num">instant</div>
      <div class="stat-label">notification speed</div>
    </div>
    <div class="stat">
      <div class="stat-num">zero</div>
      <div class="stat-label">local software needed</div>
    </div>
    <div class="stat">
      <div class="stat-num">20+</div>
      <div class="stat-label">features</div>
    </div>
  </div>
</section>

<div class="divider"></div>

<!-- Features -->
<section id="features">
  <div class="section-inner">
    <div class="section-label">What it does</div>
    <h2 class="section-title">Everything your streaming<br>community needs</h2>
    <p class="section-sub">From instant live alerts to cloud-hosted OBS video triggers, ExcelProtocol handles the streaming side so you can focus on your content.</p>
    <div class="feature-grid">
      <div class="feature-card">
        <div class="feature-icon">📺</div>
        <div class="feature-title">Instant Stream Notifications</div>
        <div class="feature-desc">Powered by Twitch EventSub webhooks — notifications fire the moment a streamer goes live, not after a polling delay.</div>
        <span class="feature-tag">EventSub</span>
      </div>
      <div class="feature-card">
        <div class="feature-icon">⚙️</div>
        <div class="feature-title">Web Dashboard</div>
        <div class="feature-desc">Manage everything from a sleek web dashboard. Add streamers, configure channels, set ping roles — no commands needed.</div>
        <span class="feature-tag">No-code setup</span>
      </div>
      <div class="feature-card">
        <div class="feature-icon">🎭</div>
        <div class="feature-title">Reaction Roles</div>
        <div class="feature-desc">Create fully customisable role panels with single-choice, multi-choice, or add-only modes. Fully managed from the dashboard.</div>
        <span class="feature-tag">Roles</span>
      </div>
      <div class="feature-card">
        <div class="feature-icon">🎬</div>
        <div class="feature-title">OBS Video Overlay</div>
        <div class="feature-desc">Trigger YouTube videos in OBS from channel point redeems or Twitch chat — no local software required. Just paste a URL into OBS and it works. Supports queuing, skip, and volume control from the dashboard.</div>
        <span class="feature-tag">Cloud-hosted</span>
      </div>
      <div class="feature-card">
        <div class="feature-icon">🟣</div>
        <div class="feature-title">Channel Point Rewards</div>
        <div class="feature-desc">Connect your Twitch broadcaster account and assign YouTube video triggers to any channel point reward. Fires instantly via EventSub — no polling, no delay.</div>
        <span class="feature-tag">Affiliates &amp; Partners</span>
      </div>
      <div class="feature-card">
        <div class="feature-icon">💬</div>
        <div class="feature-title">Twitch Chat Commands</div>
        <div class="feature-desc">Mods can trigger videos with <code style="font-family:'JetBrains Mono',monospace;color:var(--cyan);font-size:12px">!play</code>, skip with <code style="font-family:'JetBrains Mono',monospace;color:var(--cyan);font-size:12px">!skip</code>, or stop everything with <code style="font-family:'JetBrains Mono',monospace;color:var(--cyan);font-size:12px">!stop</code>. Works for non-affiliates too. Custom commands also supported.</div>
        <span class="feature-tag">All streamers</span>
      </div>
      <div class="feature-card">
        <div class="feature-icon">📊</div>
        <div class="feature-title">Server Stats</div>
        <div class="feature-desc">Display live member counts in voice channel names, auto-updating every 15 minutes.</div>
        <span class="feature-tag">Auto-update</span>
      </div>
      <div class="feature-card">
        <div class="feature-icon">🔔</div>
        <div class="feature-title">Smart Deduplication</div>
        <div class="feature-desc">Built-in duplicate prevention via Twitch message ID dedup and DB-level checks — no double notifications ever.</div>
        <span class="feature-tag">Reliability</span>
      </div>
    </div>
  </div>
</section>

<div class="divider"></div>

<!-- How it works -->
<section id="how-it-works">
  <div class="section-inner" style="display:flex; gap:80px; align-items:flex-start; flex-wrap:wrap;">
    <div style="flex:1; min-width:280px;">
      <div class="section-label">Setup in minutes</div>
      <h2 class="section-title">How it works</h2>
      <p class="section-sub">Get stream notifications running in your server in under five minutes.</p>
    </div>
    <div class="steps" style="flex:1; min-width:280px;">
      <div class="step">
        <div class="step-left">
          <div class="step-num">1</div>
          <div class="step-line"></div>
        </div>
        <div class="step-content">
          <div class="step-title">Add ExcelProtocol to your server</div>
          <div class="step-desc">Click "Add to Discord" and select your server. The bot joins with all required permissions.</div>
        </div>
      </div>
      <div class="step">
        <div class="step-left">
          <div class="step-num">2</div>
          <div class="step-line"></div>
        </div>
        <div class="step-content">
          <div class="step-title">Log into the dashboard</div>
          <div class="step-desc">Sign in with Discord OAuth at excelprotocol.fly.dev. Your servers appear automatically.</div>
        </div>
      </div>
      <div class="step">
        <div class="step-left">
          <div class="step-num">3</div>
          <div class="step-line"></div>
        </div>
        <div class="step-content">
          <div class="step-title">Add your streamers</div>
          <div class="step-desc">Search for any Twitch username and pick which channel to post notifications to.</div>
        </div>
      </div>
      <div class="step">
        <div class="step-left">
          <div class="step-num">4</div>
          <div class="step-line"></div>
        </div>
        <div class="step-content">
          <div class="step-title">Go live — get notified instantly</div>
          <div class="step-desc">EventSub subscriptions are registered automatically. The moment a streamer goes live, your server knows.</div>
        </div>
      </div>
    </div>
  </div>
</section>

<div class="divider"></div>

<!-- CTA -->
<section class="cta-section">
  <div class="cta-inner">
    <h2 class="cta-title">Ready to level up your stream?</h2>
    <p class="cta-sub">Add ExcelProtocol in seconds. Instant stream notifications, cloud-hosted OBS video triggers, and full server management — all free.</p>
    <div style="display:flex; gap:12px; justify-content:center; flex-wrap:wrap;">
      <a class="btn-primary" href="https://discord.com/oauth2/authorize?client_id=1472217050104729701&permissions=1497740488784&scope=bot+applications.commands">
        Add to Discord — it's free
      </a>
      <a class="btn-secondary" href="/app/">Open Dashboard</a>
    </div>
  </div>
</section>

<!-- Footer -->
<footer>
  <div class="footer-left">© 2026 ExcelProtocol · Built by stayexcellent</div>
  <div class="footer-links">
    <a class="footer-link" href="/app/">Dashboard</a>
    <a class="footer-link" href="/terms">Terms</a>
    <a class="footer-link" href="/privacy">Privacy</a>
  </div>
</footer>

<script>
  // Animated particle canvas
  const canvas = document.getElementById('bg-canvas');
  const ctx = canvas.getContext('2d');
  let W, H, particles = [];

  function resize() {
    W = canvas.width  = window.innerWidth;
    H = canvas.height = window.innerHeight;
  }
  resize();
  window.addEventListener('resize', resize);

  for (let i = 0; i < 60; i++) {
    particles.push({
      x: Math.random() * window.innerWidth,
      y: Math.random() * window.innerHeight,
      r: Math.random() * 1.5 + 0.3,
      dx: (Math.random() - 0.5) * 0.3,
      dy: (Math.random() - 0.5) * 0.3,
      o: Math.random() * 0.4 + 0.1
    });
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    particles.forEach(p => {
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(0,245,212,${p.o})`;
      ctx.fill();
      p.x += p.dx; p.y += p.dy;
      if (p.x < 0 || p.x > W) p.dx *= -1;
      if (p.y < 0 || p.y > H) p.dy *= -1;
    });
    requestAnimationFrame(draw);
  }
  draw();
</script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


async def terms_page(request):
    html = _legal_html(
        "Terms of Service",
        "Last updated: April 7, 2026",
        """
        <h2>1. Acceptance of Terms</h2>
        <p>By adding ExcelProtocol to your Discord server, using the ExcelProtocol dashboard at excelprotocol.fly.dev, or otherwise interacting with any ExcelProtocol service, you agree to be bound by these Terms of Service. If you do not agree, you must remove the bot from your server and discontinue use immediately.</p>
        <p>ExcelProtocol is operated by an independent developer ("we", "us", "our"). These terms constitute a legally binding agreement between you and us.</p>

        <h2>2. Eligibility</h2>
        <p>You must be at least 18 years of age to use ExcelProtocol. By using the service, you confirm that you meet this requirement. We do not knowingly collect data from or provide services to individuals under 18.</p>

        <h2>3. Description of Service</h2>
        <p>ExcelProtocol is a Discord bot and associated web dashboard that provides the following features:</p>
        <ul>
          <li>Twitch stream live notifications to Discord channels</li>
          <li>Reaction roles and server role management</li>
          <li>Twitch channel point reward integrations via EventSub webhooks</li>
          <li>Server statistics tracking and display</li>
          <li>Birthday tracking, chat commands, and other community utilities</li>
        </ul>
        <p>We reserve the right to modify, suspend, or discontinue any feature at any time without notice.</p>

        <h2>4. User Responsibilities</h2>
        <p>As a server administrator or user of ExcelProtocol, you agree to:</p>
        <ul>
          <li>Use the service only for lawful purposes and in compliance with Discord's Terms of Service and Community Guidelines</li>
          <li>Not attempt to abuse, exploit, reverse-engineer, or circumvent any feature or security measure of the bot or dashboard</li>
          <li>Not use the service to send unsolicited messages, spam, or harassing content</li>
          <li>Ensure that your use of the bot complies with applicable laws in your jurisdiction</li>
          <li>Take responsibility for all activity that occurs under your Discord server's use of ExcelProtocol</li>
        </ul>

        <h2>5. Dashboard Access and Authentication</h2>
        <p>The ExcelProtocol dashboard uses Discord OAuth2 for authentication. You must have a valid Discord account and the necessary server permissions to access server-specific settings. You are responsible for maintaining the security of your Discord account.</p>
        <p>We do not store your Discord password. Authentication tokens are used solely to verify your identity and manage your server settings.</p>

        <h2>6. Twitch Integration</h2>
        <p>ExcelProtocol integrates with Twitch's API and EventSub webhook service to provide stream notifications. By using stream notification features, you acknowledge that:</p>
        <ul>
          <li>Twitch usernames entered into the bot are stored in our database and used to register webhook subscriptions with Twitch</li>
          <li>Stream data (titles, game categories, thumbnails, viewer counts) is fetched from Twitch's public API and displayed in Discord</li>
          <li>We are not affiliated with Twitch Interactive, Inc.</li>
          <li>Notification accuracy depends on Twitch's API availability and EventSub delivery, which we do not control</li>
        </ul>

        <h2>7. Optional Tips and Donations</h2>
        <p>ExcelProtocol offers an optional tip feature accessible via the /tip command. Any tips or donations made are entirely voluntary and non-refundable. Tips do not grant any additional features, access, or service guarantees. We are not responsible for any issues arising from third-party payment processors used to facilitate tips.</p>

        <h2>8. Data and Privacy</h2>
        <p>Your use of ExcelProtocol is also governed by our <a href="/privacy">Privacy Policy</a>, which is incorporated into these Terms by reference. By using the service, you consent to the data practices described in the Privacy Policy.</p>

        <h2>9. Intellectual Property</h2>
        <p>ExcelProtocol, its name, logo, and associated materials are the intellectual property of the developer. You may not reproduce, distribute, or create derivative works without explicit written permission.</p>

        <h2>10. Disclaimer of Warranties</h2>
        <p>ExcelProtocol is provided "as is" and "as available" without warranties of any kind, either express or implied. We do not warrant that the service will be uninterrupted, error-free, or free of harmful components. Notification delivery depends on third-party services (Discord, Twitch) that we do not control.</p>

        <h2>11. Limitation of Liability</h2>
        <p>To the maximum extent permitted by applicable law, we shall not be liable for any indirect, incidental, special, consequential, or punitive damages arising from your use of or inability to use ExcelProtocol, including but not limited to missed stream notifications, loss of data, or server disruption.</p>

        <h2>12. Termination</h2>
        <p>We reserve the right to terminate or restrict access to ExcelProtocol for any server or user at our sole discretion, without notice, for conduct that we believe violates these Terms or is harmful to other users, us, or third parties.</p>
        <p>You may terminate your use at any time by removing the bot from your Discord server and contacting us to request data deletion.</p>

        <h2>13. Changes to Terms</h2>
        <p>We may update these Terms at any time. Continued use of ExcelProtocol after changes are posted constitutes acceptance of the revised Terms. We will endeavour to notify users of significant changes via the bot's support channels.</p>

        <h2>14. Contact</h2>
        <p>For questions about these Terms, contact us via Discord: <strong>stayexcellent</strong></p>
        """
    )
    return web.Response(text=html, content_type="text/html")


async def privacy_page(request):
    html = _legal_html(
        "Privacy Policy",
        "Last updated: April 7, 2026",
        """
        <h2>1. Overview</h2>
        <p>This Privacy Policy explains what data ExcelProtocol collects, how it is used, and your rights regarding that data. We are committed to being transparent and only collecting what is necessary to operate the service.</p>
        <p>ExcelProtocol is hosted on Fly.io (Frankfurt, EU region). Data is stored in a SQLite database on persistent Fly.io storage volumes.</p>

        <h2>2. What Data We Collect</h2>
        <p><strong>Discord Data</strong> — When you add ExcelProtocol to a Discord server, we collect and store:</p>
        <ul>
          <li>Discord Server (Guild) ID — to associate settings with your server</li>
          <li>Discord Channel IDs — to know which channels to send notifications to</li>
          <li>Discord Role IDs — for reaction roles and ping role features</li>
          <li>Discord User IDs — for birthday tracking and command usage</li>
          <li>Discord Message IDs — to track sent notification messages for auto-deletion</li>
        </ul>
        <p>We do not store Discord message content, usernames, profile pictures, or any personal user data beyond what is listed above.</p>

        <p><strong>Twitch Data</strong> — To provide stream notifications, we store:</p>
        <ul>
          <li>Twitch usernames (login names) of streamers added to a server</li>
          <li>Twitch user IDs — used internally to register EventSub webhooks with Twitch</li>
          <li>Broadcaster OAuth tokens — only if you connect your Twitch account for Channel Rewards; stored and used solely to manage channel point rewards on your behalf</li>
        </ul>
        <p>Stream data (titles, categories, thumbnails, viewer counts) is fetched from Twitch's API in real time and is not permanently stored.</p>

        <p><strong>Dashboard Authentication</strong> — When you log in via Discord OAuth2, we receive a temporary access token to verify your identity and server permissions. This is stored as a short-lived session cookie and not persisted in our database.</p>

        <p><strong>Notification Logs</strong> — We maintain a log of when stream notifications were sent (streamer name, guild ID, timestamp, status). This is used for debugging and is automatically trimmed to the most recent 30 days.</p>

        <p><strong>Tips and Donations</strong> — ExcelProtocol does not directly process payments. If you tip via the /tip command, you are redirected to a third-party platform. We do not receive or store your payment information.</p>

        <h2>3. How We Use Your Data</h2>
        <p>Data collected by ExcelProtocol is used exclusively to:</p>
        <ul>
          <li>Deliver stream notifications to the correct Discord channels</li>
          <li>Manage reaction roles and server configuration</li>
          <li>Display server statistics in stat channels</li>
          <li>Process Twitch channel point reward triggers for connected streamers</li>
          <li>Track and send birthday notifications where enabled</li>
          <li>Debug delivery failures and monitor service health</li>
        </ul>
        <p>We do not sell, rent, or share your data with third parties for commercial purposes.</p>

        <h2>4. Data Sharing</h2>
        <p>We share data with the following third parties only as required to operate the service:</p>
        <ul>
          <li><strong>Discord Inc.</strong> — to send messages, manage roles, and authenticate users</li>
          <li><strong>Twitch Interactive, Inc.</strong> — to register EventSub webhooks and fetch stream data</li>
          <li><strong>Fly.io</strong> — our hosting provider, which stores the database on its infrastructure</li>
        </ul>
        <p>We do not share your data with advertisers, analytics platforms, or any other third parties.</p>

        <h2>5. Data Retention</h2>
        <ul>
          <li>Server settings, streamer lists, and role configurations are retained until you remove the bot or request deletion</li>
          <li>Notification message IDs are removed automatically when streamers go offline</li>
          <li>Notification logs are trimmed automatically after 30 days</li>
          <li>Broadcaster OAuth tokens are retained until you disconnect your Twitch account or remove the bot</li>
        </ul>
        <p>When you remove ExcelProtocol from your Discord server, all data associated with that server is automatically deleted from our database.</p>

        <h2>6. Your Rights</h2>
        <p>You have the right to:</p>
        <ul>
          <li>Request a copy of the data we hold about your server</li>
          <li>Request deletion — remove the bot to trigger automatic deletion, or contact us directly</li>
          <li>Correct inaccurate data — use the dashboard or slash commands at any time</li>
        </ul>
        <p>To exercise any of these rights, contact us via Discord: <strong>stayexcellent</strong></p>

        <h2>7. Security</h2>
        <p>We take reasonable technical measures to protect your data, including:</p>
        <ul>
          <li>HTTPS-only access to the dashboard and webhook endpoints</li>
          <li>HMAC-SHA256 signature verification on all incoming Twitch EventSub webhook requests</li>
          <li>Session-based authentication with short-lived tokens for the dashboard</li>
        </ul>
        <p>No system is completely secure. We are not liable for unauthorised access resulting from factors outside our control.</p>

        <h2>8. Children's Privacy</h2>
        <p>ExcelProtocol is not intended for use by anyone under 18 years of age. We do not knowingly collect personal data from minors. If you believe a minor has used ExcelProtocol, please contact us and we will delete the relevant data.</p>

        <h2>9. Changes to This Policy</h2>
        <p>We may update this Privacy Policy from time to time. The "Last updated" date at the top will reflect any changes. Continued use of ExcelProtocol after changes constitutes acceptance of the revised policy.</p>

        <h2>10. Contact</h2>
        <p>For any privacy-related questions or data requests, contact us via Discord: <strong>stayexcellent</strong></p>
        """
    )
    return web.Response(text=html, content_type="text/html")


async def auth_logout(request):
    """Clear the session cookie."""
    session = request.get("session", {})
    token = request.cookies.get("ep_session", "")
    if token and token in _sessions:
        del _sessions[token]
    response = web.json_response({"ok": True})
    response.del_cookie("ep_session")
    return response



# ── Companion: lightweight guild info (no auth required) ─────────────────────
async def handle_companion_guild_info(request: web.Request) -> web.Response:
    guild_id = request.match_info["guild_id"]
    name = guild_id  # fallback
    if _bot_ref:
        try:
            guild_obj = _bot_ref.get_guild(int(guild_id))
            if guild_obj:
                name = guild_obj.name
        except Exception:
            pass
    return web.json_response({"guild_id": guild_id, "name": name})


# ── Companion: hotkey mappings for a guild ────────────────────────────────────
async def handle_companion_hotkeys(request: web.Request) -> web.Response:
    """Returns all reward hotkey mappings for a guild. No auth required."""
    guild_id = request.match_info["guild_id"]
    rows = await db_fetch(
        "SELECT reward_id, reward_title, hotkey FROM reward_triggers WHERE guild_id = ? AND hotkey IS NOT NULL AND hotkey != ''",
        (guild_id,)
    )
    mappings = [
        {
            "hotkey_name": f"reward_{r['reward_id']}",
            "reward_title": r["reward_title"],
            "hotkey_keys": r["hotkey"],
        }
        for r in rows
    ]
    return web.json_response({"guild_id": guild_id, "mappings": mappings})


# ── Companion App ─────────────────────────────────────────────────────────────
COMPANION_VERSION      = "1.0.1"
COMPANION_DOWNLOAD_URL = "https://github.com/stayexcellent/excelprotocol/releases/download/companion-v1.0.1/ExcelProtocol-Companion.exe"

async def handle_companion_version(request: web.Request) -> web.Response:
    return web.json_response({
        "version":      COMPANION_VERSION,
        "download_url": COMPANION_DOWNLOAD_URL,
    })


# ── App Factory ───────────────────────────────────────────────────────────────
def create_dashboard_app(bot=None):
    global _bot_ref
    _bot_ref = bot
    app = web.Application(middlewares=[error_logging_middleware, auth_middleware])

    app.router.add_get("/health",            health)
    app.router.add_get("/companion/version",             handle_companion_version)
    app.router.add_get("/companion/guild/{guild_id}",    handle_companion_guild_info)
    app.router.add_get("/companion/hotkeys/{guild_id}", handle_companion_hotkeys)
    app.router.add_get("/",                   landing_page)
    app.router.add_get("/terms",         terms_page)
    app.router.add_get("/privacy",       privacy_page)
    app.router.add_get ("/auth/login",    auth_login)
    app.router.add_get ("/auth/callback", auth_callback)
    app.router.add_post("/auth/logout",   auth_logout)
    app.router.add_get("/auth/twitch/login/{guild_id}",  twitch_broadcaster_login)
    app.router.add_get("/auth/twitch/callback",          twitch_broadcaster_callback)
    app.router.add_delete("/api/guild/{guild_id}/broadcaster",              twitch_broadcaster_disconnect)
    app.router.add_get   ("/api/guild/{guild_id}/broadcaster",              get_broadcaster_info)
    app.router.add_post  ("/api/guild/{guild_id}/broadcaster/triggers",     upsert_reward_trigger)
    app.router.add_delete("/api/guild/{guild_id}/broadcaster/triggers/{reward_id}", delete_reward_trigger)
    app.router.add_post  ("/api/guild/{guild_id}/broadcaster/rewards",      create_reward)
    app.router.add_patch ("/api/guild/{guild_id}/broadcaster/rewards/{reward_id}", edit_reward)
    app.router.add_delete("/api/guild/{guild_id}/broadcaster/rewards/{reward_id}", delete_reward)
    app.router.add_post  ("/api/eventsub/callback",                         eventsub_callback)
    app.router.add_get   ("/overlay/{guild_id}",                            overlay_page)
    app.router.add_get   ("/overlay/{guild_id}/ws",                         overlay_ws)
    app.router.add_get   ("/api/guild/{guild_id}/twitch",                    get_twitch_info)
    app.router.add_post  ("/api/guild/{guild_id}/twitch/play-enabled",       set_play_enabled)
    app.router.add_post  ("/api/guild/{guild_id}/twitch/overlay-volume",      set_overlay_volume)
    app.router.add_post  ("/api/guild/{guild_id}/twitch/play-test",           play_test_overlay)
    app.router.add_post  ("/api/guild/{guild_id}/twitch/commands",           add_twitch_command)
    app.router.add_delete("/api/guild/{guild_id}/twitch/commands/{command_name}", delete_twitch_command)
    app.router.add_patch ("/api/guild/{guild_id}/command-limit",             set_command_limit)
    app.router.add_get("/api/me",        auth_me)
    app.router.add_get("/api/guilds",    get_guilds)
    app.router.add_get("/api/guild/{guild_id}", get_guild_summary)

    app.router.add_get   ("/api/guild/{guild_id}/streamers",              get_streamers)
    app.router.add_post  ("/api/guild/{guild_id}/streamers",              add_streamer)
    app.router.add_delete("/api/guild/{guild_id}/streamers/{username}",   delete_streamer)

    app.router.add_get   ("/api/guild/{guild_id}/reaction-roles",         get_reaction_roles)
    app.router.add_delete("/api/guild/{guild_id}/reaction-roles/{role_id}", delete_reaction_role)

    app.router.add_get("/api/guild/{guild_id}/channels",                   get_channels)
    app.router.add_get("/api/guild/{guild_id}/emojis",                     get_emojis)
    app.router.add_get("/api/guild/{guild_id}/roles",                      get_roles_list)
    app.router.add_post("/api/guild/{guild_id}/reaction-roles",            create_reaction_role)
    app.router.add_get("/api/guild/{guild_id}/notiflog", get_notif_log)
    app.router.add_patch("/api/guild/{guild_id}/streamers/{username}",      edit_streamer)
    app.router.add_patch("/api/guild/{guild_id}/reaction-roles/{role_id}",  edit_reaction_role)
    app.router.add_get("/api/commands",                  get_commands)
    app.router.add_post("/api/suggest",                    post_suggestion)
    app.router.add_post("/api/support",                     post_support)

    app.router.add_get  ("/api/guild/{guild_id}/members",               get_guild_members)
    app.router.add_get  ("/api/guild/{guild_id}/birthdays",              get_birthdays)
    app.router.add_post ("/api/guild/{guild_id}/birthdays",              add_birthday)
    app.router.add_delete("/api/guild/{guild_id}/birthdays/{user_id}",  delete_birthday)
    app.router.add_get  ("/api/guild/{guild_id}/settings",              get_server_settings)
    app.router.add_patch("/api/guild/{guild_id}/settings",              patch_server_settings)
    app.router.add_patch("/api/guild/{guild_id}/streamer-limit",          set_streamer_limit)
    app.router.add_get  ("/api/guild/{guild_id}/cleanup",               get_cleanup_configs)
    app.router.add_post ("/api/guild/{guild_id}/cleanup",               add_cleanup_config)
    app.router.add_patch("/api/guild/{guild_id}/cleanup/{channel_id}",  edit_cleanup_config)
    app.router.add_delete("/api/guild/{guild_id}/cleanup/{channel_id}", delete_cleanup_config)
    app.router.add_get  ("/api/guild/{guild_id}/permission-issues",     get_permission_issues)
    app.router.add_post ("/api/guild/{guild_id}/permission-issues/recheck", recheck_permissions)
    app.router.add_post ("/api/guild/{guild_id}/permission-issues/{channel_id}/fix", fix_permissions)
    app.router.add_get  ("/api/guild/{guild_id}/unresolvable-streamers", get_unresolvable_streamers)
    app.router.add_get  ("/api/dev/global-stats",   get_global_stats)
    app.router.add_get  ("/api/dev/db-tools",       db_tools_status)
    app.router.add_post ("/api/dev/db-tools",       db_tools_action)
    app.router.add_get   ("/api/guild/{guild_id}/stat-channels",            get_stat_channels)
    app.router.add_get   ("/api/guild/{guild_id}/vc-settings",              get_vc_settings)
    app.router.add_post  ("/api/guild/{guild_id}/vc-settings",              set_vc_settings)
    app.router.add_delete("/api/guild/{guild_id}/vc-settings",              delete_vc_settings)
    app.router.add_get   ("/api/guild/{guild_id}/safety-settings",          get_safety_settings)
    app.router.add_post  ("/api/guild/{guild_id}/safety-settings",          set_safety_settings)
    app.router.add_get   ("/api/guild/{guild_id}/safety-kicks",             get_safety_kicks)
    app.router.add_post  ("/api/guild/{guild_id}/stat-channels",            set_stat_channel)
    app.router.add_delete("/api/guild/{guild_id}/stat-channels/{channel_id}", delete_stat_channel)

    dist_path = os.path.join(os.path.dirname(__file__), "dashboard", "dist")
    if os.path.exists(dist_path):
        async def serve_index(request):
            return web.FileResponse(os.path.join(dist_path, "index.html"))
        app.router.add_get("/app",  serve_index)
        app.router.add_get("/app/", serve_index)
        app.router.add_static("/app/assets", path=os.path.join(dist_path, "assets"), name="frontend_assets")
        app.router.add_static("/app",        path=dist_path,                          name="frontend_static")

    cors = cors_setup(app, defaults={
        "*": ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*",
                             allow_methods=["GET", "POST", "DELETE", "PATCH", "OPTIONS"])
    })
    for route in list(app.router.routes()):
        try:
            if hasattr(route, 'resource') and route.resource and '/ws' in str(route.resource.canonical):
                continue
            cors.add(route)
        except Exception:
            pass

    async def on_cleanup(app):
        global _http_session
        if _http_session and not _http_session.closed:
            await _http_session.close()
    app.on_cleanup.append(on_cleanup)

    return app
