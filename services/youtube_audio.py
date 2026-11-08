"""Áudio do YouTube para vídeos que o dono não deixa tocar fora do site (erro 150 no player embutido).

Nada vai para o disco: o yt-dlp (como biblioteca, carregado uma vez) só descobre o endereço do áudio no próprio YouTube e
a aba Música toca esse endereço num <audio>. Custa de 4 a 7 s na primeira vez; o endereço vale horas e fica guardado,
então repetir a música é imediato. Roda em thread: nunca segura o servidor (o plano B antigo baixava o MP3 dentro da
requisição e travava tudo por ~20 s).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger("kira.app.youtube_audio")  # filho de kira.app: vai para logs/Backend

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Opus em WebM: o Chromium do Qt toca sem codecs pagos (AAC/M4A pode não tocar nele).
AUDIO_FORMAT = "bestaudio[acodec=opus]/bestaudio[ext=webm]/bestaudio"
RESOLVE_TIMEOUT_S = 40.0
MAX_TTL_S = 5 * 3600.0
SAFETY_MARGIN_S = 15 * 60.0
MAX_CACHED = 200
MAX_PREFETCH = 2
# O endereço que o yt-dlp devolve pode ser proibido (403) para vídeo bloqueado fora do site: testa antes de entregar
# e lembra por um tempo, para o plano B (outra versão, SoundCloud) entrar na hora.
BLOCKED_TTL_S = 30 * 60.0
PROBE_TIMEOUT_S = 6.0
PROBE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
BLOCKED_MESSAGE = "Esse vídeo não toca fora do YouTube."


LOGIN_MESSAGE = r"O YouTube pediu login. Salve os cookies do YouTube em configs\secure\ytdlp_cookies.txt e tente de novo."


class YoutubeAudioError(Exception):
    def __init__(self, message: str, *, blocked: bool = False) -> None:
        super().__init__(message)
        self.blocked = blocked


def probe_stream(url: str) -> bool:
    """O endereço entrega áudio? Só 401/403/404/410 contam como proibido; falha de rede não condena o endereço."""
    request = urllib.request.Request(url, headers={"Range": "bytes=0-1", "User-Agent": PROBE_UA})
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_S) as response:
            return response.status in (200, 206)
    except urllib.error.HTTPError as exc:
        return exc.code not in (401, 403, 404, 410)
    except OSError:
        return True


def valid_video_id(value: object) -> str:
    video_id = str(value or "").strip()
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise YoutubeAudioError("Música sem um id válido do YouTube.")
    return video_id


def stream_ttl(url: str, now: float, wall_now: float) -> float:
    """Quanto tempo o endereço ainda vale: o parâmetro 'expire' do YouTube menos uma folga (no máximo 5 h)."""
    try:
        expire = float((parse_qs(urlsplit(url).query).get("expire") or ["0"])[0])
    except ValueError:
        expire = 0.0
    remaining = expire - wall_now - SAFETY_MARGIN_S if expire else MAX_TTL_S
    return now + max(60.0, min(MAX_TTL_S, remaining))


def is_youtube_media_url(url: str) -> bool:
    parts = urlsplit(str(url or ""))
    return parts.scheme == "https" and (parts.hostname or "").endswith(".googlevideo.com")


class YoutubeAudio:
    def __init__(self, base_dir: Path, *, extract: Callable[[str], dict[str, Any]] | None = None,
                 probe: Callable[[str], bool] | None = None,
                 clock: Callable[[], float] = time.monotonic, wall_clock: Callable[[], float] = time.time) -> None:
        self._base_dir = Path(base_dir)
        self._extract = extract or self._extract_with_yt_dlp
        self._probe = probe or probe_stream
        self._blocked: dict[str, float] = {}
        self._clock = clock
        self._wall_clock = wall_clock
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._ydl: Any = None
        self._ydl_lock = threading.Lock()  # um YoutubeDL não aceita duas buscas ao mesmo tempo

    def _cookies(self) -> str | None:
        # O mesmo arquivo do download de vídeo: com ele o YouTube não pede "não sou robô".
        path = Path(os.getenv("KIRA_YTDLP_COOKIES") or (self._base_dir / "configs" / "secure" / "ytdlp_cookies.txt"))
        return str(path) if path.is_file() else None

    def _extract_with_yt_dlp(self, video_id: str) -> dict[str, Any]:
        """Roda em thread. O YoutubeDL fica aberto entre as músicas (abrir o programa a cada vez custava ~3 s)."""
        with self._ydl_lock:
            if self._ydl is None:
                import yt_dlp  # pesado: só quando a primeira música precisar

                options: dict[str, Any] = {"format": AUDIO_FORMAT, "noplaylist": True, "quiet": True, "no_warnings": True,
                                           "skip_download": True, "js_runtimes": {"node": {}}}
                cookies = self._cookies()
                if cookies:
                    options["cookiefile"] = cookies
                self._ydl = yt_dlp.YoutubeDL(options)
            info = self._ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
        return {"url": str(info.get("url") or ""), "duration": float(info.get("duration") or 0), "title": str(info.get("title") or "")}

    async def resolve(self, video_id: object, *, force: bool = False) -> dict[str, Any]:
        """{'url', 'duration', 'title'} do áudio. Pedidos iguais ao mesmo tempo esperam a mesma busca."""
        vid = valid_video_id(video_id)
        now = self._clock()
        cached = self._cache.get(vid)
        if cached and not force and cached[0] > now:
            return dict(cached[1])
        if not force and self._blocked.get(vid, 0.0) > now:
            raise YoutubeAudioError(BLOCKED_MESSAGE, blocked=True)
        pending = self._pending.get(vid)
        if pending is not None:
            return dict(await asyncio.shield(pending))
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[vid] = future
        started = self._clock()
        try:
            info = await asyncio.wait_for(asyncio.to_thread(self._extract, vid), timeout=RESOLVE_TIMEOUT_S)
            if not is_youtube_media_url(info.get("url", "")):
                raise YoutubeAudioError("O YouTube não devolveu o áudio desta música.")
            if not await asyncio.to_thread(self._probe, info["url"]):
                self._blocked[vid] = self._clock() + BLOCKED_TTL_S
                raise YoutubeAudioError(BLOCKED_MESSAGE, blocked=True)
            self._cache[vid] = (stream_ttl(info["url"], self._clock(), self._wall_clock()), info)
            while len(self._cache) > MAX_CACHED:
                self._cache.pop(next(iter(self._cache)))
            logger.info("YOUTUBE_AUDIO_RESOLVED video=%s seconds=%.1f", vid, self._clock() - started)
            future.set_result(info)
            return dict(info)
        except Exception as exc:
            reason = "timeout" if isinstance(exc, asyncio.TimeoutError) else type(exc).__name__
            if getattr(exc, "blocked", False):
                reason = "blocked_403"
            logger.warning("YOUTUBE_AUDIO_FAILED video=%s reason=%s", vid, reason)
            if isinstance(exc, YoutubeAudioError):
                error = exc
            elif re.search(r"sign in to confirm|not a bot", str(exc), re.IGNORECASE):
                error = YoutubeAudioError(LOGIN_MESSAGE)
            else:
                error = YoutubeAudioError("Não consegui o áudio do YouTube agora.")
            future.set_exception(error)
            future.exception()  # quem não esperava (pré-busca) não deixa aviso de exceção perdida
            raise error from None
        finally:
            self._pending.pop(vid, None)

    def prefetch(self, video_ids: list[object]) -> int:
        """Prepara em segundo plano o áudio dos próximos prováveis (no máximo 2 por vez)."""
        started = 0
        for raw in video_ids[:MAX_PREFETCH]:
            try:
                vid = valid_video_id(raw)
            except YoutubeAudioError:
                continue
            cached = self._cache.get(vid)
            if (cached and cached[0] > self._clock()) or vid in self._pending:
                continue
            task = asyncio.ensure_future(self.resolve(vid))
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            started += 1
        return started


__all__ = ["YoutubeAudio", "YoutubeAudioError", "is_youtube_media_url", "probe_stream", "stream_ttl", "valid_video_id"]
