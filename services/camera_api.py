"""Rotas da webcam (services/camera_watch.py): a Kira pede olhar, vigiar ou gestos; a foto abre num cartão."""

from __future__ import annotations

import ctypes
import hmac
import logging
import uuid
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Request
from fastapi.responses import FileResponse

from services.camera_watch import ACTIONS, CameraWatch, build_presentation, narration
from services.local_request import is_local_request

logger = logging.getLogger("kira.app.camera")

# teclas de mídia e volume do Windows (para quando a música não é a da Kira)
MEDIA_KEYS = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "volume_up": 0xAF, "volume_down": 0xAE}


def press_media_key(action: str) -> bool:
    key = MEDIA_KEYS.get(action)
    if key is None:
        return False
    user32 = ctypes.windll.user32
    for _ in range(3 if action.startswith("volume") else 1):  # 3 passos de volume (~6%)
        user32.keybd_event(key, 0, 0, 0)
        user32.keybd_event(key, 0, 2, 0)
    return True


def create_camera_router(camera: CameraWatch, *, publish: Callable[[dict[str, Any]], Awaitable[Any]],
                         token_provider: Callable[[], str]) -> APIRouter:
    router = APIRouter()

    def authorize(request: Request, supplied: str | None) -> None:
        expected = str(token_provider() or "")
        if expected and supplied and hmac.compare_digest(str(supplied), expected):
            return
        if is_local_request(request) and not request.headers.get("x-kira-remote"):
            return
        raise HTTPException(status_code=403, detail="acao interna nao autorizada")

    @router.post("/api/vision/camera")
    async def ask(request: Request, payload: Any = Body(default=None),
                  x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        authorize(request, x_kira_internal)
        body = payload if isinstance(payload, dict) else {}
        action = str(body.get("action") or "").strip().lower()
        if action not in ACTIONS:
            raise HTTPException(status_code=422, detail="acao invalida")
        try:
            minutes = int(body.get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        facts = await camera.ask(action, minutes, str(body.get("name") or ""))
        card = build_presentation(facts, f"cam-{uuid.uuid4().hex[:10]}")
        shown = False
        if card is not None:
            try:
                shown = bool(await publish(card))
            except Exception as exc:  # cartão é extra: a resposta falada vale sozinha
                logger.info("CAMERA_CARD_FAILED reason=%s", type(exc).__name__)
        return {"ok": bool(facts.get("ok")), "summary": narration(facts), "card": shown, "image": facts.get("imagem") or ""}

    @router.get("/api/vision/image/{name}")
    async def image(name: str, request: Request) -> FileResponse:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        path = camera.image_path(name)
        if path is None:
            raise HTTPException(status_code=404, detail="imagem nao encontrada")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    return router


__all__ = ["MEDIA_KEYS", "create_camera_router", "press_media_key"]
