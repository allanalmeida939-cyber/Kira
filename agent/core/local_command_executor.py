from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import unicodedata
import zipfile

from .local_command_gate import BLOCK_FLAGS, LocalCommandIntent

logger = logging.getLogger("kira.app")

_LAST_WINDOW_TARGET_TITLE = ""


@dataclass(frozen=True)
class LocalCommandExecutionResult:
    ok: bool
    message: str
    path: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        payload = {
            "ok": self.ok,
            "message": self.message,
            "reply": self.message,
            "path": self.path,
            "error": self.error,
        }
        payload.update(BLOCK_FLAGS)
        return payload


def _home(base_home: Path | None) -> Path:
    return Path(base_home) if base_home is not None else Path.home()


def _known_folder(target: str, home: Path) -> Path:
    if str(target or "").startswith("path:"):
        return Path(str(target).removeprefix("path:")).expanduser()
    if str(target or "").startswith("project:"):
        project_root = Path(__file__).resolve().parents[2]
        label = _normalize_label(str(target).removeprefix("project:"))
        if "logs" in label:
            return project_root / "logs"
        if "backups" in label:
            return project_root / "backups"
        return project_root
    folders = {
        "documents": home / "Documents",
        "downloads": home / "Downloads",
        "desktop": home / "Desktop",
        "documents_kira": home / "Documents" / "Kira",
        "desktop_kira": home / "Desktop" / "Kira",
    }
    return folders.get(target, home)


def _open_path(path: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    if hasattr(os, "startfile"):
        os.startfile(str(path))
    else:
        subprocess.Popen(["xdg-open", str(path)], shell=False)


def _normalize_label(text: str) -> str:
    value = unicodedata.normalize("NFD", str(text or "").strip().lower())
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = re.sub(r"[^\w\s.-]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _window_lib():
    try:
        import pygetwindow as gw
        return gw
    except Exception as exc:
        logger.info("WINDOW_ACTION_FAILED reason=pygetwindow_unavailable:%s", exc)
        return None


def _visible_windows() -> list:
    gw = _window_lib()
    if gw is None:
        return []
    try:
        windows = [
            window
            for window in gw.getAllWindows()
            if str(getattr(window, "title", "") or "").strip()
            and int(getattr(window, "width", 0) or 0) > 0
            and int(getattr(window, "height", 0) or 0) > 0
        ]
        logger.info("WINDOW_LIST_SNAPSHOT count=%s", len(windows))
        return windows
    except Exception as exc:
        logger.info("WINDOW_ACTION_FAILED reason=list_failed:%s", exc)
        return []


def _active_window():
    gw = _window_lib()
    if gw is None:
        return None
    try:
        window = gw.getActiveWindow()
        title = str(getattr(window, "title", "") or "").strip() if window is not None else ""
        if title:
            logger.info("WINDOW_ACTIVE_DETECTED title=%s", title)
        return window if title else None
    except Exception as exc:
        logger.info("WINDOW_ACTION_FAILED reason=active_failed:%s", exc)
        return None


def _target_aliases(target: str) -> list[str]:
    normalized = _normalize_label(target)
    aliases = {
        "navegador": ["opera", "chrome", "browser", "mozilla", "edge"],
        "browser": ["opera", "chrome", "browser", "mozilla", "edge"],
        "opera": ["opera"],
        "chrome": ["chrome"],
        "vs code": ["visual studio code", "code"],
        "vscode": ["visual studio code", "code"],
        "bloco de notas": ["bloco de notas", "notepad"],
        "notepad": ["bloco de notas", "notepad"],
        "calculadora": ["calculadora", "calculator"],
        "explorador": ["explorador de arquivos", "file explorer", "explorer"],
        "explorador de arquivos": ["explorador de arquivos", "file explorer", "explorer"],
        "documentos": ["documentos", "documents"],
        "downloads": ["downloads"],
        "area de trabalho": ["area de trabalho", "desktop"],
        "desktop": ["area de trabalho", "desktop"],
        "kira v8": ["kira v8"],
    }
    return aliases.get(normalized, [normalized])


def _match_window(target: str):
    if _normalize_label(target) == "current":
        return _active_window(), []
    aliases = _target_aliases(target)
    ranked: list[tuple[float, object]] = []
    for window in _visible_windows():
        title = str(getattr(window, "title", "") or "").strip()
        normalized_title = _normalize_label(title)
        score = 0.0
        for alias in aliases:
            if alias and alias in normalized_title:
                score = max(score, 1.0)
            else:
                score = max(score, SequenceMatcher(None, alias, normalized_title).ratio())
        if score >= 0.48:
            ranked.append((score, window))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked:
        return None, []
    best_score, best = ranked[0]
    similar = [window for score, window in ranked if best_score - score < 0.08 and score >= 0.70]
    if len(similar) > 1:
        logger.info("WINDOW_TARGET_AMBIGUOUS candidates=%s", [str(getattr(w, "title", "") or "") for w in similar[:5]])
        return None, similar
    logger.info("WINDOW_TARGET_MATCHED title=%s confidence=%.2f", getattr(best, "title", ""), best_score)
    return best, []


def _remember_window(window) -> None:
    global _LAST_WINDOW_TARGET_TITLE
    title = str(getattr(window, "title", "") or "").strip() if window is not None else ""
    if title:
        _LAST_WINDOW_TARGET_TITLE = title


def _last_window_target():
    if not _LAST_WINDOW_TARGET_TITLE:
        return None
    window, candidates = _match_window(_LAST_WINDOW_TARGET_TITLE)
    return window if window is not None and not candidates else None


def _activate_window(window) -> None:
    if bool(getattr(window, "isMinimized", False)):
        window.restore()
    window.activate()


def _is_protected_window(title: str) -> bool:
    normalized = _normalize_label(title)
    return any(token in normalized for token in ("kira", "codex", "visual studio code", "powershell", "terminal", "agent.py", "kira_app"))


def _search_roots(home: Path) -> list[Path]:
    return [home / "Documents", home / "Downloads", home / "Desktop"]


def _find_file(query: str, home: Path) -> Path | None:
    lowered = query.lower().strip()
    for root in _search_roots(home):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and lowered in path.name.lower():
                return path
    return None


def _last_file(home: Path) -> Path | None:
    candidates: list[Path] = []
    for root in _search_roots(home):
        if root.exists():
            candidates.extend(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in {"", ".pdf", ".txt"})
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def _resolve_named_path(name: str, home: Path) -> Path | None:
    candidate = Path(name).expanduser()
    if candidate.exists():
        return candidate
    for root in _search_roots(home):
        if not root.exists():
            continue
        for direct in root.iterdir():
            if direct.name.lower() == str(name or "").strip().lower():
                return direct
    found = _find_file(name, home)
    if found is not None:
        return found
    for root in _search_roots(home):
        direct = root / name
        if direct.exists():
            return direct
    return None


def _base_from_label(label: str, home: Path) -> Path:
    normalized = str(label or "").strip().lower()
    if "download" in normalized:
        return home / "Downloads"
    if "desktop" in normalized or "area de trabalho" in normalized:
        return home / "Desktop"
    return home / "Documents"


def _folder_label_path(label: str, home: Path) -> Path | None:
    if str(label or "").startswith("path:"):
        candidate = Path(str(label).removeprefix("path:")).expanduser()
        return candidate if candidate.exists() and candidate.is_dir() else None
    normalized = _normalize_label(label)
    known = {
        "documentos": home / "Documents",
        "documents": home / "Documents",
        "downloads": home / "Downloads",
        "area de trabalho": home / "Desktop",
        "desktop": home / "Desktop",
        "kira v8": Path(__file__).resolve().parents[2],
        "logs": Path(__file__).resolve().parents[2] / "logs",
        "backups": Path(__file__).resolve().parents[2] / "backups",
    }
    if normalized in known:
        return known[normalized]
    resolved = _resolve_named_path(label, home)
    return resolved if resolved is not None and resolved.is_dir() else None


def _execute_window_command(intent: LocalCommandIntent, *, dry_run: bool) -> LocalCommandExecutionResult:
    action = intent.action
    target = intent.target or "current"
    logger.info("WINDOW_COMMAND_DETECTED action=%s target=%s", action, target)
    logger.info("WINDOW_CONTEXT_REQUESTED reason=window_command")
    if action == "list_windows":
        titles = [str(getattr(window, "title", "") or "").strip() for window in _visible_windows()]
        preview = ", ".join(titles[:8]) if titles else "nenhuma janela visivel"
        return LocalCommandExecutionResult(True, f"Interface: janelas abertas: {preview}.")
    if action == "active_window":
        window = _active_window()
        title = str(getattr(window, "title", "") or "").strip() if window is not None else ""
        if not title:
            return LocalCommandExecutionResult(False, "Interface: nao identifiquei a janela em foco.")
        _remember_window(window)
        return LocalCommandExecutionResult(True, f"Interface: janela em foco: {title}.")
    window, candidates = _match_window(target)
    if _normalize_label(target) == "current" and action in {"restore_window", "maximize_window"}:
        previous = _last_window_target()
        if previous is not None:
            window = previous
    if window is not None:
        _remember_window(window)
    if candidates:
        names = ", ".join(str(getattr(item, "title", "") or "") for item in candidates[:3])
        return LocalCommandExecutionResult(False, f"Interface: encontrei mais de uma janela parecida: {names}.")
    if window is None:
        return LocalCommandExecutionResult(False, "Interface: nao achei essa janela aberta.")
    title = str(getattr(window, "title", "") or "").strip()
    if action == "check_window":
        return LocalCommandExecutionResult(True, f"Interface: achei aberta: {title}.")
    if action == "close_window" and _is_protected_window(title):
        logger.info("WINDOW_ACTION_BLOCKED reason=protected_window title=%s", title)
        logger.info("APP_ACTION_BLOCKED reason=protected_app title=%s", title)
        return LocalCommandExecutionResult(False, "Interface: essa janela parece protegida. Preciso de confirmacao explicita para fechar.")
    logger.info("WINDOW_ACTION_BEGIN action=%s title=%s", action, title)
    try:
        if not dry_run:
            if action == "minimize_window":
                if bool(getattr(window, "isMinimized", False)):
                    logger.info("WINDOW_ACTION_OK action=%s title=%s", action, title)
                    return LocalCommandExecutionResult(True, "Interface: essa janela ja esta minimizada.")
                window.minimize()
            elif action == "maximize_window":
                if bool(getattr(window, "isMaximized", False)):
                    logger.info("WINDOW_ACTION_OK action=%s title=%s", action, title)
                    return LocalCommandExecutionResult(True, "Interface: essa janela ja esta maximizada.")
                window.maximize()
            elif action == "restore_window":
                window.restore()
            elif action == "focus_window":
                _activate_window(window)
            elif action == "close_window":
                window.close()
            else:
                return LocalCommandExecutionResult(False, "Interface: acao de janela nao reconhecida.")
        logger.info("WINDOW_ACTION_OK action=%s title=%s", action, title)
        messages = {
            "minimize_window": "Interface: janela minimizada.",
            "maximize_window": "Interface: janela maximizada.",
            "restore_window": "Interface: janela restaurada.",
            "focus_window": "Interface: trouxe a janela para frente.",
            "close_window": "Interface: janela fechada.",
        }
        return LocalCommandExecutionResult(True, messages.get(action, "Interface: comando de janela executado."))
    except Exception as exc:
        logger.info("WINDOW_ACTION_FAILED reason=%s", exc)
        return LocalCommandExecutionResult(False, "Interface: nao consegui executar a acao nessa janela.", error=str(exc))


def _execute_open_folder(intent: LocalCommandIntent, home: Path, *, dry_run: bool) -> LocalCommandExecutionResult:
    path = _known_folder(intent.target, home)
    logger.info("FILE_APP_COMMAND_DETECTED action=open_folder target=%s", intent.target)
    logger.info("FOLDER_TARGET_RESOLVED path=%s", path)
    if not path.exists():
        return LocalCommandExecutionResult(False, f"Interface: nao encontrei a pasta {path}.", str(path))
    existing, _ = _match_window(path.name)
    if existing is not None:
        title = str(getattr(existing, "title", "") or "")
        logger.info("FOLDER_ALREADY_OPEN title=%s", title)
        logger.info("DUPLICATE_WINDOW_PREVENTED target=%s", path)
        if not dry_run:
            _activate_window(existing)
        logger.info("FOLDER_ACTION_OK action=open_existing path=%s", path)
        return LocalCommandExecutionResult(True, "Interface: essa pasta ja estava aberta, trouxe para frente.", str(path))
    logger.info("FOLDER_OPEN_BEGIN path=%s", path)
    _open_path(path, dry_run=dry_run)
    logger.info("FOLDER_OPEN_OK path=%s", path)
    logger.info("FOLDER_ACTION_OK action=open path=%s", path)
    return LocalCommandExecutionResult(True, f"Interface: abri a pasta {path}.", str(path))


def _execute_close_folder(intent: LocalCommandIntent, home: Path, *, dry_run: bool) -> LocalCommandExecutionResult:
    logger.info("FILE_APP_COMMAND_DETECTED action=close_folder target=%s", intent.target)
    path = _folder_label_path(intent.target, home)
    if path is not None:
        logger.info("FOLDER_TARGET_RESOLVED path=%s", path)
    target = path.name if path is not None else intent.target
    window, candidates = _match_window(target)
    if candidates:
        return LocalCommandExecutionResult(False, "Interface: encontrei mais de uma pasta parecida.")
    if window is None:
        if path is not None and path.exists():
            logger.info("FOLDER_EXISTS_BUT_NOT_OPEN path=%s", path)
            return LocalCommandExecutionResult(False, "Interface: essa pasta existe, mas nao esta aberta agora.", str(path))
        return LocalCommandExecutionResult(False, "Interface: nao achei essa pasta aberta.")
    title = str(getattr(window, "title", "") or "")
    logger.info("APP_CLOSE_BEGIN app=%s", title)
    if not dry_run:
        window.close()
    logger.info("APP_CLOSE_OK app=%s", title)
    logger.info("FOLDER_ACTION_OK action=close path=%s", path or target)
    return LocalCommandExecutionResult(True, "Interface: pasta fechada.", str(path or ""))


def _execute_create_folder(intent: LocalCommandIntent, home: Path, *, dry_run: bool) -> LocalCommandExecutionResult:
    logger.info("FILE_APP_COMMAND_DETECTED action=create_folder target=%s", intent.target)
    base = _base_from_label(str(intent.data.get("base") or ""), home)
    path = base / intent.target
    logger.info("FOLDER_TARGET_RESOLVED path=%s", path)
    if path.exists():
        return LocalCommandExecutionResult(True, "Interface: essa pasta ja existe.", str(path))
    logger.info("FOLDER_CREATE_BEGIN path=%s", path)
    if not dry_run:
        path.mkdir(parents=True, exist_ok=False)
    logger.info("FOLDER_CREATE_OK path=%s", path)
    logger.info("FOLDER_ACTION_OK action=create path=%s", path)
    return LocalCommandExecutionResult(True, f"Interface: pasta criada em {path}.", str(path))


def _execute_rename_folder(intent: LocalCommandIntent, home: Path, *, dry_run: bool) -> LocalCommandExecutionResult:
    logger.info("FILE_APP_COMMAND_DETECTED action=rename_item target=%s", intent.target)
    path = _resolve_named_path(intent.target, home)
    new_name = str(intent.data.get("new_name") or "").strip()
    if path is None or not path.is_dir() or not new_name:
        return LocalCommandExecutionResult(False, "Interface: nao encontrei a pasta para renomear.")
    destination = path.with_name(new_name)
    if destination.exists():
        return LocalCommandExecutionResult(False, "Interface: ja existe uma pasta com esse nome.")
    logger.info("FOLDER_RENAME_BEGIN old=%s new=%s", path, destination)
    if not dry_run:
        path.rename(destination)
    logger.info("FOLDER_RENAME_OK old=%s new=%s", path, destination)
    logger.info("FOLDER_ACTION_OK action=rename path=%s", destination)
    return LocalCommandExecutionResult(True, f"Interface: renomeei para {destination.name}.", str(destination))


def _execute_volume(intent: LocalCommandIntent, *, dry_run: bool) -> LocalCommandExecutionResult:
    logger.info("VOLUME_GATE_MATCHED action=%s value=%s", intent.action, intent.value)
    if dry_run:
        if intent.action == "set_volume_absolute":
            logger.info("VOLUME_SET_PERCENT_OK value=%s dry_run=true", intent.value)
        elif intent.action == "adjust_volume_relative":
            logger.info("VOLUME_STEP_OK direction=%s dry_run=true", intent.data.get("direction") or "")
        else:
            logger.info("VOLUME_MUTE_OK action=%s dry_run=true", intent.action)
        return LocalCommandExecutionResult(True, "Interface: volume ajustado.")
    try:
        from agent.runtime.system_actions import ActionIntent, execute_action
    except Exception:
        from runtime.system_actions import ActionIntent, execute_action
    if intent.action == "set_volume_absolute":
        action = ActionIntent("volume", "set", {"value": int(intent.value or 0)})
    elif intent.action == "adjust_volume_relative":
        direction = "increase" if intent.data.get("direction") == "up" else "decrease"
        action = ActionIntent("volume", direction, {"amount": abs(int(intent.value or 10))})
    elif intent.action == "mute_volume":
        action = ActionIntent("volume", "mute")
    else:
        action = ActionIntent("volume", "unmute")
    ok, message = execute_action(action)
    if not ok:
        logger.error("VOLUME_COMMAND_FAILED reason=%s", message)
    return LocalCommandExecutionResult(bool(ok), f"Interface: {message}")


def _execute_open_app(intent: LocalCommandIntent, *, dry_run: bool) -> LocalCommandExecutionResult:
    logger.info("FILE_APP_COMMAND_DETECTED action=%s target=%s", intent.action, intent.target)
    commands = {
        "notepad": ["notepad.exe"],
        "vscode": ["code"],
        "calculator": ["calc.exe"],
        "explorer": ["explorer.exe"],
        "chrome": ["cmd", "/c", "start", "", "chrome"],
        "browser": ["cmd", "/c", "start", ""],
    }
    opera_candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Opera" / "launcher.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Opera" / "launcher.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Opera" / "launcher.exe",
    ]
    if intent.target == "opera":
        found = next((path for path in opera_candidates if path.exists()), None)
        commands["opera"] = [str(found)] if found else ["opera"]
    command = commands.get(intent.target)
    if command is None:
        return LocalCommandExecutionResult(False, f"Interface: app nao permitido ou nao reconhecido: {intent.target}.")
    logger.info("APP_TARGET_RESOLVED app=%s", intent.target)
    existing, _ = _match_window(intent.target)
    if existing is not None:
        logger.info("APP_ALREADY_RUNNING app=%s", intent.target)
        if not dry_run:
            _activate_window(existing)
        logger.info("APP_BRING_TO_FRONT_OK app=%s", intent.target)
        logger.info("DUPLICATE_WINDOW_PREVENTED target=%s", intent.target)
        return LocalCommandExecutionResult(True, f"Interface: {intent.target} ja estava aberto, trouxe para frente.")
    logger.info("APP_OPEN_BEGIN app=%s", intent.target)
    if not dry_run:
        subprocess.Popen(command, shell=False)
    logger.info("APP_OPEN_OK app=%s", intent.target)
    return LocalCommandExecutionResult(True, f"Interface: abrindo {intent.target}.")


def execute_local_command(
    intent: LocalCommandIntent,
    *,
    home: Path | None = None,
    dry_run: bool = False,
) -> LocalCommandExecutionResult:
    base_home = _home(home)
    try:
        if intent.kind == "window":
            return _execute_window_command(intent, dry_run=dry_run)
        if intent.kind == "volume":
            return _execute_volume(intent, dry_run=dry_run)
        if intent.action == "open_app":
            return _execute_open_app(intent, dry_run=dry_run)
        if intent.action == "open_folder":
            return _execute_open_folder(intent, base_home, dry_run=dry_run)
        if intent.action == "close_folder":
            return _execute_close_folder(intent, base_home, dry_run=dry_run)
        if intent.action == "open_last_file":
            path = _last_file(base_home)
            if path is None:
                return LocalCommandExecutionResult(False, "Interface: nao encontrei um arquivo recente.")
            _open_path(path, dry_run=dry_run)
            return LocalCommandExecutionResult(True, f"Interface: abri {path.name}.", str(path))
        if intent.action == "open_file_search":
            path = _find_file(intent.target, base_home)
            if path is None:
                return LocalCommandExecutionResult(False, f"Interface: nao encontrei {intent.target}.")
            _open_path(path, dry_run=dry_run)
            return LocalCommandExecutionResult(True, f"Interface: abri {path.name}.", str(path))
        if intent.action == "create_folder":
            return _execute_create_folder(intent, base_home, dry_run=dry_run)
        if intent.action == "rename_item":
            return _execute_rename_folder(intent, base_home, dry_run=dry_run)
        if intent.action == "copy_item":
            path = _resolve_named_path(intent.target, base_home)
            destination_label = str(intent.data.get("destination") or "")
            destination_dir = _base_from_label(destination_label, base_home)
            if path is None:
                return LocalCommandExecutionResult(False, "Interface: nao encontrei o item para copiar.")
            destination = destination_dir / path.name
            if not dry_run:
                if path.is_dir():
                    shutil.copytree(path, destination, dirs_exist_ok=True)
                else:
                    destination_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)
            return LocalCommandExecutionResult(True, f"Interface: copiei para {destination}.", str(destination))
        if intent.action == "zip_folder":
            path = _resolve_named_path(intent.target, base_home)
            if path is None or not path.is_dir():
                return LocalCommandExecutionResult(False, "Interface: nao encontrei a pasta para compactar.")
            zip_path = path.with_suffix(".zip")
            if not dry_run:
                with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for item in path.rglob("*"):
                        if item.is_file():
                            archive.write(item, item.relative_to(path.parent))
            return LocalCommandExecutionResult(True, f"Interface: compactei em {zip_path}.", str(zip_path))
    except Exception as exc:
        logger.warning("LOCAL_COMMAND_EXECUTOR_FAILED kind=%s action=%s error=%s", intent.kind, intent.action, exc)
        return LocalCommandExecutionResult(False, "Interface: nao consegui executar esse comando local.", error=str(exc))
    return LocalCommandExecutionResult(False, "Interface: comando local nao reconhecido.")


__all__ = ["LocalCommandExecutionResult", "execute_local_command"]
