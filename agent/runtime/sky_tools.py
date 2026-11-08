"""Tool do céu e radar (Olho de Deus ampliado): ISS, satélites, Starlink, lixo espacial, chuva nos próximos minutos,
avião passando perto e foto de satélite. Fala só com o backend local (services/sky_api.py)."""

from __future__ import annotations

import httpx
from livekit.agents import function_tool

try:
    from runtime.media_tools import _post_local
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime.media_tools import _post_local

ACTIONS = ("iss", "satelites", "starlink", "lixo", "chuva", "aviao", "foto")
UNAVAILABLE = "Não consegui consultar o céu agora."


class SkyToolsMixin:
    @function_tool(
        name="observar_ceu",
        description=(
            "Céu, chuva e aviões perto do Chefe, com dados reais de agora (abre um cartão na tela). acao: "
            "'iss' (quando a Estação Espacial passa aqui, se dá para ver a olho nu, para onde olhar); "
            "'satelites' (satélites brilhantes sobre o lugar agora); 'starlink' (quantos Starlink estão em cima agora); "
            "'lixo' (lixo espacial / detritos rastreados passando por cima); "
            "'chuva' (vai chover agora, nos próximos 15, 30, 60 minutos, radar de chuva perto; para outros dias use get_weather); "
            "'aviao' (que avião é esse passando, de onde vem e para onde vai, altitude, quantos aviões perto); "
            "'foto' (foto de satélite do lugar: a de hoje com as nuvens e uma sem nuvens bem detalhada). "
            "lugar: cidade ou bairro; vazio = a cidade salva do Chefe. Fale os números que voltarem, curto, com seu jeito."
        ),
    )
    async def observar_ceu(self, acao: str, lugar: str = "") -> str:
        action = str(acao or "").strip().lower()
        if action not in ACTIONS:
            return "Ação desconhecida: use iss, satelites, starlink, lixo, chuva, aviao ou foto."
        try:
            result = await _post_local("/api/sky/ask", {"action": action, "place": str(lugar or "")[:80]}, read_timeout=40.0)
        except (httpx.HTTPError, TimeoutError):
            return UNAVAILABLE
        return str(result.get("summary") or UNAVAILABLE)


__all__ = ["SkyToolsMixin"]
