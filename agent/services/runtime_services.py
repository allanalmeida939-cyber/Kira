from __future__ import annotations

from .notifier_service import NotifierService
from .persistence import get_session_factory, init_database
from .session_registry import SessionRegistry


_SESSION_REGISTRY: SessionRegistry | None = None
_NOTIFIER_SERVICE: NotifierService | None = None


def init_runtime_services() -> None:
    global _SESSION_REGISTRY, _NOTIFIER_SERVICE
    init_database()
    if _SESSION_REGISTRY is None:
        _SESSION_REGISTRY = SessionRegistry()
    if _NOTIFIER_SERVICE is None:
        _NOTIFIER_SERVICE = NotifierService(_SESSION_REGISTRY)


def get_session_registry() -> SessionRegistry:
    init_runtime_services()
    assert _SESSION_REGISTRY is not None
    return _SESSION_REGISTRY


def get_notifier_service() -> NotifierService:
    init_runtime_services()
    assert _NOTIFIER_SERVICE is not None
    return _NOTIFIER_SERVICE


__all__ = [
    "get_notifier_service",
    "get_session_factory",
    "get_session_registry",
    "init_runtime_services",
]
