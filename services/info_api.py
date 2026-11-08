"""Rota das informações sob pedido (services/info_feeds.py): o cartão abre na tela e volta o que a Kira vai falar."""

from __future__ import annotations

import hmac
import logging
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Request

from services.info_feeds import ACTIONS, InfoFeeds, build_presentation, narration
from services.local_request import is_local_request

logger = logging.getLogger("kira.app.info")


def create_info_router(feeds: InfoFeeds, *, publish: Callable[[dict[str, Any]], Awaitable[Any]], token_provider: Callable[[], str]) -> APIRouter:
    router = APIRouter()

    @router.post("/api/info/ask")
    async def ask(request: Request, payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        expected = str(token_provider() or "")
        internal = bool(expected and x_kira_internal and hmac.compare_digest(str(x_kira_internal), expected))
        if not internal and not (is_local_request(request) and not request.headers.get("x-kira-remote")):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")
        body = payload if isinstance(payload, dict) else {}
        action = str(body.get("action") or "").strip().lower()
        if action not in ACTIONS:
            raise HTTPException(status_code=422, detail="acao invalida")
        query = " ".join(str(body.get("query") or "").split())[:300]
        source = " ".join(str(body.get("source") or "").split())[:60]
        facts = await feeds.ask(action, query, source)
        card = build_presentation(facts)
        shown = False
        if card is not None:
            try:
                shown = bool(await publish(card))
            except Exception as exc:  # o cartão é extra
                logger.info("INFO_CARD_FAILED reason=%s", type(exc).__name__)
        logger.info("INFO_ASK action=%s ok=%s card=%s", action, str(bool(facts.get("ok"))).lower(), str(shown).lower())
        return {"ok": bool(facts.get("ok")), "summary": narration(facts), "card": shown}

    return router


__all__ = ["create_info_router"]
