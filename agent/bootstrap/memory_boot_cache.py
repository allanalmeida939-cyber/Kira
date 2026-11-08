from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from livekit.agents import ChatContext


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MEMORY_BOOT_CONTEXT_PATH = PROJECT_ROOT / "runtime" / "memory_boot_context.json"
logger = logging.getLogger(__name__)


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, list):
        return "".join(str(part) for part in content)
    return str(content)


def chat_context_to_messages(chat_ctx: ChatContext) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for item in getattr(chat_ctx, "items", []) or []:
        role = str(getattr(item, "role", "") or "").strip()
        content = _content_to_text(getattr(item, "content", None)).strip()
        if role and content:
            messages.append({"role": role, "content": content})
    return messages


def messages_to_chat_context(messages: list[dict[str, Any]]) -> ChatContext:
    chat_ctx = ChatContext()
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role and content:
            chat_ctx.add_message(role=role, content=content)
    return chat_ctx


def _build_mem0_boot_context() -> tuple[ChatContext, str, str, dict[str, Any]]:
    started = time.monotonic()
    try:
        from services.mem0_cloud_provider import Mem0CloudProvider, configured_boot_limit

        provider = Mem0CloudProvider()
        limit = configured_boot_limit(50)
        logger.info("MEM0_BOOT_CONTEXT_BEGIN limit=%s", limit)
        memories = provider.get_boot_memories(user_id=provider.user_id, limit=limit)
        provider.close()
    except TimeoutError:
        logger.warning("MEM0_BOOT_CONTEXT_TIMEOUT")
        raise
    except Exception as exc:
        logger.warning("MEM0_BOOT_CONTEXT_FAILED error_type=%s", exc.__class__.__name__)
        raise
    chat_ctx = ChatContext()
    memory_str = ""
    if memories:
        block = provider.build_context_block(memories[:limit])
        memory_str = block
        if block:
            chat_ctx.add_message(role="assistant", content=block)
        logger.info("MEM0_BOOT_CONTEXT_LOADED count=%s", len(memories))
    else:
        logger.info("MEM0_BOOT_CONTEXT_EMPTY")
    payload = {
        "ok": True,
        "source": "mem0_cloud",
        "user_id": provider.user_id,
        "memory_str": memory_str,
        "messages": chat_context_to_messages(chat_ctx),
        "items": len(memories),
        "created_at": time.time(),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    return chat_ctx, provider.user_id, memory_str, payload


def write_memory_boot_context(
    chat_ctx: ChatContext,
    *,
    user_id: str,
    memory_str: str,
    source: str = "boot_health",
) -> dict[str, Any]:
    messages = chat_context_to_messages(chat_ctx)
    payload = {
        "ok": True,
        "source": source,
        "user_id": str(user_id or "").strip() or "Chefe",
        "memory_str": str(memory_str or ""),
        "messages": messages,
        "items": len(messages),
        "created_at": time.time(),
    }
    MEMORY_BOOT_CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = MEMORY_BOOT_CONTEXT_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    tmp_path.replace(MEMORY_BOOT_CONTEXT_PATH)
    return payload


def read_memory_boot_context(*, max_age_seconds: int = 900) -> tuple[ChatContext, str, str, dict[str, Any]]:
    try:
        return _build_mem0_boot_context()
    except Exception:
        logger.info("MEM0_BOOT_CONTEXT_FALLBACK_CACHE")
    if not MEMORY_BOOT_CONTEXT_PATH.exists():
        raise FileNotFoundError(f"memory boot cache not found: {MEMORY_BOOT_CONTEXT_PATH}")
    with MEMORY_BOOT_CONTEXT_PATH.open("r", encoding="utf-8") as fp:
        payload = json.load(fp)
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise ValueError("memory boot cache is invalid")
    created_at = float(payload.get("created_at") or 0.0)
    if created_at <= 0 or (time.time() - created_at) > max_age_seconds:
        raise TimeoutError("memory boot cache is stale")
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError("memory boot cache messages missing")
    chat_ctx = messages_to_chat_context(messages)
    user_id = str(payload.get("user_id") or "").strip() or "Chefe"
    memory_str = str(payload.get("memory_str") or "")
    return chat_ctx, user_id, memory_str, payload
