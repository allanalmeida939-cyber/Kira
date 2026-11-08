"""Tool do assistente de cozinha (services/kitchen_api.py): receita de verdade num painel, passo a passo e timers."""

from __future__ import annotations

import json
import logging

import httpx
from livekit.agents import function_tool

try:
    from runtime.media_tools import _post_local
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime.media_tools import _post_local

logger = logging.getLogger("kira.kitchen_tools")

ACTIONS = ("receita", "proximo", "anterior", "ir", "repetir", "ingredientes", "timer", "cancelar_timers", "fechar", "estado")
UNAVAILABLE = "Não consegui abrir a cozinha agora."


class KitchenToolsMixin:
    @function_tool(
        name="cozinha",
        description=(
            "Assistente de cozinha: receita de verdade num painel (ingredientes, passos e timers) e guia passo a passo. "
            "Use para QUALQUER pedido de receita ou de como fazer um prato ('receita de pudim', 'como faz bolo de cenoura', "
            "'me ensina a fazer feijoada', 'vamos cozinhar estrogonofe') — nunca pesquisa web nem Wikipédia para isso. "
            "acao: 'receita' (prato obrigatório; porcoes opcional) abre a receita no passo 1; 'proximo', 'anterior', "
            "'repetir', 'ir' (passo = número) guiam enquanto o Chefe cozinha; 'ingredientes' lista de novo; 'timer' marca um "
            "timer (minutos; sem minutos usa o tempo do passo atual; rotulo opcional, ex. 'pudim no forno') que avisa por "
            "voz quando acabar; 'cancelar_timers'; 'fechar'; 'estado'. Fale como quem está na cozinha junto: curto, um passo "
            "por vez, e ofereça o timer quando o passo tiver tempo."
        ),
    )
    async def cozinha(self, acao: str, prato: str = "", porcoes: int = 0, passo: int = 0, minutos: int = 0, rotulo: str = "") -> str:
        action = str(acao or "").strip().lower()
        if action not in ACTIONS:
            return f"Ação desconhecida: use {', '.join(ACTIONS)}."
        payload = {"action": action, "dish": str(prato or ""), "servings": int(porcoes or 0), "step": int(passo or 0),
                   "minutes": int(minutos or 0), "label": str(rotulo or "")}
        try:
            result = await _post_local("/api/ui/kitchen", payload, read_timeout=45.0 if action == "receita" else 8.0)
        except (httpx.HTTPError, TimeoutError, ValueError):
            return UNAVAILABLE
        if not result.get("ok"):
            return str(result.get("message") or UNAVAILABLE)
        result.pop("ok", None)
        return json.dumps(result, ensure_ascii=False)


__all__ = ["KitchenToolsMixin"]
