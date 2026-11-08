from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


logger = logging.getLogger(__name__)


@dataclass
class TurnState:
    turn_id: str
    user_text: str
    created_at: float = field(default_factory=time.monotonic)
    preferred_reply_source: Optional[str] = None
    reply_started: bool = False
    reply_owner: Optional[str] = None
    reply_completed: bool = False
    response_id: Optional[str] = None
    speech_id: Optional[str] = None


class TurnCoordinator:
    def __init__(self) -> None:
        self._active_turns: dict[str, TurnState] = {}
        self._completed_turn_ids: set[str] = set()
        self._completed_turn_order: deque[str] = deque()
        self._max_completed_history = 100
        self._turn_timeout_s = 30.0
        self._whatsapp_tool_ack_active_until = 0.0
        self._whatsapp_tool_ack_request_id = ""
        self._pipeline = None
        self._attach_verified = False
        self._latest_turn_id = ""
        self._new_turn_listeners: list = []

    def attach_pipeline(self, pipeline) -> None:
        if pipeline is None:
            raise ValueError("TurnCoordinator.attach_pipeline: pipeline nao pode ser None")
        self._pipeline = pipeline
        self._attach_verified = True
        logger.info("TURN_COORDINATOR_ATTACHED pipeline=%s", type(pipeline).__name__)

    def _require_attach(self, operation: str) -> None:
        if not self._attach_verified or self._pipeline is None:
            logger.critical("TURN_COORDINATOR_NOT_ATTACHED operation=%s", operation)
            raise RuntimeError(
                f"TurnCoordinator.{operation} chamado sem pipeline attachado. "
                "Chame attach_pipeline(speech_coordinator) no bootstrap."
            )

    async def declare_ownership(self, turn_id: str, owner: str, source: str) -> bool:
        self._require_attach("declare_ownership")
        pipeline = self._pipeline
        acquired = await pipeline._voice_lock.acquire(
            owner=str(turn_id or owner or "unknown"),
            source=str(source or "unknown"),
            timeout=0.5 if str(source or "").strip().lower() == "ui_panel" else 4.0,
        )
        if acquired:
            logger.info(
                "[TURN] Propriedade declarada com lock unificado turn_id=%s owner=%s source=%s",
                turn_id,
                owner,
                source,
            )
        return bool(acquired)

    def release_ownership(self, turn_id: str, source: str = "unknown") -> None:
        self._require_attach("release_ownership")
        pipeline = self._pipeline
        pipeline._voice_lock.release(owner=str(turn_id or "unknown"))
        logger.info("[TURN] Propriedade liberada turn_id=%s source=%s", turn_id, source)

    def set_whatsapp_tool_ack_active(self, request_id: str, ttl_s: float = 8.0) -> None:
        self._whatsapp_tool_ack_request_id = str(request_id or "").strip()
        self._whatsapp_tool_ack_active_until = time.monotonic() + max(ttl_s, 0.0)
        logger.info("WHATSAPP_TOOL_ACK_ACTIVE_SET request_id=%s", self._whatsapp_tool_ack_request_id or "unknown")

    def clear_whatsapp_tool_ack_active(self, request_id: str = "") -> None:
        req = str(request_id or self._whatsapp_tool_ack_request_id or "").strip() or "unknown"
        self._whatsapp_tool_ack_active_until = 0.0
        self._whatsapp_tool_ack_request_id = ""
        logger.info("WHATSAPP_TOOL_ACK_ACTIVE_CLEARED request_id=%s", req)

    def is_whatsapp_tool_ack_active(self) -> bool:
        active = time.monotonic() < self._whatsapp_tool_ack_active_until
        logger.info("WHATSAPP_TOOL_ACK_ACTIVE_CHECK active=%s", str(active).lower())
        return active

    def set_voice_output_owner(self, source: str, request_id: str = "", turn_id: str = "") -> None:
        normalized_source = str(source or "unknown").strip().lower() or "unknown"
        logger.info("TURN_OWNER_SET source=%s request_id=%s turn_id=%s", normalized_source, request_id or "none", turn_id or "none")
        if normalized_source == "whatsapp_tool":
            self.set_whatsapp_tool_ack_active(request_id or turn_id or "tool_ack", ttl_s=8.0)
            logger.info("TURN_TOOL_ACK_ACTIVE")
        if normalized_source in {"creation_task_ack", "manus_completion_notify"}:
            logger.info("TURN_TOOL_ACK_ACTIVE source=%s request_id=%s", normalized_source, request_id or "none")

    def release_voice_output_owner(self, source: str, request_id: str = "", turn_id: str = "") -> None:
        normalized_source = str(source or "unknown").strip().lower() or "unknown"
        logger.info("TURN_OWNER_RELEASED source=%s request_id=%s turn_id=%s", normalized_source, request_id or "none", turn_id or "none")
        if normalized_source == "whatsapp_tool":
            self.clear_whatsapp_tool_ack_active(request_id or turn_id or "tool_ack")

    def register_user_turn(
        self,
        turn_id: str,
        user_text: str,
        *,
        preferred_reply_source: str | None = None,
    ) -> TurnState:
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            raise ValueError("turn_id is required")
        if normalized_turn_id in self._active_turns:
            turn_state = self._active_turns[normalized_turn_id]
            updated_text = str(user_text or "").strip()
            if updated_text:
                current_text = str(turn_state.user_text or "").strip()
                if updated_text != current_text and (
                    not current_text
                    or updated_text.startswith(current_text)
                    or len(updated_text) > len(current_text)
                ):
                    turn_state.user_text = updated_text
            if preferred_reply_source:
                turn_state.preferred_reply_source = preferred_reply_source
            logger.debug("[TURN] Turno ja registrado - turn_id=%s", normalized_turn_id)
            return turn_state

        turn_state = TurnState(
            turn_id=normalized_turn_id,
            user_text=str(user_text or "").strip(),
            preferred_reply_source=str(preferred_reply_source or "").strip() or None,
        )
        self._active_turns[normalized_turn_id] = turn_state
        logger.info(
            "TURN_PREFERRED_SOURCE_RESET_NEW_TURN turn_id=%s preferred=%s",
            normalized_turn_id,
            turn_state.preferred_reply_source or "any",
        )
        logger.info(
            "[TURN] Registrado - turn_id=%s texto=%r preferred=%s",
            normalized_turn_id,
            turn_state.user_text[:60],
            turn_state.preferred_reply_source or "any",
        )
        self._latest_turn_id = normalized_turn_id
        for listener in list(self._new_turn_listeners):
            try:
                listener(normalized_turn_id)
            except Exception as exc:
                logger.warning("TURN_LISTENER_FAILED error=%s", exc)
        return turn_state

    @property
    def latest_turn_id(self) -> str:
        return self._latest_turn_id

    def add_new_turn_listener(self, listener) -> None:
        """Chamado a cada turno novo de usuário (ex.: avisar a interface para descartar apresentações antigas)."""
        self._new_turn_listeners.append(listener)

    def get_turn(self, turn_id: str) -> TurnState | None:
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return None
        return self._active_turns.get(normalized_turn_id)

    def can_start_reply(self, turn_id: str, source: str) -> tuple[bool, str]:
        normalized_turn_id = str(turn_id or "").strip()
        normalized_source = str(source or "").strip() or "unknown"
        if (
            time.monotonic() < self._whatsapp_tool_ack_active_until
            and normalized_source not in {"whatsapp_tool"}
        ):
            logger.warning(
                "TURN_BLOCKED_WHATSAPP_TOOL_ACTIVE source=%s turn_id=%s",
                normalized_source,
                normalized_turn_id or "none",
            )
            return False, "whatsapp_tool_ack_active"
        if not normalized_turn_id:
            logger.warning(
                "[TURN] BLOQUEADO - turn_id ausente - source=%s",
                normalized_source,
            )
            return False, "missing_turn_id"
        if normalized_turn_id in self._completed_turn_ids:
            logger.warning(
                "[TURN] BLOQUEADO - turno ja completado - turn_id=%s source=%s",
                normalized_turn_id,
                normalized_source,
            )
            return False, "turn_already_completed"

        turn_state = self._active_turns.get(normalized_turn_id)
        if turn_state is None:
            logger.warning(
                "[TURN] BLOQUEADO - turno desconhecido - turn_id=%s source=%s",
                normalized_turn_id,
                normalized_source,
            )
            return False, "turn_not_found"

        preferred = str(turn_state.preferred_reply_source or "").strip()
        if preferred and preferred != normalized_source:
            if normalized_source == "whatsapp_tool" and preferred == "auto":
                logger.info("TURN_SOURCE_CONFLICT_FIXED old=auto new=whatsapp_tool")
            else:
                logger.warning(
                    "[TURN] BLOQUEADO - source nao preferida - turn_id=%s preferred=%s tentativa=%s",
                    normalized_turn_id,
                    preferred,
                    normalized_source,
                )
                logger.warning(
                    "TURN_PREFERRED_SOURCE_BLOCK_REASON turn_id=%s preferred=%s attempted=%s reason=preferred_source_is_%s",
                    normalized_turn_id,
                    preferred,
                    normalized_source,
                    preferred,
                )
                return False, f"preferred_source_is_{preferred}"

        if turn_state.reply_started and turn_state.reply_owner != normalized_source:
            logger.warning(
                "[TURN] BLOQUEADO - resposta ja iniciada - turn_id=%s owner=%s tentativa=%s",
                normalized_turn_id,
                turn_state.reply_owner,
                normalized_source,
            )
            return False, f"reply_owned_by_{turn_state.reply_owner}"

        logger.info(
            "[TURN] PERMITIDO - turn_id=%s source=%s",
            normalized_turn_id,
            normalized_source,
        )
        return True, "allowed"

    def is_valid_turn(self, turn_id: str) -> bool:
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return False
        if normalized_turn_id in self._completed_turn_ids:
            return False
        return normalized_turn_id in self._active_turns

    def register_tool_ack_turn(self, turn_id: str, *, user_text: str = "") -> TurnState:
        logger.info("TURN_TOOL_ACK_CREATED source=whatsapp_tool turn_id=%s", turn_id)
        return self.register_user_turn(turn_id=turn_id, user_text=user_text, preferred_reply_source="whatsapp_tool")

    def clear_preferred_source_for_creation_task(self, turn_id: str) -> str | None:
        turn_state = self.get_turn(turn_id)
        if turn_state is None:
            return None
        old_preferred = str(turn_state.preferred_reply_source or "").strip() or None
        if old_preferred:
            turn_state.preferred_reply_source = None
            logger.info("CREATION_TASK_PREFERRED_SOURCE_CLEARED old_preferred=%s", old_preferred)
        return old_preferred

    def claim_reply_ownership(
        self,
        turn_id: str,
        source: str,
        *,
        response_id: str | None = None,
        speech_id: str | None = None,
    ) -> bool:
        normalized_turn_id = str(turn_id or "").strip()
        normalized_source = str(source or "").strip() or "unknown"
        can_reply, reason = self.can_start_reply(normalized_turn_id, normalized_source)
        if not can_reply:
            logger.warning(
                "[TURN] Falha ao declarar propriedade - turn_id=%s source=%s reason=%s",
                normalized_turn_id,
                normalized_source,
                reason,
            )
            return False

        turn_state = self._active_turns[normalized_turn_id]
        turn_state.reply_started = True
        turn_state.reply_owner = normalized_source
        turn_state.response_id = str(response_id or "").strip() or turn_state.response_id
        turn_state.speech_id = str(speech_id or "").strip() or turn_state.speech_id
        logger.info(
            "[TURN] Propriedade declarada - turn_id=%s owner=%s response_id=%s speech_id=%s",
            normalized_turn_id,
            normalized_source,
            turn_state.response_id,
            turn_state.speech_id,
        )
        return True

    def mark_reply_completed(self, turn_id: str) -> None:
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return
        turn_state = self._active_turns.pop(normalized_turn_id, None)
        if turn_state is None:
            return
        turn_state.reply_completed = True
        self._completed_turn_ids.add(normalized_turn_id)
        self._completed_turn_order.append(normalized_turn_id)
        while len(self._completed_turn_order) > self._max_completed_history:
            old_turn_id = self._completed_turn_order.popleft()
            self._completed_turn_ids.discard(old_turn_id)
        logger.info(
            "[TURN] Completado - turn_id=%s owner=%s",
            normalized_turn_id,
            turn_state.reply_owner or "unknown",
        )
        logger.info("TURN_LOCK_RELEASED_AFTER_REPLY_FINISH turn_id=%s", normalized_turn_id)

    def release_preferred_source(self, turn_id: str, *, reason: str) -> bool:
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return False
        turn_state = self._active_turns.get(normalized_turn_id)
        if turn_state is None:
            return False
        if not turn_state.preferred_reply_source:
            return False
        turn_state.preferred_reply_source = None
        logger.info("TURN_PREFERRED_SOURCE_RELEASED turn_id=%s reason=%s", normalized_turn_id, reason or "unknown")
        if str(reason or "").strip() == "voice_output_finished":
            logger.info("TURN_PREFERRED_SOURCE_RELEASED_ON_FINISH turn_id=%s", normalized_turn_id)
        logger.info("TURN_MANUAL_SOURCE_CLEARED turn_id=%s", normalized_turn_id)
        return True

    def cleanup_old_turns(self) -> None:
        now = time.monotonic()
        expired_turn_ids = [
            turn_id
            for turn_id, turn_state in self._active_turns.items()
            if (now - turn_state.created_at) > self._turn_timeout_s
        ]
        for turn_id in expired_turn_ids:
            turn_state = self._active_turns.pop(turn_id, None)
            if turn_state is None:
                continue
            logger.warning(
                "[TURN] Removendo turno antigo - turn_id=%s age_s=%.1f owner=%s",
                turn_id,
                now - turn_state.created_at,
                turn_state.reply_owner or "none",
            )

    def clear_stale_locks_for_new_turn(self) -> int:
        cleared = 0
        now = time.monotonic()
        for turn_id, turn_state in list(self._active_turns.items()):
            turn_age = now - float(turn_state.created_at or now)
            if turn_age < 2.0:
                continue
            if turn_state.reply_started and not turn_state.reply_completed:
                turn_state.preferred_reply_source = None
                turn_state.reply_owner = None
                turn_state.reply_started = False
                cleared += 1
        if cleared:
            logger.info("USER_TURN_LOCKS_STALE_CLEARED count=%s", cleared)
        return cleared

    def clear_stale_voice_locks(self, *, reason: str, turn_id: str | None = None) -> int:
        normalized_turn_id = str(turn_id or "").strip() or None
        candidates = (
            [(normalized_turn_id, self._active_turns.get(normalized_turn_id))]
            if normalized_turn_id
            else list(self._active_turns.items())
        )
        cleared = 0
        for current_turn_id, turn_state in candidates:
            if turn_state is None:
                continue
            had_lock = bool(turn_state.preferred_reply_source or turn_state.reply_owner or turn_state.reply_started)
            if not had_lock:
                continue
            turn_state.preferred_reply_source = None
            turn_state.reply_owner = None
            turn_state.reply_started = False
            turn_state.speech_id = None
            cleared += 1
            logger.info(
                "TURN_STALE_VOICE_LOCK_CLEARED turn_id=%s reason=%s",
                current_turn_id,
                reason or "unknown",
            )
        return cleared


_TURN_COORDINATOR = TurnCoordinator()


def get_turn_coordinator() -> TurnCoordinator:
    return _TURN_COORDINATOR


__all__ = ["TurnCoordinator", "TurnState", "get_turn_coordinator"]
