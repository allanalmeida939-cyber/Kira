"""Endpoints de agenda e e-mail (internos para o agente) e conexão da conta Google (só da própria máquina)."""

from __future__ import annotations

import hmac
import webbrowser
from typing import Any, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from services.controllers.briefing import BriefingController
from services.controllers.personal import AgendaController, EmailAnalysisController
from services.local_request import is_local_request
from services.providers.google_oauth import GoogleAuth, NotConnected
from services.ui_actions_hub import ID_RE

DONE_PAGE = "<!doctype html><meta charset='utf-8'><title>Kira</title><p style='font-family:sans-serif'>{message} Pode fechar esta aba.</p>"


def create_ui_personal_router(
    agenda: AgendaController,
    email: EmailAnalysisController,
    auths: dict[str, GoogleAuth],
    briefing: BriefingController | None = None,
    *,
    token_provider: Callable[[], str],
    open_browser: Callable[[str], object] = lambda url: webbrowser.open(url, new=2),
) -> APIRouter:
    router = APIRouter()

    def require_internal(supplied: str | None) -> None:
        expected = str(token_provider() or "")
        if not expected or not supplied or not hmac.compare_digest(str(supplied), expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")

    def require_local(request: Request) -> None:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")

    def turn_of(body: dict) -> str | None:
        turn_id = body.get("turnId")
        return turn_id if isinstance(turn_id, str) and ID_RE.fullmatch(turn_id) else None

    def auth_for(service: str) -> GoogleAuth:
        if service not in auths:
            raise HTTPException(status_code=422, detail="servico invalido")
        return auths[service]

    @router.post("/api/ui/agenda")
    async def ask_agenda(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        require_internal(x_kira_internal)
        body = payload if isinstance(payload, dict) else {}
        return await agenda.ask(when=" ".join(str(body.get("when") or "hoje").split())[:40], turn_id=turn_of(body))

    @router.post("/api/ui/email")
    async def ask_email(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        require_internal(x_kira_internal)
        body = payload if isinstance(payload, dict) else {}
        return await email.ask(query=" ".join(str(body.get("query") or "").split())[:60], turn_id=turn_of(body))

    @router.post("/api/ui/briefing")
    async def ask_briefing(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        require_internal(x_kira_internal)
        if briefing is None:
            raise HTTPException(status_code=503, detail="briefing indisponivel")
        return await briefing.run(turn_id=turn_of(payload if isinstance(payload, dict) else {}))

    @router.post("/api/ui/briefing/step")
    async def briefing_step(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        require_internal(x_kira_internal)
        if briefing is None:
            raise HTTPException(status_code=503, detail="briefing indisponivel")
        return await briefing.step(str(payload.get("direction") or "próximo") if isinstance(payload, dict) else "próximo")

    @router.get("/api/ui/google/status")
    async def google_status(request: Request) -> dict[str, Any]:
        require_local(request)
        return {"ok": True, **{service: auth.connected for service, auth in auths.items()}}

    @router.post("/api/ui/google/connect")
    async def google_connect(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        require_local(request)
        service = str(payload.get("service") or "") if isinstance(payload, dict) else ""
        try:
            url = auth_for(service).authorization_url()
        except NotConnected as exc:
            return {"ok": False, "message": str(exc)}
        open_browser(url)
        return {"ok": True}

    @router.get("/api/ui/google/callback/{service}", response_class=HTMLResponse)
    async def google_callback(service: str, request: Request, state: str = Query(default="", max_length=200), code: str = Query(default="", max_length=2000)) -> HTMLResponse:
        require_local(request)
        try:
            await auth_for(service).complete(state, code)
        except NotConnected as exc:
            return HTMLResponse(DONE_PAGE.format(message=f"Não conectei: {exc}."), status_code=400)
        return HTMLResponse(DONE_PAGE.format(message="Conta conectada à Kira (somente leitura)."))

    @router.get("/api/ui/google/help")
    async def google_help(request: Request) -> dict[str, Any]:
        """Passo a passo da credencial do Google (o mesmo que a Kira fala: services/setup_help.py)."""
        require_local(request)
        from services.providers.google_oauth import client_credentials
        from services.setup_help import GOOGLE

        store = next(iter(auths.values()))._store if auths else None
        return {"ok": True, **GOOGLE, "has_app": client_credentials(store) is not None}

    @router.post("/api/ui/google/app")
    async def google_app(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        """Guarda a credencial do app Google colada nas Configurações (criptografada no cofre)."""
        require_local(request)
        from services.providers.google_oauth import APP_KEY

        body = payload if isinstance(payload, dict) else {}
        client_id = " ".join(str(body.get("client_id") or "").split())[:200]
        secret = " ".join(str(body.get("client_secret") or "").split())[:200]
        if not client_id.endswith(".apps.googleusercontent.com") or len(secret) < 10:
            return {"ok": False, "message": "Confira: o ID termina em .apps.googleusercontent.com e a chave secreta vem junto."}
        store = next(iter(auths.values()))._store
        store.save(APP_KEY, {"client_id": client_id, "client_secret": secret})
        return {"ok": True, "message": "Credencial salva. Agora toque em Conectar Agenda e Conectar Gmail."}

    @router.post("/api/ui/google/disconnect")
    async def google_disconnect(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        require_local(request)
        service = str(payload.get("service") or "") if isinstance(payload, dict) else ""
        return {"ok": True, "removed": auth_for(service).revoke_local()}

    return router


__all__ = ["create_ui_personal_router"]
