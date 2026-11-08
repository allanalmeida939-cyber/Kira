"""Tool de rotas (services/routes_api.py): tempo de viagem de A até B de carro, de bicicleta e a pé, com o mapa no painel."""

from __future__ import annotations

import json

import httpx
from livekit.agents import function_tool

try:
    from runtime.media_tools import _post_local
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime.media_tools import _post_local

UNAVAILABLE = "Não consegui calcular a rota agora."
MODES = {"carro": "carro", "uber": "carro", "taxi": "carro", "moto": "moto", "motocicleta": "moto", "onibus": "onibus", "ônibus": "onibus",
         "busao": "onibus", "busão": "onibus", "bike": "bike", "bicicleta": "bike",
         "a pe": "a_pe", "a pé": "a_pe", "a_pe": "a_pe", "andando": "a_pe", "caminhando": "a_pe"}


class RouteToolsMixin:
    @function_tool(
        name="rota",
        description=(
            "Rota e tempo de viagem de um lugar até outro: a tela mostra o mapa com zoom, a rota de A até B, caminhos "
            "alternativos e o tempo de carro, moto, bike, a pé e ônibus. Use para 'quanto tempo daqui até o centro', 'qual a "
            "rota até o Iguatemi', 'quanto tempo a pé até a praia', 'qual ônibus eu pego para o centro'. destino: lugar ou "
            "endereço (com bairro e cidade quando ele disser). origem: vazio ou 'casa' = casa do Chefe; senão o lugar de "
            "partida. modo: 'carro' (padrão), 'moto', 'bike', 'a_pe' ou 'onibus' (pergunta de ônibus/qual linha: use "
            "'onibus' e diga as linhas, onde embarca e o horário). nome_oficial: o nome do lugar como "
            "aparece no Google Maps, pelo que você sabe dele ('décima região militar' -> 'Comando da 10ª Região Militar'); "
            "vazio se não tiver certeza. Fale em uma ou duas frases."
        ),
    )
    async def rota(self, destino: str, origem: str = "", modo: str = "carro", nome_oficial: str = "") -> str:
        mode = MODES.get(str(modo or "carro").strip().lower(), "carro")
        try:
            result = await _post_local("/api/ui/rota", {"destination": destino, "origin": origem, "mode": mode, "official": nome_oficial}, read_timeout=70.0)
        except (httpx.HTTPError, TimeoutError, ValueError):
            return UNAVAILABLE
        if not result.get("ok"):
            return str(result.get("message") or UNAVAILABLE)
        result.pop("ok", None)
        return json.dumps(result, ensure_ascii=False)


__all__ = ["RouteToolsMixin"]
