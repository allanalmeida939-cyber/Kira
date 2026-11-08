"""Logs por canal (2026-11-06): o que acontece na TV vai para logs/TV/tv_<data>.log e o que acontece no celular para
logs/Celular/celular_<data>.log. Loggers 'kira.tv' e 'kira.phone' com arquivo próprio (não duplicam no log geral).
Guarda os últimos 7 dias. Sem segredos: texto de mensagem entra resumido (preview) e passa pelo redact do log manager.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

KEEP_DAYS = int(os.getenv("KIRA_CHANNEL_LOG_DAYS", "7") or 7)
_CHANNELS = {"tv": ("TV", "tv", "kira.tv"), "celular": ("Celular", "celular", "kira.phone")}
_ready: set[str] = set()


def _root() -> Path:
    env = os.getenv("KIRA_LOG_ROOT", "").strip()
    return Path(env) if env else Path(__file__).resolve().parent.parent / "logs"


def _setup(channel: str) -> logging.Logger:
    folder, prefix, name = _CHANNELS[channel]
    logger = logging.getLogger(name)
    if channel in _ready:
        return logger
    _ready.add(channel)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # arquivo próprio; o log geral não recebe cada evento da TV/celular
    if "pytest" in sys.modules and not os.getenv("KIRA_LOG_ROOT"):
        logger.addHandler(logging.NullHandler())  # teste não grava em logs reais
        return logger
    directory = _root() / folder
    directory.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(directory / f"{prefix}_{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    cutoff = time.time() - KEEP_DAYS * 86400
    for old in directory.glob(f"{prefix}_*.log"):  # retenção por dia
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)
    return logger


def preview(text: object, size: int = 80) -> str:
    raw = " ".join(str(text or "").split())
    try:
        from services.kira_log_manager import redact_sensitive
        raw = str(redact_sensitive(raw))
    except Exception:  # noqa: BLE001 - log nunca derruba nada
        pass
    return raw[:size] + ("…" if len(raw) > size else "")


def _log(channel: str, event: str, level: int = logging.INFO, **fields: object) -> None:
    try:
        parts = " ".join(f"{k}={str(v).replace(' ', '_')[:120]}" for k, v in fields.items() if v is not None and v != "")
        _setup(channel).log(level, f"{event} {parts}".strip())
    except Exception:  # noqa: BLE001
        pass


def tv_log(event: str, level: int = logging.INFO, **fields: object) -> None:
    _log("tv", event, level, **fields)


def phone_log(event: str, level: int = logging.INFO, **fields: object) -> None:
    _log("celular", event, level, **fields)


def tail(channel: str, lines: int = 200) -> list[str]:
    """Últimas linhas do log do canal (para a aba de logs / a Kira olhar o que houve)."""
    if channel not in _CHANNELS:
        return []
    folder, prefix, _ = _CHANNELS[channel]
    files = sorted((_root() / folder).glob(f"{prefix}_*.log"))
    if not files:
        return []
    return files[-1].read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, min(lines, 1000)):]
