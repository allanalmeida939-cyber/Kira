"""Rotas do assistente de cozinha (services/kitchen.py): a tool da Kira (token interno) e os botões do painel (tela do
PC ou celular do Chefe pelo portão)."""

from __future__ import annotations

import hmac
import logging
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Request

from services.controllers.research import ResearchController, SectionPlan
from services.kitchen import PRESENTATION_ID, Kitchen
from services.local_request import is_local_request

logger = logging.getLogger("kira.kitchen")

ACTIONS = ("receita", "proximo", "anterior", "ir", "repetir", "ingredientes", "timer", "cancelar_timers", "fechar", "estado")


def create_kitchen_router(kitchen: Kitchen, research: ResearchController, *, token_provider: Callable[[], str],
                          publish: Callable[[dict[str, Any]], Awaitable[Any]] | None = None) -> APIRouter:
    router = APIRouter()

    async def show(turn_id: str | None = None) -> None:
        async def load() -> dict[str, Any]:
            return {"data": kitchen.panel()}

        title = f"Cozinha: {kitchen.recipe['title']}" if kitchen.recipe else "Cozinha"
        await research.run(presentation_id=PRESENTATION_ID, title=title, turn_id=turn_id, sections=[SectionPlan("receita", "recipe", "Receita", load)])

    async def act(body: dict[str, Any]) -> dict[str, Any]:
        action = str(body.get("action") or "").strip().lower()
        turn = body.get("turnId") if isinstance(body.get("turnId"), str) else None
        if action not in ACTIONS:
            return {"ok": False, "message": f"Ação desconhecida. Use: {', '.join(ACTIONS)}."}
        try:
            if action == "receita":
                recipe = await kitchen.load(str(body.get("dish") or ""), int(body.get("servings") or 0))
                await show(turn)
                return {
                    "ok": True, "receita": recipe["title"], "porcoes": recipe["servings"], "tempo_total_min": recipe["totalMinutes"],
                    "ingredientes": recipe["ingredients"], "passos": len(recipe["steps"]), "passo_atual": kitchen.step_line(),
                    "dicas": recipe["tips"],
                    "como_guiar": ("Diga em uma frase o que vai precisar (resuma os ingredientes) e leia o passo 1. Depois espere o "
                                   "Chefe pedir o próximo passo; quando um passo tiver tempo, ofereça marcar o timer."),
                }
            if action == "estado":
                return {"ok": True, "receita": (kitchen.recipe or {}).get("title"), "passo_atual": kitchen.step_line(), "timers": kitchen.panel()["timers"]}
            if action == "ingredientes":
                if not kitchen.recipe:
                    return {"ok": False, "message": "Nenhuma receita aberta."}
                return {"ok": True, "ingredientes": kitchen.recipe["ingredients"]}
            if action in ("proximo", "anterior", "ir", "repetir"):
                kitchen.move(action, int(body.get("step") or 0))
                await show(turn)
                last = kitchen.recipe and kitchen.current == len(kitchen.recipe["steps"]) - 1
                return {"ok": True, "passo_atual": kitchen.step_line(), "ultimo": bool(last)}
            if action == "timer":
                minutes = int(body.get("minutes") or 0)
                if not minutes and kitchen.recipe:
                    minutes = kitchen.recipe["steps"][kitchen.current]["minutes"]
                timer = kitchen.start_timer(minutes, str(body.get("label") or ""))
                await show(turn)
                return {"ok": True, "timer": f"{timer['label']}: {timer['minutes']} min", "aviso": "Eu aviso por voz quando acabar."}
            if action == "cancelar_timers":
                count = kitchen.cancel_timers()
                await show(turn)
                return {"ok": True, "cancelados": count}
            kitchen.close()
            if publish is not None:
                await publish({"type": "close_panel", "id": PRESENTATION_ID})
            return {"ok": True}
        except ValueError as exc:
            return {"ok": False, "message": str(exc) or "Não deu."}
        except Exception as exc:  # modelo fora do ar, JSON quebrado
            logger.info("KITCHEN_FAILED action=%s reason=%s", action, type(exc).__name__)
            return {"ok": False, "message": "Não consegui montar a receita agora."}

    @router.post("/api/ui/kitchen")
    async def tool(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        expected = str(token_provider() or "")
        if not expected or not x_kira_internal or not hmac.compare_digest(str(x_kira_internal), expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")
        return await act(payload if isinstance(payload, dict) else {})

    @router.post("/api/ui/kitchen/control")
    async def control(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        """Botões do painel: passo seguinte/anterior e o timer do passo. Tela do PC ou celular do Chefe."""
        role = str(request.headers.get("x-kira-role") or "owner")
        if not is_local_request(request) or role != "owner":
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        body = payload if isinstance(payload, dict) else {}
        action = str(body.get("action") or "")
        if action not in ("proximo", "anterior", "ir", "timer", "cancelar_timers"):
            raise HTTPException(status_code=422, detail="acao invalida")
        return await act({"action": action, "step": body.get("step"), "minutes": body.get("minutes"), "label": body.get("label")})

    return router


__all__ = ["create_kitchen_router"]
