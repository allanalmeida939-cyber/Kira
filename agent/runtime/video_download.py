"""Baixar vídeo ou áudio (YouTube, Instagram, TikTok, X e +1.800 sites) com o yt-dlp — o mesmo motor do yoinks.

O link vem do pedido, da área de transferência ou da barra de endereço do navegador aberto. O download roda numa
thread; quem chama recebe um aviso no fim (para falar e, se pedido, mandar pelo WhatsApp).
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
# Endereço como o Chrome mostra (sem "https://"): "youtube.com/watch?v=...".
BARE_URL_RE = re.compile(r"^(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+/\S*$", re.IGNORECASE)
BROWSERS = {"chrome.exe", "msedge.exe", "brave.exe", "firefox.exe", "opera.exe", "opera_gx.exe", "vivaldi.exe"}
DOWNLOAD_DIR = Path(os.getenv("KIRA_DOWNLOAD_DIR") or (Path.home() / "Downloads" / "Kira"))
# O YouTube pede "confirme que não é um robô" a parte dos IPs; o yt-dlp não lê os cookies do Chrome/Edge no Windows
# (criptografia nova do navegador). Saída: cookies exportados (extensão "Get cookies.txt LOCALLY") neste arquivo.
COOKIES_FILE = Path(os.getenv("KIRA_YTDLP_COOKIES") or (Path(__file__).resolve().parents[2] / "configs" / "secure" / "ytdlp_cookies.txt"))
# H.264 + AAC primeiro: é o que o WhatsApp e qualquer player do Windows tocam (VP9/AV1 do YouTube muitas vezes não).
FORMAT_SORT = ["vcodec:h264", "acodec:m4a", "ext:mp4:m4a"]
# Teto de resolução por ordenação ("res:480" = a maior até 480p), não por filtro: um .mp4 direto sem altura
# conhecida falhava com "Requested format is not available".
QUALITIES = {"melhor": None, "1080": 1080, "720": 720, "480": 480, "360": 360, "audio": None}


def find_url(text: object) -> str | None:
    match = URL_RE.search(str(text or ""))
    if match:
        return match.group(0).rstrip(").,;]")
    bare = str(text or "").strip()
    return f"https://{bare}" if BARE_URL_RE.match(bare) else None


def quality_key(value: object) -> str | None:
    """'720p', 'hd', 'só o áudio', 'mp3', 'a melhor' -> chave de QUALITIES; None se não reconheceu."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    if any(word in text for word in ("audio", "áudio", "mp3", "musica", "música", "som")):
        return "audio"
    for height in ("1080", "720", "480", "360"):
        if height in text:
            return height
    if "full" in text:
        return "1080"
    if text in {"hd"}:
        return "720"
    if any(word in text for word in ("melhor", "maxima", "máxima", "max", "best", "alta")):
        return "melhor"
    return None


def ydl_options(quality: str, folder: Path, ffmpeg: str | None, progress: Callable[[dict], None]) -> dict:
    options: dict = {
        "format": "ba/b" if quality == "audio" else "bv*+ba/b",
        "format_sort": [f"res:{QUALITIES[quality]}" if QUALITIES[quality] else "res", *FORMAT_SORT],
        "outtmpl": str(folder / "%(title).80B [%(id)s].%(ext)s"),
        "noplaylist": True,
        # o YouTube exige resolver um desafio em JS (pacote yt-dlp-ejs); o Node já vem com a Kira (bridge do WhatsApp)
        "js_runtimes": {"node": {}},
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "restrictfilenames": False,
        "windowsfilenames": True,
        "progress_hooks": [progress],
        "merge_output_format": "mp4",
        "logger": _QuietLog(),
    }
    if COOKIES_FILE.is_file():
        options["cookiefile"] = str(COOKIES_FILE)
    if ffmpeg:
        options["ffmpeg_location"] = ffmpeg
    if quality == "audio":
        options.pop("merge_output_format")
        options["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    return options


class _QuietLog:
    """O yt-dlp escreve erros direto no terminal; aqui vão para o log da Kira, uma linha."""

    def debug(self, message: str) -> None:
        return None

    info = warning = debug

    def error(self, message: str) -> None:
        logger.info("YTDLP_ERROR %s", str(message)[:160])


def _ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


@dataclass
class Download:
    url: str
    quality: str
    title: str = ""
    percent: float = 0.0
    state: str = "baixando"  # baixando | pronto | erro
    file: Path | None = None
    error: str = ""
    started_at: float = field(default_factory=time.monotonic)

    def summary(self) -> str:
        name = self.title or self.url
        if self.state == "pronto":
            return f"'{name}' pronto em {self.file}."
        if self.state == "erro":
            return f"'{name}' falhou: {self.error}"
        return f"'{name}' em {self.percent:.0f}%."


class Downloader:
    def __init__(self, folder: Path = DOWNLOAD_DIR, *, ydl_factory=None) -> None:
        self.folder = folder
        self._ydl_factory = ydl_factory
        self._lock = threading.Lock()
        self.items: list[Download] = []

    def _factory(self):
        if self._ydl_factory is not None:
            return self._ydl_factory
        from yt_dlp import YoutubeDL

        return YoutubeDL

    def start(self, url: str, quality: str, on_done: Callable[[Download], None]) -> Download:
        item = Download(url=url, quality=quality)
        with self._lock:
            self.items = [d for d in self.items if d.state == "baixando"][-4:] + [item]
        threading.Thread(target=self._run, args=(item, on_done), name="kira-download", daemon=True).start()
        return item

    def _run(self, item: Download, on_done: Callable[[Download], None]) -> None:
        def progress(event: dict) -> None:
            total = event.get("total_bytes") or event.get("total_bytes_estimate") or 0
            if event.get("status") == "downloading" and total:
                item.percent = 100.0 * float(event.get("downloaded_bytes") or 0) / float(total)

        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with self._factory()(ydl_options(item.quality, self.folder, _ffmpeg(), progress)) as ydl:
                info = ydl.extract_info(item.url, download=True)
                if info.get("entries"):
                    info = info["entries"][0]
                item.title = str(info.get("title") or "")[:120]
                final = (info.get("requested_downloads") or [{}])[-1].get("filepath") or ydl.prepare_filename(info)
                path = Path(final)
                if item.quality == "audio":
                    path = path.with_suffix(".mp3")
                item.file = path
            item.state, item.percent = "pronto", 100.0
            logger.info("DOWNLOAD_DONE quality=%s seconds=%.0f size=%s", item.quality, time.monotonic() - item.started_at, item.file.stat().st_size if item.file and item.file.exists() else "?")
        except Exception as exc:  # o yt-dlp levanta vários tipos; para quem pediu, só importa a mensagem
            item.state, item.error = "erro", _friendly_error(exc)
            logger.warning("DOWNLOAD_FAILED error=%s", str(exc)[:200])
        on_done(item)


def _friendly_error(exc: Exception) -> str:
    text = str(exc)
    if "not a bot" in text:
        return "o YouTube pediu verificação anti-robô. Exporte os cookies do YouTube para configs/secure/ytdlp_cookies.txt."
    if "Private" in text or "login" in text.lower() or "sign in" in text.lower():
        return "o vídeo é privado ou pede login."
    if "Unsupported URL" in text:
        return "esse site não é suportado."
    if "HTTP Error 404" in text:
        return "o link não existe mais."
    return "não consegui baixar (link ou conexão)."


# ── Achar o link: área de transferência ou barra de endereço do navegador ─────────────────────────────────────────

def clipboard_text() -> str:
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.GetClipboardData.restype = ctypes.c_void_p
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(pointer) if pointer else ""
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _process_name(hwnd: int) -> str:
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        import psutil

        return psutil.Process(pid.value).name().lower()
    except Exception:
        return ""


def browser_url() -> str | None:
    """Endereço da janela de navegador mais à frente (lido pela acessibilidade do Windows, sem mexer no foco).
    Balões do navegador ("Traduzir a página?") também são janelas dele: tenta as primeiras até achar um endereço."""
    user32 = ctypes.windll.user32
    candidates: list[int] = []
    hwnd = user32.GetTopWindow(None)
    while hwnd and len(candidates) < 4:
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) and _process_name(hwnd) in BROWSERS:
            candidates.append(hwnd)
        hwnd = user32.GetWindow(hwnd, 2)  # GW_HWNDNEXT: segue a ordem de cima para baixo
    if not candidates:
        return None
    try:
        from pywinauto import Desktop

        desktop = Desktop(backend="uia")
        for handle in candidates:
            for edit in desktop.window(handle=handle).descendants(control_type="Edit")[:6]:
                url = find_url(edit.get_value() or "")
                if url:
                    return url
    except Exception as exc:
        logger.info("BROWSER_URL_READ_FAILED reason=%s", type(exc).__name__)
    return None


__all__ = ["Download", "Downloader", "browser_url", "clipboard_text", "find_url", "quality_key", "ydl_options"]
