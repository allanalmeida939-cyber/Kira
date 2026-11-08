"""Endpoints internos das tools de internet: busca, imagens, eleicao e downloads."""

from __future__ import annotations

import hmac
from typing import Any, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Request

from services.downloader import DownloadManager
from services.election import ElectionService
from services.local_request import is_local_request
from services.interests import note_search
from services.media_search import _slug
from services.panel_registry import PanelRegistry
from services.web_search import WebSearchService



def _clip(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def create_ui_web_router(
    hub: PanelRegistry,
    *,
    token_provider: Callable[[], str],
    web: WebSearchService,
    election: ElectionService,
    downloads: DownloadManager,
) -> APIRouter:
    router = APIRouter()

    def require_internal(supplied: str | None) -> None:
        expected = str(token_provider() or "")
        if not expected or not supplied or not hmac.compare_digest(str(supplied), expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")

    def require_internal_or_local(request: Request, supplied: str | None) -> None:
        expected = str(token_provider() or "")
        if expected and supplied and hmac.compare_digest(str(supplied), expected):
            return
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")

    def text_field(payload: Any, key: str, limit: int = 180) -> str:
        value = _clip(payload.get(key), limit) if isinstance(payload, dict) else ""
        if not value:
            raise HTTPException(status_code=422, detail=f"{key} ausente")
        return value

    @router.post("/api/ui/web/search")
    async def web_search(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, object]:
        require_internal(x_kira_internal)
        query = text_field(payload, "query")
        note_search(query)
        outcome = await web.search(query)
        return {"ok": bool(outcome.get("ok")), "summary": web.model_summary(outcome)}

    @router.post("/api/ui/web/images")
    async def web_images(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, object]:
        require_internal(x_kira_internal)
        query = text_field(payload, "query")
        outcome = await web.images(query)
        if not outcome.get("ok"):
            return {"ok": False, "summary": "Busca de imagens indisponível agora." if outcome.get("reason") == "indisponivel" else "Não encontrei imagens."}
        panel_id = f"gallery-web-{_slug(query)}"
        await hub.publish({"type": "open_panel", "kind": "gallery", "id": panel_id, "title": "Imagens", "props": {"query": query, "items": outcome["items"]}, "focus": True, "autoCloseMs": 600000})
        suffix = " Só Wikimedia Commons (busca geral indisponível)." if outcome.get("limited") else ""
        return {"ok": True, "summary": f"Mostrando {len(outcome['items'])} imagens de {query}.{suffix}"}  # type: ignore[arg-type]

    @router.post("/api/ui/election")
    async def show_election(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, object]:
        require_internal(x_kira_internal)
        result = await election.run_job(text_field(payload, "race", 120), publish=hub.publish)
        return result

    @router.post("/api/ui/download/request")
    async def download_request(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, object]:
        require_internal(x_kira_internal)
        url = text_field(payload, "url", 1800)
        name = _clip(payload.get("name"), 120) if isinstance(payload, dict) else ""
        return await downloads.request(url, name or None, publish=hub.publish)

    @router.post("/api/ui/download/confirm")
    async def download_confirm(request: Request, payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, object]:
        require_internal_or_local(request, x_kira_internal)
        download_id = _clip(payload.get("id"), 40) if isinstance(payload, dict) else ""
        return await downloads.confirm(download_id or None, publish=hub.publish)

    @router.post("/api/ui/download/cancel")
    async def download_cancel(request: Request, payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, object]:
        require_internal_or_local(request, x_kira_internal)
        download_id = _clip(payload.get("id"), 40) if isinstance(payload, dict) else ""
        return {"ok": await downloads.cancel(download_id, publish=hub.publish)}

    @router.post("/api/ui/download/open-folder")
    async def download_open_folder(request: Request, x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, object]:
        require_internal_or_local(request, x_kira_internal)
        return {"ok": downloads.open_folder()}

    return router


__all__ = ["create_ui_web_router"]
