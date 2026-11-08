"""Configuracoes compartilhadas."""
from __future__ import annotations

import os


KEY_COOLDOWN_QUOTA_MINUTES = max(
    int(os.getenv("KIRA_KEY_COOLDOWN_QUOTA_MINUTES", "30")),
    1,
)
KEY_COOLDOWN_DAILY_MINUTES = max(
    int(os.getenv("KIRA_KEY_COOLDOWN_DAILY_MINUTES", "1440")),
    KEY_COOLDOWN_QUOTA_MINUTES,
)
KEY_COOLDOWN_RATE_LIMIT_MINUTES = max(
    int(os.getenv("KIRA_KEY_COOLDOWN_RATE_LIMIT_MINUTES", "1")),
    1,
)


def detect_quota_type(error_message: str) -> str:
    """Detecta o tipo principal de erro de quota."""
    message = str(error_message or "").strip().lower()
    if not message:
        return "quota"
    if "please retry in" in message or "retry in" in message:
        return "rate_limit"
    if "daily" in message or "per_day" in message or "per day" in message:
        return "daily"
    if "minute" in message or "per_minute" in message or "per minute" in message or "per_min" in message:
        return "rate_limit"
    return "quota"


def get_cooldown_minutes(quota_type: str) -> int:
    """Retorna o cooldown configurado para o tipo de quota."""
    normalized = str(quota_type or "").strip().lower()
    if normalized == "daily":
        return KEY_COOLDOWN_DAILY_MINUTES
    if normalized == "rate_limit":
        return KEY_COOLDOWN_RATE_LIMIT_MINUTES
    return KEY_COOLDOWN_QUOTA_MINUTES
