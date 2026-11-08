from __future__ import annotations

import logging
import asyncio
from typing import TYPE_CHECKING, Any

from livekit.agents import ChatContext

from .memory_loader import (
    aprender_preferencias_usuario,
    revisar_historico_e_melhorar_respostas,
)
from .session_context import BACKGROUND_CONTEXT_PREFIX, normalize_chat_content

if TYPE_CHECKING:
    from mem0 import AsyncMemoryClient
else:
    AsyncMemoryClient = Any


logger = logging.getLogger(__name__)


async def persist_shutdown_context(
    chat_ctx: ChatContext,
    *,
    mem0: AsyncMemoryClient,
    user_id: str,
    memory_str: str,
) -> None:
    logging.info("Shutting down, saving chat context to memory...")

    messages_formatted = []
    user_messages = []

    logging.info("Chat context collected for shutdown.")

    for item in chat_ctx.items:
        if not hasattr(item, "content") or item.content is None:
            continue
        content_str = normalize_chat_content(item.content)

        if memory_str and memory_str in content_str:
            continue
        if content_str.startswith(BACKGROUND_CONTEXT_PREFIX):
            continue

        if item.role in ["user", "assistant"]:
            message = {"role": item.role, "content": content_str.strip()}
            messages_formatted.append(message)
            if item.role == "user" and message["content"]:
                user_messages.append(message["content"])

    try:
        from services.mem0_session_memory import flush_pending

        await asyncio.to_thread(flush_pending, trigger="shutdown", session_id=user_id, timeout_seconds=8)
    except Exception:
        logger.exception("MEM0_SHUTDOWN_SAVE_FAILED error_type=flush_exception")

    logging.info("Saving %s chat messages to memory.", len(messages_formatted))
    if messages_formatted:
        try:
            from services.mem0_cloud_provider import Mem0CloudProvider

            provider = Mem0CloudProvider()
            result = provider.add_messages(
                messages_formatted,
                user_id=provider.user_id,
                metadata={"source": "shutdown_chat_context", "session_id": str(user_id or "")},
            )
            provider.close()
            if bool(result.get("pending")):
                logging.info("MEM0_SHUTDOWN_SAVE_PENDING event_id=%s", result.get("event_id") or "")
            logging.info("MEM0_SHUTDOWN_SAVE_OK")
        except Exception as exc:
            logging.warning("MEM0_SHUTDOWN_SAVE_FAILED error_type=%s", exc.__class__.__name__)
    else:
        logging.info("MEM0_SHUTDOWN_SAVE_SKIPPED reason=no_messages")
    logging.info("Chat context saved to memory.")
