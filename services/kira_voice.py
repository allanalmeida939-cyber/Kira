"""Voz da Kira fora da sessão ao vivo (perfis comuns do celular): o mesmo timbre da voz do Chefe (a voz do Gemini escolhida
em GEMINI_REALTIME_VOICE, ou a do personagem ativo) pelo Gemini TTS, entregue em MP3 (uma frase ~15 KB, cabe nos dados
móveis). Antes, a tela do convidado lia as respostas com a voz robótica do navegador."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from collections import OrderedDict
from typing import Awaitable, Callable

logger = logging.getLogger("kira.voice")

MAX_CHARS = 700
CACHE_ITEMS = 40
PCM_RATE = 24000
# o jeito de falar vai como instrução de estilo (o modelo de voz não lê essa parte em voz alta)
STYLE = "Fale em português do Brasil, com leve sotaque cearense, de jeito natural, expressivo e descontraído: "


def clean_for_voice(text: str) -> str:
    """Tira o que não se fala (markdown, links) e limita o tamanho."""
    plain = " ".join(str(text or "").replace("*", " ").replace("#", " ").replace("_", " ").split())
    words = [word for word in plain.split(" ") if not word.startswith(("http://", "https://"))]
    return " ".join(words)[:MAX_CHARS].strip()


def pcm_to_mp3(pcm: bytes, rate: int = PCM_RATE) -> bytes:
    """PCM 16 bits mono -> MP3 48 kbps (PyAV, que já vem com o agente)."""
    import av
    import numpy as np

    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2").reshape(1, -1)
    out = io.BytesIO()
    with av.open(out, "w", format="mp3") as box:
        stream = box.add_stream("libmp3lame", rate=rate)
        stream.bit_rate = 48000
        frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
        frame.sample_rate = rate
        resampler = av.AudioResampler(format=stream.codec_context.format.name, layout="mono", rate=rate)
        for piece in resampler.resample(frame) + resampler.resample(None):
            for packet in stream.encode(piece):
                box.mux(packet)
        for packet in stream.encode(None):
            box.mux(packet)
    return out.getvalue()


class KiraVoice:
    """`synth(texto, voz)` devolve PCM 24 kHz (Gemini TTS); `voice()` diz qual voz usar agora."""

    def __init__(self, synth: Callable[[str, str], Awaitable[bytes]], voice: Callable[[], str]) -> None:
        self._synth = synth
        self._voice = voice
        self._cache: OrderedDict[str, bytes] = OrderedDict()

    async def speak(self, text: str) -> bytes:
        line = clean_for_voice(text)
        if not line:
            raise ValueError("nada para falar")
        voice = str(self._voice() or "Kore")
        key = hashlib.sha1(f"{voice}|{line}".encode("utf-8")).hexdigest()
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        pcm = await self._synth(STYLE + line, voice)
        audio = await asyncio.to_thread(pcm_to_mp3, pcm)
        self._cache[key] = audio
        while len(self._cache) > CACHE_ITEMS:
            self._cache.popitem(last=False)
        logger.info("KIRA_VOICE_TTS voice=%s chars=%d bytes=%d", voice, len(line), len(audio))
        return audio


__all__ = ["KiraVoice", "clean_for_voice", "pcm_to_mp3"]
