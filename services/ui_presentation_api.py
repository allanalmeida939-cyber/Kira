"""Endpoints de apresentação: clima pedido pelo agente, cidade autorizada e eventos de turno (início/interrupção)."""

from __future__ import annotations

import asyncio
import hmac
from typing import Any, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Request

from services.controllers.news import NewsResearchController
from services.controllers.research import ResearchController
from services.controllers.video import VideoController
from services.controllers.weather import WeatherController
from services.local_request import is_local_request
from services.ui_actions_hub import ID_RE


def create_ui_presentation_router(
    research: ResearchController,
    weather: WeatherController,
    news: NewsResearchController | None = None,
    video: VideoController | None = None,
    *,
    token_provider: Callable[[], str],
) -> APIRouter:
    router = APIRouter()

    def require_internal(supplied: str | None) -> None:
        expected = str(token_provider() or "")
        if not expected or not supplied or not hmac.compare_digest(str(supplied), expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")

    def turn_of(body: dict) -> str | None:
        turn_id = body.get("turnId")
        return turn_id if isinstance(turn_id, str) and ID_RE.fullmatch(turn_id) else None

    @router.post("/api/ui/weather")
    async def ask_weather(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        require_internal(x_kira_internal)
        body = payload if isinstance(payload, dict) else {}
        city = " ".join(str(body.get("city") or "").split())[:80] or None
        when = " ".join(str(body.get("when") or "").split())[:40]
        return await weather.ask(city=city, when=when, turn_id=turn_of(body))

    @router.post("/api/ui/news")
    async def ask_news(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        require_internal(x_kira_internal)
        if news is None:
            raise HTTPException(status_code=503, detail="noticias indisponiveis")
        body = payload if isinstance(payload, dict) else {}
        days = body.get("days") if isinstance(body.get("days"), int) and not isinstance(body.get("days"), bool) else 7
        return await news.ask(
            query=" ".join(str(body.get("query") or "").split())[:120],
            topic=" ".join(str(body.get("topic") or "").split())[:30].lower(),
            days=max(1, min(days, 30)),
            turn_id=turn_of(body),
        )

    @router.post("/api/ui/video")
    async def ask_video(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        require_internal(x_kira_internal)
        query = " ".join(str(payload.get("query") or "").split())[:100] if isinstance(payload, dict) else ""
        if video is None or len(query) < 2:
            raise HTTPException(status_code=422, detail="pedido de video invalido")
        return await video.ask(query=query, turn_id=turn_of(payload))

    @router.get("/api/ui/location")
    async def get_location(request: Request) -> dict[str, Any]:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        return {"ok": True, "city": weather.locations.get()}

    @router.put("/api/ui/location")
    async def put_location(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        city = " ".join(str(payload.get("city") or "").split())[:80] if isinstance(payload, dict) else ""
        weather.locations.set(city or None)
        return {"ok": True, "city": weather.locations.get()}

    @router.get("/api/ui/settings")
    async def get_settings(request: Request) -> dict[str, Any]:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        from services import kira_settings

        from services.global_hotkeys import active_status

        return {"ok": True, "settings": kira_settings.load(), "city": weather.locations.get(), "shortcutStatus": active_status()}

    @router.put("/api/ui/settings")
    async def put_settings(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="configuracao invalida")
        from services import kira_settings

        settings = kira_settings.save(payload)
        if "shortcuts" in payload:
            from services.global_hotkeys import reload_active

            # Registrar no Windows é rápido, mas espera a thread dos atalhos: fora do event loop.
            return {"ok": True, "settings": settings, "shortcutStatus": await asyncio.to_thread(reload_active)}
        return {"ok": True, "settings": settings}

    @router.post("/api/ui/presentation/event")
    async def turn_event(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        require_internal(x_kira_internal)
        body = payload if isinstance(payload, dict) else {}
        event, turn_id = body.get("event"), turn_of(body)
        if event == "turn_started" and turn_id:
            research.coordinator.begin_turn(turn_id)
        elif event == "interrupted":
            await research.interrupt(turn_id)
        else:
            raise HTTPException(status_code=422, detail="evento invalido")
        return {"ok": True}

    return router


__all__ = ["create_ui_presentation_router"]
