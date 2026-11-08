from __future__ import annotations

import logging
import os
from dataclasses import dataclass


DEFAULT_GEMINI_REALTIME_VOICE = "Kore"
DEFAULT_GEMINI_REALTIME_TEMPERATURE = 0.6
MIN_GEMINI_REALTIME_TEMPERATURE = 0.0
MAX_GEMINI_REALTIME_TEMPERATURE = 2.0

# O modelo de áudio nativo IGNORA speech_config.language_code (escolhe o idioma sozinho, doc do Live API);
# o que segura o sotaque é a instrução de sistema. Isto vai no topo das instruções da Kira.
PTBR_PRONUNCIATION_RULE = (
    "PRONÚNCIA: fale sempre com sotaque brasileiro (Brasil, não Portugal nem espanhol). Termine cada palavra como "
    "ela é escrita: vogal final átona 'a' soa 'a' e 'e' soa 'i' ('casa' é CA-za, nunca 'casé'; 'leite' é 'lei-tchi'). "
    "Não troque nem acentue a última sílaba, não alongue o fim da frase."
)


@dataclass(frozen=True)
class GeminiRealtimeConfig:
    voice: str
    temperature: float
    model: str


def _env_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def resolve_gemini_realtime_voice() -> str:
    try:  # voz escolhida pelo Chefe ("Kira, troca a voz pra Kore"); vazio = a do .env
        from services.voice_profile import gemini_voice
        chosen = gemini_voice()
    except Exception:  # noqa: BLE001 - perfil quebrado nunca pode tirar a voz da Kira
        chosen = ""
    return (
        chosen
        or _env_first("GEMINI_REALTIME_VOICE", "KIRA_GEMINI_LIVE_VOICE")
        or DEFAULT_GEMINI_REALTIME_VOICE
    )


def resolve_gemini_realtime_temperature(*, logger: logging.Logger | None = None) -> float:
    raw = _env_first("GEMINI_REALTIME_TEMPERATURE", "KIRA_GEMINI_LIVE_TEMPERATURE")
    if not raw:
        return DEFAULT_GEMINI_REALTIME_TEMPERATURE
    try:
        value = float(raw)
    except (TypeError, ValueError):
        if logger is not None:
            logger.warning(
                "GEMINI_REALTIME_TEMPERATURE_INVALID value=%r default=%.1f",
                raw,
                DEFAULT_GEMINI_REALTIME_TEMPERATURE,
            )
        return DEFAULT_GEMINI_REALTIME_TEMPERATURE
    if value < MIN_GEMINI_REALTIME_TEMPERATURE or value > MAX_GEMINI_REALTIME_TEMPERATURE:
        clamped = min(max(value, MIN_GEMINI_REALTIME_TEMPERATURE), MAX_GEMINI_REALTIME_TEMPERATURE)
        if logger is not None:
            logger.warning(
                "GEMINI_REALTIME_TEMPERATURE_CLAMPED value=%s clamped=%s",
                value,
                clamped,
            )
        return clamped
    return value


def resolve_gemini_realtime_model(*, default_model: str = "") -> str:
    return _env_first("GEMINI_REALTIME_MODEL", "KIRA_GEMINI_LIVE_MODEL") or default_model


def get_gemini_realtime_config(
    *,
    default_model: str = "",
    logger: logging.Logger | None = None,
) -> GeminiRealtimeConfig:
    return GeminiRealtimeConfig(
        voice=resolve_gemini_realtime_voice(),
        temperature=resolve_gemini_realtime_temperature(logger=logger),
        model=resolve_gemini_realtime_model(default_model=default_model),
    )
