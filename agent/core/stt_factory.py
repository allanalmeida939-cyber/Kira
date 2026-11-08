from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from livekit.plugins import deepgram, google

from .realtime import resolve_console_stt_provider


DEFAULT_CONSOLE_STT_SAMPLE_RATE = max(
    int(os.getenv("KIRA_CONSOLE_STT_SAMPLE_RATE", "24000")),
    8000,
)


@dataclass(frozen=True)
class SessionSTTConfig:
    provider: str
    stt: object


def build_session_stt(*, logger: logging.Logger) -> SessionSTTConfig | None:
    provider = resolve_console_stt_provider()
    providers = _candidate_providers(provider)
    last_error: Exception | None = None

    for name in providers:
        try:
            if name == "google":
                if not _google_stt_looks_configured():
                    raise ValueError(_google_stt_requirements_message())
                stt = google.STT(
                    languages="pt-BR",
                    detect_language=False,
                    interim_results=True,
                    punctuate=True,
                    enable_voice_activity_events=True,
                    sample_rate=DEFAULT_CONSOLE_STT_SAMPLE_RATE,
                    credentials_file=os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or None,
                )
            elif name == "deepgram":
                if not _deepgram_stt_looks_configured():
                    raise ValueError(
                        "DEEPGRAM_API_KEY nao esta definido para o STT do console."
                    )
                stt = deepgram.STT(
                    language="pt-BR",
                    detect_language=False,
                    interim_results=True,
                    punctuate=True,
                    vad_events=True,
                    endpointing_ms=250,
                    sample_rate=DEFAULT_CONSOLE_STT_SAMPLE_RATE,
                )
            else:
                logger.warning("Provider de STT do console ignorado porque nao eh suportado: %s", name)
                continue
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Falha ao configurar STT do console provider=%s erro=%s",
                name,
                exc,
            )
            continue

        logger.info(
            "STT do console configurado provider=%s sample_rate=%s",
            name,
            DEFAULT_CONSOLE_STT_SAMPLE_RATE,
        )
        return SessionSTTConfig(provider=name, stt=stt)

    if last_error is not None:
        logger.warning(
            "Nenhum provider de STT do console ficou disponivel. Ultimo erro=%s",
            last_error,
        )
    else:
        logger.warning("Nenhum provider de STT do console foi habilitado.")
    return None


def _candidate_providers(provider: str) -> list[str]:
    normalized = str(provider or "").strip().lower()
    if normalized in {"google", "deepgram"}:
        return [normalized]
    has_deepgram = _deepgram_stt_looks_configured()
    has_google = _google_stt_looks_configured()

    if has_deepgram and not has_google:
        return ["deepgram", "google"]
    if has_google and not has_deepgram:
        return ["google", "deepgram"]
    if has_deepgram and has_google:
        return ["deepgram", "google"]
    return ["deepgram", "google"]


def _google_stt_looks_configured() -> bool:
    credentials_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    return bool(credentials_file)


def _deepgram_stt_looks_configured() -> bool:
    return bool(os.getenv("DEEPGRAM_API_KEY", "").strip())


def _google_stt_requirements_message() -> str:
    if os.getenv("GOOGLE_API_KEY", "").strip():
        return (
            "GOOGLE_API_KEY serve para o Gemini API, mas nao autentica o livekit.plugins.google.STT. "
            "Para Google STT no console, configure GOOGLE_APPLICATION_CREDENTIALS com um JSON do Google Cloud Speech."
        )
    return (
        "GOOGLE_APPLICATION_CREDENTIALS nao esta definido para o Google STT do console."
    )
