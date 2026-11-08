from __future__ import annotations

import logging
import os


RATE_LIMIT_FRAGMENTS = (
    "429",
    "resource_exhausted",
    "quota exceeded",
    "quota",
    "rate limit",
    "too many requests",
)
TTS_MODEL_MISSING_FRAGMENTS = (
    "without a tts model",
    "tts model",
)


def env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def is_debug_enabled() -> bool:
    return env_flag("KIRA_DEBUG", default=False)


def exception_text(error: BaseException | object) -> str:
    return " ".join(str(error or "").strip().split()).lower()


def is_rate_limit_error(error: BaseException | object) -> bool:
    message = exception_text(error)
    return any(fragment in message for fragment in RATE_LIMIT_FRAGMENTS)


def is_missing_tts_model_error(error: BaseException | object) -> bool:
    message = exception_text(error)
    return any(fragment in message for fragment in TTS_MODEL_MISSING_FRAGMENTS)


def log_exception_safely(
    logger: logging.Logger,
    message: str,
    error: BaseException | object,
    *,
    level: int = logging.WARNING,
) -> None:
    if is_debug_enabled():
        logger.log(level, "%s: %s", message, error, exc_info=isinstance(error, BaseException))
        return
    logger.log(level, "%s: %s", message, error)
