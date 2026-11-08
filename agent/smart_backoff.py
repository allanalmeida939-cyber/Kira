from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Awaitable, Callable

from resilience import exception_text

if __package__ and __package__.startswith("agent"):
    from agent.settings import detect_quota_type
else:  # pragma: no cover - suporte a execucao direta em agent/
    from settings import detect_quota_type


logger = logging.getLogger(__name__)


class ErrorType(Enum):
    QUOTA_EXCEEDED = "quota"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    AUTH = "auth"
    SERVER = "server"
    UNKNOWN = "unknown"


def classify_error(error: BaseException | object) -> ErrorType:
    message = exception_text(error).lower()
    quota_type = detect_quota_type(message)

    if "401" in message or "403" in message or "auth" in message or "permission" in message:
        return ErrorType.AUTH
    if "500" in message or "502" in message or "503" in message or "internal" in message:
        return ErrorType.SERVER
    if "network" in message or "connection" in message or "timeout" in message or "temporarily unavailable" in message:
        return ErrorType.NETWORK
    if (
        "resource_exhausted" in message
        or "quota exceeded" in message
        or "exceeded your current quota" in message
    ):
        return ErrorType.QUOTA_EXCEEDED
    if quota_type == "rate_limit" or "too many requests" in message:
        return ErrorType.RATE_LIMIT
    if "429" in message or "resource_exhausted" in message or "quota" in message:
        return ErrorType.QUOTA_EXCEEDED
    return ErrorType.UNKNOWN


class SmartBackoff:
    """Aplica estratégias simples de backoff e recovery por tipo de erro."""

    @staticmethod
    async def handle_error(
        error: BaseException | object,
        *,
        operation_name: str,
        key_type: str = "tts",
        retry_func: Callable[[], Awaitable[object] | object] | None = None,
        key_manager=None,
    ) -> bool:
        """
        Retorna True se vale tentar novamente; False se deve abortar.
        """
        error_type = classify_error(error)
        logger.warning(
            "[BACKOFF] operation=%s error_type=%s detail=%s",
            operation_name,
            error_type.value,
            str(error)[:240],
        )

        if error_type == ErrorType.QUOTA_EXCEEDED:
            if key_manager is not None:
                try:
                    key_manager.mark_quota_exceeded(key_type, reason=str(error))
                except Exception as exc:
                    logger.warning("[BACKOFF] Falha ao marcar quota exceeded: %s", exc)
            return False

        if error_type == ErrorType.RATE_LIMIT:
            await asyncio.sleep(5)
            if retry_func is None:
                return True
            try:
                result = retry_func()
                if asyncio.iscoroutine(result):
                    await result
                return True
            except Exception as exc:
                logger.warning("[BACKOFF] Retry apos rate_limit falhou: %s", exc)
                return False

        if error_type == ErrorType.NETWORK:
            for delay in (1, 2, 4):
                await asyncio.sleep(delay)
                if retry_func is None:
                    continue
                try:
                    result = retry_func()
                    if asyncio.iscoroutine(result):
                        await result
                    return True
                except Exception:
                    continue
            return False

        if error_type == ErrorType.SERVER:
            await asyncio.sleep(3)
            return True

        if error_type == ErrorType.AUTH:
            logger.critical("[BACKOFF] Erro de autenticacao em %s; intervencao manual necessaria.", operation_name)
            return False

        await asyncio.sleep(2)
        return False
