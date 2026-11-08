"""Download seguro de arquivos: confirmacao previa, guarda SSRF, limites e nunca executa nada."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from services.image_proxy import RESOLVER, ProxyError, _system_resolver, assert_public_https
from services.media_search import PUBLISHER
from services.shared_http import ensure_tls

logger = logging.getLogger("kira.app")

MAX_BYTES = 100 * 1024 * 1024
TOTAL_TIMEOUT_SECONDS = 300.0
CONFIRM_TTL_SECONDS = 60.0
MAX_REDIRECTS = 3
PROGRESS_INTERVAL_SECONDS = 0.5
BLOCKED_EXTENSIONS = frozenset(
    ".exe .msi .bat .cmd .ps1 .vbs .js .jar .scr .dll .lnk .reg .com .hta .apk".split()
)
RESERVED_NAMES = frozenset({"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))})
INVALID_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def default_downloads_dir() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home()) / "Downloads" / "Kira"


def sanitize_filename(raw: object, fallback: str = "arquivo") -> str:
    """Nome seguro para o Windows: sem caminho, '..', caracteres invalidos ou nomes reservados."""
    text = unquote(str(raw or "")).replace("\\", "/").split("/")[-1]
    text = INVALID_CHARS_RE.sub("_", text).replace("..", "_").strip(" .")
    stem, dot, ext = text.rpartition(".") if "." in text else (text, "", "")
    if stem.lower() in RESERVED_NAMES:
        stem = f"_{stem}"
    text = f"{stem}.{ext}" if dot else stem
    text = text[:120].strip(" .")
    return text if re.search(r"[^\W_]", text) else fallback


def blocked_extension(name: str) -> str:
    ext = Path(name).suffix.lower()
    return ext if ext in BLOCKED_EXTENSIONS else ""


def unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    counter = 1
    while candidate.exists():
        candidate = directory / f"{Path(name).stem} ({counter}){Path(name).suffix}"
        counter += 1
    return candidate


def _format_size(size: int) -> str:
    return f"{size / 1048576:.1f} MB" if size >= 1048576 else f"{max(size // 1024, 1)} KB"


class DownloadManager:
    def __init__(
        self,
        *,
        directory: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: RESOLVER = _system_resolver,
        clock: Callable[[], float] = time.monotonic,
        max_bytes: int = MAX_BYTES,
        total_timeout: float = TOTAL_TIMEOUT_SECONDS,
    ) -> None:
        self._dir = directory or default_downloads_dir()
        self._transport = transport
        self._resolver = resolver
        self._clock = clock
        self._max_bytes = max_bytes
        self._total_timeout = total_timeout
        self._pending: dict[str, dict[str, object]] = {}
        self._tasks: dict[str, asyncio.Task[object]] = {}
        self._folder_opened: list[str] = []

    @property
    def directory(self) -> Path:
        return self._dir

    # ---------- confirmacao ----------
    def _purge(self) -> None:
        now = self._clock()
        for key in [k for k, v in self._pending.items() if v["expires"] <= now]:  # type: ignore[operator]
            self._pending.pop(key, None)

    async def request(self, url: str, name: str | None, *, publish: PUBLISHER) -> dict[str, object]:
        """Valida e abre o painel pedindo confirmacao; nada e baixado ainda."""
        url = str(url or "").strip()
        try:
            await assert_public_https(url, self._resolver)
        except ProxyError:
            return {"ok": False, "summary": "Só baixo arquivos de endereços https públicos; esse link não serve."}
        parsed = urlsplit(url)
        filename = sanitize_filename(name or parsed.path)
        blocked = blocked_extension(filename)
        if blocked:
            return {"ok": False, "summary": f"Não baixo arquivos {blocked}: podem executar código no seu computador."}
        self._purge()
        download_id = secrets.token_hex(6)
        host = parsed.hostname or ""
        self._pending[download_id] = {"url": url, "name": filename, "host": host, "expires": self._clock() + CONFIRM_TTL_SECONDS}
        await self._panel(publish, download_id, filename, host, "confirm", 0, 0)
        return {"ok": True, "id": download_id, "summary": f"Vou baixar {filename} de {host}. Pergunte ao usuário se pode e, se ele disser sim, chame confirm_download."}

    async def confirm(self, download_id: str | None, *, publish: PUBLISHER) -> dict[str, object]:
        self._purge()
        key = download_id if download_id in self._pending else next(iter(reversed(self._pending)), None)
        item = self._pending.pop(key, None) if key else None
        if not item or not key:
            return {"ok": False, "summary": "Não há download aguardando confirmação (ele expira em 60 segundos)."}
        task = asyncio.ensure_future(self._run(key, item, publish))
        self._tasks[key] = task
        task.add_done_callback(lambda done, k=key: self._tasks.pop(k, None))
        return {"ok": True, "id": key, "summary": f"Baixando {item['name']}."}

    async def cancel(self, download_id: str, *, publish: PUBLISHER | None = None) -> bool:
        item = self._pending.pop(download_id, None)
        task = self._tasks.get(download_id)
        if task and not task.done():
            task.cancel()
            return True
        if item and publish:
            await self._panel(publish, download_id, str(item["name"]), str(item["host"]), "cancelled", 0, 0)
        return bool(item)

    def open_folder(self) -> bool:
        """Abre so a pasta no Explorer; nunca abre ou executa o arquivo."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            if hasattr(os, "startfile"):
                os.startfile(str(self._dir))  # type: ignore[attr-defined]
            self._folder_opened.append(str(self._dir))
            return True
        except OSError:
            return False

    # ---------- download ----------
    async def _panel(self, publish: PUBLISHER, download_id: str, name: str, host: str, state: str, received: int, total: int, error: str = "") -> None:
        props: dict[str, object] = {
            "name": name, "source": host, "state": state, "value": received, "duration": total,
            "text": error or (f"{_format_size(received)}" + (f" de {_format_size(total)}" if total else "")),
        }
        try:
            await publish({"type": "open_panel", "kind": "download", "id": f"download-{download_id}", "title": "Download", "props": props, "focus": state == "confirm", "autoCloseMs": 600000})
        except Exception:
            return

    async def _run(self, download_id: str, item: dict[str, object], publish: PUBLISHER) -> None:
        name, host, url = str(item["name"]), str(item["host"]), str(item["url"])
        part = self._dir / f".{download_id}.part"
        await self._panel(publish, download_id, name, host, "downloading", 0, 0)
        try:
            final_name, size = await asyncio.wait_for(self._download(download_id, part, url, name, host, publish), self._total_timeout)
            target = unique_path(self._dir, final_name)
            await asyncio.to_thread(part.replace, target)
            logger.info("DOWNLOAD_DONE host=%s name=%s size=%s", host, target.name, size)
            await self._panel(publish, download_id, target.name, host, "done", size, size)
        except asyncio.CancelledError:
            await self._cleanup(part)
            logger.info("DOWNLOAD_CANCELLED host=%s name=%s", host, name)
            await self._panel(publish, download_id, name, host, "cancelled", 0, 0)
        except (ProxyError, httpx.HTTPError, OSError, asyncio.TimeoutError, ValueError) as exc:
            await self._cleanup(part)
            reason = getattr(exc, "code", type(exc).__name__)
            logger.info("DOWNLOAD_FAILED host=%s name=%s reason=%s", host, name, reason)
            await self._panel(publish, download_id, name, host, "failed", 0, 0, error=self._reason_text(reason))

    @staticmethod
    def _reason_text(reason: str) -> str:
        return {"muito_grande": "Arquivo maior que 100 MB.", "extensao_bloqueada": "Tipo de arquivo bloqueado por segurança.", "TimeoutError": "Tempo esgotado."}.get(reason, "Falha ao baixar o arquivo.")

    @staticmethod
    async def _cleanup(part: Path) -> None:
        await asyncio.to_thread(part.unlink, missing_ok=True)

    async def _download(self, download_id: str, part: Path, url: str, name: str, host: str, publish: PUBLISHER) -> tuple[str, int]:
        self._dir.mkdir(parents=True, exist_ok=True)
        headers = {"User-Agent": "KiraAssistant/8.0 python-httpx", "Accept": "*/*"}
        verify = await ensure_tls()
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), headers=headers, transport=self._transport, verify=verify) as client:
            current = url
            for _ in range(MAX_REDIRECTS + 1):
                await assert_public_https(current, self._resolver)
                async with client.stream("GET", current, follow_redirects=False) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise ProxyError("redirect_invalido")
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise ProxyError("status_invalido")
                    final_name = self._final_name(name, response.headers.get("content-disposition"))
                    if blocked_extension(final_name):
                        raise ProxyError("extensao_bloqueada")
                    length = response.headers.get("content-length")
                    total = int(length) if length and length.isdigit() else 0
                    if total > self._max_bytes:
                        raise ProxyError("muito_grande")
                    size, last_report = 0, 0.0
                    with part.open("wb") as handle:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > self._max_bytes:
                                raise ProxyError("muito_grande")
                            await asyncio.to_thread(handle.write, chunk)
                            now = self._clock()
                            if now - last_report >= PROGRESS_INTERVAL_SECONDS:
                                last_report = now
                                await self._panel(publish, download_id, final_name, host, "downloading", size, total)
                    return final_name, size
        raise ProxyError("redirects_demais")

    @staticmethod
    def _final_name(requested: str, disposition: str | None) -> str:
        match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", disposition or "", re.IGNORECASE)
        return sanitize_filename(match.group(1), fallback=requested) if match else requested

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)


__all__ = ["BLOCKED_EXTENSIONS", "DownloadManager", "blocked_extension", "default_downloads_dir", "sanitize_filename", "unique_path"]
