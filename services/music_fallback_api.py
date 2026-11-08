"""Rotas do plano B da música (services/music_fallback.py): procurar outra fonte e tocar a conversão do SoundCloud.

Só da própria máquina (a tela da Kira). A rota de áudio só aceita um código que o backend criou ao achar a música:
nunca converte um endereço vindo de fora.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse

from services.local_request import is_local_request
from services.music_fallback import MusicFallback, MusicFallbackError


def create_music_fallback_router(get_fallback: Callable[[], MusicFallback]) -> APIRouter:
    router = APIRouter()

    def require_local(request: Request) -> None:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")

    @router.post("/api/music/fallback")
    async def music_fallback(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        """O YouTube não tocou: outra versão no YouTube ou a mesma música no SoundCloud."""
        require_local(request)
        body = payload if isinstance(payload, dict) else {}
        try:
            duration = float(body.get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        try:
            data = await get_fallback().find(video_id=str(body.get("videoId") or ""), title=str(body.get("title") or "")[:200],
                                             channel=str(body.get("channel") or "")[:120], duration=duration)
        except MusicFallbackError as exc:
            return {"success": False, "data": None, "error": str(exc)}
        return {"success": True, "data": data, "error": None}

    @router.get("/api/music/stream/{token}")
    async def music_stream(request: Request, token: str, start: float = 0.0) -> StreamingResponse:
        """Áudio convertido (Opus/WebM) da fonte achada; start em segundos para pular para um ponto."""
        require_local(request)
        fallback = get_fallback()
        if not fallback.has_stream(token):
            raise HTTPException(status_code=404, detail="fonte expirada")
        return StreamingResponse(fallback.stream(token, max(0.0, float(start))), media_type="audio/webm",
                                 headers={"Cache-Control": "no-store"})

    return router


__all__ = ["create_music_fallback_router"]
