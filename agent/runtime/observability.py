from __future__ import annotations

import logging
import os


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def should_enable_aiomonitor(runtime_mode: str) -> bool:
    default_enabled = runtime_mode in {"console", "dev"}
    return _env_flag("KIRA_ENABLE_AIOMONITOR", default=default_enabled)


def init_sentry(*, logger: logging.Logger) -> bool:
    dsn = str(
        os.getenv("KIRA_SENTRY_DSN")
        or os.getenv("SENTRY_DSN")
        or ""
    ).strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
    except Exception as exc:
        logger.warning("[OBS] sentry-sdk indisponivel: %s", exc)
        return False

    sentry_sdk.init(
        dsn=dsn,
        traces_sample_rate=float(os.getenv("KIRA_SENTRY_TRACES_SAMPLE_RATE", "1.0")),
        profiles_sample_rate=float(os.getenv("KIRA_SENTRY_PROFILES_SAMPLE_RATE", "1.0")),
    )
    logger.info("[OBS] Sentry inicializado.")
    return True


def maybe_start_aiomonitor(*, loop, logger: logging.Logger, enabled: bool):
    if not enabled:
        return None
    try:
        import aiomonitor
    except Exception as exc:
        logger.warning("[OBS] aiomonitor indisponivel: %s", exc)
        return None

    try:
        monitor = aiomonitor.start_monitor(loop=loop)
    except Exception as exc:
        logger.warning("[OBS] Falha ao iniciar aiomonitor: %s", exc)
        return None

    logger.info("[OBS] aiomonitor iniciado.")
    return monitor


def close_aiomonitor(handle, *, logger: logging.Logger) -> None:
    if handle is None:
        return
    close = getattr(handle, "close", None)
    if callable(close):
        try:
            close()
            logger.info("[OBS] aiomonitor encerrado.")
        except Exception as exc:
            logger.warning("[OBS] Falha ao encerrar aiomonitor: %s", exc)
        return
    exit_fn = getattr(handle, "__exit__", None)
    if callable(exit_fn):
        try:
            exit_fn(None, None, None)
            logger.info("[OBS] aiomonitor encerrado.")
        except Exception as exc:
            logger.warning("[OBS] Falha ao encerrar aiomonitor: %s", exc)
