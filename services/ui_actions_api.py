"""Rotas locais autenticadas para o hub de ações Kira → UI."""

from __future__ import annotations

import hmac
from urllib.parse import urlsplit
from typing import Any, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Request, WebSocket, WebSocketDisconnect

from services.ui_actions_hub import MAX_PAYLOAD_BYTES, UiActionsHub, validate_actions


def _local_origin(origin: str | None, port: int) -> bool:
    if not origin:
        return False
    try:
        parsed = urlsplit(str(origin).strip())
        origin_port = parsed.port
    except Exception:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost"}
        and origin_port == int(port)
        and not parsed.username
        and not parsed.password
    )


def create_ui_actions_router(
    hub: UiActionsHub,
    *,
    token_provider: Callable[[], str],
    port: int,
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/ui/actions")
    async def post_ui_actions(
        request: Request,
        payload: Any = Body(default=None),
        x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal"),
    ) -> dict[str, object]:
        expected = str(token_provider() or "")
        supplied = str(x_kira_internal or "")
        if not expected or not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")
        content_type = str(request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise HTTPException(status_code=415, detail="content-type deve ser application/json")
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > MAX_PAYLOAD_BYTES:
            raise HTTPException(status_code=413, detail="payload muito grande")
        actions = validate_actions(payload)
        if actions is None:
            raise HTTPException(status_code=422, detail="acao invalida")
        delivered = await hub.publish(actions)
        return {"ok": True, "accepted": len(actions), "delivered": delivered}

    @router.websocket("/ws/ui-actions")
    async def ws_ui_actions(websocket: WebSocket) -> None:
        if not _local_origin(websocket.headers.get("origin"), port):
            await websocket.close(code=1008)
            return
        if not hub.add_client(websocket):
            await websocket.close(code=1013)
            return
        await websocket.accept()
        try:
            while True:
                # A UI nunca publica ações por esta conexão; mensagens recebidas são ignoradas.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            hub.remove_client(websocket)

    return router


__all__ = ["create_ui_actions_router"]
