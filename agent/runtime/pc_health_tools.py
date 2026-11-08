"""Tool 'saude_pc' (services/pc_health.py): como está o PC e limpar a memória RAM."""

from __future__ import annotations

import json

import httpx
from livekit.agents import function_tool

try:
    from runtime.media_tools import _post_local
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime.media_tools import _post_local


class PcHealthToolsMixin:
    @function_tool(
        name="saude_pc",
        description=(
            "Saúde do computador do Chefe. acao 'status' ('como está o PC?', 'quanto de memória tem livre?', 'o que está pesando?'): "
            "memória, processador, discos, os programas que mais gastam memória e os avisos recentes (a Kira já limpa a RAM "
            "sozinha quando passa do limite). acao 'otimizar' ('limpa a memória', 'otimiza o PC'): limpa a RAM agora com o "
            "Reduce Memory dele. Responda curto, com os números que importam."
        ),
    )
    async def saude_pc(self, acao: str = "status") -> str:
        try:
            result = await _post_local("/api/ui/saude-pc", {"action": acao}, read_timeout=120.0)
        except (httpx.HTTPError, TimeoutError, ValueError):
            return "Não consegui olhar o PC agora."
        return json.dumps(result, ensure_ascii=False)


__all__ = ["PcHealthToolsMixin"]
