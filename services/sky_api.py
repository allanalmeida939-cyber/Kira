"""Rotas do céu e radar (services/sky_watch.py): a Kira pergunta, o cartão abre na tela e volta o que ela vai falar."""

from __future__ import annotations

import asyncio
import hmac
import logging
import uuid
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Request
from fastapi.responses import FileResponse

from services.local_request import is_local_request
from services.sky_watch import ACTIONS, Place, SkyWatch, build_presentation, narration, radar_section

logger = logging.getLogger("kira.app.sky")


def create_sky_router(sky: SkyWatch, *, publish: Callable[[dict[str, Any]], Awaitable[Any]], token_provider: Callable[[], str]) -> APIRouter:
    router = APIRouter()
    pending: set[asyncio.Task] = set()

    async def add_radar(request_id: str, place: Place) -> None:
        """O radar entra no cartão depois da resposta falada (montá-lo leva alguns segundos)."""
        try:
            radar = await sky.radar_image(place)
        except Exception as exc:  # radar é extra: o cartão fica com o aviso
            logger.info("SKY_RADAR_FAILED reason=%s", type(exc).__name__)
            radar = {"ok": False}
        try:
            await publish({"type": "present_patch", "presentationId": "sky-kira", "requestId": request_id, "sections": [radar_section(radar)]})
        except Exception as exc:
            logger.info("SKY_RADAR_PATCH_FAILED reason=%s", type(exc).__name__)

    def authorize(request: Request, supplied: str | None) -> None:
        expected = str(token_provider() or "")
        if expected and supplied and hmac.compare_digest(str(supplied), expected):
            return
        if is_local_request(request) and not request.headers.get("x-kira-remote"):
            return
        raise HTTPException(status_code=403, detail="acao interna nao autorizada")

    @router.post("/api/sky/ask")
    async def ask(request: Request, payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        authorize(request, x_kira_internal)
        body = payload if isinstance(payload, dict) else {}
        action = str(body.get("action") or "").strip().lower()
        if action not in ACTIONS:
            raise HTTPException(status_code=422, detail="acao invalida")
        place = " ".join(str(body.get("place") or "").split())[:80]
        facts = await sky.ask(action, place)
        card = build_presentation(facts, f"sky-{uuid.uuid4().hex[:10]}")
        shown = False
        if card is not None:
            try:
                shown = bool(await publish(card))
            except Exception as exc:  # cartão é extra: a resposta falada vale sozinha
                logger.info("SKY_CARD_FAILED reason=%s", type(exc).__name__)
        logger.info("SKY_ASK action=%s ok=%s card=%s", action, str(bool(facts.get("ok"))).lower(), str(shown).lower())
        if shown and action == "chuva":
            task = asyncio.ensure_future(add_radar(card["requestId"], Place(facts["lat"], facts["lon"], facts.get("lugar") or "")))
            pending.add(task)
            task.add_done_callback(pending.discard)
        return {"ok": bool(facts.get("ok")), "summary": narration(facts), "card": shown}

    @router.get("/api/sky/image/{name}")
    async def image(name: str, request: Request) -> FileResponse:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        path = sky.image_path(name)
        if path is None:
            raise HTTPException(status_code=404, detail="imagem nao encontrada")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    return router


__all__ = ["create_sky_router"]
