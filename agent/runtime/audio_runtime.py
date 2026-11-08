from __future__ import annotations

import asyncio
import inspect
import logging
import time
from datetime import datetime
from typing import Any

from livekit import rtc

from resilience import log_exception_safely


logger = logging.getLogger(__name__)


def _track_source_label(source: rtc.TrackSource.ValueType) -> str:
    if source == rtc.TrackSource.SOURCE_CAMERA:
        return "camera"
    if source == rtc.TrackSource.SOURCE_MICROPHONE:
        return "microphone"
    if source == rtc.TrackSource.SOURCE_UNKNOWN:
        return "unknown"
    return "unknown"


def _participant_kind_label(kind: rtc.ParticipantKind.ValueType) -> str:
    try:
        return rtc.ParticipantKind.Name(kind)
    except Exception:
        return str(kind)


def _is_audio_publication(publication) -> bool:
    return bool(publication) and publication.kind == rtc.TrackKind.KIND_AUDIO


def _is_audio_track(track) -> bool:
    return bool(track) and track.kind == rtc.TrackKind.KIND_AUDIO


def _is_candidate_user_audio_source(source: rtc.TrackSource.ValueType) -> bool:
    return source in {
        rtc.TrackSource.SOURCE_MICROPHONE,
        rtc.TrackSource.SOURCE_UNKNOWN,
    }


class RoomAudioBridge:
    def __init__(self, *, room: rtc.Room, session) -> None:
        self._room = room
        self._session = session
        self._registered = False

    def register_handlers(self) -> None:
        if self._registered:
            return

        self._registered = True
        self._room.on("participant_connected", self._on_participant_connected)
        self._room.on("active_speakers_changed", self._on_active_speakers_changed)
        self._room.on("track_published", self._on_track_published)
        self._room.on("track_subscribed", self._on_track_subscribed)
        self._room.on("track_unsubscribed", self._on_track_unsubscribed)
        self._room.on("track_subscription_failed", self._on_track_subscription_failed)

        self._patch_room_audio_inputs(reason="register_handlers")
        for participant in list(self._room.remote_participants.values()):
            self._on_participant_connected(participant)
            for publication in list(participant.track_publications.values()):
                if not _is_audio_publication(publication):
                    continue
                if publication.track is not None:
                    self._on_track_subscribed(publication.track, publication, participant)
                else:
                    self._ensure_audio_subscription(
                        publication,
                        participant,
                        reason="register_existing_publication",
                    )

        logger.info(
            "Handlers de audio registrados. Audio remoto tera subscribe reforcado e suporte a source=unknown."
        )

    async def shutdown(self) -> None:
        if not self._registered:
            return

        self._room.off("participant_connected", self._on_participant_connected)
        self._room.off("active_speakers_changed", self._on_active_speakers_changed)
        self._room.off("track_published", self._on_track_published)
        self._room.off("track_subscribed", self._on_track_subscribed)
        self._room.off("track_unsubscribed", self._on_track_unsubscribed)
        self._room.off("track_subscription_failed", self._on_track_subscription_failed)
        self._registered = False

    def _on_participant_connected(self, participant: rtc.RemoteParticipant) -> None:
        self._maybe_select_room_participant(participant, reason="participant_connected")
        self._patch_room_audio_inputs(reason="participant_connected")
        audio_publications = [
            publication
            for publication in participant.track_publications.values()
            if _is_audio_publication(publication)
        ]
        logger.info(
            "Participante conectado para voz participant=%s kind=%s audio_publications=%s",
            participant.identity,
            _participant_kind_label(participant.kind),
            [
                {
                    "sid": publication.sid,
                    "source": _track_source_label(publication.source),
                    "subscribed": bool(getattr(publication, "subscribed", False)),
                }
                for publication in audio_publications
            ],
        )
        for publication in audio_publications:
            self._ensure_audio_subscription(
                publication,
                participant,
                reason="participant_connected",
            )

    def _on_active_speakers_changed(self, speakers: list[rtc.Participant]) -> None:
        if not speakers:
            logger.debug("LiveKit room voice: active_speakers_changed vazio")
            return

        logger.debug(
            "LiveKit room voice: active_speakers_changed participantes=%s",
            [
                getattr(speaker, "identity", "")
                for speaker in speakers
                if getattr(speaker, "identity", "")
            ],
        )

    def _on_track_published(
        self,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        self._maybe_select_room_participant(participant, reason="track_published")
        logger.info(
            "Track publicada na room participant=%s kind=%s source=%s sid=%s subscribed=%s",
            participant.identity,
            "audio" if publication.kind == rtc.TrackKind.KIND_AUDIO else "video",
            _track_source_label(publication.source),
            publication.sid,
            bool(getattr(publication, "subscribed", False)),
        )
        if not _is_audio_publication(publication):
            return
        self._ensure_audio_subscription(publication, participant, reason="track_published")
        if publication.track is not None:
            self._attach_track_to_room_audio_input(
                publication.track,
                publication,
                participant,
                reason="track_published_existing_track",
            )

    def _on_track_subscribed(
        self,
        track: rtc.RemoteTrack,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if not _is_audio_track(track) or not _is_audio_publication(publication):
            return

        self._maybe_select_room_participant(participant, reason="track_subscribed")
        logger.info(
            "Track de audio subscribed participant=%s source=%s sid=%s",
            participant.identity,
            _track_source_label(publication.source),
            publication.sid,
        )
        self._patch_room_audio_inputs(reason="track_subscribed")
        self._attach_track_to_room_audio_input(
            track,
            publication,
            participant,
            reason="track_subscribed",
        )

    def _on_track_unsubscribed(
        self,
        track: rtc.RemoteTrack | None,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        del track
        if not _is_audio_publication(publication):
            return

        logger.info(
            "Track de audio unsubscribed participant=%s source=%s sid=%s",
            participant.identity,
            _track_source_label(publication.source),
            publication.sid,
        )

    def _on_track_subscription_failed(
        self,
        participant: rtc.RemoteParticipant,
        track_sid: str,
        error: str,
    ) -> None:
        logger.warning(
            "Falha ao assinar track de audio/video participant=%s sid=%s erro=%s",
            participant.identity,
            track_sid,
            error,
        )

    def _ensure_audio_subscription(
        self,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
        *,
        reason: str,
    ) -> None:
        if not _is_audio_publication(publication):
            return
        if not _is_candidate_user_audio_source(publication.source):
            logger.debug(
                "Track de audio ignorada para voz participant=%s source=%s sid=%s reason=%s",
                participant.identity,
                _track_source_label(publication.source),
                publication.sid,
                reason,
            )
            return
        if publication.subscribed:
            logger.debug(
                "Track de audio ja subscribed participant=%s source=%s sid=%s reason=%s",
                participant.identity,
                _track_source_label(publication.source),
                publication.sid,
                reason,
            )
            return

        logger.info(
            "Solicitando subscribe da track de audio participant=%s source=%s sid=%s reason=%s",
            participant.identity,
            _track_source_label(publication.source),
            publication.sid,
            reason,
        )
        try:
            publication.set_subscribed(True)
        except Exception as exc:
            log_exception_safely(
                logger,
                f"Falha ao solicitar subscribe da track de audio participant={participant.identity} sid={publication.sid}",
                exc,
                level=logging.WARNING,
            )

    def _maybe_select_room_participant(
        self,
        participant: rtc.RemoteParticipant,
        *,
        reason: str,
    ) -> None:
        room_io = getattr(self._session, "_room_io", None)
        if room_io is None:
            return
        linked_participant = getattr(room_io, "linked_participant", None)
        linked_identity = str(getattr(linked_participant, "identity", "") or "").strip()
        if linked_identity:
            return
        current_identity = str(getattr(room_io, "_participant_identity", "") or "").strip()
        if current_identity:
            return
        set_participant = getattr(room_io, "set_participant", None)
        if not callable(set_participant):
            return
        try:
            set_participant(participant.identity)
            logger.info(
                "RoomIO vinculado ao participante para voz participant=%s kind=%s reason=%s",
                participant.identity,
                _participant_kind_label(participant.kind),
                reason,
            )
        except Exception as exc:
            log_exception_safely(
                logger,
                f"Falha ao vincular RoomIO ao participante {participant.identity}",
                exc,
                level=logging.WARNING,
            )

    def _patch_room_audio_inputs(self, *, reason: str) -> None:
        found = False
        for audio_input in self._iter_audio_inputs():
            accepted_sources = getattr(audio_input, "_accepted_sources", None)
            if not isinstance(accepted_sources, set):
                continue
            found = True
            accepted_sources.add(rtc.TrackSource.SOURCE_UNKNOWN)
            logger.debug(
                "Room audio input pronto reason=%s participant=%s accepted_sources=%s",
                reason,
                str(getattr(audio_input, "_participant_identity", "") or "auto"),
                [_track_source_label(source) for source in sorted(accepted_sources)],
            )
            self._attach_existing_audio_tracks(audio_input, reason=reason)

        if not found:
            logger.warning(
                "Nao achei Room audio input para patch de source=unknown. O audio remoto dependera do fluxo padrao."
            )

    def _attach_existing_audio_tracks(self, audio_input: Any, *, reason: str) -> None:
        participant_identity = str(getattr(audio_input, "_participant_identity", "") or "").strip()
        if not participant_identity:
            return
        participant = self._room.remote_participants.get(participant_identity)
        if participant is None:
            return
        attach_fn = getattr(audio_input, "_on_track_available", None)
        if not callable(attach_fn):
            return
        for publication in participant.track_publications.values():
            if not _is_audio_publication(publication) or publication.track is None:
                continue
            if self._audio_input_has_publication(audio_input, publication, participant):
                logger.debug(
                    "Track de audio ja estava anexada ao pipeline participant=%s source=%s sid=%s reason=%s",
                    participant.identity,
                    _track_source_label(publication.source),
                    publication.sid,
                    reason,
                )
                continue
            try:
                attached = bool(attach_fn(publication.track, publication, participant))
            except Exception as exc:
                log_exception_safely(
                    logger,
                    f"Falha ao anexar track de audio existente participant={participant.identity} sid={publication.sid}",
                    exc,
                    level=logging.WARNING,
                )
                continue
            if attached:
                logger.info(
                    "Track de audio anexada ao pipeline participant=%s source=%s sid=%s reason=%s",
                    participant.identity,
                    _track_source_label(publication.source),
                    publication.sid,
                    reason,
                )

    def _attach_track_to_room_audio_input(
        self,
        track: rtc.RemoteTrack,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
        *,
        reason: str,
    ) -> None:
        attached = False
        states: list[dict[str, object]] = []
        for audio_input in self._iter_audio_inputs():
            self._bind_audio_input_participant(
                audio_input,
                participant,
                reason=reason,
            )
            if self._audio_input_has_publication(audio_input, publication, participant):
                attached = True
                states.append(self._audio_input_state(audio_input))
                continue
            attach_fn = getattr(audio_input, "_on_track_available", None)
            if not callable(attach_fn):
                states.append(self._audio_input_state(audio_input))
                continue
            try:
                attached = bool(attach_fn(track, publication, participant)) or attached
            except Exception as exc:
                log_exception_safely(
                    logger,
                    f"Falha ao anexar track de audio ao pipeline participant={participant.identity} sid={publication.sid}",
                    exc,
                    level=logging.WARNING,
                )
                states.append(self._audio_input_state(audio_input))
                continue
            states.append(self._audio_input_state(audio_input))
            if not attached and self._audio_input_has_publication(audio_input, publication, participant):
                attached = True
        if attached:
            logger.info(
                "Track de audio anexada ao pipeline participant=%s source=%s sid=%s reason=%s",
                participant.identity,
                _track_source_label(publication.source),
                publication.sid,
                reason,
            )
            return

        logger.warning(
            "Track de audio subscribed ainda nao anexada ao pipeline participant=%s source=%s sid=%s reason=%s audio_inputs=%s",
            participant.identity,
            _track_source_label(publication.source),
            publication.sid,
            reason,
            states,
        )

    def _iter_audio_inputs(self):
        seen_ids: set[int] = set()
        current = getattr(getattr(self._session, "input", None), "audio", None)
        while current is not None and id(current) not in seen_ids:
            seen_ids.add(id(current))
            yield current
            current = getattr(current, "source", None)

    def _bind_audio_input_participant(
        self,
        audio_input: Any,
        participant: rtc.RemoteParticipant,
        *,
        reason: str,
    ) -> None:
        participant_identity = str(getattr(audio_input, "_participant_identity", "") or "").strip()
        if participant_identity == participant.identity:
            return
        set_participant = getattr(audio_input, "set_participant", None)
        if not callable(set_participant):
            return
        try:
            set_participant(participant.identity)
            logger.info(
                "Room audio input vinculado ao participante participant=%s reason=%s",
                participant.identity,
                reason,
            )
        except Exception as exc:
            log_exception_safely(
                logger,
                f"Falha ao vincular Room audio input ao participante {participant.identity}",
                exc,
                level=logging.WARNING,
            )

    def _audio_input_has_publication(
        self,
        audio_input: Any,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> bool:
        current_identity = str(getattr(audio_input, "_participant_identity", "") or "").strip()
        current_publication = getattr(audio_input, "_publication", None)
        current_sid = str(getattr(current_publication, "sid", "") or "").strip()
        return current_identity == participant.identity and current_sid == publication.sid

    def _audio_input_state(self, audio_input: Any) -> dict[str, object]:
        accepted_sources = getattr(audio_input, "_accepted_sources", None)
        current_publication = getattr(audio_input, "_publication", None)
        return {
            "type": type(audio_input).__name__,
            "participant": str(getattr(audio_input, "_participant_identity", "") or "auto"),
            "publication_sid": str(getattr(current_publication, "sid", "") or ""),
            "accepted_sources": (
                [_track_source_label(source) for source in sorted(accepted_sources)]
                if isinstance(accepted_sources, set)
                else None
            ),
        }


class HighPriorityMicCapture:
    """
    Buffer leve para garantir ingestao continua dos transcripts do microfone.

    O audio bruto continua sob responsabilidade do LiveKit/RoomIO; aqui a gente
    protege a entrega dos eventos transcritos para que a fala atual da Kira nao
    atrase nem descarte a proxima pergunta do usuario.
    """

    def __init__(
        self,
        *,
        label: str = "room_mic",
        max_backlog: int = 12,
        processing_timeout: float = 1.0,
    ) -> None:
        self._label = label
        self._queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=max(1, max_backlog))
        self._processing_timeout = max(processing_timeout, 0.1)
        self._callbacks: list = []
        self._task: asyncio.Task | None = None
        self._running = False
        self.captured_count = 0
        self.processed_count = 0
        self.dropped_count = 0
        self.last_capture_at = 0.0
        self.last_capture_iso: str | None = None

    def add_callback(self, callback) -> None:
        self._callbacks.append(callback)

    async def start_capture(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._process_loop(), name=f"{self._label}-capture")
        logger.info("[MIC] capture_started label=%s", self._label)

    async def stop_capture(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        logger.info("[MIC] capture_stopped label=%s", self._label)

    def submit_transcript(
        self,
        text: str,
        *,
        is_final: bool = True,
        source: str = "voice",
        turn_id: str | None = None,
    ) -> bool:
        normalized = " ".join(str(text or "").strip().split())
        if not normalized:
            return False
        self.captured_count += 1
        self.last_capture_at = time.monotonic()
        self.last_capture_iso = datetime.now().isoformat()
        payload = {
            "text": normalized,
            "is_final": bool(is_final),
            "source": source,
            "turn_id": str(turn_id or "").strip() or None,
            "captured_at": self.last_capture_at,
            "captured_at_iso": self.last_capture_iso,
        }
        try:
            self._queue.put_nowait(payload)
            return True
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.dropped_count += 1
            logger.warning(
                "[MIC] backlog_full label=%s dropped_total=%s keeping_latest=true",
                self._label,
                self.dropped_count,
            )
            try:
                self._queue.put_nowait(payload)
                return True
            except asyncio.QueueFull:
                return False

    async def _process_loop(self) -> None:
        try:
            while self._running:
                item = await self._queue.get()
                try:
                    await asyncio.wait_for(self._dispatch(item), timeout=self._processing_timeout)
                    self.processed_count += 1
                except asyncio.TimeoutError:
                    logger.warning("[MIC] processing_timeout label=%s", self._label)
                except Exception as exc:
                    log_exception_safely(
                        logger,
                        f"Falha ao processar transcript do mic label={self._label}",
                        exc,
                        level=logging.WARNING,
                    )
        except asyncio.CancelledError:
            raise

    async def _dispatch(self, item: dict[str, object]) -> None:
        for callback in list(self._callbacks):
            try:
                result = callback(item)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                log_exception_safely(
                    logger,
                    f"Falha em callback de mic label={self._label}",
                    exc,
                    level=logging.WARNING,
                )

    def get_stats(self) -> dict[str, object]:
        return {
            "is_capturing": self._running,
            "captured": self.captured_count,
            "processed": self.processed_count,
            "dropped": self.dropped_count,
            "queue_size": self._queue.qsize(),
            "last_capture": self.last_capture_iso,
        }
