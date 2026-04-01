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

# Bot reference — set by create_dashboard_app() so we can reload views
_bot_ref = None

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
    """Returns {role_id: {name, color}} for all roles in a guild. Not cached — roles can be renamed."""
    try:
        async with http_client.ClientSession() as session:
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
                # Type 0 = text channel, type 2 = voice channel, type 4 = category
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
        async with http_client.ClientSession() as session:
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
    # DEV_TOKEN is only for internal server use — never expose to browser clients
    # It's blocked for any request coming from outside (i.e. must use OAuth2)
    if DEV_TOKEN and token == DEV_TOKEN:
        # Only allow from localhost / internal — reject if X-Forwarded-For is present
        # (Fly.io proxy sets this header for all external requests)
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return None  # External request trying to use dev token — reject
        return {"dev": True}
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
        raise web.HTTPInternalServerError(reason=f"{type(e).__name__}: {str(e)[:100]}")

# ── Auth Middleware ───────────────────────────────────────────────────────────
def _session_can_access_guild(session: dict, guild_id: str) -> bool:
    """Check the session has access to the requested guild."""
    if session.get("dev"):
        return True  # Dev token has full access — only used server-side/internally
    guilds = session.get("guilds", [])
    return any(str(g["id"]) == str(guild_id) for g in guilds)

@web.middleware
async def auth_middleware(request: web.Request, handler):
    public = ("/health", "/auth/login", "/auth/callback", "/auth/dev", "/auth/twitch/callback", "/api/eventsub/callback")
    if request.path in public or request.path.startswith("/app") or request.path.startswith("/overlay") or request.path.startswith("/auth/twitch/login"):
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

    # Only include guilds where user has Manage Server AND the bot is present
    bot_guild_ids = {str(g.id) for g in _bot_ref.guilds} if _bot_ref else set()
    managed = [
        {"id": g["id"], "name": g["name"], "icon": g.get("icon")}
        for g in guilds
        if int(g.get("permissions", 0)) & 0x20
        and (not bot_guild_ids or g["id"] in bot_guild_ids)
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
        return web.json_response({"username": "Dev", "avatar": None, "guilds": guilds, "is_dev": True})
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
        "is_dev":   False,
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
        async with http_client.ClientSession() as session:
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
        async with http_client.ClientSession() as session:
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
    async with http_client.ClientSession() as session:
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
    body_text  = body.get("body_text", "").strip() or None

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
    if "body_text" in body: entry["body_text"] = body["body_text"].strip() or None
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
    if not row:
        return web.json_response({"linked": False, "channel": None, "commands": [], "count": 0, "limit": 50})

    channel = row["twitch_channel"]
    commands = await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.get_twitch_commands(channel))
    limit = await _asyncio.get_event_loop().run_in_executor(None, lambda: _bot_ref.db.get_command_limit(int(guild_id)))

    # Check if bot is modded in the channel
    bot_is_modded = False
    BOT_TWITCH_LOGIN = "excelprotocol"
    try:
        broadcaster = await get_twitch_token()
        async with http_client.ClientSession() as sess:
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
    })

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
    """Redirect streamer to Twitch OAuth — stores guild_id in state param."""
    guild_id = request.match_info["guild_id"]
    import urllib.parse
    params = urllib.parse.urlencode({
        "client_id": TWITCH_CLIENT_ID,
        "redirect_uri": TWITCH_REDIRECT_URI,
        "response_type": "code",
        "scope": "channel:read:redemptions channel:manage:redemptions",
        "state": guild_id,
        "force_verify": "true",
    })
    raise web.HTTPFound(f"https://id.twitch.tv/oauth2/authorize?{params}")

async def twitch_broadcaster_callback(request):
    """Handle Twitch OAuth callback — exchange code for tokens."""
    code     = request.rel_url.query.get("code")
    guild_id = request.rel_url.query.get("state")
    if not code or not guild_id:
        raise web.HTTPBadRequest(reason="Missing code or state")

    async with http_client.ClientSession() as sess:
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
    secret = os.getenv("EVENTSUB_SECRET", "excelprotocol_eventsub_secret")
    try:
        async with http_client.ClientSession() as sess:
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
    secret = os.getenv("EVENTSUB_SECRET", "excelprotocol_eventsub_secret").encode()

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

    if msg_type == "notification" and data.get("subscription", {}).get("type") == "channel.channel_points_custom_reward_redemption.add":
        event = data.get("event", {})
        reward_id         = event.get("reward", {}).get("id")
        broadcaster_login = event.get("broadcaster_user_login", "").lower()
        redeemer          = event.get("user_name", "")

        # Find which guild this broadcaster belongs to
        rows = await db_fetch("SELECT guild_id FROM broadcaster_tokens WHERE twitch_login = ?", (broadcaster_login,))
        for row in rows:
            guild_id = str(row["guild_id"])
            # Find matching trigger
            trigger_rows = await db_fetch(
                "SELECT video_url, volume FROM reward_triggers WHERE guild_id = ? AND reward_id = ?",
                (guild_id, reward_id)
            )
            if trigger_rows:
                trigger = trigger_rows[0]
                import json as _json
                payload = _json.dumps({
                    "type": "play",
                    "video_url": trigger["video_url"],
                    "volume": trigger["volume"],
                    "redeemer": redeemer,
                })
                # Push to all connected overlays for this guild
                dead = set()
                for ws in _overlay_connections.get(guild_id, set()):
                    try:
                        await ws.send_str(payload)
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
    try:
        async for msg in ws:
            pass  # overlay only receives, doesn't send
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
  #overlay {{ position:fixed; inset:0; display:flex; align-items:center; justify-content:center; pointer-events:none; }}
  video {{ max-width:100%; max-height:100%; object-fit:contain; display:none; border-radius:8px; }}
  #redeemer {{ position:fixed; bottom:20px; left:50%; transform:translateX(-50%);
    background:rgba(0,0,0,0.7); color:#fff; padding:6px 16px; border-radius:20px;
    font-family:sans-serif; font-size:14px; display:none; white-space:nowrap; }}
</style>
</head>
<body>
<div id="overlay"><video id="vid" playsinline></video></div>
<div id="redeemer" id="rdm"></div>
<script>
const guildId = "{guild_id}";
const vid = document.getElementById("vid");
const rdm = document.getElementById("redeemer");
const queue = [];
let playing = false;

const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
const ws = new WebSocket(wsProto + "//" + location.host + "/overlay/" + guildId + "/ws");

ws.onmessage = e => {{
  const msg = JSON.parse(e.data);
  if (msg.type === "play") {{ queue.push(msg); processQueue(); }}
}};

ws.onclose = () => {{ setTimeout(() => location.reload(), 3000); }};

function processQueue() {{
  if (playing || queue.length === 0) return;
  const item = queue.shift();
  playing = true;
  vid.src = item.video_url;
  vid.volume = Math.max(0, Math.min(1, item.volume || 1));
  vid.style.display = "block";
  rdm.textContent = item.redeemer ? item.redeemer + " redeemed!" : "";
  rdm.style.display = item.redeemer ? "block" : "none";
  vid.play().catch(() => {{}});
  vid.onended = () => {{
    vid.style.display = "none";
    rdm.style.display = "none";
    vid.src = "";
    playing = false;
    setTimeout(processQueue, 500);
  }};
}}
</script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")

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
        async with http_client.ClientSession() as sess:
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
                return web.json_response({"connected": True, "not_affiliate": True, "twitch_login": rows2[0]["twitch_login"] if rows2 else "", "rewards": [], "triggers": []})
    except Exception as e:
        logger.error(f"Error fetching rewards for guild {guild_id}: {e}")

    # Get existing triggers
    triggers = await db_fetch("SELECT reward_id, reward_title, video_url, volume FROM reward_triggers WHERE guild_id = ?", (guild_id,))

    return web.json_response({
        "connected": True,
        "twitch_login": twitch_login,
        "rewards": rewards,
        "triggers": triggers,
        "overlay_url": f"https://excelprotocol.fly.dev/overlay/{guild_id}",
    })

async def upsert_reward_trigger(request):
    """Add or update a video trigger for a reward."""
    guild_id = request.match_info["guild_id"]
    body = await request.json()
    reward_id    = body.get("reward_id", "").strip()
    reward_title = body.get("reward_title", "").strip()
    video_url    = body.get("video_url", "").strip()
    volume       = float(body.get("volume", 1.0))
    if not reward_id or not video_url:
        raise web.HTTPBadRequest(reason="reward_id and video_url are required")
    await db_execute(
        "INSERT INTO reward_triggers (guild_id, reward_id, reward_title, video_url, volume) VALUES (?, ?, ?, ?, ?) ON CONFLICT(guild_id, reward_id) DO UPDATE SET reward_title=excluded.reward_title, video_url=excluded.video_url, volume=excluded.volume",
        (guild_id, reward_id, reward_title, video_url, volume)
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
    async with http_client.ClientSession() as sess:
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
    async with http_client.ClientSession() as sess:
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
    async with http_client.ClientSession() as sess:
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

# ── Stat Channels ─────────────────────────────────────────────────────────────
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

async def auth_dev(request):
    """Password-protected dev login — creates a full-access session."""
    password = request.rel_url.query.get("password", "")
    if not DEV_TOKEN or password != DEV_TOKEN:
        raise web.HTTPForbidden(reason="Invalid dev password")
    session_token = secrets.token_hex(32)
    _sessions[session_token] = {"dev": True}
    raise web.HTTPFound(f"/app/?token={session_token}")

# ── App Factory ───────────────────────────────────────────────────────────────
def create_dashboard_app(bot=None):
    global _bot_ref
    _bot_ref = bot
    app = web.Application(middlewares=[error_logging_middleware, auth_middleware])

    app.router.add_get("/health",        health)
    app.router.add_get("/auth/login",    auth_login)
    app.router.add_get("/auth/callback", auth_callback)
    app.router.add_get("/auth/dev",      auth_dev)
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
    app.router.add_get   ("/api/guild/{guild_id}/stat-channels",            get_stat_channels)
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
            # Skip WebSocket routes — CORS breaks the upgrade handshake
            if hasattr(route, 'resource') and route.resource and '/ws' in str(route.resource.canonical):
                continue
            cors.add(route)
        except Exception:
            pass
    return app
