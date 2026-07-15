import asyncio
import os
import time
from collections import deque

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

_cookies_env = os.environ.get("YTDLP_COOKIES")

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "socket_timeout": 10,
    "extractor_args": {
        "youtube": {"player_client": ["tv_downgraded", "web_safari"] if _cookies_env else ["android_vr"]}
    },
}

if _cookies_env:
    _cookies_path = "/tmp/cookies.txt"
    with open(_cookies_path, "w") as f:
        f.write(_cookies_env)
    YDL_OPTS["cookiefile"] = _cookies_path

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

PLAYLIST_LIMIT = 200
BOT_OWNER_ID = int(os.environ["BOT_OWNER_ID"]) if os.environ.get("BOT_OWNER_ID") else None
FARM_DEFAULT_URL = os.environ.get("FARM_DEFAULT_URL")
REPORT_OWNER_ID = int(os.environ.get("REPORT_OWNER_ID", BOT_OWNER_ID or "0"))

# ════════════════════════════════════════════════════════════════════
#  NUEVAS VARIABLES PARA EL CENTINELA
# ════════════════════════════════════════════════════════════════════
CANAL_VOZ_ID = int(os.environ.get("CANAL_VOZ_ID", "0"))  # ID del canal a proteger
BOT_CENTINELA_ID = int(os.environ.get("BOT_CENTINELA_ID", "0"))  # ID del otro bot (el centinela)
MODO_CENTINELA = os.environ.get("MODO_CENTINELA", "false").lower() == "true"  # true = este bot es el centinela
BOT_PRINCIPAL_ID = int(os.environ.get("BOT_PRINCIPAL_ID", "0"))  # ID del bot principal (si este es el centinela)

# ════════════════════════════════════════════════════════════════════


def format_duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class Track:
    def __init__(self, title, webpage_url, thumbnail, duration, stream_url=None, requester=None, author=None):
        self.title = title
        self.webpage_url = webpage_url
        self.thumbnail = thumbnail
        self.duration = duration
        self.stream_url = stream_url
        self.requester = requester
        self.author = author or "Desconocido"


def _search_with(opts: dict, query: str):
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
    if info is None:
        return []
    if "entries" in info:
        return [e for e in info["entries"] if e]
    return [info]


def extract_track(query: str) -> Track:
    is_url = query.startswith("http://") or query.startswith("https://")
    info = None

    if is_url:
        try:
            results = _search_with(YDL_OPTS, query)
            info = results[0] if results else None
        except yt_dlp.utils.DownloadError:
            info = None
    else:
        search_opts = dict(YDL_OPTS)
        search_opts["default_search"] = "ytsearch5"
        try:
            candidates = _search_with(search_opts, query)
        except yt_dlp.utils.DownloadError:
            candidates = []

        for candidate in candidates:
            try:
                webpage_url = candidate.get("webpage_url") or candidate.get("url")
                with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
                    info = ydl.extract_info(webpage_url, download=False)
                break
            except yt_dlp.utils.DownloadError:
                continue

    if info is None and not is_url:
        soundcloud_opts = dict(YDL_OPTS)
        soundcloud_opts["default_search"] = "scsearch1"
        try:
            results = _search_with(soundcloud_opts, query)
            info = results[0] if results else None
        except yt_dlp.utils.DownloadError:
            info = None

    if info is None:
        raise yt_dlp.utils.DownloadError("No se encontró en YouTube ni en SoundCloud.")

    return Track(
        title=info.get("title", "Desconocido"),
        webpage_url=info.get("webpage_url", query),
        thumbnail=info.get("thumbnail"),
        duration=info.get("duration", 0),
        stream_url=info["url"],
        author=info.get("artist") or info.get("uploader") or info.get("channel"),
    )


def extract_playlist(url: str) -> list[Track]:
    opts = dict(YDL_OPTS)
    opts.update({"noplaylist": False, "extract_flat": True, "playlistend": PLAYLIST_LIMIT})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    tracks = []
    for entry in (info.get("entries") or [])[:PLAYLIST_LIMIT]:
        if entry is None:
            continue
        video_id = entry.get("id")
        webpage_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else entry.get("url")
        tracks.append(
            Track(
                title=entry.get("title", "Desconocido"),
                webpage_url=webpage_url,
                thumbnail=entry.get("thumbnail"),
                duration=entry.get("duration", 0),
                author=entry.get("artist") or entry.get("uploader") or entry.get("channel"),
            )
        )
    return tracks


def resolve_stream(track: Track):
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(track.webpage_url, download=False)
        track.stream_url = info["url"]
        track.thumbnail = track.thumbnail or info.get("thumbnail")
        track.duration = track.duration or info.get("duration", 0)


class GuildPlayer:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queue: deque[Track] = deque()
        self.history: list[Track] = []
        self.current: Track | None = None
        self.next_override: Track | None = None
        self.voice_client: discord.VoiceClient | None = None
        self.now_playing_message: discord.Message | None = None
        self.started_at: float | None = None
        self.paused_at: float | None = None
        self.paused_total: float = 0.0
        self.update_task: asyncio.Task | None = None
        self.idle_task: asyncio.Task | None = None
        self.locked: bool = False
        self.locked_channel_id: int | None = None
        self.locked_recovering: bool = False
        self.loop_prep_task: asyncio.Task | None = None
        self._next_loop_url: str | None = None
        self.loop_current: bool = False
        self.farm_channel_id: int | None = None

    def start_idle_timer(self):
        if self.idle_task is None:
            self.idle_task = asyncio.create_task(self._idle_disconnect())

    def cancel_idle_timer(self):
        if self.idle_task:
            self.idle_task.cancel()
            self.idle_task = None

    async def _start(self, track: Track):
        self.cancel_idle_timer()

        if track.stream_url is None:
            await asyncio.to_thread(resolve_stream, track)
        self.current = track
        self.started_at = time.monotonic()
        self.paused_at = None
        self.paused_total = 0.0

        source = discord.FFmpegPCMAudio(track.stream_url, **FFMPEG_OPTS)

        def after(error):
            if not self.locked_recovering:
                asyncio.run_coroutine_threadsafe(self._on_track_end(), self.bot.loop)

        self.voice_client.play(source, after=after, bitrate=384, signal_type="music")
        embed, view = self.build_now_playing(track)
        try:
            self.now_playing_message = await self.voice_client.channel.send(embed=embed, view=view)
        except discord.HTTPException:
            self.now_playing_message = None

        if self.loop_prep_task:
            self.loop_prep_task.cancel()
        self._next_loop_url = None
        if self.loop_current:
            self.loop_prep_task = asyncio.create_task(self._prepare_next_loop(track))

    async def _prepare_next_loop(self, track: Track):
        wait_seconds = max((track.duration or 0) - 300, 0)
        try:
            await asyncio.sleep(wait_seconds)
            fresh = Track(track.title, track.webpage_url, track.thumbnail, track.duration, requester=track.requester, author=track.author)
            await asyncio.to_thread(resolve_stream, fresh)
            self._next_loop_url = fresh.stream_url
        except asyncio.CancelledError:
            raise
        except Exception:
            self._next_loop_url = None

    async def _on_track_end(self):
        if self.locked_recovering:
            return
        if self.update_task:
            self.update_task.cancel()
            self.update_task = None

        if self.next_override is not None:
            track = self.next_override
            self.next_override = None
            await self._start(track)
            return

        if self.loop_current and self.current is not None:
            track = self.current
            if self._next_loop_url:
                track.stream_url = self._next_loop_url
            else:
                track.stream_url = None
            await self._start(track)
            return

        if self.current:
            self.history.append(self.current)
        if not self.queue:
            self.current = None
            self.start_idle_timer()
            return
        track = self.queue.popleft()
        await self._start(track)

    async def play_next(self):
        await self._on_track_end()

    async def go_previous(self) -> bool:
        if not self.history:
            return False
        if self.current:
            self.queue.appendleft(self.current)
        self.next_override = self.history.pop()
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()
        else:
            await self._on_track_end()
        return True

    async def _idle_disconnect(self):
        try:
            await asyncio.sleep(24 * 60 * 60)
        except asyncio.CancelledError:
            return
        await self.stop_all()

    async def stop_all(self):
        if self.update_task:
            self.update_task.cancel()
            self.update_task = None
        if self.idle_task:
            self.idle_task.cancel()
            self.idle_task = None
        if self.loop_prep_task:
            self.loop_prep_task.cancel()
            self.loop_prep_task = None
        self.queue.clear()
        self.history.clear()
        self.current = None
        self.next_override = None
        self.loop_current = False
        self.farm_channel_id = None
        if self.voice_client:
            self.voice_client.stop()
            await self.voice_client.disconnect()
            self.voice_client = None

    def build_now_playing(self, track: Track):
        embed = discord.Embed(title=track.title, url=track.webpage_url, color=discord.Color.blurple())
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        lines = [f"**Author:** {track.author}", f"**Duration:** {format_duration(track.duration)}"]
        if track.requester:
            lines.append(f"**Requester:** {track.requester.mention}")
        embed.description = "\n".join(f"• {line}" for line in lines)
        if self.queue:
            embed.set_footer(text=f"{len(self.queue)} canción(es) en cola")
        return embed, NowPlayingView(self)


class MoveConfirmView(discord.ui.View):
    def __init__(self, author: discord.Member):
        super().__init__(timeout=30)
        self.author = author
        self.result: asyncio.Future = asyncio.get_event_loop().create_future()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "❌ Solo quien usó el comando puede decidir esto.", ephemeral=True
            )
            return False
        return True

    def _disable_all(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Mover de todos modos", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable_all()
        await interaction.response.edit_message(content="✅ Moviendo al nuevo canal...", embed=None, view=self)
        if not self.result.done():
            self.result.set_result(True)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable_all()
        await interaction.response.edit_message(content="❌ Cancelado, no me moví.", embed=None, view=self)
        if not self.result.done():
            self.result.set_result(False)

    async def on_timeout(self):
        if not self.result.done():
            self.result.set_result(False)


async def ensure_voice_connection(author: discord.Member, send, player: "GuildPlayer") -> bool:
    if author.voice is None or author.voice.channel is None:
        await send("❌ Debes estar en un canal de voz para usar este comando.")
        return False

    target_channel = author.voice.channel

    if player.voice_client is None:
        player.voice_client = await target_channel.connect()
        return True

    if player.voice_client.channel.id == target_channel.id:
        return True

    current_channel = player.voice_client.channel

    if player.locked:
        await send(
            f"🔒 Estoy fijado (lock) en **{current_channel.name}** y no me puedo mover. "
            "Pídele al owner del bot que quite el modo lock ahí primero."
        )
        return False

    if player.loop_current:
        await send(
            f"🌾 Estoy en modo farm en **{current_channel.name}** y no me puedo mover. "
            "Pídele al owner del bot que apague el modo farm ahí primero."
        )
        return False

    embed = discord.Embed(
        title="⚠️ Ya estoy en otro canal",
        description=(
            f"Estoy en **{current_channel.name}**. ¿Quieres que me mueva a **{target_channel.name}**?"
        ),
        color=discord.Color.orange(),
    )
    view = MoveConfirmView(author)
    msg = await send(embed=embed, view=view)
    confirmed = await view.result
    try:
        await msg.delete()
    except discord.HTTPException:
        pass

    if not confirmed:
        return False

    await player.voice_client.move_to(target_channel)
    return True


def _custom_icon(env_var: str, name: str):
    emoji_id = os.environ.get(env_var)
    if not emoji_id:
        return None
    return discord.PartialEmoji(name=name, id=int(emoji_id))


ICON_PREVIOUS = _custom_icon("ICON_PREVIOUS_ID", "previous")
ICON_PLAY = _custom_icon("ICON_PLAY_ID", "play")
ICON_PAUSE = _custom_icon("ICON_PAUSE_ID", "pause")
ICON_NEXT = _custom_icon("ICON_NEXT_ID", "next")
ICON_STOP = _custom_icon("ICON_STOP_ID", "stop")


class NowPlayingView(discord.ui.View):
    def __init__(self, player: GuildPlayer):
        super().__init__(timeout=None)
        self.player = player

    async def _in_voice(self, interaction: discord.Interaction) -> bool:
        vc = self.player.voice_client
        if not vc or not interaction.user.voice or interaction.user.voice.channel != vc.channel:
            await interaction.response.send_message("Debes estar en el canal de voz del bot.", ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji=ICON_PREVIOUS, label="Anterior", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._in_voice(interaction):
            return
        ok = await self.player.go_previous()
        msg = "Reproduciendo anterior." if ok else "No hay canción anterior."
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(emoji=ICON_PAUSE, label="Pausar", style=discord.ButtonStyle.primary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._in_voice(interaction):
            return
        vc = self.player.voice_client
        if vc.is_playing():
            vc.pause()
            self.player.paused_at = time.monotonic()
            button.emoji = ICON_PLAY
            button.label = "Reanudar"
            await interaction.response.edit_message(view=self)
        elif vc.is_paused():
            self.player.paused_total += time.monotonic() - self.player.paused_at
            self.player.paused_at = None
            vc.resume()
            button.emoji = ICON_PAUSE
            button.label = "Pausar"
            await interaction.response.edit_message(view=self)
        else:
            await interaction.response.send_message("No hay nada sonando.", ephemeral=True)

    @discord.ui.button(emoji=ICON_NEXT, label="Saltar", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._in_voice(interaction):
            return
        if not (self.player.voice_client.is_playing() or self.player.voice_client.is_paused()):
            await interaction.response.send_message("No hay nada sonando.", ephemeral=True)
            return
        self.player.voice_client.stop()
        await interaction.response.send_message("Saltando...", ephemeral=True)

    @discord.ui.button(emoji=ICON_STOP, label="Detener", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._in_voice(interaction):
            return
        await self.player.stop_all()
        await interaction.response.send_message("Detenido y desconectado.", ephemeral=True)


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        self.assigned_user_id: int | None = None

    def _has_special_access(self, user_id: int, guild: discord.Guild) -> bool:
        return user_id == BOT_OWNER_ID or user_id == self.assigned_user_id or user_id == guild.owner_id

    def _is_owner_only(self, user_id: int, guild: discord.Guild) -> bool:
        return user_id == BOT_OWNER_ID or user_id == guild.owner_id

    # ════════════════════════════════════════════════════════════════════
    #  FUNCIONES DEL CENTINELA
    # ════════════════════════════════════════════════════════════════════

    async def _identificar_culpable(self, guild: discord.Guild, accion, objetivo_id: int, max_edad: float = 15.0):
        """Identifica al responsable de una acción en el audit log."""
        try:
            async for entry in guild.audit_logs(limit=10, action=accion):
                if entry.target and entry.target.id == objetivo_id:
                    edad = (discord.utils.utcnow() - entry.created_at).total_seconds()
                    if edad <= max_edad:
                        return entry.user
        except discord.Forbidden:
            return None
        return None

    async def _quitar_permisos_canal(self, miembro: discord.Member, canal: discord.VoiceChannel) -> bool:
        """Quita TODOS los permisos de un miembro en un canal de voz."""
        try:
            deny = discord.PermissionOverwrite(
                connect=False,
                move_members=False,
                mute_members=False,
                deafen_members=False,
                priority_speaker=False,
                stream=False,
                use_voice_activity=False,
                speak=False,
                request_to_speak=False,
                create_instant_invite=False,
                manage_channels=False,
                manage_roles=False,
                kick_members=False,
                ban_members=False,
                administrator=False,
            )
            await canal.set_permissions(miembro, overwrite=deny)
            return True
        except:
            return False

    async def _restaurar_permisos_canal(self, miembro: discord.Member, canal: discord.VoiceChannel) -> bool:
        """Elimina los permisos personalizados de un miembro en un canal."""
        try:
            await canal.set_permissions(miembro, overwrite=None)
            return True
        except:
            return False

    async def _reportar_agresor(self, guild: discord.Guild, canal: discord.VoiceChannel, culpable: discord.Member, accion: str):
        """Reporta al owner por DM con los detalles del incidente."""
        try:
            owner = await self.bot.fetch_user(REPORT_OWNER_ID)
            if not owner:
                return
            embed = discord.Embed(
                title="🚨 ALERTA DEL CENTINELA",
                description=(
                    f"**Servidor:** {guild.name}\n"
                    f"**Canal:** {canal.name if canal else 'Desconocido'}\n"
                    f"**Responsable:** {culpable.mention} (`{culpable.id}`)\n"
                    f"**Acción:** {accion}\n"
                    f"**Medida:** Se quitaron permisos en el canal"
                ),
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow()
            )
            await owner.send(embed=embed)
        except:
            pass

    async def _reconectar_bot(self, guild: discord.Guild, canal: discord.VoiceChannel, bot_id: int):
        """Reconecta a un bot al canal de voz."""
        try:
            bot_member = guild.get_member(bot_id)
            if bot_member:
                await bot_member.move_to(canal)
                return True
        except:
            pass
        return False

    # ════════════════════════════════════════════════════════════════════
    #  EVENTO on_voice_state_update (MEJORADO PARA EL CENTINELA)
    # ════════════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        # ── Protección del bot principal (si este bot es el centinela) ──
        if MODO_CENTINELA and BOT_PRINCIPAL_ID > 0:
            if member.id == BOT_PRINCIPAL_ID:
                if before.channel and not after.channel:
                    # El bot principal fue desconectado
                    guild = member.guild
                    canal_voz = guild.get_channel(CANAL_VOZ_ID)
                    culpable = await self._identificar_culpable(guild, discord.AuditLogAction.member_disconnect, BOT_PRINCIPAL_ID)

                    if culpable and canal_voz:
                        await self._quitar_permisos_canal(culpable, canal_voz)
                        await self._reportar_agresor(guild, canal_voz, culpable, "Intentó desconectar al bot principal")
                        await self._reconectar_bot(guild, canal_voz, BOT_PRINCIPAL_ID)

                    # Asegurar que el centinela sigue en el canal
                    if not self.voice_client or not self.voice_client.is_connected():
                        if canal_voz:
                            try:
                                player = self.get_player(guild.id)
                                player.voice_client = await canal_voz.connect()
                            except:
                                pass
                    return

        # ── Protección del propio bot (lock / farm) ──
        if member.id == self.bot.user.id:
            player = self.get_player(member.guild.id)

            # Detectar si el bot fue movido de su canal protegido
            if player.locked and before.channel and before.channel.id == player.locked_channel_id:
                if after.channel and after.channel.id != player.locked_channel_id:
                    canal_original = member.guild.get_channel(player.locked_channel_id)
                    if canal_original:
                        culprit = await self._identificar_culpable(member.guild, discord.AuditLogAction.member_move, self.bot.user.id)
                        if culprit:
                            await self._quitar_permisos_canal(culpultor, canal_original)
                            await self._reportar_agresor(member.guild, canal_original, culprit, "Intentó mover al bot con lock activo")
                        await asyncio.sleep(0.5)
                        try:
                            player.voice_client = await canal_original.connect()
                        except:
                            pass
                return

            if player.loop_current and player.farm_channel_id and before.channel and before.channel.id == player.farm_channel_id:
                if after.channel and after.channel.id != player.farm_channel_id:
                    canal_original = member.guild.get_channel(player.farm_channel_id)
                    if canal_original:
                        culprit = await self._identificar_culpable(member.guild, discord.AuditLogAction.member_move, self.bot.user.id)
                        if culprit:
                            await self._quitar_permisos_canal(culprit, canal_original)
                            await self._reportar_agresor(member.guild, canal_original, culprit, "Intentó mover al bot con farm activo")
                        await asyncio.sleep(0.5)
                        try:
                            player.voice_client = await canal_original.connect()
                            # Reanudar reproducción en el canal original
                            if player.current:
                                track = player.current
                                track.stream_url = None
                                await player._start(track)
                        except:
                            pass
                return

            # Protección contra desconexión forzada
            if before.channel and not after.channel:
                # Fue desconectado
                player = self.get_player(member.guild.id)

                canal_origen = None
                modo = None

                if player.locked and player.locked_channel_id:
                    canal_origen = member.guild.get_channel(player.locked_channel_id)
                    modo = "lock"
                elif player.loop_current and player.farm_channel_id:
                    canal_origen = member.guild.get_channel(player.farm_channel_id)
                    modo = "farm"

                if canal_origen and modo:
                    culprit = await self._identificar_culpable(member.guild, discord.AuditLogAction.member_disconnect, self.bot.user.id)
                    if culprit:
                        await self._quitar_permisos_canal(culprit, canal_origen)
                        await self._reportar_agresor(member.guild, canal_origen, culprit, f"Intentó desconectar al bot en modo {modo}")
                    await asyncio.sleep(0.5)
                    try:
                        player.voice_client = await canal_origen.connect()
                        if modo == "farm" and player.current:
                            track = player.current
                            track.stream_url = None
                            await player._start(track)
                    except:
                        pass
                return

        # ── Si un humano se une al canal protegido, cancelar idle timer ──
        if member.bot:
            return
        player = self.players.get(member.guild.id)
        if not player or not player.voice_client:
            return
        vc_channel = player.voice_client.channel
        if after.channel == vc_channel:
            player.cancel_idle_timer()
        elif before.channel == vc_channel and after.channel != vc_channel:
            if not any(not m.bot for m in vc_channel.members):
                player.start_idle_timer()

    # ════════════════════════════════════════════════════════════════════
    #  COMANDOS DEL CENTINELA (solo para el bot centinela)
    # ════════════════════════════════════════════════════════════════════

    @app_commands.command(name="centinela_estado", description="Muestra el estado del sistema centinela")
    async def cmd_estado_centinela(self, interaction: discord.Interaction):
        if not MODO_CENTINELA:
            await interaction.response.send_message("❌ Este bot no está configurado como centinela.", ephemeral=True)
            return

        guild = interaction.guild
        canal_voz = guild.get_channel(CANAL_VOZ_ID)
        bot_principal = guild.get_member(BOT_PRINCIPAL_ID) if BOT_PRINCIPAL_ID else None

        embed = discord.Embed(
            title="🛡️ Estado del Centinela",
            color=discord.Color.blurple()
        )
        embed.add_field(name="Canal protegido", value=canal_voz.name if canal_voz else "❌ No encontrado", inline=False)
        embed.add_field(name="Bot principal", value=f"{bot_principal.display_name} ✅" if bot_principal and bot_principal.voice else "❌ No está en el canal", inline=False)
        embed.add_field(name="Centinela en canal", value="✅ Sí" if self.voice_client and self.voice_client.is_connected() else "❌ No", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="centinela_restaurar", description="Restaura permisos a un usuario en el canal (solo owner)")
    @app_commands.describe(usuario="Usuario a restaurar")
    async def cmd_restaurar(self, interaction: discord.Interaction, usuario: discord.Member):
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message("❌ Solo el owner puede usar este comando.", ephemeral=True)
            return

        canal = interaction.guild.get_channel(CANAL_VOZ_ID)
        if not canal:
            await interaction.response.send_message("❌ Canal no encontrado.", ephemeral=True)
            return

        if await self._restaurar_permisos_canal(usuario, canal):
            await interaction.response.send_message(f"✅ Permisos restaurados para {usuario.mention}")
        else:
            await interaction.response.send_message(f"❌ No se pudieron restaurar los permisos para {usuario.mention}")

    @app_commands.command(name="centinela_proteger", description="Quita permisos a un usuario en el canal (solo owner)")
    @app_commands.describe(usuario="Usuario a bloquear")
    async def cmd_proteger(self, interaction: discord.Interaction, usuario: discord.Member):
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message("❌ Solo el owner puede usar este comando.", ephemeral=True)
            return

        canal = interaction.guild.get_channel(CANAL_VOZ_ID)
        if not canal:
            await interaction.response.send_message("❌ Canal no encontrado.", ephemeral=True)
            return

        if await self._quitar_permisos_canal(usuario, canal):
            await interaction.response.send_message(f"🔒 Permisos quitados a {usuario.mention} en el canal")
        else:
            await interaction.response.send_message(f"❌ No se pudieron quitar los permisos a {usuario.mention}")

    # ════════════════════════════════════════════════════════════════════
    #  COMANDOS ORIGINALES (sin cambios)
    # ════════════════════════════════════════════════════════════════════

    async def _find_recent_culprit(self, guild: discord.Guild, action, max_age_seconds: float = 15.0):
        try:
            async for entry in guild.audit_logs(limit=10, action=action):
                if entry.target is None or entry.target.id != self.bot.user.id:
                    continue
                age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                if age > max_age_seconds:
                    return None
                return entry.user
        except discord.Forbidden:
            return None
        return None

    async def _report_disconnect(self, guild: discord.Guild, mode: str, channel_id: int, culprit):
        try:
            owner = await self.bot.fetch_user(REPORT_OWNER_ID)
        except discord.HTTPException:
            return
        channel = guild.get_channel(channel_id)
        channel_name = channel.name if channel else "canal desconocido"
        mode_label = "🔒 lock" if mode == "lock" else "🌾 farm"
        if culprit is not None:
            who = f"**{culprit}** (`{culprit.id}`)"
        else:
            who = "No pude confirmar con certeza quién fue (sin permiso de ver audit log, o no hubo una entrada reciente que apuntara a mí)."
        embed = discord.Embed(
            title="⚠️ Me desconectaron/movieron estando protegido",
            description=(
                f"Servidor: **{guild.name}**\n"
                f"Canal: **{channel_name}**\n"
                f"Modo: {mode_label}\n"
                f"Responsable: {who}"
            ),
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        try:
            await owner.send(embed=embed)
        except discord.HTTPException:
            pass

    async def _enforce_protected_channel(self, guild: discord.Guild, before, after):
        player = self.players.get(guild.id)
        if not player:
            return

        if player.locked and before.channel is not None and before.channel.id == player.locked_channel_id:
            protected_channel_id = player.locked_channel_id
            mode = "lock"
        elif (
            player.loop_current
            and player.farm_channel_id
            and before.channel is not None
            and before.channel.id == player.farm_channel_id
        ):
            protected_channel_id = player.farm_channel_id
            mode = "farm"
        else:
            return

        if after.channel is not None and after.channel.id == protected_channel_id:
            return

        player.locked_recovering = True

        await asyncio.sleep(1.5)
        action = discord.AuditLogAction.member_disconnect if after.channel is None else discord.AuditLogAction.member_move
        culprit = await self._find_recent_culprit(guild, action)

        reason_text = (
            "Desconectó/movió al bot de música estando fijado (lock) a un canal."
            if mode == "lock"
            else "Desconectó/movió al bot de música estando en modo farm en un canal."
        )

        if culprit is not None:
            try:
                await guild.kick(culprit, reason=reason_text)
            except discord.Forbidden:
                pass

        await self._report_disconnect(guild, mode, protected_channel_id, culprit)

        channel = guild.get_channel(protected_channel_id)
        if channel is not None:
            player.voice_client = await channel.connect()
            player.locked_recovering = False
            if mode == "farm" and player.current is not None:
                track = player.current
                track.stream_url = None
                await player._start(track)
            elif player.current:
                player.queue.appendleft(player.current)
                player.current = None
                await player.play_next()
        else:
            player.locked_recovering = False

    @app_commands.command(name="lock", description="Fija o desfija el bot al canal de voz actual (dueños/asignado)")
    async def lock(self, interaction: discord.Interaction):
        if not self._has_special_access(interaction.user.id, interaction.guild):
            await interaction.response.send_message(
                "❌ Solo el dueño del servidor, el owner del bot, o la persona asignada pueden usar este comando.",
                ephemeral=True,
            )
            return

        player = self.get_player(interaction.guild.id)
        if player.locked:
            player.locked = False
            player.locked_channel_id = None
            await interaction.response.send_message("🔓 Modo fijo desactivado.")
            return

        if player.voice_client is None:
            await interaction.response.send_message("❌ El bot debe estar conectado a un canal de voz para fijarlo.")
            return

        player.locked = True
        player.locked_channel_id = player.voice_client.channel.id
        await interaction.response.send_message(
            f"🔒 Bot fijado al canal **{player.voice_client.channel.name}**. "
            "Si alguien lo desconecta o lo mueve de ahí, será expulsado del servidor."
        )

    @app_commands.command(name="assign", description="(Solo owner del bot) Da o quita acceso a lock/farm a una persona")
    @app_commands.describe(user="Persona a asignar. Déjalo vacío para quitar la asignación actual.")
    async def assign(self, interaction: discord.Interaction, user: discord.Member = None):
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message("❌ Solo el owner del bot puede usar este comando.", ephemeral=True)
            return

        self.assigned_user_id = user.id if user else None
        if user:
            await interaction.response.send_message(
                f"✅ {user.mention} ahora tiene acceso a `/lock` y `/farm` en este bot. "
                "(Esto se pierde si el bot se reinicia; hay que volver a asignarlo.)"
            )
        else:
            await interaction.response.send_message("✅ Se quitó la asignación actual.")

    def get_player(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self.players:
            self.players[guild_id] = GuildPlayer(self.bot)
        return self.players[guild_id]

    def _prefixes_display(self) -> str:
        prefix = self.bot.command_prefix
        if isinstance(prefix, (list, tuple)):
            return ", ".join(prefix)
        return str(prefix)

    def _status_line(self, player: "GuildPlayer") -> str:
        if player.voice_client is None:
            channel_desc = "no estoy en ningún canal de voz"
        else:
            channel_desc = f"estoy en **{player.voice_client.channel.name}**"
            if player.locked:
                channel_desc += " (🔒 lock)"
            elif player.loop_current:
                channel_desc += " (🌾 farm)"
        return f"{channel_desc}. Mis prefijos son: `{self._prefixes_display()}`."

    @commands.command(name="farm", aliases=["join"])
    async def swarm_farm(self, ctx: commands.Context, *, query: str = None):
        mentioned_bots = [m for m in ctx.message.mentions if m.bot]
        if mentioned_bots:
            if ctx.guild.me not in mentioned_bots:
                return
            for m in ctx.message.mentions:
                query = (query or "").replace(m.mention, "").replace(f"<@!{m.id}>", "").replace(f"<@{m.id}>", "")
            query = query.strip() or None

            player = self.get_player(ctx.guild.id)
            await ctx.send(f"👋 {ctx.guild.me.mention} — {self._status_line(player)}")

        if query is None:
            query = FARM_DEFAULT_URL
            if not query:
                await ctx.send("❌ No pusiste un link y no hay `FARM_DEFAULT_URL` configurado en las variables de entorno.")
                return

        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("❌ Debes estar en un canal de voz para usar este comando.")
            return

        player = self.get_player(ctx.guild.id)
        if not await ensure_voice_connection(ctx.author, ctx.send, player):
            return

        async with ctx.typing():
            try:
                track = await asyncio.to_thread(extract_track, query)
            except yt_dlp.utils.DownloadError:
                await ctx.send("❌ No pude encontrar o reproducir eso. Revisa el link o el nombre.")
                return

        track.requester = ctx.author
        player.loop_current = True
        player.farm_channel_id = player.voice_client.channel.id
        player.next_override = track
        if player.voice_client.is_playing() or player.voice_client.is_paused():
            player.voice_client.stop()
        else:
            await player._start(track)

        await ctx.send(
            f"🌾 Modo farm activado con **{track.title}**. Se repetirá en bucle indefinidamente; "
            "para detenerlo, presiona Detener en el mensaje de reproducción."
        )

    @commands.command(name="sts")
    async def status(self, ctx: commands.Context):
        mentioned_bots = [m for m in ctx.message.mentions if m.bot]
        if mentioned_bots and ctx.guild.me not in mentioned_bots:
            return

        player = self.get_player(ctx.guild.id)
        railway_service = os.environ.get("RAILWAY_SERVICE_NAME") or "desconocido"
        repo_owner = os.environ.get("RAILWAY_GIT_REPO_OWNER")
        repo_name = os.environ.get("RAILWAY_GIT_REPO_NAME")
        if repo_name:
            github_repo = f"{repo_owner}/{repo_name}" if repo_owner else repo_name
        else:
            github_repo = "desconocido"
        embed = discord.Embed(title=f"🤖 Estado de {ctx.guild.me.display_name}", color=discord.Color.blurple())
        embed.add_field(name="Servicio en Railway", value=railway_service, inline=False)
        embed.add_field(name="Repositorio de GitHub", value=github_repo, inline=False)
        embed.add_field(name="Canal / modo", value=self._status_line(player), inline=False)

        # Si es centinela, mostrar info extra
        if MODO_CENTINELA:
            embed.add_field(name="🛡️ Modo", value="Centinela activo", inline=False)
        await ctx.send(embed=embed)

    @app_commands.command(name="play", description="Reproduce un link o busca una canción por nombre/artista")
    @app_commands.describe(query="Link de YouTube/SoundCloud, link de playlist, o nombre de la canción")
    async def play(self, interaction: discord.Interaction, query: str):
        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.response.send_message("❌ Debes estar en un canal de voz para usar este comando.", ephemeral=True)
            return

        await interaction.response.defer()

        async def send(content=None, **kwargs):
            return await interaction.followup.send(content, **kwargs)

        player = self.get_player(interaction.guild.id)
        if not await ensure_voice_connection(interaction.user, send, player):
            return

        is_playlist = "list=" in query

        try:
            if is_playlist:
                tracks = await asyncio.to_thread(extract_playlist, query)
                if not tracks:
                    await send("❌ No pude leer esa playlist.")
                    return
                for track in tracks:
                    track.requester = interaction.user
                player.queue.extend(tracks)
                await send(f"✅ Se agregaron **{len(tracks)}** canciones de la playlist a la cola.")
            else:
                track = await asyncio.to_thread(extract_track, query)
                track.requester = interaction.user
                player.queue.append(track)
                await send(f"✅ Agregado a la cola: **{track.title}**")
        except yt_dlp.utils.DownloadError:
            await send("❌ No pude encontrar o reproducir eso. Revisa el link o el nombre.")
            return

        if player.current is None:
            await player.play_next()

    @app_commands.command(name="join", description="Conecta el bot a tu canal de voz (sin reproducir nada)")
    async def join(self, interaction: discord.Interaction):
        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.response.send_message("❌ Debes estar en un canal de voz para usar este comando.", ephemeral=True)
            return

        await interaction.response.defer()

        async def send(content=None, **kwargs):
            return await interaction.followup.send(content, **kwargs)

        player = self.get_player(interaction.guild.id)
        if await ensure_voice_connection(interaction.user, send, player):
            await send(f"✅ Me uní a **{interaction.user.voice.channel.name}**.")

    @app_commands.command(name="farm", description="Reproduce en bucle infinito para quedarte en el canal (dueños/asignado)")
    @app_commands.describe(query="Link o nombre. Si no pones nada, usa FARM_DEFAULT_URL")
    async def farm_slash(self, interaction: discord.Interaction, query: str = None):
        if not self._has_special_access(interaction.user.id, interaction.guild):
            await interaction.response.send_message(
                "❌ Solo el dueño del servidor, el owner del bot, o la persona asignada pueden usar este comando.",
                ephemeral=True,
            )
            return

        if query is None:
            query = FARM_DEFAULT_URL
            if not query:
                await interaction.response.send_message(
                    "❌ No pusiste un link y no hay `FARM_DEFAULT_URL` configurado en las variables de entorno.",
                    ephemeral=True,
                )
                return

        if interaction.user.voice is None or interaction.user.voice.channel is None:
            await interaction.response.send_message("❌ Debes estar en un canal de voz para usar este comando.", ephemeral=True)
            return

        await interaction.response.defer()

        async def send(content=None, **kwargs):
            return await interaction.followup.send(content, **kwargs)

        player = self.get_player(interaction.guild.id)
        if not await ensure_voice_connection(interaction.user, send, player):
            return

        try:
            track = await asyncio.to_thread(extract_track, query)
        except yt_dlp.utils.DownloadError:
            await send("❌ No pude encontrar o reproducir eso. Revisa el link o el nombre.")
            return

        track.requester = interaction.user
        player.loop_current = True
        player.farm_channel_id = player.voice_client.channel.id
        player.next_override = track
        if player.voice_client.is_playing() or player.voice_client.is_paused():
            player.voice_client.stop()
        else:
            await player._start(track)

        await send(
            f"🌾 Modo farm activado con **{track.title}**. Se repetirá en bucle indefinidamente; "
            "para detenerlo, presiona Detener en el mensaje de reproducción."
        )

    @app_commands.command(name="queue", description="Muestra qué está sonando y la cola")
    async def queue_(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild.id)
        if player.current is None and not player.queue:
            await interaction.response.send_message("La cola está vacía.")
            return
        lines = []
        if player.current:
            lines.append(f"🎵 Sonando: **{player.current.title}**")
        for i, track in enumerate(list(player.queue)[:10], start=1):
            lines.append(f"{i}. {track.title}")
        if len(player.queue) > 10:
            lines.append(f"... y {len(player.queue) - 10} más")
        embed = discord.Embed(title="Cola de reproducción", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="help", description="Muestra todos los comandos del bot")
    async def help_(self, interaction: discord.Interaction):
        embed = discord.Embed(title="🎵 Comandos del bot de música", color=discord.Color.blurple())
        embed.add_field(
            name="/play <link o nombre>",
            value=(
                "Todos pueden usarlo. Reproduce un link de YouTube/SoundCloud, un link de playlist "
                "(hasta 200 canciones), o busca por nombre/artista (prueba varios resultados si el "
                "primero falla).\n"
                "Ej: `/play rauw alejandro todo de ti`\n"
                "Ej: `/play https://youtube.com/playlist?list=...`"
            ),
            inline=False,
        )
        embed.add_field(
            name="/queue",
            value="Todos pueden usarlo. Muestra qué está sonando y las próximas canciones en cola.",
            inline=False,
        )
        embed.add_field(
            name="/join",
            value="Todos pueden usarlo. Solo conecta el bot a tu canal de voz, sin reproducir nada.",
            inline=False,
        )
        embed.add_field(
            name="/farm [link o nombre]",
            value=(
                "Solo dueño del servidor, owner del bot, o persona asignada con `/assign`. "
                "Reproduce en bucle infinito para quedarte fijo en el canal acumulando horas. "
                "Si no pones link, usa `FARM_DEFAULT_URL`. Se detiene con el botón Detener."
            ),
            inline=False,
        )
        embed.add_field(
            name="/lock",
            value=(
                "Solo dueño del servidor, owner del bot, o persona asignada. Fija/desfija el bot al canal "
                "actual. Con el modo activo (o en farm), si alguien lo desconecta o mueve, es expulsado "
                "del servidor (si el audit log confirma quién fue), el bot se reconecta solo, y se manda "
                "un reporte por DM al owner del bot."
            ),
            inline=False,
        )
        embed.add_field(
            name="/assign [@persona]",
            value=(
                "Solo el owner del bot. Le da a esa persona acceso a `/lock` y `/farm` igual que un dueño. "
                "Sin persona, quita la asignación actual. Se pierde si el bot se reinicia."
            ),
            inline=False,
        )
        embed.add_field(
            name="#join [link o nombre] / #join @bot1 @bot2 (prefijo de TEXTO, no slash)",
            value=(
                "Para que varios bots respondan y se unan a la vez. Si mencionas bots específicos, "
                "solo esos responden y cada uno anuncia su estado antes de unirse."
            ),
            inline=False,
        )
        embed.add_field(
            name="#sts [@bot1 @bot2] (prefijo de TEXTO, no slash)",
            value=(
                "Cada bot dice a qué servicio de Railway y a qué repositorio de GitHub está conectado, "
                "en qué canal está y en qué modo (lock/farm)."
            ),
            inline=False,
        )
        embed.add_field(
            name="Al usar /play en otro canal",
            value=(
                "Si ya estoy tocando música en otro canal (sin lock ni farm), te pregunto con botones "
                "si quieres que me mueva. Si estoy en lock o farm en otro canal, no me muevo y aviso "
                "que hay que pedirle al owner que lo quite ahí primero."
            ),
            inline=False,
        )
        embed.add_field(
            name="Botones en el mensaje de reproducción",
            value=(
                "Anterior — vuelve a la canción pasada\n"
                "Pausar / Reanudar\n"
                "Saltar — pasa a la próxima en cola\n"
                "Detener — para todo y el bot sale del canal de voz\n"
                "Cualquiera en el mismo canal de voz que el bot puede usarlos."
            ),
            inline=False,
        )
        # Comandos del centinela
        if MODO_CENTINELA:
            embed.add_field(
                name="🛡️ Comandos del Centinela",
                value=(
                    "/centinela_estado — Muestra el estado del sistema centinela\n"
                    "/centinela_proteger @usuario — Quita permisos en el canal (solo owner)\n"
                    "/centinela_restaurar @usuario — Restaura permisos en el canal (solo owner)"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))