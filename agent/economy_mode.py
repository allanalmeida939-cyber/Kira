from __future__ import annotations

import logging
from datetime import timedelta

from time_utils import now_brasilia


logger = logging.getLogger(__name__)


class EconomyMode:
    """
    Reduz a proatividade autonoma quando o usuario esta inativo.
    """

    INACTIVITY_THRESHOLD_MINUTES = 30
    DEEP_INACTIVITY_THRESHOLD_MINUTES = 120

    def __init__(self) -> None:
        self.last_user_interaction = now_brasilia()
        self.is_economy = False
        self.is_deep_economy = False

    def register_user_interaction(self) -> None:
        self.last_user_interaction = now_brasilia()
        if self.is_economy or self.is_deep_economy:
            logger.info("[ECONOMY] Usuario voltou; saindo do modo economia.")
        self.is_economy = False
        self.is_deep_economy = False

    def check_and_update(self) -> None:
        now = now_brasilia()
        elapsed = now - self.last_user_interaction
        if elapsed >= timedelta(minutes=self.DEEP_INACTIVITY_THRESHOLD_MINUTES):
            if not self.is_deep_economy:
                logger.warning("[ECONOMY] Deep economy ativado minutos_inativo=%.1f", elapsed.total_seconds() / 60.0)
            self.is_deep_economy = True
            self.is_economy = True
            return
        if elapsed >= timedelta(minutes=self.INACTIVITY_THRESHOLD_MINUTES):
            if not self.is_economy:
                logger.info("[ECONOMY] Economia ativada minutos_inativo=%.1f", elapsed.total_seconds() / 60.0)
            self.is_economy = True
            self.is_deep_economy = False
            return
        self.is_economy = False
        self.is_deep_economy = False

    def should_block_proactive(self) -> bool:
        self.check_and_update()
        return self.is_deep_economy

    def should_reduce_proactive(self) -> bool:
        self.check_and_update()
        return self.is_economy

    def get_status(self) -> dict[str, object]:
        self.check_and_update()
        elapsed = now_brasilia() - self.last_user_interaction
        return {
            "is_economy": self.is_economy,
            "is_deep_economy": self.is_deep_economy,
            "minutes_inactive": round(elapsed.total_seconds() / 60.0, 1),
            "last_interaction": self.last_user_interaction.isoformat(),
        }
