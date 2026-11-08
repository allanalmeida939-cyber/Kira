"""Rotas da tool 'rota' (services/routes.py): calcula, faz a rota aparecer flutuando na tela (mapa com zoom, modelo
escolhido pelo Chefe) e devolve os números e as linhas de ônibus para a Kira falar."""

from __future__ import annotations

import hmac
import logging
import os
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Request

from services.local_request import is_local_request
from services.routes import MODE_NAMES, MODES, RoutePlanner

logger = logging.getLogger("kira.app.routes")

HOME_ENV = "KIRA_CASA"
# modelo escolhido pelo Chefe (cockpit, holograma, itinerario, radar): layout, entrada e desenho da rota
STYLE_ENV = "KIRA_ROTA_ESTILO"
STYLES = ("cockpit", "holograma", "itinerario", "radar", "fusao1", "fusao2", "fusao3", "fusao4")
# entrada da rota escolhida (routeIntros.js); vazio = a entrada própria do modelo
INTRO_ENV = "KIRA_ROTA_ENTRADA"
INTROS = ("quantica", "lidar", "portal", "projecao", "neural", "energia", "pulso", "vidro", "dobra", "gravidade", "lidar_neural_energia",
          "combo_genese", "combo_tatica", "combo_portal", "combo_nucleo")
HOME_WORDS = {"", "casa", "minha casa", "daqui", "aqui", "de casa", "onde estou", "lar"}


def home_address() -> str:
    return str(os.getenv(HOME_ENV) or "").strip()


def route_style() -> str:
    style = str(os.getenv(STYLE_ENV) or "").strip().lower()
    return style if style in STYLES else "cockpit"


def route_intro() -> str:
    intro = str(os.getenv(INTRO_ENV) or "").strip().lower()
    return intro if intro in INTROS else ""


def bus_summary(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"tempo": option["duration"], "sai_de_casa": option["leave"], "anda_a_pe": option["walk"], "trocas": option["transfers"],
             "linhas": [{"linha": line["line"], "nome": line["name"], "sentido": line["headsign"], "embarca_em": line["from"],
                         "desce_em": line["to"], "partida": line["depart"]} for line in option["lines"]]} for option in plan.get("bus") or []]


def create_routes_router(planner: RoutePlanner, *, token_provider: Callable[[], str],
                         show: Callable[[dict[str, Any]], Awaitable[Any]]) -> APIRouter:
    """show({id, model, mode}): a tela em uso (PC ou celular) busca o plano e desenha (window.KiraRouteOverlay.show)."""
    router = APIRouter()

    @router.post("/api/ui/rota")
    async def tool(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        expected = str(token_provider() or "")
        if not expected or not x_kira_internal or not hmac.compare_digest(str(x_kira_internal), expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")
        body = payload if isinstance(payload, dict) else {}
        destination = str(body.get("destination") or "").strip()
        origin = str(body.get("origin") or "").strip()
        mode = str(body.get("mode") or "carro").strip().lower()
        mode = mode if mode in MODES else "carro"
        if not destination:
            return {"ok": False, "message": "Diga para onde é a rota."}
        if origin.lower() in HOME_WORDS:
            origin = home_address()
            if not origin:
                return {"ok": False, "message": ("Não sei o endereço da casa do Chefe. Pergunte de onde ele sai (rua, bairro e cidade); "
                                                 f"para não perguntar mais, o endereço vai no .env como {HOME_ENV}.")}
        try:
            plan = await planner.plan(origin, destination, mode, str(body.get("official") or ""))
        except Exception as exc:  # noqa: BLE001 - rede de fora: a Kira fala que não deu
            logger.info("ROUTE_FAILED reason=%s detail=%s", type(exc).__name__, str(exc)[:160])
            return {"ok": False, "message": "Não consegui calcular a rota agora."}
        if not plan.get("ok"):
            return plan
        style = route_style()
        try:
            shown = await show({"id": plan["id"], "model": style, "mode": plan["mode"], "intro": route_intro()})
        except RuntimeError as exc:  # tela ainda não pronta
            logger.info("ROUTE_UI_UNAVAILABLE reason=%s", exc)
            shown = None
        logger.info("ROUTE_SHOWN mode=%s seconds=%s model=%s bus=%s ui=%s", plan["mode"], plan["seconds"], style, len(plan["bus"]), bool(shown))
        answer = {
            "ok": True, "de": plan["origin"]["label"], "para": plan["destination"]["label"], "modo": MODE_NAMES[plan["mode"]],
            "tempo": plan["duration"], "chegada_prevista": plan["arrive"],
            "todos_os_modos": {MODE_NAMES[m["mode"]]: m["duration"] + (" (estimado)" if m["estimate"] else "") for m in plan["modes"]},
            "caminhos_de_carro": [f"{r['duration']}, {r['distance']}" for r in plan["routes"] if r["mode"] == "carro"],
            "como_responder": ("Diga o tempo do modo pedido e a hora de chegada numa frase; a tela já mostra o mapa e os outros modos. "
                               "Carro e moto são sem trânsito ao vivo (no pico demora mais); moto é estimativa."),
        }
        if plan["mode"] == "onibus" or body.get("bus"):
            answer["onibus"] = bus_summary(plan) or plan["busNote"]
            answer["como_responder"] = ("Diga as linhas de ônibus (número e nome), onde embarca e o horário da próxima partida, e o tempo total. "
                                        "Os dados são os horários publicados das linhas metropolitanas: se a partida for em outro dia, avise.")
        return answer

    @router.get("/api/route/data/{plan_id}")
    async def data(plan_id: str, request: Request) -> dict[str, Any]:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        plan = planner.get(plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="rota nao encontrada")
        return plan

    return router


__all__ = ["create_routes_router", "route_style", "bus_summary", "HOME_ENV", "STYLE_ENV", "STYLES"]
