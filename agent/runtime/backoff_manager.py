from __future__ import annotations

import logging
import re
import time


logger = logging.getLogger(__name__)


class BackoffManager:
    """Gerencia backoff progressivo para evitar martelar APIs."""

    BACKOFF_SCHEDULE = [5, 15, 60, 300]

    def get_wait_time(self, attempt: int, retry_after: int | None = None) -> int:
        """
        Calcula tempo de espera progressivo.

        attempt:
            Numero da tentativa (1, 2, 3...)
        retry_after:
            Retry-After da API, quando disponivel.
        """
        normalized_attempt = max(int(attempt or 1), 1)
        if normalized_attempt == 1:
            logger.info("[BACKOFF] Tentativa 1 -> espera 0s (primeira nao espera)")
            return 0

        if retry_after is not None and int(retry_after) > 0:
            wait_time = int(retry_after)
            logger.info("[BACKOFF] API solicitou espera de %ds", wait_time)
            return wait_time

        index = min(normalized_attempt - 2, len(self.BACKOFF_SCHEDULE) - 1)
        wait_time = int(self.BACKOFF_SCHEDULE[index])
        logger.info("[BACKOFF] Tentativa %d -> espera %ds", normalized_attempt, wait_time)
        return wait_time

    def wait_before_retry(self, attempt: int, retry_after: int | None = None) -> int:
        """Espera antes de uma nova tentativa e retorna o tempo aguardado."""
        wait_time = self.get_wait_time(attempt, retry_after=retry_after)
        if wait_time <= 0:
            return 0

        logger.warning(
            "[BACKOFF] Aguardando %ds antes da tentativa %d...",
            wait_time,
            max(int(attempt or 1), 1),
        )
        time.sleep(wait_time)
        return wait_time

    @staticmethod
    def extract_retry_after(exception: Exception | str | object) -> int | None:
        """Extrai Retry-After de mensagens de erro."""
        exc_text = str(exception or "")
        if not exc_text:
            return None

        match = re.search(r"Retry-After[:=\s]+(\d+)", exc_text, re.IGNORECASE)
        if match:
            retry_after = int(match.group(1))
            logger.info("[BACKOFF] Retry-After detectado na resposta: %ds", retry_after)
            return retry_after
        return None


__all__ = ["BackoffManager"]
