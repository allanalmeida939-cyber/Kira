from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any, Callable

from fastapi import WebSocket

from services.interests import note_user_text
from services.session_recap import get_session_recap

try:
    from agent.runtime.gemini_output_filter import extract_clean_response
    from agent.runtime.transcript_filter import sanitize_for_transcript
except Exception:  # pragma: no cover - fallback para execucao local em agent/
    from runtime.gemini_output_filter import extract_clean_response
    from runtime.transcript_filter import sanitize_for_transcript


log = logging.getLogger("transcription_bus")


_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AIza[0-9A-Za-z_\-]{10,}"),
    re.compile(r"\b(?:api[_-]?key|token|secret)\s*[:=]\s*\S+", re.IGNORECASE),
)

_TECHNICAL_VISUAL_PREFIX_RE = re.compile(
    r"^\s*(?:vision_\S*|\[kira\s+vision\]|(?:monitor(?:\s+\d+)?|tela\s+\d+|frame(?:\s+atual)?|visual\s+context|contexto\s+visual)\s*:)",
    re.IGNORECASE,
)


def _is_internal_visual_text(text: str) -> bool:
    cleaned = str(text or "").strip()
    if not cleaned:
        return False
    if _TECHNICAL_VISUAL_PREFIX_RE.search(cleaned):
        return True
    lowered = cleaned.lower()
    return any(
        term in lowered
        for term in (
            "janela do navegador foi fechada",
            "screen change summary",
            "visual context update",
            "contexto visual leve",
            "frame summary",
        )
    )


def _redact_transcript_secrets(text: str) -> str:
    redacted = str(text or "")
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted


def _record_final_dialogue(text: str, speaker: str, source: str) -> None:
    """Falas finais do Chefe e da Kira vão para o registro da última conversa (best-effort)."""
    del source
    if speaker not in ("user", "kira"):
        return
    try:
        get_session_recap().add(speaker, text)
    except Exception as exc:
        log.warning("SESSION_RECAP_RECORD_FAILED ignored=true error=%s", exc)


class TranscriptionBus:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        # A interface pode conectar depois que uma tool já alterou a aparência.  Mantemos
        # o último estado no processo e o reenviamos no próximo handshake; o retorno da
        # tool só é sucesso depois que a tela confirma a aplicação.
        self._appearance_state: dict[str, str] = {}
        self._appearance_revision = 0
        self._appearance_waiters: dict[str, dict[str, Any]] = {}
        self._appearance_ack_timeout_s = 1.0
        self._recent_hashes: dict[str, float] = {}
        self._dedup_window_s = 5.0
        self._long_reply_window_s = 90.0
        self._sequence_counter = 0
        self._turn_user_final_seen: dict[str, float] = {}
        self._turn_pending_kira: dict[str, list[dict[str, Any]]] = {}
        self._turn_last_user_payload: dict[str, dict[str, Any]] = {}
        self._turn_cleanup_window_s = 30.0
        self.tagger: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    def _next_sequence_id(self) -> int:
        self._sequence_counter += 1
        return self._sequence_counter

    def _cleanup_turn_buffers(self) -> None:
        now = time.monotonic()
        stale_turns = [
            turn_id
            for turn_id, seen_at in self._turn_user_final_seen.items()
            if now - float(seen_at or now) > self._turn_cleanup_window_s
        ]
        for turn_id in stale_turns:
            self._turn_user_final_seen.pop(turn_id, None)
            self._turn_pending_kira.pop(turn_id, None)
            self._turn_last_user_payload.pop(turn_id, None)

    def _payload_sequence_id(self, payload: dict[str, Any]) -> int:
        raw_seq = payload.get("sequence_id")
        try:
            sequence_id = int(raw_seq)
        except Exception:
            sequence_id = 0
        if sequence_id <= 0:
            sequence_id = self._next_sequence_id()
            payload["sequence_id"] = sequence_id
        else:
            self._sequence_counter = max(self._sequence_counter, sequence_id)
        return sequence_id

    def _payload_turn_id(self, payload: dict[str, Any]) -> str:
        turn_id = str(payload.get("turn_id") or payload.get("parent_turn_id") or "").strip()
        if turn_id:
            return turn_id
        message_id = str(payload.get("message_id") or payload.get("request_id") or "").strip()
        return message_id or "turnless"

    def _is_duplicate(self, text: str, speaker: str) -> bool:
        normalized_text = " ".join(str(text or "").strip().lower().split())
        key = hashlib.md5(f"{speaker}:{normalized_text}".encode("utf-8")).hexdigest()
        now = time.monotonic()
        window = self._long_reply_window_s if speaker.lower() in {"kira", "assistant"} and len(normalized_text) > 40 else self._dedup_window_s
        self._recent_hashes = {h: t for h, t in self._recent_hashes.items() if (now - t) < self._long_reply_window_s}
        if key in self._recent_hashes and (now - self._recent_hashes[key]) < window:
            return True
        self._recent_hashes[key] = now
        return False

    def _normalize_speaker(self, speaker: str | None, source: str) -> str:
        raw = str(speaker or "").strip().lower()
        normalized_source = str(source or "").strip().lower()
        if raw in {"user", "usuario", "voce", "você", "you"}:
            return "user"
        if raw in {"kira", "assistant", "agent", "kira-agent"}:
            return "kira"
        if raw in {"system", "sistema"}:
            return "system"
        if normalized_source in {"user", "usuario", "voice", "mic", "speech", "transcript", "livekit_user"}:
            return "user"
        if normalized_source in {
            "kira",
            "assistant",
            "agent",
            "assistant_text",
            "agent_speech_committed",
            "tool_ack",
            "fallback",
            "livekit_transcription",
            "realtime_output_transcript",
            "kira_streaming",
        }:
            return "kira"
        return "system"

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self.clients.add(websocket)
            total = len(self.clients)
        log.info("TRANSCRIPTION_WS_CLIENT_CONNECTED total=%s", total)
        if self._appearance_state:
            # Replay sem request_id: é sincronização de estado, não uma nova ação da Kira.
            await self.send_to(
                websocket,
                {
                    "type": "ui_appearance",
                    "revision": self._appearance_revision,
                    "replay": True,
                    **self._appearance_state,
                },
            )

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self.clients.discard(websocket)
            total = len(self.clients)
        log.info("TRANSCRIPTION_WS_CLIENT_DISCONNECTED total=%s", total)

    async def _send_payload(self, websocket: WebSocket, payload: dict[str, Any]) -> bool:
        try:
            await websocket.send_text(json.dumps(payload, ensure_ascii=False))
            return True
        except Exception as exc:
            log.warning("TRANSCRIPT_UI_EVENT_FAILED_NON_FATAL error=%s", exc)
            return False

    async def send_to(self, websocket: WebSocket, payload: dict[str, Any]) -> bool:
        enriched = dict(payload or {})
        enriched.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        ok = await self._send_payload(websocket, enriched)
        if not ok:
            await self.disconnect(websocket)
        return ok

    async def broadcast(self, payload: dict[str, Any]) -> int:
        enriched = dict(payload or {})
        if self.tagger is not None and enriched.get("type") in {"transcription", "transcript_message"}:
            enriched = self.tagger(enriched)  # de onde veio a fala ("celular"): services/remote_devices.py
        enriched.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        async with self._lock:
            clients = list(self.clients)
        if not clients:
            return 0

        dead: list[WebSocket] = []
        for websocket in clients:
            if not await self._send_payload(websocket, enriched):
                dead.append(websocket)

        if dead:
            async with self._lock:
                for websocket in dead:
                    self.clients.discard(websocket)

        if enriched.get("type") in {"transcription", "transcript_message"}:
            log.info(
                "TRANSCRIPTION_BROADCAST source=%s final=%s text=%s",
                enriched.get("source"),
                enriched.get("is_final", enriched.get("final")),
                str(enriched.get("text") or "")[:120],
            )
        return max(0, len(clients) - len(dead))

    async def publish_appearance(self, patch: dict[str, str], *, timeout_s: float | None = None) -> dict[str, Any]:
        """Publica aparência e só confirma quando uma UI conectada responde com ACK."""
        normalized = {
            str(key): str(value).strip().lower()
            for key, value in (patch or {}).items()
            if str(key).strip() and str(value).strip()
        }
        if not normalized:
            return {"ok": False, "applied": False, "reason": "empty"}
        self._appearance_state.update(normalized)
        self._appearance_revision += 1
        request_id = f"appearance_{uuid4().hex[:12]}"
        event = asyncio.Event()
        self._appearance_waiters[request_id] = {"event": event, "applied": False}
        delivered = await self.broadcast(
            {
                "type": "ui_appearance",
                "request_id": request_id,
                "revision": self._appearance_revision,
                **normalized,
            }
        )
        if not delivered:
            self._appearance_waiters.pop(request_id, None)
            log.warning(
                "UI_APPEARANCE_DELIVERY applied=false delivered=0 reason=ui_not_connected request_id=%s",
                request_id,
            )
            return {
                "ok": False,
                "applied": False,
                "queued": True,
                "delivered": 0,
                "reason": "ui_not_connected",
                "request_id": request_id,
            }
        timeout = self._appearance_ack_timeout_s if timeout_s is None else max(0.05, float(timeout_s))
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning(
                "UI_APPEARANCE_DELIVERY applied=false delivered=%s reason=ui_ack_timeout request_id=%s",
                delivered,
                request_id,
            )
        waiter = self._appearance_waiters.pop(request_id, {})
        applied = bool(waiter.get("applied"))
        if applied:
            log.info(
                "UI_APPEARANCE_DELIVERY applied=true delivered=%s request_id=%s",
                delivered,
                request_id,
            )
        return {
            "ok": applied,
            "applied": applied,
            "delivered": delivered,
            "reason": "ack" if applied else "ui_ack_missing",
            "request_id": request_id,
        }

    async def acknowledge_appearance(self, websocket: WebSocket, payload: dict[str, Any]) -> bool:
        request_id = str((payload or {}).get("request_id") or "").strip()
        if not request_id:
            return False
        async with self._lock:
            waiter = self._appearance_waiters.get(request_id)
            connected = websocket in self.clients
        if waiter is None or not connected:
            return False
        applied = bool((payload or {}).get("applied"))
        waiter["applied"] = applied
        event = waiter.get("event")
        if isinstance(event, asyncio.Event):
            event.set()
        log.info(
            "UI_APPEARANCE_ACK applied=%s request_id=%s",
            str(applied).lower(),
            request_id,
        )
        return True

    async def _broadcast_transcription_payload(self, payload: dict[str, Any]) -> None:
        await self.broadcast(payload)

    async def _flush_pending_kira_for_turn(self, turn_id: str) -> None:
        pending = self._turn_pending_kira.pop(turn_id, [])
        if not pending:
            return
        for payload in sorted(pending, key=lambda item: int(item.get("sequence_id") or 0)):
            log.info(
                "TRANSCRIPTION_KIRA_RELEASED_AFTER_USER turn_id=%s",
                turn_id,
            )
            await self._broadcast_transcription_payload(payload)

    async def _release_kira_after_timeout(self, turn_id: str, sequence_id: int) -> None:
        await asyncio.sleep(0.45)
        pending = self._turn_pending_kira.get(turn_id)
        if not pending:
            return
        if turn_id in self._turn_user_final_seen:
            await self._flush_pending_kira_for_turn(turn_id)
            return
        ready: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        for payload in pending:
            if int(payload.get("sequence_id") or 0) == sequence_id:
                ready.append(payload)
            else:
                remaining.append(payload)
        if remaining:
            self._turn_pending_kira[turn_id] = remaining
        else:
            self._turn_pending_kira.pop(turn_id, None)
        for payload in ready:
            log.info("TRANSCRIPTION_ORDER_TIMEOUT reason=user_final_missing turn_id=%s", turn_id)
            await self._broadcast_transcription_payload(payload)

    async def _ordered_broadcast_transcription(self, payload: dict[str, Any]) -> bool:
        self._cleanup_turn_buffers()
        sequence_id = self._payload_sequence_id(payload)
        turn_id = self._payload_turn_id(payload)
        payload.setdefault("turn_id", turn_id)
        payload.setdefault("created_at", payload.get("timestamp") or datetime.now(timezone.utc).isoformat())
        payload.setdefault("message_kind", payload.get("transcript_kind") or ("conversation_user" if payload.get("speaker") == "user" else "kira_response"))
        payload.setdefault("transcript_kind", payload.get("message_kind"))
        speaker = str(payload.get("speaker") or "").strip().lower()
        log.info(
            "TRANSCRIPTION_BUS_BUFFERED seq=%s speaker=%s turn_id=%s",
            sequence_id,
            speaker or "unknown",
            turn_id,
        )
        log.info("TRANSCRIPTION_BUS_ORDER_BY_SEQUENCE")
        if speaker == "user":
            previous = self._turn_last_user_payload.get(turn_id)
            if previous is not None:
                payload["sequence_id"] = int(previous.get("sequence_id") or sequence_id)
                payload["message_id"] = str(previous.get("message_id") or payload.get("message_id") or "")
                payload["replace_previous"] = True
                payload["replace_message_id"] = payload["message_id"]
                log.info("TRANSCRIPTION_FINAL_REPLACES_PARTIAL turn_id=%s", turn_id)
                log.info("TRANSCRIPTION_UI_REPLACE_SENT seq=%s", payload["sequence_id"])
                log.info("TRANSCRIPTION_DUPLICATE_USER_SUPPRESSED")
            self._turn_last_user_payload[turn_id] = dict(payload)
            self._turn_user_final_seen[turn_id] = time.monotonic()
            await self._broadcast_transcription_payload(payload)
            log.info("TRANSCRIPTION_USER_FINAL_RELEASED_BEFORE_KIRA turn_id=%s", turn_id)
            await self._flush_pending_kira_for_turn(turn_id)
            return True
        if speaker == "kira" and turn_id != "turnless" and turn_id not in self._turn_user_final_seen:
            self._turn_pending_kira.setdefault(turn_id, []).append(dict(payload))
            log.info("TRANSCRIPTION_KIRA_HELD_WAITING_USER turn_id=%s", turn_id)
            asyncio.create_task(self._release_kira_after_timeout(turn_id, sequence_id))
            return False
        await self._broadcast_transcription_payload(payload)
        return True

    async def send_transcription(
        self,
        text: str,
        *,
        speaker: str | None = None,
        source: str = "system",
        final: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> None:
        normalized = str(text or "").strip()
        if not normalized:
            return
        normalized_source = str(source or "system").strip().lower() or "system"
        if normalized_source not in {"user", "usuario", "voice", "mic", "speech", "transcript"}:
            normalized = extract_clean_response(normalized)
            if not normalized:
                log.warning(
                    "TRANSCRIPTION_BLOCKED_EMPTY_AFTER_SANITIZE source=%s",
                    normalized_source,
                )
                return
        cleaned = sanitize_for_transcript(_redact_transcript_secrets(normalized), source=normalized_source)
        if cleaned is None:
            log.info(
                "TRANSCRIPTION_BLOCKED source=%s text=%s",
                normalized_source,
                normalized[:120],
            )
            return
        if not final:
            turn_id = str((extra or {}).get("turn_id") or (extra or {}).get("parent_turn_id") or "turnless")
            log.info("TRANSCRIPTION_PARTIAL_BUFFERED turn_id=%s", turn_id)
            log.info(
                "TRANSCRIPT_FINAL_IGNORED source=%s reason=partial_blocked text_preview=%r",
                normalized_source,
                cleaned[:40],
            )
            return
        effective_speaker = self._normalize_speaker(
            speaker if speaker is not None else (extra or {}).get("speaker"),
            normalized_source,
        )
        if effective_speaker == "kira" and _is_internal_visual_text(cleaned):
            log.info("TRANSCRIPTION_BUS_BLOCKED_INTERNAL_VISUAL_TEXT")
            log.info("TRANSCRIPTION_VISUAL_INTERNAL_BLOCKED")
            return
        if effective_speaker == "kira" and normalized_source == "visual_question":
            log.info("TRANSCRIPTION_BUS_ALLOWED_USER_FACING_VISUAL_REPLY")
        transcript_kind = str((extra or {}).get("transcript_kind") or (extra or {}).get("message_kind") or "").strip().lower()
        bypass_text_dedupe = normalized_source in {"ui_panel", "volume_command", "local_command"} or transcript_kind in {"panel_command", "local_command"}
        if bypass_text_dedupe:
            log.info(
                "TRANSCRIPTION_DEDUPE_BYPASSED source=%s kind=%s reason=local_command_ack",
                normalized_source,
                transcript_kind or "none",
            )
        elif self._is_duplicate(cleaned, effective_speaker):
            log.info("TRANSCRIPTION_DEDUPE_SKIPPED key=%s", effective_speaker)
            log.info("TRANSCRIPT_VISUAL_DEDUPE_HIT speaker=%s reason=recent_duplicate text_preview=%r", effective_speaker, cleaned[:40])
            return
        log.info(
            "TRANSCRIPT_FINAL_ACCEPTED source=%s text_preview=%r",
            normalized_source,
            cleaned[:40],
        )
        _record_final_dialogue(cleaned, effective_speaker, normalized_source)
        if effective_speaker == "user":
            note_user_text(cleaned)
            log.info('TRANSCRIPTION_INPUT_RECEIVED speaker=VOCE text="%s"', cleaned[:120])
            log.info("TRANSCRIPT_USER_PUBLISHED source=%s", normalized_source)
        elif effective_speaker == "kira":
            log.info('TRANSCRIPTION_OUTPUT_RECEIVED speaker=KIRA text="%s"', cleaned[:120])
            log.info("TRANSCRIPT_ASSISTANT_PUBLISHED source=%s", normalized_source)
        payload: dict[str, Any] = {
            "type": "transcript_message",
            "role": "assistant" if effective_speaker == "kira" else ("user" if effective_speaker == "user" else "system"),
            "speaker": effective_speaker,
            "source": normalized_source,
            "text": cleaned,
            "is_final": bool(final),
            "final": bool(final),
            "message_id": str((extra or {}).get("message_id") or (extra or {}).get("reply_id") or (extra or {}).get("request_id") or f"msg_{uuid4().hex[:12]}"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            payload.update(extra)
        if not payload.get("sequence_id"):
            payload["sequence_id"] = self._next_sequence_id()
        payload.setdefault("turn_id", str((extra or {}).get("turn_id") or (extra or {}).get("parent_turn_id") or ""))
        payload.setdefault(
            "message_kind",
            (extra or {}).get("transcript_kind")
            or ("conversation_user" if effective_speaker == "user" else ("kira_response" if effective_speaker == "kira" else "system")),
        )
        payload.setdefault("transcript_kind", payload.get("message_kind"))
        published_now = await self._ordered_broadcast_transcription(payload)
        if not published_now:
            log.info(
                "TRANSCRIPTION_PUBLISH_HELD speaker=%s source=%s reason=waiting_user_final",
                effective_speaker,
                normalized_source,
            )
            return
        log.info(
            "TRANSCRIPTION_PUBLISHED speaker=%s final=%s",
            "VOCE" if effective_speaker == "user" else effective_speaker.upper(),
            str(bool(final)).lower(),
        )
        log.info(
            "TRANSCRIPTION_BUS_PUBLISH speaker=%s source=%s text_len=%d final=%s",
            payload.get("speaker"),
            payload.get("source"),
            len(str(payload.get("text") or "")),
            payload.get("final"),
        )

    async def send_status(self, state: dict[str, Any]) -> None:
        await self.broadcast(
            {
                "type": "status",
                "livekit_status": state.get("livekit_status"),
                "voice_mode": state.get("voice_mode"),
                "restart_required_for_voice": bool(state.get("restart_required_for_voice")),
                "livekit_message": state.get("livekit_message"),
                "livekit_current_slot": state.get("livekit_current_slot"),
                "livekit_total_slots": state.get("livekit_total_slots"),
                "livekit_key": state.get("livekit_key"),
            }
        )


transcription_bus = TranscriptionBus()
