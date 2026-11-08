from __future__ import annotations

import asyncio
import inspect
import logging
import time


TRANSCRIPT_PREVIEW_LIMIT = 160
LATENCY_CALLBACK_THRESHOLD_SECONDS = 5.0
# Acima disso, o marco de inicio de fala e considerado VELHO (turno de texto reaproveitando
# um speech_started_at de um turno de voz anterior) -> nao dispara recuperacao (FASE 2).
_MAX_PLAUSIBLE_REPLY_LATENCY_S = 120.0


def _stale_final(latency_state: dict, now: float) -> bool:
    """Marca do fim da fala vazia ou velha (fala filtrada sem resposta) = começa de novo.
    Antes ficava a de 70 s atrás e o log dizia final_transcript_to_reply_start=73 s numa resposta de 3 s."""
    previous = latency_state.get("final_transcript_at")
    return previous is None or now - previous > 20.0


def _preview_text(value: object, *, limit: int = TRANSCRIPT_PREVIEW_LIMIT) -> str:
    normalized = " ".join(str(value or "").strip().split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3]}..."


def _track_source_label(source: object) -> str:
    try:
        from livekit import rtc

        return rtc.TrackSource.Name(source)
    except Exception:
        return str(source)


def _voice_latency_state(session) -> dict[str, float | None]:
    state = getattr(session, "_kira_voice_latency_state", None)
    if state is None:
        state = {
            "speech_turn_seq": 0,
            "speech_started_at": None,
            "speech_stopped_at": None,
            "first_partial_at": None,
            "final_transcript_at": None,
            "reply_started_at": None,
        }
        setattr(session, "_kira_voice_latency_state", state)
    return state


def _log_latency(
    logger: logging.Logger,
    *,
    name: str,
    started_at: float | None,
    ended_at: float | None = None,
) -> None:
    if started_at is None:
        return
    finished = ended_at if ended_at is not None else time.monotonic()
    if finished < started_at:
        return
    elapsed = finished - started_at
    logger.info("LiveKit voice: latency %s=%.3fs", name, elapsed)
    if name == "user_speech_start_to_reply_start" and elapsed > 10.0:
        logger.error(
            "[LATENCIA CRITICA] reply_start demorou %.1fs - possivel fila travada",
            elapsed,
        )


def attach_session_voice_probe(*, session, logger: logging.Logger, on_latency_critical=None) -> None:
    latency_state = _voice_latency_state(session)

    def _emit_latency_critical(elapsed: float) -> None:
        if elapsed < LATENCY_CALLBACK_THRESHOLD_SECONDS or on_latency_critical is None:
            return
        maybe_result = on_latency_critical(elapsed)
        if inspect.isawaitable(maybe_result):
            asyncio.create_task(maybe_result)

    @session.on("user_state_changed")
    def _on_user_state_changed(event) -> None:
        logger.debug(
            "LiveKit voice: user_state %s -> %s",
            getattr(event, "old_state", "?"),
            getattr(event, "new_state", "?"),
        )

    @session.on("agent_state_changed")
    def _on_agent_state_changed(event) -> None:
        old_state = getattr(event, "old_state", "?")
        new_state = getattr(event, "new_state", "?")
        logger.info(
            "LiveKit voice: agent_state %s -> %s",
            old_state,
            new_state,
        )
        logger.info("AGENT_STATE_CHANGE old=%s new=%s", old_state, new_state)
        if old_state != "speaking" and new_state == "speaking":
            logger.info("LiveKit voice: agent_reply_started")
            logger.info("REPLY_START")
            now = time.monotonic()
            latency_state["reply_started_at"] = now
            _log_latency(
                logger,
                name="final_transcript_to_reply_start",
                started_at=latency_state.get("final_transcript_at"),
                ended_at=now,
            )
            _log_latency(
                logger,
                name="user_speech_start_to_reply_start",
                started_at=latency_state.get("speech_started_at"),
                ended_at=now,
            )
            speech_started_at = latency_state.get("speech_started_at")
            if speech_started_at is not None and now >= speech_started_at:
                elapsed_since_speech = now - speech_started_at
                # FASE 2: so dispara o "latencia critica" (que INTERROMPE a sessao e drena a
                # fila) quando o marco e plausivel. Em turno de TEXTO o speech_started_at fica
                # velho (de um turno de voz anterior) e gerava 430s fantasma -> recuperacao
                # desnecessaria que TRAVAVA de verdade. Acima de 120s = marco velho: ignora.
                if elapsed_since_speech <= _MAX_PLAUSIBLE_REPLY_LATENCY_S:
                    _emit_latency_critical(elapsed_since_speech)
                else:
                    logger.info(
                        "LATENCY_CRITICAL_SKIPPED_STALE_BASELINE elapsed_s=%.1f reason=text_turn_or_stale_voice_baseline",
                        elapsed_since_speech,
                    )
            # Consome o marco: o proximo turno (ex.: texto) NAO reaproveita um valor antigo.
            latency_state["speech_started_at"] = None
            latency_state["final_transcript_at"] = None
        elif old_state == "speaking" and new_state != "speaking":
            logger.info("LiveKit voice: agent_reply_finished")
            logger.info("REPLY_FINISH")

    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(event) -> None:
        is_final = bool(getattr(event, "is_final", False))
        transcript = _preview_text(getattr(event, "transcript", ""))
        if not is_final:
            logger.debug(
                "LiveKit voice: user_input_transcribed final=%s speaker=%s texto=%s",
                is_final,
                getattr(event, "speaker_id", None),
                transcript,
            )
            return

        now = time.monotonic()
        if _stale_final(latency_state, now):
            latency_state["final_transcript_at"] = now
        logger.info(
            "LiveKit voice: user_input_transcribed final=%s speaker=%s texto=%s",
            is_final,
            getattr(event, "speaker_id", None),
            transcript,
        )
        logger.info(
            "VOICE_TURN_FINALIZED_BY_TRANSCRIPT source=livekit input_seq=%s",
            int(latency_state.get("speech_turn_seq") or 0),
        )
        _log_latency(
            logger,
            name="user_speech_start_to_final_transcript",
            started_at=latency_state.get("speech_started_at"),
            ended_at=latency_state.get("final_transcript_at"),
        )

    @session.on("conversation_item_added")
    def _on_conversation_item_added(event) -> None:
        item = getattr(event, "item", None)
        role = getattr(item, "role", None)
        content = getattr(item, "content", None)
        if not isinstance(content, str):
            return
        logger.debug(
            "LiveKit voice: conversation_item_added role=%s texto=%s",
            role,
            _preview_text(content),
        )

    @session.on("speech_created")
    def _on_speech_created(event) -> None:
        logger.info(
            "LiveKit voice: speech_created source=%s user_initiated=%s",
            getattr(event, "source", "?"),
            bool(getattr(event, "user_initiated", False)),
        )
        logger.info(
            "AUDIO_OUTPUT_START source=%s user_initiated=%s",
            getattr(event, "source", "?"),
            bool(getattr(event, "user_initiated", False)),
        )

    @session.on("error")
    def _on_error(event) -> None:
        source = getattr(event, "source", None)
        error = getattr(event, "error", None)
        logger.error(
            "LiveKit voice: session error source=%s error=%s",
            type(source).__name__ if source is not None else "unknown",
            error,
        )

    @session.on("close")
    def _on_close(event) -> None:
        logger.warning(
            "LiveKit voice: session closed reason=%s error=%s",
            getattr(event, "reason", "?"),
            getattr(event, "error", None),
        )


def attach_realtime_voice_probe(*, session, logger: logging.Logger) -> bool:
    activity = getattr(session, "_activity", None)
    realtime_session = getattr(activity, "realtime_llm_session", None)
    if realtime_session is None:
        logger.warning("Gemini Live probe indisponivel: realtime session ainda nao foi criada.")
        return False
    latency_state = _voice_latency_state(session)

    @realtime_session.on("input_speech_started")
    def _on_input_speech_started(_event) -> None:
        latency_state["speech_turn_seq"] = int(latency_state.get("speech_turn_seq") or 0) + 1
        latency_state["speech_started_at"] = time.monotonic()
        latency_state["speech_stopped_at"] = None
        latency_state["first_partial_at"] = None
        latency_state["final_transcript_at"] = None
        latency_state["reply_started_at"] = None
        logger.info("Gemini Live: input_speech_started")
        logger.info("INPUT_SPEECH_STARTED")
        logger.info("USER_INPUT_RECEIVED source=speech")
        logger.info(
            "VOICE_VAD_WAIT_BEGIN source=gemini input_seq=%s",
            int(latency_state.get("speech_turn_seq") or 0),
        )

    @realtime_session.on("input_speech_stopped")
    def _on_input_speech_stopped(event) -> None:
        latency_state["speech_stopped_at"] = time.monotonic()
        logger.debug(
            "Gemini Live: input_speech_stopped user_transcription_enabled=%s",
            bool(getattr(event, "user_transcription_enabled", False)),
        )
        logger.info(
            "VOICE_VAD_WAIT_END source=gemini reason=input_speech_stopped input_seq=%s",
            int(latency_state.get("speech_turn_seq") or 0),
        )

    @realtime_session.on("input_audio_transcription_completed")
    def _on_input_audio_transcription_completed(event) -> None:
        is_final = bool(getattr(event, "is_final", False))
        transcript = _preview_text(getattr(event, "transcript", ""))
        now = time.monotonic()
        if is_final:
            if _stale_final(latency_state, now):
                latency_state["final_transcript_at"] = now
            logger.info(
                "Gemini Live: input_audio_transcription_completed final=%s texto=%s",
                is_final,
                transcript,
            )
            logger.info("FINAL_TRANSCRIPT text=%s", transcript)
            logger.info(
                "VOICE_TURN_FINALIZED_BY_TRANSCRIPT source=gemini input_seq=%s",
                int(latency_state.get("speech_turn_seq") or 0),
            )
            _log_latency(
                logger,
                name="user_speech_start_to_final_transcript",
                started_at=latency_state.get("speech_started_at"),
                ended_at=latency_state.get("final_transcript_at"),
            )
            return

        if latency_state.get("first_partial_at") is None:
            latency_state["first_partial_at"] = now
            logger.info(
                "Gemini Live: first_partial_transcript texto=%s",
                transcript,
            )
            logger.info("FIRST_PARTIAL text=%s", transcript)
            _log_latency(
                logger,
                name="user_speech_start_to_first_partial",
                started_at=latency_state.get("speech_started_at"),
                ended_at=now,
            )
            return

        logger.debug(
            "Gemini Live: input_audio_transcription_completed final=%s texto=%s",
            is_final,
            transcript,
        )

    @realtime_session.on("generation_created")
    def _on_generation_created(event) -> None:
        logger.info(
            "Gemini Live: generation_created response_id=%s user_initiated=%s",
            getattr(event, "response_id", None),
            bool(getattr(event, "user_initiated", False)),
        )
        logger.info(
            "GENERATION_CREATED response_id=%s user_initiated=%s",
            getattr(event, "response_id", None),
            bool(getattr(event, "user_initiated", False)),
        )

    @realtime_session.on("session_reconnected")
    def _on_session_reconnected(_event) -> None:
        logger.warning("Gemini Live: session_reconnected")

    @realtime_session.on("gemini_disconnect_warning")
    def _on_disconnect_warning(event) -> None:
        logger.warning(
            "Gemini Live: disconnect_warning time_left=%ss",
            getattr(event, "time_left_seconds", "?"),
        )

    @realtime_session.on("error")
    def _on_realtime_error(event) -> None:
        logger.error(
            "Gemini Live: realtime error recoverable=%s error=%s",
            bool(getattr(event, "recoverable", False)),
            getattr(event, "error", None),
        )

    logger.info("Gemini Live probe conectado aos eventos realtime.")
    return True


def log_session_io_status(*, session, logger: logging.Logger) -> None:
    audio_input = getattr(getattr(session, "input", None), "audio", None)
    video_input = getattr(getattr(session, "input", None), "video", None)
    audio_output = getattr(getattr(session, "output", None), "audio", None)
    transcription_output = getattr(getattr(session, "output", None), "transcription", None)
    room_io = getattr(session, "_room_io", None)
    input_origin = "none"
    if audio_input is not None:
        input_origin = "room_participant_audio" if room_io is not None else "console_microphone"
    logger.info(
        "LiveKit voice: io_status input_origin=%s input_audio=%s input_video=%s output_audio=%s transcription_output=%s user_state=%s agent_state=%s",
        input_origin,
        type(audio_input).__name__ if audio_input is not None else "None",
        type(video_input).__name__ if video_input is not None else "None",
        type(audio_output).__name__ if audio_output is not None else "None",
        type(transcription_output).__name__ if transcription_output is not None else "None",
        getattr(session, "user_state", "?"),
        getattr(session, "agent_state", "?"),
    )
    if room_io is None:
        logger.info("LiveKit voice: room_io ainda nao inicializado.")
        return

    linked_participant = getattr(room_io, "linked_participant", None)
    linked_identity = getattr(linked_participant, "identity", None) if linked_participant is not None else None
    room_audio_input = getattr(room_io, "audio_input", None)
    room_audio_publication = getattr(room_audio_input, "_publication", None) if room_audio_input is not None else None
    mic_subscribed_ready = bool(
        room_audio_publication is not None
        and getattr(room_audio_publication, "track", None) is not None
    )
    logger.info(
        "LiveKit voice: room_io linked_participant=%s mic_subscribed_ready=%s room_connected=%s",
        linked_identity or None,
        mic_subscribed_ready,
        bool(getattr(getattr(room_io, "room", None), "isconnected", lambda: False)()),
    )
    accepted_sources = getattr(audio_input, "_accepted_sources", None)
    if isinstance(accepted_sources, set):
        logger.info(
            "LiveKit voice: input_audio accepted_sources=%s participant=%s",
            [_track_source_label(source) for source in sorted(accepted_sources)],
            str(getattr(audio_input, "_participant_identity", "") or "auto"),
        )
