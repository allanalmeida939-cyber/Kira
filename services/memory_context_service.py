from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("kira.memory_context")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_MEMORY_CANDIDATE_DIRS = (
    PROJECT_ROOT / "agent" / "memory" / "local_memory",
    PROJECT_ROOT / "agent" / "local_memory",
)
MAX_MEMORY_FILE_BYTES = 256 * 1024


def _empty_result(reason: str) -> dict:
    logger.info("BLOCO7_MEMORY_CONTEXT_EMPTY reason=%s", reason)
    return {
        "ok": True,
        "items": [],
        "text": "",
        "source": "none",
        "empty": True,
        "reason": reason,
    }


def _read_json_limited(path: Path, default: Any) -> Any:
    try:
        if not path.exists() or not path.is_file():
            return default
        if path.stat().st_size > MAX_MEMORY_FILE_BYTES:
            return default
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
        return data
    except Exception:
        return default


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _basename(value: Any) -> str:
    text = _as_text(value)
    if not text:
        return ""
    return os.path.basename(text) or text


def _append_item(items: list[dict[str, Any]], *, text: str, source: str, created_at: Any = None, updated_at: Any = None) -> None:
    clean_text = _as_text(text)
    if not clean_text:
        return
    item = {
        "text": clean_text,
        "source": source,
    }
    if created_at not in (None, ""):
        item["created_at"] = created_at
    if updated_at not in (None, ""):
        item["updated_at"] = updated_at
    items.append(item)


def _collect_from_memory_dir(memory_dir: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    profile = _read_json_limited(memory_dir / "user_profile.json", {})
    if isinstance(profile, dict):
        display_name = _as_text(profile.get("display_name"))
        if display_name:
            _append_item(
                items,
                text=f"O usuario se chama {display_name}.",
                source="local_memory:user_profile",
                updated_at=profile.get("updated_at") or profile.get("timestamp"),
            )

    session_context = _read_json_limited(memory_dir / "session_context.json", {})
    if isinstance(session_context, dict):
        last_project = _as_text(session_context.get("last_project_path"))
        if last_project:
            _append_item(
                items,
                text=f"Projeto recente: {_basename(last_project)}.",
                source="local_memory:session_context",
                updated_at=session_context.get("started_at"),
            )

        last_folder = _as_text(session_context.get("last_working_folder"))
        if last_folder:
            _append_item(
                items,
                text=f"Pasta recente: {_basename(last_folder)}.",
                source="local_memory:session_context",
                updated_at=session_context.get("started_at"),
            )

        for file_path in list(session_context.get("recent_files") or [])[:3]:
            file_name = _basename(file_path)
            if file_name:
                _append_item(
                    items,
                    text=f"Arquivo recente: {file_name}.",
                    source="local_memory:session_context",
                    updated_at=session_context.get("started_at"),
                )

        for action in list(session_context.get("recent_actions") or [])[:5]:
            action_name = ""
            timestamp = None
            if isinstance(action, dict):
                action_name = _as_text(action.get("action") or action.get("event"))
                timestamp = action.get("timestamp") or action.get("updated_at")
            else:
                action_name = _as_text(action)
            if action_name and action_name != "user_context_classified":
                _append_item(
                    items,
                    text=f"Acao recente: {action_name}.",
                    source="local_memory:session_context",
                    updated_at=timestamp,
                )

    summary = _read_json_limited(memory_dir / "last_session_summary.json", {})
    if isinstance(summary, dict):
        summary_text = _as_text(summary.get("summary"))
        if summary_text:
            _append_item(
                items,
                text=f"Resumo da ultima sessao: {summary_text}",
                source="local_memory:last_session_summary",
                created_at=summary.get("started_at"),
                updated_at=summary.get("ended_at"),
            )

    return items


def build_safe_memory_context(max_items: int = 5, max_chars: int = 1200) -> dict:
    try:
        from services.memory_provider_manager import load_memory_context

        context = load_memory_context(max_items=max_items, max_chars=max_chars)
        provider = str(context.get("provider") or context.get("source") or "none")
        if bool(context.get("empty")):
            logger.info("MEMORY_CONTEXT_EMPTY_CONTINUING provider=%s", provider)
        else:
            logger.info("MEMORY_CONTEXT_OPTIONAL_LOADED provider=%s", provider)
        return context
    except Exception as exc:
        logger.warning("MEMORY_PROVIDER_CONTEXT_FAILED_CONTINUING reason=%s", exc)

    try:
        item_limit = max(0, int(max_items))
        char_limit = max(0, int(max_chars))
    except Exception:
        item_limit = 5
        char_limit = 1200

    if item_limit <= 0:
        return _empty_result("max_items_not_positive")

    try:
        collected: list[dict[str, Any]] = []
        for memory_dir in LOCAL_MEMORY_CANDIDATE_DIRS:
            if memory_dir.exists() and memory_dir.is_dir():
                collected.extend(_collect_from_memory_dir(memory_dir))
            if len(collected) >= item_limit:
                break

        if not collected:
            return _empty_result("no_memory_available")

        limited_items = collected[:item_limit]
        lines = [f"- {item['text']}" for item in limited_items if _as_text(item.get("text"))]
        text = "\n".join(lines).strip()
        if char_limit <= 0:
            text = ""
        elif len(text) > char_limit:
            text = text[:char_limit].rstrip()

        logger.info("BLOCO7_MEMORY_CONTEXT_BUILT items=%s chars=%s", len(limited_items), len(text))
        return {
            "ok": True,
            "items": limited_items,
            "text": text,
            "source": "local_memory",
            "empty": False,
            "reason": "",
        }
    except Exception as exc:
        logger.warning("BLOCO7_MEMORY_CONTEXT_ERROR error=%s", exc)
        return _empty_result("memory_context_error")
