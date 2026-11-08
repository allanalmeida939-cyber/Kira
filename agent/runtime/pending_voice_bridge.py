from __future__ import annotations

import logging
from typing import Awaitable, Callable

from core.pending_system import PendingAnnouncement


logger = logging.getLogger(__name__)


PublishFn = Callable[[str], Awaitable[None]]
SpeakFn = Callable[[str], Awaitable[bool]]
StyleFn = Callable[..., str]


async def deliver_pending_announcement(
    announcement: PendingAnnouncement,
    *,
    publish_ui: PublishFn,
    speak_voice: SpeakFn,
    user_context: dict[str, object] | None = None,
    style_response: StyleFn | None = None,
) -> bool:
    raw_message = str(announcement.message or "").strip()
    if callable(style_response):
        message = style_response(
            raw_message,
            user_context=user_context,
            personality_context="proactive",
        )
    else:
        message = raw_message
    if not message:
        return False
    reminder_id = announcement.primary_item_id or ",".join(announcement.item_ids)
    logger.info("[KIRA PENDING] reminder_generated id=%s", reminder_id)
    logger.info("[KIRA PENDING] ui_publish_start id=%s", reminder_id)
    try:
        await publish_ui(message)
        logger.info("[KIRA PENDING] published_ui id=%s", reminder_id)
        logger.info("[KIRA PENDING] ui_publish_done id=%s", reminder_id)
    except Exception as exc:
        logger.warning("[KIRA PENDING] ui_publish_failed id=%s reason=%s", reminder_id, exc)

    logger.info("[KIRA PENDING] voice_start id=%s", reminder_id)
    spoken = await speak_voice(message)
    if spoken:
        logger.info("[KIRA PENDING] spoken_ok id=%s", reminder_id)
        logger.info("[KIRA PENDING] voice_success id=%s", reminder_id)
        return True
    logger.warning("[KIRA PENDING] spoken_failed id=%s reason=tts_unavailable", reminder_id)
    logger.warning("[KIRA PENDING] voice_failure id=%s", reminder_id)
    return False
