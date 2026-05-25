"""
music.py — ExcelProtocol Music
Registers /mjoin and /mleave on discord_bot.tree directly (discord.Client pattern).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands

logger = logging.getLogger(__name__)

SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
IDLE_TIMEOUT          = 300  # seconds

# ── Track ──────────────────────────────────────────────────────────────────────

@dataclass
class Track:
    title:     str
    url:       str
    webpage:   str
    duration:  int
    requester: discord.Member
    thumbnail: str = ""

# ── Per-guild player ───────────────────────────────────────────────────────────

class GuildPlayer:
    def __init__(self, guild_id: int):
        self.guild_id     = guild_id
        self.queue: deque[Track] = deque()
        self.current:     Optional[Track] = None
        self.voice:       Optional[discord.VoiceClient] = None
        self.control_msg: Optional[discord.Message] = None
        self.paused       = False
        self.loop         = False
        self.volume       = 0.7
        self.quality      = "medium"
        self.last_active  = time.time()
        self.source       = None
        self.tmp_path     = None
        self._play_lock   = asyncio.Lock()

    def is_playing(self):
        return self.voice is not None and self.voice.is_playing()

    def is_paused(self):
        return self.voice is not None and self.voice.is_paused()

# ── Module-level state ─────────────────────────────────────────────────────────

_players: dict[int, GuildPlayer] = {}
_bot_ref = None

def get_player(guild_id: int) -> GuildPlayer:
    if guild_id not in _players:
        player = GuildPlayer(guild_id)
        try:
            if _bot_ref:
                rows = _bot_ref.db.db_fetch(
                    "SELECT default_volume, quality FROM guild_music_settings WHERE guild_id=?",
                    (guild_id,)
                )
                if rows:
                    player.volume  = rows[0]["default_volume"] / 100.0
                    player.quality = rows[0].get("quality", "medium")
                    logger.info(f"Loaded volume={player.volume:.2f} quality={player.quality} for guild {guild_id}")
                else:
                    logger.info(f"No music settings for guild {guild_id}, using defaults")
        except Exception as e:
            logger.warning(f"Failed to load settings for guild {guild_id}: {e}")
        _players[guild_id] = player
    return _players[guild_id]

def get_all_players() -> dict[int, GuildPlayer]:
    return _players

# ── yt-dlp helpers (SoundCloud backend) ───────────────────────────────────────

QUALITY_FORMATS = {
    "low":    "worstaudio/worst",
    "medium": "bestaudio[abr<=128]/bestaudio/best",
    "high":   "bestaudio/best",
}

def get_ytdl_options(quality: str = "medium") -> dict:
    return {
        "format":         QUALITY_FORMATS.get(quality, QUALITY_FORMATS["medium"]),
        "noplaylist":     True,
        "quiet":          True,
        "no_warnings":    True,
        "default_search": "scsearch",
        "source_address": "0.0.0.0",
    }

FFMPEG_OPTIONS_FILE = {
    "before_options": "",
    "options":        "-vn -af aresample=48000 -ar 48000 -hide_banner -loglevel error",
}
FFMPEG_OPTIONS_STREAM = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options":        "-vn -af aresample=48000 -ar 48000 -hide_banner -loglevel error",
}
FFMPEG_OPTIONS = FFMPEG_OPTIONS_FILE

async def resolve_query(query: str, quality: str = "medium") -> Optional[Track]:
    import yt_dlp
    loop = asyncio.get_event_loop()

    if "open.spotify.com/track" in query:
        query = await _spotify_track_to_search(query) or query

    opts = get_ytdl_options(quality)

    def _extract(q):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(q, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return info

    try:
        info = await loop.run_in_executor(None, _extract, query)
    except Exception as e:
        logger.warning(f"yt-dlp failed for {query!r}: {type(e).__name__}: {e}")
        return None

    return Track(
        title     = info.get("title", "Unknown"),
        url       = info["url"],
        webpage   = info.get("webpage_url", query),
        duration  = int(info.get("duration", 0) or 0),
        requester = None,
        thumbnail = info.get("thumbnail", ""),
    )

async def resolve_spotify_playlist(url: str) -> list[str]:
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
        sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET))
        results = sp.playlist_tracks(url)
        tracks = []
        while results:
            for item in results["items"]:
                t = item.get("track")
                if t:
                    artists = ", ".join(a["name"] for a in t.get("artists", []))
                    tracks.append(f"{artists} - {t['name']}")
            results = sp.next(results) if results.get("next") else None
        return tracks
    except Exception as e:
        logger.warning(f"Spotify playlist resolve failed: {e}")
        return []

async def _spotify_track_to_search(url: str) -> Optional[str]:
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
        sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET))
        tid = url.split("/track/")[1].split("?")[0]
        t   = sp.track(tid)
        artists = ", ".join(a["name"] for a in t.get("artists", []))
        return f"{artists} - {t['name']}"
    except Exception as e:
        logger.warning(f"Spotify track resolve failed: {e}")
        return None

def fmt_duration(s) -> str:
    s = int(s or 0)
    if not s: return "?"
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

# ── Embed ──────────────────────────────────────────────────────────────────────

def build_embed(player: GuildPlayer, guild: discord.Guild) -> discord.Embed:
    t = player.current
    if not t:
        e = discord.Embed(title="🎵 Nothing playing", color=0x1db954)
        e.set_footer(text="Use the ➕ Add button to queue a song")
        return e
    status = "⏸ Paused" if player.is_paused() else "▶ Playing"
    loop   = " 🔁" if player.loop else ""
    e = discord.Embed(title=f"{status}{loop}", description=f"[{t.title}]({t.webpage})", color=0x1db954)
    e.add_field(name="Duration",  value=fmt_duration(t.duration), inline=True)
    e.add_field(name="Requested", value=t.requester.display_name if t.requester else "?", inline=True)
    e.add_field(name="Volume",    value=f"{int(player.volume*100)}%", inline=True)
    e.add_field(name="Queue",     value=f"{len(player.queue)} track(s)", inline=True)
    if t.thumbnail:
        e.set_thumbnail(url=t.thumbnail)
    e.set_footer(text=f"{guild.name} • ExcelProtocol Music")
    return e

async def _predownload(player: GuildPlayer):
    """Download the next queued track to a temp file in the background."""
    if not player.queue:
        return
    next_track = list(player.queue)[0]
    if getattr(next_track, "_tmp_path", None):
        return
    try:
        import tempfile, yt_dlp
        with tempfile.NamedTemporaryFile(suffix=".%(ext)s", delete=False, dir="/tmp") as tmp:
            tmp_template = tmp.name.replace(".%(ext)s", "")

        opts = {**get_ytdl_options(player.quality),
                "outtmpl": tmp_template + ".%(ext)s",
                "quiet": True}

        def _dl(url):
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])

        await asyncio.get_event_loop().run_in_executor(None, _dl, next_track.webpage)

        # Find the downloaded file
        import glob
        files = glob.glob(tmp_template + ".*")
        if files:
            next_track._tmp_path = files[0]
            logger.info(f"Pre-downloaded: {next_track.title} → {files[0]}")
    except Exception as e:
        logger.warning(f"Pre-download failed: {e}")

# ── Playback ───────────────────────────────────────────────────────────────────

async def _advance(player: GuildPlayer):
    async with player._play_lock:
        if not player.voice or not player.voice.is_connected():
            return
        if player.is_playing():
            return
        if player.loop and player.current:
            next_track = player.current
        elif player.queue:
            next_track = player.queue.popleft()
        else:
            player.current = None
            await _refresh_embed(player)
            return

        player.current     = next_track
        player.last_active = time.time()

        # Use pre-downloaded file if available
        pre_path = getattr(next_track, "_tmp_path", None)
        if pre_path and os.path.exists(pre_path):
            play_source = pre_path
            player.tmp_path = pre_path
            logger.info(f"Using pre-downloaded file for: {next_track.title}")
        else:
            # Download now using yt-dlp (handles auth headers correctly)
            import tempfile, yt_dlp, glob
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(delete=False, dir="/tmp") as tmp:
                    tmp_template = tmp.name

                opts = {**get_ytdl_options(player.quality),
                        "outtmpl": tmp_template + ".%(ext)s",
                        "quiet": True}

                def _download(webpage):
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([webpage])

                await asyncio.get_event_loop().run_in_executor(None, _download, next_track.webpage)

                files = glob.glob(tmp_template + ".*")
                if files:
                    play_source = files[0]
                    player.tmp_path = files[0]
                else:
                    raise Exception("No output file found after download")
            except Exception as e:
                logger.warning(f"Download failed ({e}), falling back to stream")
                refreshed   = await resolve_query(next_track.webpage, player.quality)
                play_source = refreshed.url if refreshed else next_track.url

        ffmpeg_opts = FFMPEG_OPTIONS_FILE if player.tmp_path else FFMPEG_OPTIONS_STREAM
        source = discord.FFmpegPCMAudio(play_source, **ffmpeg_opts)
        source = discord.PCMVolumeTransformer(source, volume=player.volume)
        player.source = source
        logger.info(f"Playing at volume {player.volume:.2f} in guild {player.guild_id}")

        def after(err):
            if err:
                logger.warning(f"Playback error: {err}")
            if player.tmp_path:
                try:
                    os.remove(player.tmp_path)
                except Exception:
                    pass
                player.tmp_path = None
            player.last_active = time.time()
            asyncio.run_coroutine_threadsafe(_advance(player), _bot_ref.loop)

        player.voice.play(source, after=after)
        await _refresh_embed(player)

        # Start pre-downloading next track in background
        if player.queue:
            asyncio.create_task(_predownload(player))

async def _refresh_embed(player: GuildPlayer):
    if not player.control_msg or not _bot_ref:
        return
    guild = _bot_ref.get_guild(player.guild_id)
    if not guild:
        return
    try:
        view = ControlView(player)
        await player.control_msg.edit(embed=build_embed(player, guild), view=view)
    except discord.NotFound:
        player.control_msg = None
    except Exception as e:
        logger.warning(f"Embed refresh failed: {e}")

async def _disconnect(player: GuildPlayer, guild: discord.Guild, reason: str = ""):
    if player.voice:
        try:
            player.voice.stop()
            await player.voice.disconnect()
        except Exception:
            pass
    player.voice   = None
    player.current = None
    player.source  = None
    # Clean up current temp file
    if player.tmp_path:
        try:
            os.remove(player.tmp_path)
        except Exception:
            pass
        player.tmp_path = None
    # Clean up any pre-downloaded queue files
    for track in player.queue:
        pre = getattr(track, "_tmp_path", None)
        if pre:
            try:
                os.remove(pre)
            except Exception:
                pass
    player.queue.clear()
    player.paused  = False
    if player.control_msg:
        try:
            e = discord.Embed(
                title       = "⏹ Disconnected",
                description = f"Session ended{' (idle timeout)' if reason=='idle' else ''}.",
                color       = 0x555555,
            )
            await player.control_msg.edit(embed=e, view=None)
        except Exception:
            pass
        player.control_msg = None
    # Remove from cache so next /mjoin re-reads settings from DB
    _players.pop(guild.id, None)

async def _music_enabled(guild_id: int) -> bool:
    if not _bot_ref:
        return False
    try:
        rows = _bot_ref.db.db_fetch(
            "SELECT music_enabled FROM guild_music_settings WHERE guild_id=?",
            (guild_id,)
        )
        return bool(rows and rows[0]["music_enabled"])
    except Exception:
        return False

# ── Idle watchdog ──────────────────────────────────────────────────────────────

async def _idle_watchdog():
    while True:
        await asyncio.sleep(30)
        for gid, player in list(_players.items()):
            if not player.voice:
                continue
            humans = [m for m in player.voice.channel.members if not m.bot]
            idle   = not player.is_playing() and not player.is_paused()
            stale  = (time.time() - player.last_active) > IDLE_TIMEOUT
            if not humans or (idle and stale):
                guild = _bot_ref.get_guild(gid) if _bot_ref else None
                if guild:
                    await _disconnect(player, guild, reason="idle")

# ── Views ──────────────────────────────────────────────────────────────────────

class AddModal(discord.ui.Modal, title="Add to Queue"):
    query = discord.ui.TextInput(
        label       = "Song / SoundCloud / Spotify link",
        placeholder = "e.g. Never Gonna Give You Up  or  https://soundcloud.com/...  or  https://open.spotify.com/...",
        max_length  = 300,
    )

    def __init__(self, player: GuildPlayer):
        super().__init__()
        self.player = player

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        q = self.query.value.strip()

        if "open.spotify.com/playlist" in q:
            searches = await resolve_spotify_playlist(q)
            if not searches:
                await interaction.followup.send("❌ Could not resolve Spotify playlist.", ephemeral=True)
                return
            added = 0
            for s in searches[:50]:
                track = await resolve_query(s, self.player.quality)
                if track:
                    track.requester = interaction.user
                    self.player.queue.append(track)
                    added += 1
            await interaction.followup.send(f"✅ Added **{added}** tracks from playlist.", ephemeral=True)
        else:
            track = await resolve_query(q, self.player.quality)
            if not track:
                await interaction.followup.send("❌ Could not find that track.", ephemeral=True)
                return
            track.requester = interaction.user
            self.player.queue.append(track)
            await interaction.followup.send(f"✅ Added **{track.title}** to queue.", ephemeral=True)

        if not self.player.is_playing() and not self.player.is_paused():
            await _advance(self.player)
        else:
            await _refresh_embed(self.player)


class QueueView(discord.ui.View):
    def __init__(self, player: GuildPlayer, page: int = 0):
        super().__init__(timeout=60)
        self.player = player
        self.page   = page
        self._update_buttons()

    def _update_buttons(self):
        pages = max(1, (len(self.player.queue) + 9) // 10)
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= pages - 1

    def build_embed(self) -> discord.Embed:
        items = list(self.player.queue)
        total = len(items)
        pages = max(1, (total + 9) // 10)
        start = self.page * 10
        chunk = items[start:start + 10]
        e = discord.Embed(title=f"📋 Queue — Page {self.page+1}/{pages}", color=0x1db954)
        if self.player.current:
            e.add_field(name="Now Playing", value=f"[{self.player.current.title}]({self.player.current.webpage})", inline=False)
        if chunk:
            lines = [
                f"`{start+i+1}.` [{t.title}]({t.webpage}) — {fmt_duration(t.duration)} • {t.requester.display_name if t.requester else '?'}"
                for i, t in enumerate(chunk)
            ]
            e.add_field(name="Up Next", value="\n".join(lines), inline=False)
        else:
            e.description = "Queue is empty."
        return e

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class ControlView(discord.ui.View):
    def __init__(self, player: GuildPlayer):
        super().__init__(timeout=None)
        self.player = player

    @discord.ui.button(label="➕ Add", style=discord.ButtonStyle.primary, custom_id="music_add")
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddModal(self.player))

    @discord.ui.button(label="⏸ Pause", style=discord.ButtonStyle.secondary, custom_id="music_pause")
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.player.is_playing():
            self.player.voice.pause()
            self.player.paused = True
            button.label = "▶ Resume"
        elif self.player.is_paused():
            self.player.voice.resume()
            self.player.paused = False
            button.label = "⏸ Pause"
        await interaction.response.edit_message(
            embed=build_embed(self.player, interaction.guild), view=self)

    @discord.ui.button(label="⏭ Skip", style=discord.ButtonStyle.secondary, custom_id="music_skip")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.player.voice and (self.player.is_playing() or self.player.is_paused()):
            self.player.voice.stop()
        await interaction.response.defer()

    @discord.ui.button(label="📋 Queue", style=discord.ButtonStyle.secondary, custom_id="music_queue")
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = QueueView(self.player)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

    @discord.ui.button(label="🔁 Loop", style=discord.ButtonStyle.secondary, custom_id="music_loop")
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.loop = not self.player.loop
        button.style = discord.ButtonStyle.success if self.player.loop else discord.ButtonStyle.secondary
        await interaction.response.edit_message(
            embed=build_embed(self.player, interaction.guild), view=self)

    @discord.ui.button(label="⏹ Disconnect", style=discord.ButtonStyle.danger, custom_id="music_dc")
    async def dc_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _disconnect(self.player, interaction.guild)
        await interaction.response.defer()

# ── Setup ──────────────────────────────────────────────────────────────────────

async def setup(discord_bot):
    global _bot_ref
    _bot_ref = discord_bot

    # Start idle watchdog
    discord_bot.loop.create_task(_idle_watchdog())

    @discord_bot.tree.command(name="mjoin", description="Join your voice channel and start the music player")
    async def mjoin(interaction: discord.Interaction):
        if not await _music_enabled(interaction.guild_id):
            await interaction.response.send_message(
                "❌ Music is not enabled for this server. Ask an admin to enable it in the ExcelProtocol dashboard.",
                ephemeral=True,
            )
            return

        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.voice or not member.voice.channel:
            await interaction.response.send_message(
                "❌ You must be in a voice channel first.", ephemeral=True)
            return

        player = get_player(interaction.guild_id)

        if player.voice and player.voice.is_connected():
            if player.voice.channel.id == member.voice.channel.id:
                await interaction.response.send_message(
                    "Already in your channel!", ephemeral=True)
                return
            await player.voice.move_to(member.voice.channel)
        else:
            player.voice = await member.voice.channel.connect()

        await interaction.response.defer()
        view  = ControlView(player)
        embed = build_embed(player, interaction.guild)
        msg   = await interaction.followup.send(embed=embed, view=view)
        player.control_msg = msg
        player.last_active = time.time()

    @discord_bot.tree.command(name="mleave", description="Disconnect the music bot from voice")
    async def mleave(interaction: discord.Interaction):
        player = get_player(interaction.guild_id)
        if not player.voice:
            await interaction.response.send_message(
                "Not in a voice channel.", ephemeral=True)
            return
        await _disconnect(player, interaction.guild)
        await interaction.response.send_message("👋 Disconnected.", ephemeral=True)

    @discord_bot.tree.command(name="msettings", description="Adjust music volume and quality live")
    @app_commands.describe(
        volume="Volume 1-100 (default 70)",
        quality="Audio quality: low, medium, or high",
    )
    @app_commands.choices(quality=[
        app_commands.Choice(name="Low (less CPU)",  value="low"),
        app_commands.Choice(name="Medium",           value="medium"),
        app_commands.Choice(name="High (more CPU)",  value="high"),
    ])
    async def msettings(interaction: discord.Interaction, volume: Optional[int] = None, quality: Optional[str] = None):
        player = _players.get(interaction.guild_id)
        if not player or not player.voice:
            await interaction.response.send_message("❌ Not currently playing in this server.", ephemeral=True)
            return
        changes = []
        if volume is not None:
            volume = max(1, min(100, volume))
            player.volume = volume / 100.0
            if player.source:
                player.source.volume = player.volume
            changes.append(f"Volume → **{volume}%**")
        if quality is not None:
            player.quality = quality
            changes.append(f"Quality → **{quality}** (applies to next track)")
        if not changes:
            await interaction.response.send_message(
                f"Current settings — Volume: **{int(player.volume*100)}%** | Quality: **{player.quality}**",
                ephemeral=True)
            return
        # Write back to DB so dashboard reflects changes
        try:
            discord_bot.db.set_music_settings(
                interaction.guild_id,
                volume=int(player.volume * 100),
                quality=player.quality
            )
        except Exception as e:
            logger.warning(f"Failed to write music settings to DB: {e}")
        await interaction.response.send_message(f"✅ Updated: {', '.join(changes)}", ephemeral=True)
        await _refresh_embed(player)

    logger.info("Music commands registered")
