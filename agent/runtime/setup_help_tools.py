"""Tool de ajuda de configuração: a Kira explica como conectar Agenda, Gmail e Instagram (services/setup_help.py, o mesmo
texto das Configurações)."""

from __future__ import annotations

from livekit.agents import function_tool

from services.setup_help import as_text


class SetupHelpToolsMixin:
    @function_tool(
        name="como_configurar",
        description=(
            "Passo a passo para configurar/conectar recursos da Kira: assunto 'google' (Agenda e Gmail: credencial no "
            "Google Cloud, onde colar na Kira, autorizar) ou 'instagram'. Use quando perguntarem como conectar, onde pegar "
            "a API/código/chave, ou quando agenda/e-mail disserem que falta a credencial. Explique os passos com calma, "
            "um ou dois por vez, e diga que o passo a passo também está em Configurações > Kira > Recursos."
        ),
    )
    async def como_configurar(self, assunto: str) -> str:
        return as_text(assunto)


__all__ = ["SetupHelpToolsMixin"]
