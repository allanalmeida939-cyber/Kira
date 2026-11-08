"""Saúde: consentimento, importação e revogação só pela própria máquina; consulta interna para o agente."""

from __future__ import annotations

import hmac
from typing import Any, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Request

from services.controllers.health import CONSENT_SCOPE, LIMITATIONS, UNITS, HealthVault, enabled, summarize
from services.controllers.research import ResearchController, SectionPlan
from services.local_request import is_local_request

PRESENTATION_ID = "health-kira"


def create_ui_health_router(research: ResearchController, vault: HealthVault, *, token_provider: Callable[[], str]) -> APIRouter:
    router = APIRouter()

    def require_internal(supplied: str | None) -> None:
        expected = str(token_provider() or "")
        if not expected or not supplied or not hmac.compare_digest(str(supplied), expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")

    def require_local(request: Request) -> None:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        if not enabled():
            raise HTTPException(status_code=409, detail="saude desligada")

    def panel_data() -> dict[str, Any]:
        consented = vault.consent() is not None
        records = vault.records() if consented else []
        categories = sorted({r["category"] for r in records})
        return {
            "consented": consented, "scope": CONSENT_SCOPE, "limitations": LIMITATIONS, "units": {k: sorted(v) for k, v in UNITS.items()},
            "summaries": [summarize(records, c) for c in categories], "total": len(records),
        }

    async def show(turn_id: str | None = None) -> None:
        async def load() -> dict[str, Any]:
            return {"data": panel_data()}

        await research.run(presentation_id=PRESENTATION_ID, title="Saúde (seus registros)", turn_id=turn_id, sections=[SectionPlan("saude", "health", "Registros autorizados", load)])

    @router.post("/api/ui/health/query")
    async def query(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        require_internal(x_kira_internal)
        if not enabled():
            return {"ok": False, "message": "Os dados de saúde estão desligados nas configurações da Kira."}
        body = payload if isinstance(payload, dict) else {}
        category = str(body.get("category") or "").strip().lower()
        await show(body.get("turnId") if isinstance(body.get("turnId"), str) else None)
        if vault.consent() is None:
            return {"ok": False, "message": "Ainda não há autorização: abri o painel para o usuário decidir e importar os dados."}
        records = vault.records()
        if category and category not in UNITS:
            return {"ok": False, "message": f"Categoria não suportada. Disponíveis: {', '.join(sorted(UNITS))}."}
        summaries = [summarize(records, category)] if category else [summarize(records, c) for c in sorted({r['category'] for r in records})]
        if not summaries:
            return {"ok": True, "resumos": [], "observacao": "Não há registros importados; isso não indica nada sobre a sua saúde."}
        return {"ok": True, "resumos": summaries, "regra": "Não diagnostique; cite datas, origem e limitações."}

    @router.post("/api/ui/health/consent")
    async def consent(request: Request) -> dict[str, Any]:
        require_local(request)
        vault.give_consent()
        await show()
        return {"ok": True}

    @router.post("/api/ui/health/import")
    async def import_records(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        require_local(request)
        rows = payload.get("records") if isinstance(payload, dict) else payload
        try:
            result = vault.import_records(rows if isinstance(rows, list) else [])
        except PermissionError:
            raise HTTPException(status_code=403, detail="sem consentimento")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        await show()
        return {"ok": True, **result}

    @router.post("/api/ui/health/revoke")
    async def revoke_local(request: Request) -> dict[str, Any]:
        require_local(request)
        removed = vault.revoke()
        await show()
        return {"ok": True, "removed": removed}

    @router.post("/api/ui/health/revoke-by-voice")
    async def revoke_voice(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        require_internal(x_kira_internal)
        if not (isinstance(payload, dict) and payload.get("confirmed") is True):
            return {"ok": False, "message": "Peça confirmação em voz alta antes de apagar todos os dados de saúde."}
        return {"ok": True, "removed": vault.revoke()}

    return router


__all__ = ["create_ui_health_router"]
