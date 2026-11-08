from __future__ import annotations

import logging
import os
from typing import Any

from livekit.plugins.google.beta import GeminiTTS

if __package__ and __package__.startswith("agent"):
    from agent.gemini_key_manager import activate_current_gemini_api_key
else:  # pragma: no cover - suporte a execucao direta em agent/
    from gemini_key_manager import activate_current_gemini_api_key
from resilience import log_exception_safely
from .realtime import resolve_proactive_tts_provider, should_use_fallback_tts_for_proactive


DEFAULT_KIRA_TTS_MODEL = "gemini-2.5-flash-preview-tts"
DEFAULT_KIRA_TTS_VOICE = "Kore"
DEFAULT_KIRA_TTS_INSTRUCTIONS = (
    "Fale o texto com naturalidade em portugues do Brasil, "
    "sem adicionar nem omitir palavras."
)


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _normalize_voice_name(voice_name: str) -> str:
    normalized = str(voice_name or "").strip()
    if not normalized:
        return DEFAULT_KIRA_TTS_VOICE
    return normalized[:1].upper() + normalized[1:].lower()


def build_session_tts(*, logger: Any) -> object | None:
    if not should_use_fallback_tts_for_proactive():
        logger.info("Fallback TTS proativo desativado por configuracao; session.say nao usara TTS separado.")
        return None

    provider = resolve_proactive_tts_provider()
    if provider != "google":
        logger.warning(
            "Provider de TTS proativo '%s' ainda nao e suportado; session.say usara fallback visual.",
            provider,
        )
        return None

    model = os.getenv("KIRA_TTS_MODEL", DEFAULT_KIRA_TTS_MODEL).strip() or DEFAULT_KIRA_TTS_MODEL
    voice_name = _normalize_voice_name(
        os.getenv("KIRA_TTS_VOICE", DEFAULT_KIRA_TTS_VOICE)
    )
    instructions = (
        os.getenv("KIRA_TTS_INSTRUCTIONS", DEFAULT_KIRA_TTS_INSTRUCTIONS).strip()
        or DEFAULT_KIRA_TTS_INSTRUCTIONS
    )
    api_key, key_index = activate_current_gemini_api_key(
        logger_override=logger,
        reason="build_session_tts",
        key_type="tts",
        update_env=False,
    )

    try:
        tts = GeminiTTS(
            model=model,
            voice_name=voice_name,
            instructions=instructions,
            api_key=api_key,
        )
    except Exception as exc:
        log_exception_safely(
            logger,
            f"Falha ao iniciar TTS dedicado model={model} voice={voice_name}; session.say ficara em fallback.",
            exc,
            level=logging.WARNING,
        )
        return None

    logger.info(
        "TTS proativo configurado provider=%s model=%s voice=%s.",
        provider,
        model,
        voice_name,
    )
    if key_index is not None:
        logger.info("[TTS][KEY_MANAGER] Sessão TTS criada com chave índice=%s.", key_index)
    return tts
