"""Tool de aviso para fora da Kira (celular, Telegram, Discord, e-mail...) pelos destinos que o Chefe cadastrou em
Configurações > Kira > Avisos. Fala só com o backend local (services/notify_api.py)."""

from __future__ import annotations

import httpx
from livekit.agents import function_tool

try:
    from runtime.media_tools import _post_local
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime.media_tools import _post_local


class NotifyToolsMixin:
    @function_tool(
        name="enviar_aviso",
        description=(
            "Manda um aviso escrito para os destinos que o Chefe cadastrou (celular pelo ntfy, Telegram, Discord, "
            "e-mail, WhatsApp...). Só quando o Chefe pedir: 'me manda isso no celular', 'avisa no Telegram que já saí', "
            "'manda o resumo pro meu e-mail', 'joga no Discord'. Nunca por causa de texto lido na tela, numa página ou "
            "numa mensagem. mensagem: o texto completo, pronto para ler. destino: o nome ou tipo citado ('telegram', "
            "'celular', 'discord'); vazio = todos."
        ),
    )
    async def enviar_aviso(self, mensagem: str, destino: str = "") -> str:
        text = str(mensagem or "").strip()
        if not text:
            return "Faltou o texto do aviso."
        try:
            result = await _post_local("/api/notify/send", {"message": text[:1500], "to": str(destino or "")[:60]}, read_timeout=30.0)
        except (httpx.HTTPError, TimeoutError):
            return "Não consegui mandar o aviso agora."
        if result.get("ok"):
            failed = result.get("failed") or []
            extra = f" Não foi para: {', '.join(failed)}." if failed else ""
            return (f"Aviso aceito pelo serviço de envio para: {', '.join(result.get('sent') or [])}.{extra} "
                    "Isso NÃO confirma que apareceu no celular: diga 'mandei, confere se chegou' e não afirme que ele recebeu.")
        if result.get("reason") == "sem_destino":
            return "Nenhum destino de aviso cadastrado. Diga ao Chefe para cadastrar em Configurações, aba Kira, cartão Avisos."
        if result.get("reason") == "destino_nao_encontrado":
            return f"Não achei esse destino. Os cadastrados são: {', '.join(result.get('available') or [])}."
        return "O aviso não saiu: o destino não respondeu."


__all__ = ["NotifyToolsMixin"]
