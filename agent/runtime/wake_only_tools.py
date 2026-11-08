"""Ferramenta 'modo_economia' (2026-11-07): liga/desliga o modo "só responde quando chamada" (estilo Alexa)."""

from __future__ import annotations

from livekit.agents import function_tool

from services import wake_only_mode


class WakeOnlyToolsMixin:
    @function_tool(
        name="modo_economia",
        description=(
            "Liga ou desliga o modo 'só responde quando chamada'. Use acao='ligar' quando o Chefe disser 'entra no modo "
            "economia' ou 'só responde quando eu te chamar'; acao='desligar' para 'sai do modo economia' ou 'pode "
            "conversar normal'; acao='ver' para saber o estado. Ligado: continue ouvindo, mas NÃO responda nem aja em "
            "falas sem uma palavra de ativação (kira, oi, ei, acorda, tá aí, escuta, me ajuda, bom dia...)."
        ),
    )
    async def modo_economia(self, acao: str = "ver") -> str:
        acao = str(acao or "ver").strip().lower()
        if acao in {"ligar", "ativar", "on"}:
            wake_only_mode.set_active(True, source="ferramenta")
            return "Modo só-quando-chamada LIGADO. Confirme em uma frase curta e depois só responda quando chamada."
        if acao in {"desligar", "desativar", "off"}:
            wake_only_mode.set_active(False, source="ferramenta")
            return "Modo só-quando-chamada DESLIGADO. Conversa normal."
        return "Modo só-quando-chamada está " + ("LIGADO." if wake_only_mode.is_active() else "DESLIGADO.")
