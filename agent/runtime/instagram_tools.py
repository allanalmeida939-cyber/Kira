"""Tool do Instagram (services/instagram_api.py): novidades, mensagens, seguidores, quem não segue de volta, verificados
e busca por nome, pela conta logada no navegador da Kira. Só leitura."""

from __future__ import annotations

import json
import logging

import httpx
from livekit.agents import function_tool

try:
    from runtime.media_tools import _post_local
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime.media_tools import _post_local

logger = logging.getLogger("kira.instagram_tools")

ACTIONS = ("novidades", "mensagens", "seguidores_novos", "nao_seguem", "verificados", "buscar", "perfil", "login")
UNAVAILABLE = "Não consegui abrir o Instagram agora."


class InstagramToolsMixin:
    @function_tool(
        name="instagram",
        description=(
            "Instagram do Chefe (a conta logada no navegador da Kira), com painel em formato de celular. Use para QUALQUER "
            "pergunta sobre o Instagram dele. acao: 'novidades' ('qual a novidade do meu Instagram hoje', 'alguém me mandou "
            "mensagem hoje', 'ganhei seguidor?': seguidores que entraram e saíram, mensagens de hoje e não lidas, curtidas, "
            "comentários e reels de hoje); 'mensagens' (o mesmo, foco nas mensagens); 'seguidores_novos'; 'nao_seguem' (quem "
            "ele segue e não segue de volta); 'verificados' (contas verificadas que ele segue e que seguem ele); 'buscar' "
            "(nome obrigatório: quantas pessoas/contas têm esse nome, na busca e entre seguidores/seguindo); 'perfil' "
            "(números da conta); 'login' (abre o login do Instagram na aba Internet — o Chefe digita a senha dele lá; o "
            "Instagram não deixa a Kira entrar sozinha). Só leitura: não segue, não curte, não manda mensagem."
        ),
    )
    async def instagram(self, acao: str, nome: str = "") -> str:
        action = str(acao or "").strip().lower()
        if action not in ACTIONS:
            return f"Ação desconhecida: use {', '.join(ACTIONS)}."
        try:
            result = await _post_local("/api/ui/instagram", {"action": action, "name": str(nome or "")},
                                       read_timeout=120.0 if action in ("novidades", "mensagens", "seguidores_novos", "nao_seguem", "verificados", "buscar") else 30.0)
        except (httpx.HTTPError, TimeoutError, ValueError):
            return UNAVAILABLE
        if not result.get("ok"):
            return str(result.get("message") or UNAVAILABLE)
        result.pop("ok", None)
        return json.dumps(result, ensure_ascii=False)


__all__ = ["InstagramToolsMixin"]
