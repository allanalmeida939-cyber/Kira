from pathlib import Path

_ROOT_SERVICES_DIR = Path(__file__).resolve().parents[2] / "services"
if _ROOT_SERVICES_DIR.exists():
    _root_services_path = str(_ROOT_SERVICES_DIR)
    if _root_services_path not in __path__:
        __path__.append(_root_services_path)

from .notifier_service import NotifierService
from .runtime_services import (
    get_notifier_service,
    get_session_factory,
    get_session_registry,
    init_runtime_services,
)
from .session_registry import ActiveSessionHandle, SessionRegistry

__all__ = [
    "ActiveSessionHandle",
    "NotifierService",
    "SessionRegistry",
    "get_notifier_service",
    "get_session_factory",
    "get_session_registry",
    "init_runtime_services",
]
