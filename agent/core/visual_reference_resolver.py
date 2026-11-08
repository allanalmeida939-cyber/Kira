from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re
import time
import unicodedata
from typing import Any

from .local_command_gate import LocalCommandIntent

logger = logging.getLogger("kira.app")


@dataclass(frozen=True)
class VisualReferenceResolution:
    handled: bool
    intent: LocalCommandIntent | None = None
    notice: str = ""
    source_label: str = "visual_local_command"


def _normalize(text: str | None) -> str:
    value = unicodedata.normalize("NFD", str(text or "").strip().lower())
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = re.sub(r"[^\w\s.%/-]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def has_visual_local_reference(text: str | None) -> bool:
    normalized = _normalize(text)
    if not normalized:
        return False
    return bool(
        re.search(
            r"\b(essa|esse|isso|ai|aqui|tela|aparecendo|visivel|vendo)\b",
            normalized,
        )
        and re.search(
            r"\b(abre|abrir|fecha|fechar|minimiza|maximiza|restaura|traz|frente|janela|pasta|aplicativo|app)\b",
            normalized,
        )
    )


def _window_lib():
    try:
        import pygetwindow as gw
        return gw
    except Exception as exc:
        logger.info("VISUAL_REFERENCE_FAILED reason=pygetwindow_unavailable:%s", exc)
        return None


def _active_window():
    gw = _window_lib()
    if gw is None:
        return None
    try:
        window = gw.getActiveWindow()
    except Exception as exc:
        logger.info("VISUAL_REFERENCE_FAILED reason=active_window_failed:%s", exc)
        return None
    title = str(getattr(window, "title", "") or "").strip() if window is not None else ""
    if title:
        logger.info("WINDOW_ACTIVE_DETECTED title=%s", title)
    return window if title else None


def _visible_window_titles() -> list[str]:
    gw = _window_lib()
    if gw is None:
        return []
    try:
        windows = [
            w
            for w in gw.getAllWindows()
            if str(getattr(w, "title", "") or "").strip()
            and int(getattr(w, "width", 0) or 0) > 0
            and int(getattr(w, "height", 0) or 0) > 0
        ]
    except Exception as exc:
        logger.info("VISUAL_REFERENCE_FAILED reason=list_windows_failed:%s", exc)
        return []
    logger.info("WINDOW_LIST_SNAPSHOT count=%s", len(windows))
    return [str(getattr(w, "title", "") or "").strip() for w in windows]


def _active_window_title() -> str:
    window = _active_window()
    return str(getattr(window, "title", "") or "").strip() if window is not None else ""


def _is_explorer_like(title: str) -> bool:
    normalized = _normalize(title)
    known = ("documentos", "downloads", "area de trabalho", "desktop", "explorador", "file explorer")
    if any(token in normalized for token in known):
        return True
    return bool(normalized and not any(token in normalized for token in ("kira", "chrome", "opera", "visual studio code", "bloco de notas", "calculadora")))


def _folder_target_from_title(title: str) -> str:
    normalized = _normalize(title)
    if "documentos" in normalized or "documents" in normalized:
        return "documentos"
    if "downloads" in normalized:
        return "downloads"
    if "area de trabalho" in normalized or "desktop" in normalized:
        return "desktop"
    if "kira v8" in normalized:
        return "kira v8"
    return normalized


def _known_desktop_folders() -> list[Path]:
    desktop = Path.home() / "Desktop"
    if not desktop.exists():
        return []
    try:
        return [item for item in desktop.iterdir() if item.is_dir() and not item.name.startswith(".")]
    except Exception:
        return []


def _visual_store_context(*, wait_timeout_seconds: float = 0.7) -> dict[str, Any]:
    logger.info("VISUAL_REFERENCE_RESOLVE_BEGIN")
    logger.info("VISION_FRAME_REQUESTED_FOR_LOCAL_COMMAND")
    try:
        try:
            from agent.runtime.vision_frame_store import get_vision_frame_store
        except Exception:
            from runtime.vision_frame_store import get_vision_frame_store
        store = get_vision_frame_store()
    except Exception as exc:
        logger.info("VISUAL_REFERENCE_CONTEXT_MISSING")
        logger.info("VISION_FRAME_TIMEOUT_FOR_LOCAL_COMMAND")
        logger.info("VISUAL_REFERENCE_FAILED reason=frame_store_unavailable:%s", exc)
        return {}
    if store is None:
        logger.info("VISUAL_REFERENCE_CONTEXT_MISSING")
        logger.info("VISION_FRAME_TIMEOUT_FOR_LOCAL_COMMAND")
        return {}

    deadline = time.monotonic() + max(float(wait_timeout_seconds), 0.0)
    latest = None
    while True:
        try:
            latest = store.get_latest(max_age_seconds=2.0)
        except Exception:
            latest = None
        if latest is not None:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.08)

    context = None
    try:
        getter = getattr(store, "get_light_visual_context", None)
        if callable(getter):
            context = getter(max_age_seconds=10.0)
    except Exception:
        context = None

    if latest is None:
        logger.info("VISUAL_REFERENCE_CONTEXT_MISSING")
        logger.info("VISION_FRAME_TIMEOUT_FOR_LOCAL_COMMAND")
        if context is not None:
            logger.info("VISUAL_REFERENCE_CONTEXT_AVAILABLE age_ms=%s", context.get("age_ms"))
        return {"context": context}

    age_ms = int(latest.get("age_ms") or 0)
    logger.info("VISION_FRAME_READY_FOR_LOCAL_COMMAND")
    logger.info("VISUAL_REFERENCE_CONTEXT_AVAILABLE age_ms=%s", age_ms)
    logger.info("VISION_USED_FOR_LOCAL_COMMAND")
    logger.info("VISION_CONTEXT_STAYS_INTERNAL")
    return {"frame": latest, "context": context, "age_ms": age_ms}


def _action_from_text(text: str) -> str:
    if re.search(r"\b(minimiza|minimizar)\b", text):
        return "minimize_window"
    if re.search(r"\b(maximiza|maximizar)\b", text):
        return "maximize_window"
    if re.search(r"\b(restaura|restaurar)\b", text):
        return "restore_window"
    if re.search(r"\b(fecha|fechar)\b", text):
        return "close_window"
    if re.search(r"\b(traz|frente)\b", text):
        return "focus_window"
    if re.search(r"\b(abre|abrir)\b", text):
        return "open"
    return "focus_window"


def _resolve_folder_from_context(text: str, context_text: str) -> tuple[str, str, float] | None:
    active_title = _active_window_title()
    if active_title and _is_explorer_like(active_title):
        target = _folder_target_from_title(active_title)
        logger.info("FOLDER_CURRENT_EXPLORER_CONTEXT_USED path=%s", target)
        return target, active_title, 0.86

    candidates: list[tuple[str, str, float]] = []
    for label, target in (
        ("documentos", "documentos"),
        ("documents", "documentos"),
        ("downloads", "downloads"),
        ("area de trabalho", "desktop"),
        ("desktop", "desktop"),
        ("kira v8", "kira v8"),
    ):
        if label in context_text or label in text:
            candidates.append((target, label, 0.82))
    for folder in _known_desktop_folders():
        name = _normalize(folder.name)
        if name and (name in context_text or name in text):
            candidates.append((f"path:{folder}", folder.name, 0.9))

    unique: dict[str, tuple[str, str, float]] = {item[0]: item for item in candidates}
    if len(unique) == 1:
        return next(iter(unique.values()))
    if len(unique) > 1:
        logger.info("VISUAL_REFERENCE_AMBIGUOUS candidates=%s", [item[1] for item in unique.values()])
    return None


def _resolve_app_from_context(text: str, context_text: str) -> tuple[str, str, float] | None:
    aliases = {
        "bloco de notas": "notepad",
        "notepad": "notepad",
        "calculadora": "calculator",
        "calculator": "calculator",
        "explorador": "explorer",
        "chrome": "chrome",
        "opera": "opera",
    }
    candidates: list[tuple[str, str, float]] = []
    combined = f"{text} {context_text}"
    for alias, app in aliases.items():
        if alias in combined:
            candidates.append((app, alias, 0.86))
    unique: dict[str, tuple[str, str, float]] = {item[0]: item for item in candidates}
    if len(unique) == 1:
        return next(iter(unique.values()))
    if len(unique) > 1:
        logger.info("VISUAL_REFERENCE_AMBIGUOUS candidates=%s", [item[1] for item in unique.values()])
    return None


def resolve_visual_local_command(text: str | None) -> VisualReferenceResolution:
    raw = str(text or "").strip()
    normalized = _normalize(raw)
    if not has_visual_local_reference(normalized):
        logger.info("VISION_NOT_USED reason=no_visual_reference")
        return VisualReferenceResolution(False)

    logger.info("VISUAL_LOCAL_COMMAND_REFERENCE_DETECTED text=%s", raw[:180])
    visual_payload = _visual_store_context(wait_timeout_seconds=0.7)
    context = dict(visual_payload.get("context") or {})
    context_text = _normalize(context.get("summary") or "")
    action = _action_from_text(normalized)
    is_folder = "pasta" in normalized
    is_app = "aplicativo" in normalized or re.search(r"\bapp\b", normalized)
    is_window = "janela" in normalized or action in {"minimize_window", "maximize_window", "restore_window", "close_window", "focus_window"}

    if is_folder:
        logger.info("FOLDER_VISUAL_REFERENCE_DETECTED")
        resolved = _resolve_folder_from_context(normalized, context_text)
        if resolved is None:
            logger.info("VISUAL_REFERENCE_FAILED reason=folder_ambiguous_or_missing")
            return VisualReferenceResolution(True, notice="Nao consegui identificar qual pasta. Qual delas voce quer?")
        target, label, confidence = resolved
        logger.info("FOLDER_VISUAL_TARGET_RESOLVED name=%s path=%s confidence=%.2f", label, target, confidence)
        logger.info("VISUAL_REFERENCE_RESOLVED type=folder target=%s confidence=%.2f", target, confidence)
        if action == "close_window":
            return VisualReferenceResolution(True, LocalCommandIntent("file", "close_folder", raw, normalized, target=target))
        if action == "focus_window":
            return VisualReferenceResolution(True, LocalCommandIntent("window", "focus_window", raw, normalized, target=label))
        return VisualReferenceResolution(True, LocalCommandIntent("file", "open_folder", raw, normalized, target=target))

    if is_app:
        logger.info("APP_VISUAL_REFERENCE_DETECTED")
        resolved_app = _resolve_app_from_context(normalized, context_text)
        if resolved_app is None:
            logger.info("VISUAL_REFERENCE_FAILED reason=app_ambiguous_or_missing")
            return VisualReferenceResolution(True, notice="Nao consegui identificar qual aplicativo. Qual app voce quer?")
        app, label, confidence = resolved_app
        logger.info("APP_VISUAL_TARGET_RESOLVED app=%s confidence=%.2f", app, confidence)
        logger.info("VISUAL_REFERENCE_RESOLVED type=app target=%s confidence=%.2f", app, confidence)
        if action == "open":
            return VisualReferenceResolution(True, LocalCommandIntent("app", "open_app", raw, normalized, target=app))
        window_action = action if action != "open" else "focus_window"
        return VisualReferenceResolution(True, LocalCommandIntent("window", window_action, raw, normalized, target=label))

    if is_window:
        active_title = _active_window_title()
        if not active_title:
            logger.info("VISUAL_REFERENCE_FAILED reason=no_active_window")
            return VisualReferenceResolution(True, notice="Nao consegui identificar a janela ativa agora.")
        if visual_payload:
            logger.info("WINDOW_VISUAL_CONTEXT_USED")
            titles = [_normalize(title) for title in _visible_window_titles()]
            active_norm = _normalize(active_title)
            context_has_active = bool(active_norm and (active_norm in context_text or not context_text))
            other_matches = [title for title in titles if title and title != active_norm and title in context_text]
            if other_matches and not context_has_active:
                logger.info("WINDOW_VISUAL_ACTIVE_MISMATCH_CONFIRMATION_REQUIRED")
                logger.info("VISUAL_REFERENCE_AMBIGUOUS candidates=%s", other_matches[:3])
                return VisualReferenceResolution(True, notice="A janela em foco e a tela parecem diferentes. Qual janela voce quer usar?")
            logger.info("WINDOW_VISUAL_ACTIVE_MATCH_CONFIRMED")
        logger.info("WINDOW_ACTIVE_CONTEXT_USED")
        logger.info("VISUAL_REFERENCE_RESOLVED type=window target=current confidence=0.86")
        return VisualReferenceResolution(True, LocalCommandIntent("window", action, raw, normalized, target="current"))

    logger.info("VISUAL_REFERENCE_FAILED reason=unsupported_reference")
    return VisualReferenceResolution(True, notice="Nao consegui identificar o alvo. Pode dizer o nome da janela, pasta ou app?")


__all__ = [
    "VisualReferenceResolution",
    "has_visual_local_reference",
    "resolve_visual_local_command",
]
