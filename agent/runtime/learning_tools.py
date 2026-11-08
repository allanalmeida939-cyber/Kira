"""Aprendizado falado (Fase 1D, 2026-11-07).

- aprender_giria: o Chefe explicou uma gíria/palavra -> guarda (significado, exemplo) e a Kira passa a usar às vezes.
- aprender_licao: a Kira errou e o Chefe ensinou o jeito -> lição por contexto na memória de experiência
  (services.experience_memory.record_correction; o memory_retriever já devolve a lição quando o assunto volta).
"""

from __future__ import annotations

import asyncio

from livekit.agents import function_tool


class LearningToolsMixin:
    @function_tool(
        name="aprender_giria",
        description=(
            "Salva uma gíria/palavra que o Chefe explicou. Quando ele usar uma gíria que você REALMENTE não conhece, "
            "pergunte o que significa (no máximo uma pergunta dessas por conversa; nunca pergunte gíria comum) e, "
            "quando ele explicar, chame isto com palavra, significado e um exemplo de uso. Depois use às vezes."
        ),
    )
    async def aprender_giria(self, palavra: str, significado: str, exemplo: str = "") -> str:
        learning = getattr(self, "_learning", None)
        try:
            if learning is not None:
                learning.learn_slang(palavra, significado, exemplo)
            else:
                from learning.vocabulary_learner import VocabularyLearner
                VocabularyLearner().learn_slang(palavra, significado, exemplo)
        except Exception as exc:  # noqa: BLE001
            return f"Não salvei ({exc})."
        return f"Aprendi: '{palavra}' = {significado}. Use de vez em quando, sem forçar."

    @function_tool(
        name="aprender_licao",
        description=(
            "Grava uma lição de AÇÃO: você errou e o Chefe ensinou o jeito certo (ex.: contexto='pausar Netflix na TV', "
            "licao='tem que apertar o botão duas vezes'). Na próxima vez nesse contexto, a lição volta para você usar."
        ),
    )
    async def aprender_licao(self, contexto: str, licao: str) -> str:
        try:
            from services.experience_memory import get_experience_memory
            result = await asyncio.to_thread(get_experience_memory().record_correction,
                                             correction=licao, goal=contexto, source="voice_lesson")
        except Exception as exc:  # noqa: BLE001
            return f"Não consegui gravar a lição ({type(exc).__name__})."
        if not result.get("ok", True):
            return "Não gravei: parece ter dado sensível."
        return f"Lição gravada para '{contexto}': {licao}. Da próxima vez faço assim."


__all__ = ["LearningToolsMixin"]
