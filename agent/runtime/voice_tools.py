"""Ferramenta 'voz' (2026-11-05): trocar, voltar e explicar a voz da Kira (services/voice_profile.py)."""

from __future__ import annotations

from livekit.agents import function_tool

from services import voice_profile

HOW_IT_WORKS = (
    "Hoje minha voz sai direto do Gemini (voz nativa), por isso respondo rápido. Posso trocar entre as vozes dele "
    f"({', '.join(voice_profile.GEMINI_VOICES)}) e voltar para a minha original quando você pedir; a original fica sempre guardada. "
    "Clonar uma voz (VoiceStudio): você grava uns 10 a 30 segundos de alguém falando limpo, o VoiceStudio cria a voz e eu "
    "falaria com ela. Ele precisa de placa de vídeo com uns 5 GB livres; esta GTX 550 Ti tem 1 GB e não é aceita, e no "
    "processador ficaria de 10 a 50 vezes mais lento. Dá para rodar o VoiceStudio em outro PC e eu chamar pela rede, "
    "com uns 2 a 4 segundos a mais por resposta."
)


class VoiceToolsMixin:
    @function_tool(
        name="voz",
        description=(
            "Voz da Kira. acao: trocar (nome = voz do Gemini como Kore, Aoede, Puck... ou voz clonada registrada) | "
            "voltar (volta para a voz original, sempre guardada: 'volta pra sua voz') | explicar (como funciona e como "
            "clonar uma voz com o VoiceStudio, para ensinar o Chefe) | registrar_clone (nome + url do servidor VoiceStudio)."
        ),
    )
    async def voz(self, acao: str, nome: str = "", url: str = "") -> str:
        acao = str(acao or "").strip().lower()
        if acao == "trocar":
            return voice_profile.switch(nome)
        if acao == "voltar":
            return voice_profile.restore()
        if acao == "registrar_clone" and nome and url:
            return voice_profile.register_clone(nome, url)
        return HOW_IT_WORKS
