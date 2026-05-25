"""
music.py — ExcelProtocol Music Cog
Handles /mjoin, queue management, Spotify resolution, yt-dlp streaming.
Disabled by default; enabled per-guild via dashboard (admin/dev only).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

IDLE_TIMEOUT = 300  # seconds before auto-disconnect on empty/idle VC

# ── Track dataclass ────────────────────────────────────────────────────────────

@dataclass
class Track:
    title:     str
    url:       str          # direct stream URL (resolved by yt-dlp)
    webpage:   str          # original user input / yt page URL
    duration:  int          # seconds, 0 if unknown
    requester: discord.Member
    thumbnail: str = ""

# ── Per-guild player state ─────────────────────────────────────────────────────

class GuildPlayer:
    def __init__(self, guild_id: int):
        self.guild_id    = guild_id
        self.queue: deque[Track] = deque()
        self.current:    Optional[Track] = None
        self.voice:      Optional[discord.VoiceClient] = None
        self.control_msg: Optional[discord.Message] = None
        self.paused      = False
        self.loop        = False
        self.volume      = 0.7
        self.last_active = time.time()
        self._play_lock  = asyncio.Lock()

    def is_playing(self) -> bool:
        return self.voice is not None and self.voice.is_playing()

    def is_paused(self) -> bool:
        return self.voice is not None and self.voice.is_paused()

# ── YT-DLP helpers ─────────────────────────────────────────────────────────────

YTDL_OPTIONS = {
    "format":           "bestaudio/best",
    "noplaylist":       True,
    "quiet":            True,
    "no_warnings":      True,
    "default_search":   "ytsearch",
    "source_address":   "0.0.0.0",
    "extract_flat":     False,
}

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options":        "-vn",
}

async def resolve_query(query: str) -> Optional[Track]:
    """Resolve a search query, YouTube URL, or Spotify track URL to a Track."""
    import yt_dlp  # imported lazily so bot starts even if not installed

    loop = asyncio.get_event_loop()

    # Spotify track URL → extract title+artist via Spotipy, then search YT
    if "open.spotify.com/track" in query:
        query = await _spotify_track_to_search(query)
        if not query:
            return None

    def _extract(q):
        with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
            info = ydl.extract_info(q, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return info

    try:
        info = await loop.run_in_executor(None, _extract, query)
    except Exception as e:
        logger.warning(f"yt-dlp failed for {query!r}: {e}")
        return None

    return Track(
        title     = info.get("title", "Unknown"),
        url       = info["url"],
        webpage   = info.get("webpage_url", query),
        duration  = info.get("duration", 0) or 0,
        requester = None,  # filled in by caller
        thumbnail = info.get("thumbnail", ""),
    )


async def resolve_spotify_playlist(playlist_url: str) -> list[str]:
    """Return a list of 'artist - title' search strings from a Spotify playlist."""
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
        sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
        ))
        results = sp.playlist_tracks(playlist_url)
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
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
        ))
        tid = url.split("/track/")[1].split("?")[0]
        t   = sp.track(tid)
        artists = ", ".join(a["name"] for a in t.get("artists", []))
        return f"{artists} - {t['name']}"
    except Exception as e:
        logger.warning(f"Spotify track resolve failed: {e}")
        return None


def fmt_duration(seconds: int) -> str:
    if not seconds:
        return "?"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ── Control embed + views ──────────────────────────────────────────────────────

def build_embed(player: GuildPlayer, guild: discord.Guild) -> discord.Embed:
    t = player.current
    if not t:
        e = discord.Embed(title="🎵 Nothing playing", color=0x1db954)
        e.set_footer(text="Use /mjoin to start")
        return e

    status = "⏸ Paused" if player.is_paused() else "▶ Playing"
    loop   = " 🔁" if player.loop else ""
    e = discord.Embed(
        title       = f"{status}{loop}",
        description = f"[{t.title}]({t.webpage})",
        color       = 0x1db954,
    )
    e.add_field(name="Duration",   value=fmt_duration(t.duration), inline=True)
    e.add_field(name="Requested",  value=t.requester.display_name if t.requester else "?", inline=True)
    e.add_field(name="Volume",     value=f"{int(player.volume*100)}%", inline=True)
    e.add_field(name="Queue",      value=f"{len(player.queue)} track(s)", inline=True)
    if t.thumbnail:
        e.set_thumbnail(url=t.thumbnail)
    e.set_footer(text=f"{guild.name} • ExcelProtocol Music")
    return e


class AddModal(discord.ui.Modal, title="Add to Queue"):
    query = discord.ui.TextInput(
        label       = "Song / Spotify / YouTube link",
        placeholder = "e.g. Never Gonna Give You Up or https://open.spotify.com/...",
        max_length  = 300,
    )

    def __init__(self, cog: "MusicCog", player: GuildPlayer):
        super().__init__()
        self.cog    = cog
        self.player = player

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)
        q = self.query.value.strip()

        # Spotify playlist
        if "open.spotify.com/playlist" in q:
            searches = await resolve_spotify_playlist(q)
            if not searches:
                await interaction.followup.send("❌ Could not resolve Spotify playlist.", ephemeral=True)
                return
            added = 0
            for s in searches[:50]:  # cap at 50
                track = await resolve_query(s)
                if track:
                    track.requester = interaction.user
                    self.player.queue.append(track)
                    added += 1
            await interaction.followup.send(f"✅ Added **{added}** tracks from playlist.", ephemeral=True)
        else:
            track = await resolve_query(q)
            if not track:
                await interaction.followup.send("❌ Could not find that track.", ephemeral=True)
                return
            track.requester = interaction.user
            self.player.queue.append(track)
            await interaction.followup.send(f"✅ Added **{track.title}** to queue.", ephemeral=True)

        if not self.player.is_playing() and not self.player.is_paused():
            await self.cog._advance(self.player)
        else:
            await self.cog._refresh_embed(self.player)


class QueueView(discord.ui.View):
    def __init__(self, player: GuildPlayer, page: int = 0):
        super().__init__(timeout=60)
        self.player = player
        self.page   = page
        self._update_buttons()

    def _update_buttons(self):
        total = len(self.player.queue)
        pages = max(1, (total + 9) // 10)
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= pages - 1

    def build_embed(self) -> discord.Embed:
        items  = list(self.player.queue)
        total  = len(items)
        pages  = max(1, (total + 9) // 10)
        start  = self.page * 10
        chunk  = items[start:start + 10]

        e = discord.Embed(title=f"📋 Queue — Page {self.page+1}/{pages}", color=0x1db954)
        if self.player.current:
            e.add_field(name="Now Playing", value=f"[{self.player.current.title}]({self.player.current.webpage})", inline=False)
        if chunk:
            lines = [f"`{start+i+1}.` [{t.title}]({t.webpage}) — {fmt_duration(t.duration)} • {t.requester.display_name if t.requester else '?'}" for i, t in enumerate(chunk)]
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
    def __init__(self, cog: "MusicCog", player: GuildPlayer):
        super().__init__(timeout=None)
        self.cog    = cog
        self.player = player

    @discord.ui.button(label="➕ Add", style=discord.ButtonStyle.primary,  custom_id="music_add")
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddModal(self.cog, self.player))

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
        await interaction.response.edit_message(embed=build_embed(self.player, interaction.guild), view=self)

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
        await interaction.response.edit_message(embed=build_embed(self.player, interaction.guild), view=self)

    @discord.ui.button(label="⏹ Disconnect", style=discord.ButtonStyle.danger, custom_id="music_dc")
    async def dc_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog._disconnect(self.player, interaction.guild)
        await interaction.response.defer()


# ── Music Cog ─────────────────────────────────────────────────────────────────

class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot     = bot
        self._players: dict[int, GuildPlayer] = {}
        self._idle_task = bot.loop.create_task(self._idle_watchdog())

    def cog_unload(self):
        self._idle_task.cancel()

    def get_player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self._players:
            self._players[guild_id] = GuildPlayer(guild_id)
        return self._players[guild_id]

    # ── idle watchdog ──────────────────────────────────────────────────────────

    async def _idle_watchdog(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(30)
            for gid, player in list(self._players.items()):
                if player.voice is None:
                    continue
                vc     = player.voice
                humans = [m for m in vc.channel.members if not m.bot]
                idle   = not player.is_playing() and not player.is_paused()
                stale  = (time.time() - player.last_active) > IDLE_TIMEOUT
                if not humans or (idle and stale):
                    guild = self.bot.get_guild(gid)
                    if guild:
                        await self._disconnect(player, guild, reason="idle")

    # ── playback ──────────────────────────────────────────────────────────────

    async def _advance(self, player: GuildPlayer):
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
                await self._refresh_embed(player)
                return

            player.current      = next_track
            player.last_active  = time.time()

            # Re-resolve URL to avoid expired streams
            refreshed = await resolve_query(next_track.webpage)
            stream_url = refreshed.url if refreshed else next_track.url

            source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)
            source = discord.PCMVolumeTransformer(source, volume=player.volume)

            def after(err):
                if err:
                    logger.warning(f"Playback error: {err}")
                player.last_active = time.time()
                asyncio.run_coroutine_threadsafe(self._advance(player), self.bot.loop)

            player.voice.play(source, after=after)
            await self._refresh_embed(player)

    async def _refresh_embed(self, player: GuildPlayer):
        if not player.control_msg:
            return
        guild = self.bot.get_guild(player.guild_id)
        if not guild:
            return
        try:
            view = ControlView(self, player)
            await player.control_msg.edit(
                embed = build_embed(player, guild),
                view  = view,
            )
        except discord.NotFound:
            player.control_msg = None
        except Exception as e:
            logger.warning(f"Embed refresh failed: {e}")

    async def _disconnect(self, player: GuildPlayer, guild: discord.Guild, reason: str = ""):
        if player.voice:
            try:
                player.voice.stop()
                await player.voice.disconnect()
            except Exception:
                pass
        player.voice    = None
        player.current  = None
        player.queue.clear()
        player.paused   = False
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

    # ── music enabled check ───────────────────────────────────────────────────

    async def _music_enabled(self, guild_id: int) -> bool:
        try:
            loop = asyncio.get_event_loop()
            rows = await loop.run_in_executor(
                None,
                lambda: self.bot.db.db_fetch(
                    "SELECT music_enabled FROM guild_music_settings WHERE guild_id=?",
                    (guild_id,)
                )
            )
            return bool(rows and rows[0]["music_enabled"])
        except Exception:
            return False

    # ── slash commands ────────────────────────────────────────────────────────

    @app_commands.command(name="mjoin", description="Join your voice channel and start the music player")
    async def mjoin(self, interaction: discord.Interaction):
        if not await self._music_enabled(interaction.guild_id):
            await interaction.response.send_message(
                "❌ Music is not enabled for this server. Ask an admin to enable it in the ExcelProtocol dashboard.",
                ephemeral=True,
            )
            return

        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.voice or not member.voice.channel:
            await interaction.response.send_message("❌ You must be in a voice channel first.", ephemeral=True)
            return

        player = self.get_player(interaction.guild_id)

        # Already connected somewhere
        if player.voice and player.voice.is_connected():
            if player.voice.channel.id == member.voice.channel.id:
                await interaction.response.send_message("Already in your channel!", ephemeral=True)
                return
            await player.voice.move_to(member.voice.channel)
        else:
            player.voice = await member.voice.channel.connect()

        await interaction.response.defer()
        view  = ControlView(self, player)
        embed = build_embed(player, interaction.guild)
        msg   = await interaction.followup.send(embed=embed, view=view)
        player.control_msg  = msg
        player.last_active  = time.time()

    @app_commands.command(name="mleave", description="Disconnect the music bot")
    async def mleave(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild_id)
        if not player.voice:
            await interaction.response.send_message("Not in a voice channel.", ephemeral=True)
            return
        await self._disconnect(player, interaction.guild)
        await interaction.response.send_message("👋 Disconnected.", ephemeral=True)

    def get_all_players(self) -> dict[int, GuildPlayer]:
        return self._players


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
    logger.info("MusicCog loaded")
