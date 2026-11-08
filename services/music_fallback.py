"""Plano B da música: quando o vídeo do YouTube não toca nem pelo áudio (o dono bloqueia fora do site e o endereço do
áudio responde 403), a Kira procura a mesma música em outra versão do YouTube (áudio oficial, letra, canal "Topic") e,
se não achar, no SoundCloud.

Só aceita o que bate com a música pedida: palavras do nome, o artista e a duração (até 10 s ou 6% de diferença), e
recusa cover, karaokê, remix e afins que o original não tinha (D98 tirou o SoundCloud porque tocava outra música).
O SoundCloud entrega HLS/AAC, que o Chromium do Qt não toca: o backend converte com o ffmpeg para Opus/WebM numa rota
local, por um código que só ele conhece (nunca um endereço qualquer vindo de fora). Uma conversão por vez.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import shutil
import subprocess
import threading
import time
import unicodedata
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger("kira.app.music_fallback")  # filho de kira.app: vai para logs/Backend

YOUTUBE_CANDIDATES = 8
SOUNDCLOUD_CANDIDATES = 5
MAX_YOUTUBE_TRIES = 2
FOUND_TTL_S = 2 * 3600.0  # a mesma música de novo toca na hora
STREAM_TTL_S = 3 * 3600.0
MAX_STREAMS = 20
SEARCH_TIMEOUT_S = 40.0
NOISE = {"oficial", "official", "video", "videoclipe", "clipe", "clip", "audio", "letra", "lyric", "lyrics", "hd", "4k",
         "feat", "ft", "part", "participacao", "com", "the", "de", "da", "do", "das", "dos", "e", "a", "o", "em", "no", "na",
         "music", "musica", "vevo", "topic", "records", "tv", "canal", "channel", "remastered", "remaster", "versao", "version"}
# Versões que não são a música pedida, a não ser que o pedido já tenha a palavra.
OTHER_VERSION = {"cover", "karaoke", "instrumental", "remix", "acustico", "acoustic", "slowed", "reverb", "sped", "8d",
                 "nightcore", "mashup", "medley", "reaction", "react", "tutorial", "aula", "cifra", "piano", "playback"}
# Ordem de preferência das outras versões: áudio oficial do YouTube Music ("- Topic"), "áudio", letra; depois o resto.
PREFERRED = ("topic", "audio", "letra", "lyric")


def preference(candidate: dict[str, Any]) -> int:
    text = fold(f"{candidate.get('title', '')} {candidate.get('uploader', '')}")
    return next((rank for rank, mark in enumerate(PREFERRED) if mark in text), len(PREFERRED))


class MusicFallbackError(Exception):
    pass


def fold(text: object) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(ch for ch in raw if not unicodedata.combining(ch))


def words(text: object) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(text))


def tokens(text: object) -> set[str]:
    return {word for word in words(text) if len(word) >= 2 and word not in NOISE}


class Wanted:
    """A música pedida, a partir do título e do canal do YouTube ("Gustavo Mioto - Contramão", "Gustavo Mioto")."""

    def __init__(self, title: str, channel: str = "", duration: float = 0.0) -> None:
        self.extras = set(words(title)) & OTHER_VERSION  # o pedido já era remix, ao vivo...
        title = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", str(title or ""))  # "(Clipe Oficial)", "[4K]"
        left, sep, right = title.partition(" - ")
        self.artist = tokens(left) if sep else set()
        self.artist |= {word for word in tokens(re.sub(r"(?i)(vevo|\s*-\s*topic|oficial|official)$", "", str(channel or "").strip()))}
        song = tokens(right if sep else title) - self.artist
        self.song = song or tokens(title)
        self.duration = float(duration or 0.0)
        self.query = " ".join(dict.fromkeys(words(left if sep else channel) + words(right if sep else title)))[:120] or fold(title)

    def matches(self, candidate: dict[str, Any]) -> bool:
        cand_words = set(words(f"{candidate.get('title', '')} {candidate.get('uploader', '')}"))
        if not self.song or not self.song <= cand_words:
            return False
        if self.artist and not (self.artist & cand_words):
            return False
        if (cand_words & OTHER_VERSION) - self.extras:
            return False
        other = float(candidate.get("duration") or 0.0)
        if self.duration > 0 and other > 0 and abs(self.duration - other) > max(10.0, self.duration * 0.06):
            return False
        return True


def find_ffmpeg(base_dir: Path) -> str:
    local = Path(base_dir) / "bin" / "ffmpeg" / "ffmpeg.exe"
    if local.is_file():
        return str(local)
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg") or ""


class MusicFallback:
    def __init__(self, *, youtube_audio: Any, base_dir: Path, search: Callable[[str, bool], list[dict]] | None = None,
                 ffmpeg: str | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._yt = youtube_audio
        self._search = search or self._search_with_yt_dlp
        self._ffmpeg = ffmpeg if ffmpeg is not None else find_ffmpeg(base_dir)
        self._clock = clock
        self._streams: dict[str, dict[str, Any]] = {}
        self._process: subprocess.Popen | None = None
        self._process_lock = threading.Lock()
        self._ydl: dict[bool, Any] = {}
        self._ydl_lock: dict[bool, threading.Lock] = {False: threading.Lock(), True: threading.Lock()}
        self._found: dict[str, tuple[float, dict[str, Any]]] = {}

    # ---------- busca ----------
    def _search_with_yt_dlp(self, query: str, full: bool) -> list[dict]:
        """Roda em thread. ytsearch: lista rápida (sem endereço); scsearch: completa (com o endereço do áudio)."""
        with self._ydl_lock[full]:  # um YoutubeDL por tipo: a busca no YouTube e no SoundCloud correm juntas
            ydl = self._ydl.get(full)
            if ydl is None:
                import yt_dlp  # pesado: só quando o plano B for usado

                options: dict[str, Any] = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
                options.update({"format": "bestaudio/best"} if full else {"extract_flat": "in_playlist"})
                ydl = self._ydl[full] = yt_dlp.YoutubeDL(options)
            info = ydl.extract_info(query, download=False) or {}
        found = []
        for entry in info.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            found.append({"id": str(entry.get("id") or ""), "title": str(entry.get("title") or ""),
                          "uploader": str(entry.get("uploader") or entry.get("channel") or ""),
                          "duration": float(entry.get("duration") or 0.0), "url": str(entry.get("url") or ""),
                          "headers": dict(entry.get("http_headers") or {})})
        return found

    async def _candidates(self, query: str, full: bool) -> list[dict]:
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._search, query, full), timeout=SEARCH_TIMEOUT_S)
        except Exception as exc:
            logger.warning("MUSIC_FALLBACK_SEARCH_FAILED source=%s reason=%s", query.split(":", 1)[0], type(exc).__name__)
            return []

    async def _youtube_alternative(self, wanted: Wanted, video_id: str) -> dict[str, Any] | None:
        from services.youtube_audio import YoutubeAudioError

        candidates = [c for c in await self._candidates(f"ytsearch{YOUTUBE_CANDIDATES}:{wanted.query}", False)
                      if c["id"] and c["id"] != video_id and wanted.matches(c)]
        candidates.sort(key=preference)
        for candidate in candidates[:MAX_YOUTUBE_TRIES]:
            try:
                info = await self._yt.resolve(candidate["id"])
            except YoutubeAudioError:
                continue
            return {"source": "youtube_alt", "videoId": candidate["id"], "title": candidate["title"],
                    "channel": candidate["uploader"], "streamUrl": info["url"],
                    "duration": float(info.get("duration") or candidate["duration"] or 0.0)}
        return None

    async def _soundcloud(self, wanted: Wanted) -> dict[str, Any] | None:
        for candidate in await self._candidates(f"scsearch{SOUNDCLOUD_CANDIDATES}:{wanted.query}", True):
            if candidate["url"].startswith("https://") and wanted.matches(candidate):
                token = self._register(candidate)
                return {"source": "soundcloud", "title": candidate["title"], "channel": candidate["uploader"],
                        "streamUrl": f"/api/music/stream/{token}", "duration": candidate["duration"]}
        return None

    async def find(self, *, video_id: str, title: str, channel: str = "", duration: float = 0.0) -> dict[str, Any]:
        """Outra versão do YouTube que toque ou a mesma música no SoundCloud. Erro se nada bater.

        A busca no SoundCloud começa junto com as tentativas no YouTube: se o YouTube não der, ela já está pronta."""
        cached = self._found.get(video_id)
        if cached and cached[0] > self._clock() and (cached[1]["source"] != "soundcloud" or self.has_stream(cached[1]["streamUrl"].rsplit("/", 1)[1])):
            return dict(cached[1])
        wanted = Wanted(title, channel, duration)
        started = self._clock()
        soundcloud = asyncio.ensure_future(self._soundcloud(wanted)) if self._ffmpeg else None
        try:
            found = await self._youtube_alternative(wanted, video_id)
            if found is None and soundcloud is not None:
                found = await soundcloud
        finally:
            if soundcloud is not None and not soundcloud.done():
                soundcloud.cancel()
        if found is None:
            logger.info("MUSIC_FALLBACK_NOT_FOUND seconds=%.1f", self._clock() - started)
            raise MusicFallbackError("Não achei essa música em outra fonte.")
        logger.info("MUSIC_FALLBACK_FOUND source=%s seconds=%.1f", found["source"], self._clock() - started)
        if video_id:
            self._found[video_id] = (self._clock() + FOUND_TTL_S, dict(found))
        return found

    # ---------- conversão ----------
    def _register(self, candidate: dict) -> str:
        now = self._clock()
        for token in [t for t, item in self._streams.items() if item["expires"] <= now][:MAX_STREAMS]:
            self._streams.pop(token, None)
        while len(self._streams) >= MAX_STREAMS:
            self._streams.pop(next(iter(self._streams)))
        token = secrets.token_urlsafe(16)
        self._streams[token] = {"url": candidate["url"], "headers": candidate.get("headers") or {}, "expires": now + STREAM_TTL_S}
        return token

    def has_stream(self, token: str) -> bool:
        item = self._streams.get(str(token or ""))
        return bool(item and item["expires"] > self._clock())

    def command(self, token: str, start: float = 0.0) -> list[str]:
        item = self._streams[token]
        args = [self._ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
        headers = "".join(f"{key}: {value}\r\n" for key, value in item["headers"].items() if "\n" not in f"{key}{value}")
        if headers:
            args += ["-headers", headers]
        if start > 0:
            args += ["-ss", f"{min(float(start), 6 * 3600):.1f}"]
        return args + ["-i", item["url"], "-vn", "-c:a", "libopus", "-b:a", "128k", "-f", "webm", "pipe:1"]

    def stream(self, token: str, start: float = 0.0) -> Iterator[bytes]:
        """Áudio em Opus/WebM, em pedaços. Começar outra conversão encerra a anterior (uma música toca por vez)."""
        args = self.command(token, start)
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with self._process_lock:
            previous, self._process = self._process, process
        if previous is not None and previous.poll() is None:
            previous.kill()
        try:
            while True:
                chunk = process.stdout.read(32768) if process.stdout else b""
                if not chunk:
                    break
                yield chunk
        finally:
            if process.poll() is None:
                process.kill()
            with self._process_lock:
                if self._process is process:
                    self._process = None

    def stop(self) -> None:
        with self._process_lock:
            process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.kill()


__all__ = ["MusicFallback", "MusicFallbackError", "Wanted", "find_ffmpeg"]
