"""Áudio da Kira no WhatsApp (2026-11-06): texto -> voz atual dela (TTS do Gemini) -> OGG/Opus de mensagem de voz.

Política (prioridade áudio, revezando com texto): a 1ª mensagem de uma conversa vai escrita (com "Kira:", para a
pessoa saber quem é); link, número, e-mail, lista ou texto longo vão escritos; nunca dois áudios longos seguidos.
Se o TTS falhar, quem chama manda o texto.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FFMPEG = ROOT / "bin" / "ffmpeg" / "ffmpeg.exe"
OUT_DIR = Path(tempfile.gettempdir()) / "kira_whatsapp_voz"
TTS_MODELS = ("gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts")
MAX_AUDIO_CHARS = 420  # ~30 s de fala; maior que isso vai escrito
LONG_CHARS = 220
_TEXT_ONLY = re.compile(r"https?://|www\.|@\w|\d{4,}|\n\s*[-*•\d]", re.IGNORECASE)


def wants_audio(text: str, *, first_message: bool, last_was_long_audio: bool) -> bool:
    """Áudio por padrão; escrito quando ler é melhor que ouvir."""
    if first_message or not text or len(text) > MAX_AUDIO_CHARS or _TEXT_ONLY.search(text):
        return False
    return not (last_was_long_audio and len(text) > LONG_CHARS)


def current_voice() -> str:
    try:
        from services.voice_profile import gemini_voice
        chosen = gemini_voice()
    except Exception:  # noqa: BLE001 - perfil quebrado: voz do .env
        chosen = ""
    return chosen or os.getenv("GEMINI_REALTIME_VOICE", "").strip() or "Kore"


def to_ogg(pcm: bytes, rate: int = 24000) -> Path:
    """PCM 16 bits mono -> OGG/Opus (formato de mensagem de voz do WhatsApp)."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"kira_{uuid.uuid4().hex[:10]}.ogg"
    cmd = [str(FFMPEG if FFMPEG.exists() else "ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(rate),
           "-ac", "1", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k", "-application", "voip", str(out)]
    subprocess.run(cmd, input=pcm, check=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return out


def synthesize(text: str, api_key: str, voice: str | None = None, timeout_ms: int = 20000) -> Path:
    """Fala o texto com a voz da Kira e devolve o .ogg (lança exceção se não der: quem chama manda escrito)."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=timeout_ms))
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice or current_voice())), language_code="pt-BR"),
    )
    last: Exception | None = None
    for model in TTS_MODELS:
        try:
            response = client.models.generate_content(model=model, contents=text, config=config)
            part = response.candidates[0].content.parts[0].inline_data
            rate = int((re.search(r"rate=(\d+)", part.mime_type or "") or [0, 24000])[1])
            return to_ogg(part.data, rate)
        except Exception as exc:  # noqa: BLE001 - modelo fora ou sem cota: tenta o próximo
            last = exc
    raise RuntimeError(f"TTS indisponível: {type(last).__name__}") from last


__all__ = ["wants_audio", "current_voice", "synthesize", "to_ogg"]
