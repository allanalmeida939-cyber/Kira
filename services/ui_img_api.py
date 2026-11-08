"""Endpoint GET /api/ui/img: entrega imagens remotas validadas pelo ImageProxy."""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request, Response

from services.image_proxy import THUMBNAIL_WIDTHS, ImageProxy, ProxyError
from services.local_request import is_local_request

logger = logging.getLogger("kira.app")


def create_ui_img_router(proxy: ImageProxy) -> APIRouter:
    router = APIRouter()

    @router.get("/api/ui/img")
    async def get_image(request: Request, u: str = Query(default="", max_length=2000), w: int = Query(default=0, ge=0, le=2000)) -> Response:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        try:
            data, content_type = await (proxy.thumbnail(u, w) if w in THUMBNAIL_WIDTHS else proxy.fetch(u))
        except ProxyError as exc:
            logger.info("IMG_PROXY_REJECTED host=%s reason=%s", urlsplit(u).hostname or "", exc.code)
            raise HTTPException(status_code=404, detail="imagem indisponivel") from exc
        return Response(
            content=data,
            media_type=content_type,
            headers={
                "Cache-Control": "private, max-age=86400",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    return router


__all__ = ["create_ui_img_router"]
