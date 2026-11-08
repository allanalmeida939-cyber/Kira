"""Rotas dos avisos (services/notify.py). Cadastrar, remover e testar destinos só na tela do próprio PC; enviar, também
pela Kira (chave interna)."""

from __future__ import annotations

import hmac
from typing import Any, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Request

from services.local_request import is_local_request
from services.notify import Notifier, NotifyError


def create_notify_router(notifier: Notifier, *, token_provider: Callable[[], str]) -> APIRouter:
    router = APIRouter()

    def local_only(request: Request) -> None:
        if not is_local_request(request) or request.headers.get("x-kira-remote"):
            raise HTTPException(status_code=403, detail="so na tela do PC")

    def internal_or_local(request: Request, supplied: str | None) -> None:
        expected = str(token_provider() or "")
        if expected and supplied and hmac.compare_digest(str(supplied), expected):
            return
        local_only(request)

    @router.get("/api/notify/destinations")
    async def destinations(request: Request) -> dict[str, Any]:
        local_only(request)
        return {"ok": True, "destinations": notifier.destinations()}

    @router.post("/api/notify/destinations")
    async def add(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        local_only(request)
        body = payload if isinstance(payload, dict) else {}
        try:
            notifier.add(str(body.get("name") or ""), str(body.get("url") or ""))
        except NotifyError as exc:
            return {"ok": False, "error": str(exc), "destinations": notifier.destinations()}
        return {"ok": True, "destinations": notifier.destinations()}

    @router.delete("/api/notify/destinations/{destination_id}")
    async def remove(destination_id: str, request: Request) -> dict[str, Any]:
        local_only(request)
        notifier.remove(destination_id)
        return {"ok": True, "destinations": notifier.destinations()}

    @router.post("/api/notify/test/{destination_id}")
    async def test(destination_id: str, request: Request) -> dict[str, Any]:
        local_only(request)
        if not any(item["id"] == destination_id for item in notifier.destinations()):
            raise HTTPException(status_code=404, detail="destino nao encontrado")
        return await notifier.send("Teste da Kira: se chegou, este destino está funcionando.", to=destination_id)

    @router.post("/api/notify/send")
    async def send(request: Request, payload: Any = Body(default=None),
                   x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        internal_or_local(request, x_kira_internal)
        body = payload if isinstance(payload, dict) else {}
        return await notifier.send(str(body.get("message") or ""), title=str(body.get("title") or "Kira"), to=str(body.get("to") or ""))

    return router


__all__ = ["create_notify_router"]
