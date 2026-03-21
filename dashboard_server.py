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

# ── Auth Middleware ───────────────────────────────────────────────────────────
def _session_can_access_guild(session: dict, guild_id: str) -> bool:
    """Check the session has access to the requested guild."""
    if session.get("dev"):
        return True  # Dev token has full access — only used server-side/internally
    guilds = session.get("guilds", [])
    return any(str(g["id"]) == str(guild_id) for g in guilds)

@web.middleware
async def auth_middleware(request: web.Request, handler):
    public = ("/health", "/auth/login", "/auth/callback", "/auth/dev")
    if request.path in public or request.path.startswith("/app"):
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
    channels = await get_guild_channels(guild_id)
    # Also get the default notification channel from server_settings
    rows = await db_fetch("SELECT notification_channel_id FROM server_settings WHERE guild_id = ?", (guild_id,))
    default_channel_id = str(rows[0]["notification_channel_id"]) if rows else None
    return web.json_response({"channels": channels, "default_channel_id": default_channel_id})

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
        roles = await discord_get(f"/guilds/{guild_id}/roles")
        return web.json_response([
            {"id": str(r["id"]), "name": r["name"], "color": r["color"]}
            for r in roles if r["name"] != "@everyone"
        ])
    except Exception:
        return web.json_response([])

# ── Create Reaction Role Panel ────────────────────────────────────────────────
async def _resolve_role_id(guild_id: str, role_id: str, new_role_name: str = None) -> str:
    """If role_id is __create__, create the role in Discord and return the real ID."""
    if role_id != "__create__":
        return role_id
    if not new_role_name or not new_role_name.strip():
        raise web.HTTPBadRequest(reason="New role name is required when creating a role")
    async with http_client.ClientSession() as session:
        resp = await session.post(
            f"{DISCORD_API}/guilds/{guild_id}/roles",
            headers={"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"},
            json={"name": new_role_name.strip()},
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
    embed = discord.Embed(title=title, color=embed_color)

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
    }
    temp_view = rr_module._build_view(temp_entry, _bot_ref)
    message = await channel.send(embed=embed, view=temp_view)

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
    )

    logger.info(f"Dashboard created RR panel '{title}' in #{channel.name} (msg {message.id})")
    return web.json_response({"ok": True, "message_id": str(message.id)})

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

    if _bot_ref is None:
        raise web.HTTPInternalServerError(reason="Bot not available")

    import reaction_roles as rr_module
    import discord

    # Get current entry from DB
    entry = _bot_ref.db.rr_get(int(message_id))
    if not entry or str(entry["guild_id"]) != guild_id:
        raise web.HTTPNotFound(reason="Panel not found")

    # Merge updates into the entry
    if "title" in body:   entry["title"]     = body["title"]
    if "type" in body:    entry["type"]      = body["type"]
    if "only_add" in body: entry["only_add"] = body["only_add"]
    if "max_roles" in body: entry["max_roles"] = body["max_roles"]
    if "roles" in body:
        # Convert role_ids to int
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
    )

    # Edit the actual Discord message
    guild = _bot_ref.get_guild(int(guild_id))
    if guild:
        channel = guild.get_channel(entry["channel_id"])
        if channel:
            try:
                msg = await channel.fetch_message(int(message_id))
                embed_color = _bot_ref.db.get_embed_color(int(guild_id))
                embed = discord.Embed(title=entry["title"], color=embed_color)
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
        "SELECT notification_channel_id, embed_color, auto_delete_notifications, milestone_notifications FROM server_settings WHERE guild_id = ?",
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


# ── Dev Login ─────────────────────────────────────────────────────────────────
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
    app = web.Application(middlewares=[auth_middleware])

    app.router.add_get("/health",        health)
    app.router.add_get("/auth/login",    auth_login)
    app.router.add_get("/auth/callback", auth_callback)
    app.router.add_get("/auth/dev",      auth_dev)
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

    app.router.add_get  ("/api/guild/{guild_id}/members",               get_guild_members)
    app.router.add_get  ("/api/guild/{guild_id}/birthdays",              get_birthdays)
    app.router.add_post ("/api/guild/{guild_id}/birthdays",              add_birthday)
    app.router.add_delete("/api/guild/{guild_id}/birthdays/{user_id}",  delete_birthday)
    app.router.add_get  ("/api/guild/{guild_id}/settings",              get_server_settings)
    app.router.add_patch("/api/guild/{guild_id}/settings",              patch_server_settings)
    app.router.add_get  ("/api/guild/{guild_id}/cleanup",               get_cleanup_configs)
    app.router.add_post ("/api/guild/{guild_id}/cleanup",               add_cleanup_config)
    app.router.add_patch("/api/guild/{guild_id}/cleanup/{channel_id}",  edit_cleanup_config)
    app.router.add_delete("/api/guild/{guild_id}/cleanup/{channel_id}", delete_cleanup_config)

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
            cors.add(route)
        except Exception:
            pass
    return app
