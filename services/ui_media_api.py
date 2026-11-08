"""Endpoint interno que agenda pesquisas de mídia sem bloquear a conversa."""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Request

from services.local_request import is_local_request
from services.live_score import LiveScoreService
from services.media_search import MediaSearchService
from services.media_story import MediaStory
from services.media_topics import clean_topic
from services.panel_registry import PanelRegistry
from services.ui_media_settings import UiMediaSettings

logger = logging.getLogger("kira.app.ui_media")  # filho de kira.app: vai para logs/Backend


def create_ui_media_router(
    hub: PanelRegistry,
    *,
    token_provider: Callable[[], str],
    service: MediaSearchService,
    story: MediaStory,
    settings: UiMediaSettings | None = None,
    score_service: LiveScoreService | None = None,
    windows_summary: Callable[[], str] | None = None,
) -> APIRouter:
    scores = score_service or LiveScoreService()
    router = APIRouter()
    media_settings = settings or getattr(service, "_settings", None) or UiMediaSettings(Path(__file__).resolve().parents[1] / "configs" / "ui_media.json")

    local_origin = is_local_request

    def require_internal(supplied: str | None) -> None:
        expected = str(token_provider() or "")
        if not expected or not supplied or not hmac.compare_digest(str(supplied), expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")

    def require_local_or_internal(request: Request, supplied: str | None) -> None:
        expected = str(token_provider() or "")
        provided = str(supplied or "")
        if expected and provided and hmac.compare_digest(provided, expected):
            return
        if local_origin(request):
            return
        raise HTTPException(status_code=403, detail="acao interna nao autorizada")

    @router.post("/api/ui/media/show")
    async def show_media_request(
        payload: Any = Body(default=None),
        x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal"),
    ) -> dict[str, object]:
        expected = str(token_provider() or "")
        supplied = str(x_kira_internal or "")
        if not expected or not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="payload de mídia inválido")
        topic = " ".join(str(payload.get("topic") or "").split())[:180].strip()
        if not topic:
            raise HTTPException(status_code=422, detail="tema de mídia ausente")
        kinds = payload.get("kinds")
        if isinstance(kinds, str):
            kinds = [kinds]
        if kinds is not None and (not isinstance(kinds, list) or any(not isinstance(kind, str) for kind in kinds)):
            raise HTTPException(status_code=422, detail="tipos de mídia inválidos")
        normalized_kinds = [str(kind).strip().lower() for kind in (kinds or ["images"])]
        if not normalized_kinds or any(kind not in {"images", "articles", "videos"} for kind in normalized_kinds):
            raise HTTPException(status_code=422, detail="tipos de mídia inválidos")
        if not service.enabled:
            return {"ok": False, "disabled": True, "message": "Mídia desativada pelo usuário."}
        if not clean_topic(topic):
            return {"ok": False, "accepted": False, "summary": "Não identifiquei um assunto para pesquisar; nada foi aberto."}
        if normalized_kinds == ["videos"]:
            service.start_media_job(topic, kinds=normalized_kinds, language="pt", publish=hub.publish)
        else:
            service.start_background(story.start(topic, with_article="articles" in normalized_kinds))
        return {"ok": True, "accepted": True, "message": "Mídia sendo carregada…"}

    @router.post("/api/ui/media/plan")
    async def plan_visuals_request(
        payload: Any = Body(default=None),
        x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal"),
    ) -> dict[str, object]:
        require_internal(x_kira_internal)
        body = payload if isinstance(payload, dict) else {}
        topic = " ".join(str(body.get("topic") or "").split())[:160]
        raw_beats = body.get("beats") if isinstance(body.get("beats"), list) else []
        beats = [
            {key: " ".join(str(b.get(key) or "").split())[:160] for key in ("beat", "query", "caption")}
            for b in raw_beats[:6]
            if isinstance(b, dict)
        ]
        if not service.enabled:
            return {"ok": False, "summary": "A mídia automática está desativada pelo usuário."}
        if not clean_topic(topic) or not beats:
            return {"ok": False, "summary": "Não identifiquei o que mostrar; nada foi aberto."}
        service.start_background(story.plan(topic, beats))
        return {"ok": True, "summary": "ok"}

    @router.post("/api/ui/media/recall")
    async def recall_image_request(
        payload: Any = Body(default=None),
        x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal"),
    ) -> dict[str, object]:
        require_internal(x_kira_internal)
        description = " ".join(str(payload.get("description") or "").split())[:160] if isinstance(payload, dict) else ""
        return await story.recall(description)

    @router.post("/api/ui/media/panels")
    async def list_active_panels(x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, object]:
        require_internal(x_kira_internal)
        summary = f"Janela principal: {hub.describe()}"
        if windows_summary is not None:
            summary += f" | Janelas: {windows_summary()}"
        return {"ok": True, "summary": summary, "panels": hub.active()}

    @router.post("/api/ui/vitals/state")
    async def report_vitals_state(request: Request, payload: Any = Body(default=None)) -> dict[str, object]:
        if not local_origin(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        if not isinstance(payload, dict) or not isinstance(payload.get("visible"), bool):
            raise HTTPException(status_code=422, detail="estado invalido")
        hub.set_vitals_visible(payload["visible"])
        return {"ok": True}

    @router.post("/api/ui/media/panels/state")
    async def report_panel_state(request: Request, payload: Any = Body(default=None)) -> dict[str, object]:
        if not local_origin(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        panel_id = str(payload.get("id") or "") if isinstance(payload, dict) else ""
        state = str(payload.get("state") or "") if isinstance(payload, dict) else ""
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", panel_id, re.IGNORECASE) or state not in {"closed", "touched", "pinned", "unpinned"}:
            raise HTTPException(status_code=422, detail="estado de painel invalido")
        return {"ok": hub.report(panel_id, state)}

    @router.post("/api/ui/score/show")
    async def show_score_request(
        payload: Any = Body(default=None),
        x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal"),
    ) -> dict[str, object]:
        expected = str(token_provider() or "")
        supplied = str(x_kira_internal or "")
        if not expected or not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")
        team = " ".join(str(payload.get("team") or "").split())[:80].strip() if isinstance(payload, dict) else ""
        if not team:
            raise HTTPException(status_code=422, detail="time ausente")
        return await scores.run_score_job(team, publish=hub.publish)

    @router.post("/api/ui/score/today")
    async def show_scores_today_request(
        x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal"),
    ) -> dict[str, object]:
        expected = str(token_provider() or "")
        supplied = str(x_kira_internal or "")
        if not expected or not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")
        return await scores.run_today_job(publish=hub.publish)

    @router.post("/api/ui/score/stop")
    async def stop_score_updates(
        request: Request,
        payload: Any = Body(default=None),
        x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal"),
    ) -> dict[str, object]:
        require_local_or_internal(request, x_kira_internal)
        panel_id = str(payload.get("panel_id") or "") if isinstance(payload, dict) else ""
        return {"ok": True, "stopped": scores.stop_watching(panel_id)}

    @router.post("/api/ui/media/close")
    async def close_media_request(
        request: Request,
        payload: Any = Body(default=None),
        x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal"),
    ) -> dict[str, object]:
        require_local_or_internal(request, x_kira_internal)
        target = str(payload.get("target") or "all") if isinstance(payload, dict) else "all"
        closed = await hub.close(target)
        logger.info("UI_MEDIA_CLOSE target=%s closed=%d", " ".join(target.split())[:60], closed)
        await scores.shutdown()
        return {"ok": True, "closed": closed}

    @router.get("/api/ui/media/settings")
    async def get_media_settings(
        request: Request,
        x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal"),
    ) -> dict[str, object]:
        require_local_or_internal(request, x_kira_internal)
        return {"ok": True, "enabled": media_settings.enabled}

    @router.post("/api/ui/media/settings")
    async def set_media_settings(
        request: Request,
        payload: Any = Body(default=None),
        x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal"),
    ) -> dict[str, object]:
        require_local_or_internal(request, x_kira_internal)
        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            raise HTTPException(status_code=422, detail="preferencia de mídia inválida")
        enabled = await asyncio.to_thread(media_settings.set_enabled, payload["enabled"])
        return {"ok": True, "enabled": enabled}

    @router.post("/api/ui/media/focus")
    async def focus_media_request(
        payload: Any = Body(default=None),
        x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal"),
    ) -> dict[str, object]:
        expected = str(token_provider() or "")
        supplied = str(x_kira_internal or "")
        if not expected or not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="payload de mídia inválido")
        panel_id = str(payload.get("panel_id") or "").strip()
        index = payload.get("index", 0)
        if not re.fullmatch(r"^[a-z0-9_-]{1,64}$", panel_id, re.IGNORECASE) or not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 10000:
            raise HTTPException(status_code=422, detail="item de mídia inválido")
        delivered = await hub.publish({"type": "media_cue", "panelId": panel_id, "index": index})
        return {"ok": True, "delivered": delivered}

    return router


__all__ = ["create_ui_media_router"]
