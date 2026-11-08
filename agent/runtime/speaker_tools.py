"""Ferramentas de reconhecimento de voz para o Gemini: cadastrar quem está falando e dizer quem a Kira conhece."""

from __future__ import annotations

from livekit.agents import function_tool


def _session():
    try:
        from core.realtime import active_session
    except ImportError:  # pragma: no cover - execução como pacote agent.*
        from agent.core.realtime import active_session
    return active_session()


class SpeakerToolsMixin:
    @function_tool(
        name="registrar_voz",
        description=(
            "Começa a gravar a voz de quem está falando, com o nome dito, para a Kira reconhecer depois: uma barra "
            "na tela enche enquanto a pessoa fala e, cheia, a voz é salva. Use na hora em que a pessoa pedir "
            "('cadastra minha voz, sou o Chefe') ou disser quem é depois de você perguntar ('sou a Maria'), ANTES de "
            "falar qualquer coisa: a gravação só começa com esta chamada. Chame uma vez só: o resultado chega depois "
            "numa nota do sistema dizendo se a voz FOI SALVA ou NÃO foi salva. nome: só o primeiro nome."
        ),
    )
    async def registrar_voz(self, nome: str) -> str:
        session = _session()
        start = getattr(session, "start_voice_enrollment", None)
        if not callable(start):
            return "O reconhecimento de voz não está disponível agora."
        return start(nome)

    @function_tool(
        name="vozes_cadastradas",
        description="Diz de quem a Kira conhece a voz e quem pode dar comandos: 'de quem você conhece a voz?', 'você me reconhece?'.",
    )
    async def vozes_cadastradas(self) -> str:
        session = _session()
        guard = getattr(session, "speaker_guard", None)
        if guard is None:
            return "O reconhecimento de voz não está disponível agora."
        verdict = guard.current()
        now = f" Quem falou por último: {verdict.name or 'voz desconhecida'}." if verdict is not None else ""
        return guard.known_voices() + now


__all__ = ["SpeakerToolsMixin"]
