"""Tool do Cérebro (services/brain_api.py): perguntas sobre os documentos do Chefe, núcleo de memória que a Kira
mesma mantém (ideia do Letta/MemGPT) e a pasta dos documentos. A busca por significado nas conversas entra pela tool
de conversas que já existia (lembrar_contexto_diario)."""

from __future__ import annotations

import httpx
from livekit.agents import function_tool

try:
    from runtime.media_tools import _post_local
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime.media_tools import _post_local

ACTIONS = ("documentos", "ler_documentos", "anotar", "esquecer", "o_que_sabe", "pasta")
UNAVAILABLE = "O cérebro não respondeu agora."


async def ask_brain(action: str, text: str = "", *, read_timeout: float = 60.0) -> dict:
    try:
        return await _post_local("/api/brain/ask", {"action": action, "text": str(text or "")[:1000]}, read_timeout=read_timeout)
    except (httpx.HTTPError, TimeoutError):
        return {"ok": False, "summary": UNAVAILABLE}


class BrainToolsMixin:
    @function_tool(
        name="cerebro",
        description=(
            "Cérebro da Kira. acao: "
            "'documentos' (pergunta sobre os documentos do Chefe: PDFs, Word e textos da pasta Documentos\\Kira Documentos: "
            "'o que diz o contrato sobre multa?', 'qual o prazo no edital?', 'resume a apostila de física'; texto = a pergunta); "
            "'ler_documentos' ('acabei de colocar um arquivo na pasta', 'lê os documentos'); "
            "'pasta' ('abre a pasta dos documentos da Kira'); "
            "'anotar' (núcleo de memória: um fato duradouro sobre o Chefe, que vale em toda conversa. Use SOZINHA, sem "
            "perguntar, quando ele contar algo que vale a pena lembrar sempre: aniversário de alguém, nome de pessoa "
            "próxima, gosto forte, rotina, projeto, saúde que ele mesmo contou; texto = o fato em uma frase, em "
            "terceira pessoa: 'A mãe do Chefe faz aniversário em 12 de março'. Não anote o que é passageiro); "
            "'esquecer' (texto = o fato a tirar); 'o_que_sabe' ('o que você sabe sobre mim?'). "
            "Para conversas antigas use lembrar_contexto_diario."
        ),
    )
    async def cerebro(self, acao: str, texto: str = "") -> str:
        action = str(acao or "").strip().lower()
        if action not in ACTIONS:
            return "Ação desconhecida: use documentos, ler_documentos, pasta, anotar, esquecer ou o_que_sabe."
        result = await ask_brain(action, texto)
        return str(result.get("summary") or UNAVAILABLE)


__all__ = ["BrainToolsMixin", "ask_brain"]
