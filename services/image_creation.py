"""Criar imagens por voz: gera na nuvem (Pollinations/Flux, grátis e sem chave), salva em Imagens\\Kira e abre o painel.

O Fooocus (SDXL local) precisa de GPU com 4 GB+; a GTX 550 Ti (1 GB, Fermi) não roda. O modelo de imagem do Gemini
tem cota zero no plano gratuito ("limit: 0"), por isso não é o padrão.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import random
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Body, Header, HTTPException, Request
from fastapi.responses import FileResponse

from services.local_request import is_local_request

logger = logging.getLogger("kira.app.image_creation")  # filho de kira.app: vai para logs/Backend

CREATIONS_DIR = Path(os.getenv("KIRA_CREATIONS_DIR") or (Path.home() / "Pictures" / "Kira"))
PROVIDER_URL = "https://image.pollinations.ai/prompt/{prompt}"
PANEL_ID = "creation"
SIZES = {"quadrado": (1024, 1024), "paisagem": (1344, 768), "retrato": (768, 1344)}
MAX_BYTES = 15 * 1024 * 1024
NAME_RE = re.compile(r"^[a-z0-9-]{1,60}-\d{10}\.(?:jpg|png|webp)$")
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
MAX_LIMIT_WAIT_SECONDS = 50.0
RETRY_AFTER_SECONDS = 3.0  # erro passageiro do serviço gratuito (5xx, conexão caída): tenta mais uma vez


def slug(text: str) -> str:
    folded = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", folded).strip("-")[:40] or "imagem"


def provider_url(prompt: str, width: int, height: int, seed: int) -> str:
    return (
        PROVIDER_URL.format(prompt=quote(prompt, safe=""))
        + f"?width={width}&height={height}&seed={seed}&model=flux&nologo=true&safe=true"
    )


class FreeLimit(Exception):
    """Plano gratuito: 1 imagem a cada 45 s por IP; a resposta diz quantos segundos faltam."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"free limit, retry after {retry_after:.0f}s")
        self.retry_after = retry_after


async def _download(url: str) -> tuple[bytes, str]:
    token = os.getenv("POLLINATIONS_TOKEN", "").strip()  # opcional: sem limite de 45 s
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
    if response.status_code == 402:
        found = re.search(r"Retry after (\d+)s", response.text)
        raise FreeLimit(float(found.group(1)) if found else 45.0)
    response.raise_for_status()
    return response.content, response.headers.get("content-type", "").split(";")[0].strip().lower()


class ImageCreator:
    def __init__(
        self,
        publish: Callable[[dict[str, Any]], Awaitable[Any]],
        *,
        folder: Path = CREATIONS_DIR,
        fetch: Callable[[str], Awaitable[tuple[bytes, str]]] = _download,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._publish = publish
        self.folder = folder
        self._fetch = fetch
        self._clock = clock
        self._lock = asyncio.Lock()
        self.last: dict[str, Any] | None = None

    def path_for(self, name: str) -> Path | None:
        if not NAME_RE.match(str(name or "")):
            return None
        path = self.folder / name
        return path if path.is_file() else None

    async def _fetch_with_retry(self, url: str) -> tuple[bytes, str]:
        try:
            return await self._fetch(url)
        except FreeLimit:
            raise
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and status < 500 and status != 429:
                raise
            logger.info("IMAGE_CREATION_RETRY reason=%s status=%s", type(exc).__name__, status or "-")
            await asyncio.sleep(RETRY_AFTER_SECONDS)
            return await self._fetch(url)

    async def create(self, prompt: str, title: str = "", shape: str = "quadrado") -> dict[str, Any]:
        prompt = " ".join(str(prompt or "").split())[:500]
        if not prompt:
            return {"ok": False, "summary": "Descreva a imagem."}
        title = " ".join(str(title or "").split())[:60] or prompt[:40]
        width, height = SIZES.get(str(shape or "").lower(), SIZES["quadrado"])
        seed = random.randint(1, 2**31 - 1)
        async with self._lock:  # uma geração por vez: o serviço gratuito limita requisições simultâneas
            started = self._clock()
            url = provider_url(prompt, width, height, seed)
            try:
                try:
                    data, kind = await self._fetch_with_retry(url)
                except FreeLimit as limit:
                    if limit.retry_after > MAX_LIMIT_WAIT_SECONDS:
                        raise
                    logger.info("IMAGE_CREATION_FREE_LIMIT_WAIT seconds=%.0f", limit.retry_after)
                    await asyncio.sleep(limit.retry_after + 1)
                    data, kind = await self._fetch_with_retry(url)
            except FreeLimit as limit:
                return {"ok": False, "summary": f"O serviço gratuito libera uma imagem a cada 45 segundos; tente de novo em {limit.retry_after:.0f} s."}
            except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", "-")
                logger.warning("IMAGE_CREATION_FAILED reason=%s status=%s", type(exc).__name__, status)
                return {"ok": False, "summary": "O serviço de imagens não respondeu agora; tente de novo em instantes."}
            if kind not in EXTENSIONS or not data or len(data) > MAX_BYTES:
                logger.warning("IMAGE_CREATION_BAD_RESPONSE type=%s bytes=%s", kind, len(data or b""))
                return {"ok": False, "summary": "O serviço devolveu algo que não é imagem; tente reformular."}
            self.folder.mkdir(parents=True, exist_ok=True)
            name = f"{slug(title)}-{int(self._clock())}.{EXTENSIONS[kind]}"
            path = self.folder / name
            await asyncio.to_thread(path.write_bytes, data)
            self.last = {"name": name, "prompt": prompt, "title": title, "shape": shape, "path": str(path)}
            logger.info("IMAGE_CREATED seconds=%.1f bytes=%d shape=%s", self._clock() - started, len(data), shape)
        await self._publish({
            "type": "open_panel", "kind": "creation", "id": PANEL_ID, "title": "Criação", "focus": True,
            "props": {"title": title, "imageUrl": f"/api/ui/creation/file/{name}", "name": name, "prompt": prompt},
        })
        return {"ok": True, "name": name, "path": str(path), "summary": f"Imagem '{title}' criada e salva em Imagens, pasta Kira."}


async def send_to_whatsapp(path: Path, contact: str, token: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.post(
                "http://127.0.0.1:5050/send-media", headers={"X-Kira-Internal": token},
                json={"contact": contact, "path": str(path), "caption": ""},
            )
        result = response.json() if response.content else {}
    except (httpx.HTTPError, ValueError):
        result = {"success": False, "message": "O WhatsApp da Kira está desligado ou sem conexão."}
    return {"ok": bool(result.get("success")), "summary": str(result.get("message") or "")}


def create_image_creation_router(creator: ImageCreator, *, token_provider: Callable[[], str]) -> APIRouter:
    router = APIRouter()

    def require_internal(supplied: str | None) -> None:
        expected = str(token_provider() or "")
        if not expected or not supplied or not hmac.compare_digest(str(supplied), expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")

    def require_local(request: Request) -> None:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")

    def file_of(name: object) -> Path:
        path = creator.path_for(str(name or ""))
        if path is None:
            raise HTTPException(status_code=404, detail="imagem não encontrada")
        return path

    @router.post("/api/ui/creation/image")
    async def create_image(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        require_internal(x_kira_internal)
        data = payload if isinstance(payload, dict) else {}
        return await creator.create(str(data.get("prompt") or ""), str(data.get("title") or ""), str(data.get("shape") or "quadrado"))

    @router.post("/api/ui/creation/send")
    async def send_creation(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        require_internal(x_kira_internal)
        data = payload if isinstance(payload, dict) else {}
        contact = " ".join(str(data.get("contact") or "").split())[:80]
        if not contact:
            raise HTTPException(status_code=422, detail="contato ausente")
        name = str(data.get("name") or (creator.last or {}).get("name") or "")
        return await send_to_whatsapp(file_of(name), contact, str(token_provider() or ""))

    @router.get("/api/ui/creation/file/{name}")
    async def creation_file(name: str, request: Request) -> FileResponse:
        require_local(request)
        return FileResponse(file_of(name), headers={"Cache-Control": "no-store"})

    return router


__all__ = ["ImageCreator", "create_image_creation_router", "provider_url", "slug"]
