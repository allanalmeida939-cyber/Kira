from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from livekit.agents import ChatContext


logger = logging.getLogger(__name__)

BACKGROUND_CONTEXT_PREFIX = "[BG_CONTEXT]"
SESSION_CONTEXT_PATH = Path(__file__).resolve().parents[1] / "agent_session_context.json"
SESSION_CONTEXT_EXCLUDED_PREFIXES = (
    "Aqui estao informacoes importantes sobre o usuario que voce deve lembrar e usar nas conversas:",
    "Resumo local de ontem para contexto adicional:",
)
SESSION_CONTEXT_MAX_MESSAGES = max(int(os.getenv("SESSION_CONTEXT_MAX_MESSAGES", "12")), 4)
SESSION_CONTEXT_MAX_AGE_SECONDS = max(
    int(os.getenv("SESSION_CONTEXT_MAX_AGE_SECONDS", "21600")),
    300,
)
SESSION_CONTEXT_WRITE_INTERVAL_SECONDS = max(
    float(os.getenv("SESSION_CONTEXT_WRITE_INTERVAL_SECONDS", "8")),
    3.0,
)


def normalize_chat_content(content) -> str:
    if content is None:
        return ""
    if isinstance(content, list):
        return "".join(str(part) for part in content)
    return str(content)


def session_context_messages(
    chat_ctx: ChatContext,
    *,
    memory_str: str = "",
    max_messages: int = SESSION_CONTEXT_MAX_MESSAGES,
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []

    for item in chat_ctx.items:
        role = str(getattr(item, "role", "") or "").strip()
        if role not in {"user", "assistant"}:
            continue

        content_str = normalize_chat_content(getattr(item, "content", None)).strip()
        if not content_str or content_str.startswith(BACKGROUND_CONTEXT_PREFIX):
            continue
        if memory_str and memory_str in content_str:
            continue
        if any(content_str.startswith(prefix) for prefix in SESSION_CONTEXT_EXCLUDED_PREFIXES):
            continue

        messages.append({"role": role, "content": content_str})

    return messages[-max(max_messages, 1):]


def save_session_context_snapshot(
    chat_ctx: ChatContext,
    *,
    user_id: str,
    memory_str: str = "",
) -> int:
    payload = {
        "user_id": user_id,
        "saved_at": time.time(),
        "messages": session_context_messages(chat_ctx, memory_str=memory_str),
    }

    try:
        SESSION_CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SESSION_CONTEXT_PATH.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
    except OSError:
        logger.exception("Falha ao salvar snapshot local da conversa.")
        return 0

    return len(payload["messages"])


def load_session_context_snapshot(
    *,
    user_id: str,
    max_age_seconds: int = SESSION_CONTEXT_MAX_AGE_SECONDS,
) -> list[dict[str, str]]:
    if not SESSION_CONTEXT_PATH.exists():
        return []

    try:
        with SESSION_CONTEXT_PATH.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        logger.exception("Falha ao ler snapshot local da conversa.")
        return []

    if not isinstance(payload, dict):
        return []
    if str(payload.get("user_id") or "").strip() != user_id:
        return []

    try:
        saved_at = float(payload.get("saved_at") or 0.0)
    except (TypeError, ValueError):
        saved_at = 0.0

    if saved_at <= 0 or (time.time() - saved_at) > max_age_seconds:
        return []

    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        return []

    restored: list[dict[str, str]] = []
    for item in raw_messages[-SESSION_CONTEXT_MAX_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        restored.append({"role": role, "content": content})

    return restored
