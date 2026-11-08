from __future__ import annotations
import asyncio
import inspect
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
import uuid

from livekit import rtc

from resilience import log_exception_safely
from core.personality import apply_kira_personality_style
from core.tone_engine import apply_kira_tone, resolve_kira_tone_mode
from bootstrap.session_context import BACKGROUND_CONTEXT_PREFIX
from .audio_runtime import RoomAudioBridge
from .video_runtime import RoomVideoBridge
from .voice_output_manager import get_voice_output_manager
from core.whatsapp_outbound import is_whatsapp_intent
from core.command_router import CommandGate, route_user_command
from core.safety_gate import check_safety_gate
from .transcript_filter import is_empty_user_input_with_reason


logger = logging.getLogger(__name__)
_TOOL_RESULT_MANAGER = None
_LEGACY_FILE_MARKER = "[KIRA FILE]"


def _safe_user_text_preview(text: str, *, max_chars: int = 180) -> str:
    try:
        from services.memory_guard import safe_log_text_preview
    except Exception:
        try:
            from memory_guard import safe_log_text_preview  # type: ignore
        except Exception:
            return str(text or "").strip()[:max_chars]
    return safe_log_text_preview(text, max_chars=max_chars)


async def _notify_backend_economy_interaction(*, reason: str) -> None:
    base_url = str(os.getenv("KIRA_BACKEND_BASE_URL", "http://127.0.0.1:3000") or "").strip().rstrip("/")
    if not base_url:
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=0.3, read=1.0, write=0.5, pool=0.3), verify=base_url.lower().startswith("https://")) as client:
            response = await client.post(
                f"{base_url}/api/economy/reset-idle-timer",
                json={"reason": reason},
            )
        if response.status_code < 400:
            logger.info("ECONOMY_IDLE_TIMER_RESET_NOTIFIED source=%s", reason)
        else:
            logger.warning(
                "ECONOMY_IDLE_TIMER_RESET_NOTIFY_FAILED status=%s source=%s",
                response.status_code,
                reason,
            )
    except Exception as exc:
        logger.debug("ECONOMY_IDLE_TIMER_RESET_NOTIFY_SKIPPED source=%s reason=%s", reason, exc)


def _ensure_project_root_on_path() -> None:
    project_root = Path(__file__).resolve().parents[2]
    root_text = str(project_root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)


def _combine_extra_instructions(existing: str, addition: str) -> str:
    base = str(existing or "").strip()
    extra = str(addition or "").strip()
    if not extra:
        return base
    if not base:
        return extra
    return f"{base}\n\n{extra}".strip()


def build_voice_memory_context_instruction(
    user_text: str,
    *,
    source: str = "voice",
    max_items: int = 5,
    db_path: str | Path | None = None,
    turn_id: str | None = None,
) -> str:
    text = str(user_text or "").strip()
    if not text:
        logger.info("MEMORY_CONTEXT_SKIPPED source=%s reason=empty_text", source)
        return ""
    session_instruction = ""
    try:
        from services.session_context_tracker import apply_resolved_entity_to_turn
        resolved_payload = apply_resolved_entity_to_turn(text, turn_id=turn_id, source=source)
        if bool(resolved_payload.get("applied")):
            text = str(resolved_payload.get("text") or text).strip() or text
            session_instruction = str(resolved_payload.get("instruction") or "").strip()
            entity = str(resolved_payload.get("entity") or "").strip()
            logger.info("NORMAL_REPLY_USING_RESOLVED_ENTITY entity=%s turn_id=%s source=%s", entity or "none", str(turn_id or "none"), source)
    except Exception as exc:
        logger.debug("MEMORY_CONTEXT_COREFERENCE_APPLY_SKIPPED source=%s reason=%s", source, exc)
    try:
        try:
            from services.memory_retriever import build_memory_context_for_reply
        except Exception:
            _ensure_project_root_on_path()
            from services.memory_retriever import build_memory_context_for_reply
        payload = build_memory_context_for_reply(
            text,
            source=source,
            max_items=max_items,
            db_path=db_path,
            turn_id=turn_id,
        )
    except Exception as exc:
        logger.warning("MEMORY_RETRIEVER_FAILED_NON_FATAL source=%s error=%s", source, exc)
        return ""

    block = str(payload.get("text") or "").strip()
    if not block:
        logger.info("MEMORY_CONTEXT_SKIPPED source=%s reason=empty", source)
        return ""

    item_count = len(payload.get("results") or [])
    logger.info("MEMORY_CONTEXT_INJECTED source=%s items=%s", source, item_count)
    logger.info("MEM0_CONTEXT_INJECTED source=%s", source)
    if source == "voice":
        logger.info("VOICE_MEM0_CONTEXT_APPLIED")
    context_text = (
        f"{block}\n"
        "Use a memoria real para adaptar tom, detalhes e decisoes quando for relevante. "
        "Se a pergunta for sobre o que voce lembra e houver itens acima, responda com um resumo natural desses itens. "
        "Nunca diga que nao tem contexto quando o CONTEXTO REAL DISPONIVEL responder a pergunta. "
        "nao invente memoria. "
        "Nunca verbalize SYSTEM_NOTE_FOR_REASONING_ONLY, SYSTEM_NOTE ou o bloco tecnico."
    )
    if session_instruction:
        context_text = f"{session_instruction}\n\n{context_text}".strip()
    return context_text


def _get_tool_result_manager():
    global _TOOL_RESULT_MANAGER
    if _TOOL_RESULT_MANAGER is None:
        from agent.runtime.tool_action_result_manager import ToolActionResultManager

        _TOOL_RESULT_MANAGER = ToolActionResultManager()
    return _TOOL_RESULT_MANAGER
VISUAL_QUERY_RE = re.compile(
    r"(o que (voce|c[eê]) (esta|t[aá]) vendo|o que tem na tela|quais [\w\s]{0,20}icones|esta vendo isso|o que aparece (a[ií]|na tela)|me descreve a tela|descreva a tela)",
    re.IGNORECASE,
)
VISUAL_QUERY_WAIT_TIMEOUT_SECONDS = 2.2


def should_accept_manual_user_input(text: str) -> tuple[bool, str]:
    normalized = str(text or "").strip()
    if not normalized:
        return False, "empty"
    if normalized.lower() in {"oi kira", "olá kira", "ola kira", "bom dia"}:
        return True, "short_allowed"
    word_count = len(re.findall(r"[A-Za-zÀ-ÿ0-9]+", normalized))
    if word_count >= 3:
        return True, "valid_text"
    rejected, reason = is_empty_user_input_with_reason(normalized)
    if rejected:
        return False, reason
    return True, "short_allowed"


def _is_incomplete_memory_reference(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    normalized = re.sub(r"[?!]+$", ".", normalized)
    return bool(re.fullmatch(r"(?:kira,?\s*)?lembra(?:r)?\s+(?:de|da|do)\.?", normalized))


async def _await_if_needed(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def _call_with_optional_app_origem(func, text: str, *, app_origem: str):
    try:
        signature = inspect.signature(func)
    except Exception:
        return await _await_if_needed(func(text))

    accepts_kwargs = any(
        param.kind == inspect.Parameter.VAR_KEYWORD
        for param in signature.parameters.values()
    )
    if accepts_kwargs or "app_origem" in signature.parameters:
        return await _await_if_needed(func(text, app_origem=app_origem))
    return await _await_if_needed(func(text))


async def _call_with_optional_source(func, text: str, *, source: str):
    try:
        signature = inspect.signature(func)
    except Exception:
        return await _await_if_needed(func(text))

    accepts_kwargs = any(
        param.kind == inspect.Parameter.VAR_KEYWORD
        for param in signature.parameters.values()
    )
    if accepts_kwargs or "source" in signature.parameters:
        return await _await_if_needed(func(text, source=source))
    return await _await_if_needed(func(text))


def _resolve_observer_state(observer):
    state = getattr(observer, "state", None)
    if callable(state):
        try:
            state = state()
        except Exception as exc:
            logger.warning("TEXT_CONTEXT_TASK_FAILED_NON_FATAL reason=observer_state_callable_failed:%s", exc)
            return None
    return state


def _observer_visual_context_active(observer) -> bool:
    state = _resolve_observer_state(observer)
    return bool(getattr(state, "visual_context_active", False))


def _observer_compact_summary(observer) -> str:
    state = _resolve_observer_state(observer)
    if state is None:
        logger.warning("TEXT_CONTEXT_COMPACT_SUMMARY_FAILED_NON_FATAL reason=observer_state_missing")
        logger.info("TEXT_TURN_CONTINUING_AFTER_CONTEXT_ERROR")
        return ""
    if isinstance(state, dict):
        summary = str(state.get("compact_summary") or state.get("summary") or "").strip()
        logger.info("TEXT_CONTEXT_COMPACT_SUMMARY_OK source=dict")
        return summary
    compact_summary = getattr(state, "compact_summary", None)
    if compact_summary is None:
        logger.info("TEXT_CONTEXT_COMPACT_SUMMARY_OK source=missing")
        return ""
    try:
        value = compact_summary() if callable(compact_summary) else compact_summary
        logger.info("TEXT_CONTEXT_COMPACT_SUMMARY_OK source=observer")
        return str(value or "").strip()
    except Exception as exc:
        logger.warning("TEXT_CONTEXT_COMPACT_SUMMARY_FAILED_NON_FATAL reason=observer_compact_summary_failed:%s", exc)
        logger.info("TEXT_TURN_CONTINUING_AFTER_CONTEXT_ERROR")
        return ""


class _ObserverStateProxy:
    def __init__(self, observer, state) -> None:
        self._observer = observer
        self.state = state

    def __getattr__(self, name: str):
        return getattr(self._observer, name)


def _observer_for_reply(observer):
    state = getattr(observer, "state", None)
    if not callable(state):
        return observer
    resolved_state = _resolve_observer_state(observer)
    if resolved_state is None:
        logger.info("TEXT_TURN_CONTINUING_AFTER_CONTEXT_ERROR")
        return observer
    logger.warning("TEXT_CONTEXT_TASK_FAILED_NON_FATAL reason=observer_state_was_callable")
    logger.info("TEXT_TURN_CONTINUING_AFTER_CONTEXT_ERROR")
    return _ObserverStateProxy(observer, resolved_state)


async def dispatch_text_input_to_reply(
    *,
    text: str,
    speech_coordinator,
    observer,
    agent,
    handle_local_quick_command,
    extra_instructions: str = "",
    client_turn_id: str = "",
) -> bool:
    normalized_text = str(text or "").strip()
    normalized_client_turn_id = str(client_turn_id or "").strip()
    accepted, accept_reason = should_accept_manual_user_input(normalized_text)
    if not accepted:
        logger.info("USER_INPUT_REJECTED_NOISE source=manual reason=%s text=%r", accept_reason, _safe_user_text_preview(normalized_text, max_chars=40))
        rejected_turn_id = f"turn-{uuid.uuid4().hex[:12]}"
        try:
            from services.local_command_guard import mark_turn_rejected, should_block_rejected_turn
        except Exception:
            _ensure_project_root_on_path()
            from services.local_command_guard import mark_turn_rejected, should_block_rejected_turn
        mark_turn_rejected(rejected_turn_id, reason=accept_reason)
        should_block_rejected_turn(turn_id=rejected_turn_id, source="manual")
        return False
    if accept_reason == "short_allowed":
        logger.info("USER_INPUT_SHORT_ALLOWED source=manual command=%r", _safe_user_text_preview(normalized_text))
    else:
        logger.info("USER_INPUT_ACCEPTED source=manual reason=valid_text")
    if _is_incomplete_memory_reference(normalized_text):
        rejected_turn_id = f"turn-{uuid.uuid4().hex[:12]}"
        try:
            from services.local_command_guard import mark_turn_rejected, should_block_rejected_turn
        except Exception:
            _ensure_project_root_on_path()
            from services.local_command_guard import mark_turn_rejected, should_block_rejected_turn
        mark_turn_rejected(rejected_turn_id, reason="incomplete_memory_reference")
        should_block_rejected_turn(turn_id=rejected_turn_id, source="manual")
        logger.info("TEXT_INPUT_LOCAL_COMMAND_EARLY_RETURN reason=incomplete_memory_reference")
        return True
    logger.info("TEXT_INPUT_ROUTER_BEGIN")
    logger.info("TEXT_INPUT_RECEIVED text=%r", _safe_user_text_preview(normalized_text))
    logger.info("TEXT_TURN_RECEIVED source=text text=%r", _safe_user_text_preview(normalized_text))
    logger.info("USER_TEXT_RECEIVED text=%r", _safe_user_text_preview(normalized_text))
    logger.info(
        "USER_TURN_ROUTER_BEGIN source=text client_turn_id=%s text=%r",
        normalized_client_turn_id or "none",
        _safe_user_text_preview(normalized_text),
    )
    logger.info("TEXT_TURN_ROUTER_BEGIN source=text")
    logger.info("TRANSCRIPTION_DEDUPE_SKIPPED key=text_input_ui_mirror source=text")
    async def _text_safety_gate(text_value: str, _context: dict) -> dict:
        safety = check_safety_gate(text_value)
        if not safety.matched:
            return {"handled": False}
        publisher = getattr(agent, "publish_ui_panel_text_notice", None)
        if callable(publisher):
            await publisher(safety.response, turn_id=None)
        if safety.reason == "adult_safe_response":
            logger.info(
                "ADULT_SAFE_RESPONSE_DELIVERED turn_id=none source=text text_preview=%r delivered=true voice=false",
                _safe_user_text_preview(safety.response, max_chars=80),
            )
        logger.warning("SAFETY_GATE_MATCHED severity=critical")
        return {"handled": True, "kind": "safety", "user_visible_text": safety.response, "voice_allowed": False}

    def _agent_gate(name: str, method_name: str | None = None):
        async def _run(text_value: str, _context: dict) -> dict:
            gate = getattr(agent, method_name or f"try_handle_{name}_command", None)
            if not callable(gate):
                return {"handled": False}
            try:
                handled_value = await _call_with_optional_source(gate, text_value, source="text")
            except Exception as exc:
                logger.warning("TEXT_INPUT_LOCAL_COMMAND_GATE_FAILED type=%s error=%s", name, exc)
                return {"handled": False}
            return {"handled": bool(handled_value)}
        return _run

    route_result = await route_user_command(
        normalized_text,
        source="text",
        gates=(
            CommandGate("safety", _text_safety_gate),
            CommandGate("ui_panel", _agent_gate("ui_panel")),
            CommandGate("local_command", _agent_gate("local_command")),
            CommandGate("volume", _agent_gate("volume")),
            CommandGate("creation_task", _agent_gate("creation_task")),
            CommandGate("artifact_command", _agent_gate("artifact_command", "try_handle_artifact_command")),
            CommandGate("whatsapp", _agent_gate("whatsapp")),
            CommandGate("browser_task", _agent_gate("browser_task")),
        ),
    )
    if bool(route_result.get("handled") or route_result.get("stop_routing")):
        gate_name = str(route_result.get("gate") or route_result.get("kind") or "unknown")
        logger.info("TEXT_INPUT_LOCAL_COMMAND_MATCHED type=%s", gate_name)
        logger.info("USER_TURN_NORMAL_REPLY_BLOCKED reason=gate_handled gate=%s", gate_name)
        logger.info("MEMORY_CONTEXT_SKIPPED source=voice reason=handled_command gate=%s", gate_name)
        logger.info("TEXT_INPUT_LOCAL_COMMAND_EARLY_RETURN")
        return True

    logger.info("TEXT_TURN_AFTER_UI_PANEL_GATE matched=false")
    logger.info("TEXT_TURN_ROUTER_NO_MATCH route=gemini")
    logger.info(
        "USER_TRANSCRIPT_ACCEPTED source=text client_turn_id=%s text=%r",
        normalized_client_turn_id or "none",
        _safe_user_text_preview(normalized_text),
    )
    logger.info("USER_TURN_TEXT_ACCEPTED text=%r", _safe_user_text_preview(normalized_text))
    old_turn_id = str(getattr(speech_coordinator, "_current_user_turn_id", "") or "").strip()
    new_turn_id = f"turn-{uuid.uuid4().hex[:12]}"
    try:
        from agent.runtime.turn_coordinator import get_turn_coordinator
    except Exception:  # pragma: no cover
        from runtime.turn_coordinator import get_turn_coordinator
    coordinator = get_turn_coordinator()
    coordinator.cleanup_old_turns()
    coordinator.clear_stale_locks_for_new_turn()
    coordinator.register_user_turn(new_turn_id, normalized_text, preferred_reply_source="manual")
    set_current_user_turn = getattr(speech_coordinator, "set_current_user_turn", None)
    if callable(set_current_user_turn):
        set_current_user_turn(new_turn_id, user_text=normalized_text)
    else:
        logger.info("SPEECH_COORDINATOR_TURN_LINK_SKIPPED reason=optional_mock_or_degraded_runtime")
    try:
        from services.session_context_tracker import get_session_context_tracker
    except Exception:
        _ensure_project_root_on_path()
        from services.session_context_tracker import get_session_context_tracker
    try:
        context_tracker = get_session_context_tracker()
        context_tracker.update_from_text(normalized_text, turn_id=new_turn_id, speaker="user")
    except Exception as exc:
        logger.warning("SESSION_CONTEXT_UPDATE_FAILED source=text turn_id=%s error=%s", new_turn_id, exc)
        context_tracker = None
    resolved_turn_text = normalized_text
    resolved_instruction = ""
    try:
        from services.session_context_tracker import apply_resolved_entity_to_turn
        resolved_payload = apply_resolved_entity_to_turn(normalized_text, turn_id=new_turn_id, source="text")
        if bool(resolved_payload.get("applied")):
            resolved_turn_text = str(resolved_payload.get("text") or normalized_text).strip() or normalized_text
            resolved_instruction = str(resolved_payload.get("instruction") or "").strip()
            entity = str(resolved_payload.get("entity") or "").strip()
            logger.info("MEMORY_QUERY_USING_RESOLVED_ENTITY entity=%s turn_id=%s source=text", entity or "none", new_turn_id)
            logger.info("NORMAL_REPLY_USING_RESOLVED_ENTITY entity=%s turn_id=%s source=text", entity or "none", new_turn_id)
        elif bool(resolved_payload.get("ambiguous")):
            logger.info("SESSION_CONTEXT_COREFERENCE_AMBIGUOUS turn_id=%s source=text", new_turn_id)
    except Exception as exc:
        logger.warning("SESSION_CONTEXT_COREFERENCE_APPLY_FAILED source=text turn_id=%s error=%s", new_turn_id, exc)
    logger.info(
        "USER_TURN_NEW_ID_CREATED source=text client_turn_id=%s old_turn_id=%s new_turn_id=%s",
        normalized_client_turn_id or "none",
        old_turn_id or "none",
        new_turn_id,
    )
    if old_turn_id and old_turn_id != new_turn_id:
        logger.info("USER_TURN_OLD_COMPLETED_ID_DISCARDED old_turn_id=%s", old_turn_id)
        logger.info("USER_TURN_LOCKS_STALE_CLEARED old_turn_id=%s", old_turn_id)
    logger.info("TEXT_INPUT_NORMAL_REPLY_ALLOWED")
    logger.info("USER_TURN_NORMAL_REPLY_ALLOWED reason=no_gate_matched")
    try:
        try:
            from services.memory_retriever import build_memory_query_gate_response
            from services.kira_log_manager import log_command_event
        except Exception:
            _ensure_project_root_on_path()
            from services.memory_retriever import build_memory_query_gate_response
            from services.kira_log_manager import log_command_event
        memory_gate = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: build_memory_query_gate_response(
                    resolved_turn_text,
                    source="text",
                    turn_id=new_turn_id,
                )
            ),
            timeout=1.5,
        )
    except asyncio.TimeoutError:
        logger.warning("MEMORY_QUERY_GATE_TIMEOUT source=text turn_id=%s timeout_s=1.5", new_turn_id)
        memory_gate = {"handled": False}
    except Exception as exc:
        logger.warning("MEMORY_QUERY_GATE_ERROR source=text turn_id=%s error=%s", new_turn_id, exc)
        memory_gate = {"handled": False}
    try:
        log_command_event(
            "MEMORY_QUERY_GATE",
            {
                "turn_id": new_turn_id,
                "source": "text",
                "raw_text_preview": _safe_user_text_preview(normalized_text),
                "normalized_text_preview": _safe_user_text_preview(resolved_turn_text),
                "gate": "memory_query",
                "matched": bool(memory_gate.get("matched") or memory_gate.get("handled")),
                "handled": bool(memory_gate.get("handled")),
                "route_final": "memory_query" if bool(memory_gate.get("handled")) else "",
                "blocked_next_routes": ["gemini_normal_reply"] if bool(memory_gate.get("handled")) else [],
                "reason": str(memory_gate.get("reason") or memory_gate.get("subject") or "not_matched"),
            },
        )
    except Exception:
        logger.info("COMMAND_JSONL_WRITE_FAILED event=MEMORY_QUERY_GATE")
    if bool(memory_gate.get("handled")):
        response_text = str(memory_gate.get("response") or "").strip()
        if response_text:
            publisher = getattr(agent, "publish_ui_panel_text_notice", None)
            if callable(publisher):
                await publisher(response_text, turn_id=new_turn_id, role="assistant", source="memory_query")
            else:
                await speech_coordinator.speak_proactive_message(
                    response_text,
                    label="MEMORY_QUERY",
                    add_to_chat_ctx=True,
                )
        logger.info("MEMORY_QUERY_GATE_TURN_CLOSED turn_id=%s", new_turn_id)
        logger.info("MEMORY_QUERY_GATE_BLOCKED_NORMAL_REPLY turn_id=%s", new_turn_id)
        logger.info("USER_TURN_NORMAL_REPLY_BLOCKED reason=memory_query_gate")
        logger.info("TEXT_INPUT_LOCAL_COMMAND_EARLY_RETURN")
        return True
    memory_context_instruction = await asyncio.to_thread(
        build_voice_memory_context_instruction,
        resolved_turn_text,
        source="text",
        turn_id=new_turn_id,
    )
    if resolved_instruction:
        extra_instructions = _combine_extra_instructions(extra_instructions, resolved_instruction)
    extra_instructions = _combine_extra_instructions(extra_instructions, memory_context_instruction)
    logger.info(
        "USER_TURN_NORMAL_REPLY_DISPATCHED source=text client_turn_id=%s turn_id=%s route=gemini_auto",
        normalized_client_turn_id or "none",
        new_turn_id,
    )
    logger.info(
        "ASSISTANT_REPLY_REQUESTED source=text client_turn_id=%s turn_id=%s",
        normalized_client_turn_id or "none",
        new_turn_id,
    )
    try:
        await speech_coordinator.speak_user_reply(
            user_input=resolved_turn_text,
            observer=_observer_for_reply(observer),
            input_modality="text",
            turn_id=new_turn_id,
            build_runtime_context_instruction=build_runtime_context_instruction,
            extra_instructions=extra_instructions,
        )
        if memory_context_instruction:
            logger.info("MEMORY_NATURAL_CONTEXT_USED_IN_REPLY turn_id=%s source=text", new_turn_id)
        logger.info("TEXT_TURN_REPLY_DISPATCHED source=text")
        logger.info("TEXT_INPUT_NORMAL_REPLY_CLEANUP_OK")
    except Exception as exc:
        cleanup = getattr(speech_coordinator, "cleanup_failed_user_reply", None)
        if callable(cleanup):
            await _await_if_needed(cleanup(turn_id=new_turn_id, reason="text_input_reply_failed"))
        logger.warning("TEXT_INPUT_REPLY_FAILED_CLEANUP_OK error=%s", exc)
    return True


def _participant_identity(participant) -> str:
    if participant is None:
        return ""
    try:
        return str(getattr(participant, "identity", "") or "").strip()
    except Exception:
        return ""


def build_background_context_message(summary: str) -> str:
    return (
        f"{BACKGROUND_CONTEXT_PREFIX} Contexto atual observado: {summary}. "
        "Use isso como pista visual so com modo visao ativo, sem fingir certeza. "
        "Entenda a UI em silencio e fale de forma curta so quando isso realmente ajudar. "
        "Se o usuario pedir uma acao baseada no que esta visivel, use esse contexto para escolher a ferramenta mais segura. "
        "Se faltar confianca visual, peca confirmacao curta. "
        "Se faltar contexto sobre jogo, app ou site, pesquise antes de orientar."
    )


def build_runtime_context_instruction(summary: str) -> str:
    return (
        f"Contexto operacional visual: {summary}. "
        "Use isso apenas como pista visual com modo visao ativo, sem fingir certeza. "
        "Entenda a UI em silencio, seja objetiva e use ferramentas quando o usuario pedir acao baseada no que estiver visivel. "
        "Se a confianca visual estiver fraca, confirme em uma frase curta antes de agir. "
        "Nunca verbalize metadados operacionais, classificacoes internas ou notas de controle."
    )


def build_voice_user_context_instruction(user_ctx: dict[str, object]) -> str:
    return (
        "Contexto interno de tom para esta resposta falada: "
        f"contexto_usuario_tipo={user_ctx.get('tipo', 'casual')}; "
        f"contexto_usuario_intensidade={float(user_ctx.get('intensidade', 0.0) or 0.0):.2f}. "
        "Use isso apenas para ajustar o tom. "
        "Em voz, responda somente com a fala final. "
        "Nunca inclua SYSTEM_NOTE_FOR_REASONING_ONLY, contexto_usuario_* "
        "ou metadados tecnicos na resposta."
    )


def build_visual_context_disabled_message() -> str:
    return (
        f"{BACKGROUND_CONTEXT_PREFIX} "
        "Modo visao desligado. Ignore qualquer contexto visual anterior "
        "ate o usuario ativar a visao novamente."
    )


def parse_room_packet(packet: rtc.DataPacket) -> dict[str, object] | None:
    try:
        payload = json.loads(packet.data.decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


class RoomRuntimeCoordinator:
    def __init__(
        self,
        *,
        ctx,
        session,
        agent,
        observer,
        commentary_engine,
        speech_coordinator,
        visual_runtime=None,
        enable_room_audio_bridge: bool = True,
    ) -> None:
        self._ctx = ctx
        self._session = session
        self._agent = agent
        self._observer = observer
        self._commentary_engine = commentary_engine
        self._speech_coordinator = speech_coordinator
        try:
            setattr(self._speech_coordinator, "_runtime_event_handlers", self)
        except Exception:
            pass
        self._visual_runtime = visual_runtime
        self._background_tasks: set[asyncio.Task] = set()
        self._chat_ctx_update_lock = asyncio.Lock()
        self._last_context_summary = ""
        self._last_context_note_at = 0.0
        self._last_visual_ui_refresh_at = 0.0
        self._audio_bridge = (
            RoomAudioBridge(
                room=ctx.room,
                session=session,
            )
            if enable_room_audio_bridge
            else None
        )
        self._video_bridge = RoomVideoBridge(
            room=ctx.room,
            observer=observer,
            signal_handler=self._schedule_bridge_signal,
            visual_runtime=visual_runtime,
            voice_state_provider=getattr(speech_coordinator, "get_runtime_voice_state", None),
        )
        self._whatsapp_processing = False

    def pop_screen_proactive_events(self) -> list[dict[str, object]]:
        return self._video_bridge.pop_screen_proactive_events()

    def track_task(self, task: asyncio.Task) -> None:
        self._background_tasks.add(task)

        def _cleanup(done: asyncio.Task) -> None:
            self._background_tasks.discard(done)
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("TEXT_CONTEXT_TASK_FAILED_NON_FATAL reason=%s", exc)
                logger.info("TEXT_TURN_CONTINUING_AFTER_CONTEXT_ERROR")
                log_exception_safely(
                    logger,
                    "Falha em tarefa de contexto em background",
                    exc,
                    level=logging.WARNING,
                )

        task.add_done_callback(_cleanup)

    def register_room_handlers(self) -> None:
        self._ctx.room.on("data_received", self._on_data_received)
        audio_bridge_enabled = self._audio_bridge is not None
        if self._audio_bridge is not None:
            self._audio_bridge.register_handlers()
        self._video_bridge.register_handlers()
        logger.info(
            "Handlers de sala registrados para dados, audio_bridge=%s e video_bridge=%s. Voz e visao seguem pelo fluxo padrao do LiveKit + Gemini.",
            audio_bridge_enabled,
            True,
        )

    def _schedule_bridge_signal(self, payload: dict[str, object]) -> None:
        self.track_task(asyncio.create_task(self._handle_signal_payload(payload)))

    async def shutdown(self) -> None:
        if self._audio_bridge is not None:
            await self._audio_bridge.shutdown()
        await self._video_bridge.shutdown()
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*list(self._background_tasks), return_exceptions=True)

    async def _append_runtime_chat_message(self, *, role: str, content: str) -> None:
        text = str(content or "").strip()
        if not text:
            return

        try:
            async with self._chat_ctx_update_lock:
                new_ctx = self._agent.chat_ctx.copy()
                new_ctx.add_message(role=role, content=text)
                await self._agent.update_chat_ctx(new_ctx)
        except Exception as exc:
            log_exception_safely(
                logger,
                "Falha ao atualizar chat context em runtime",
                exc,
                level=logging.WARNING,
            )

    async def _remember_context(self, update) -> None:
        if update.event_kind == "visual_context_stopped":
            self._last_context_summary = ""
            self._last_context_note_at = time.monotonic()
            await self._append_runtime_chat_message(
                role="assistant",
                content=build_visual_context_disabled_message(),
            )
            logger.info("Contexto visual desativado porque o modo visao foi desligado.")
            return

        if not update.notable and update.importance < 0.7:
            return

        summary = _observer_compact_summary(update)
        now = time.monotonic()
        if not summary:
            return
        if summary == self._last_context_summary and now - self._last_context_note_at < 45.0:
            return

        self._last_context_summary = summary
        self._last_context_note_at = now
        await self._append_runtime_chat_message(
            role="assistant",
            content=build_background_context_message(summary),
        )
        logger.info("Contexto observado: %s", summary)

    async def _handle_signal_payload(self, payload: dict[str, object]) -> None:
        signal_type = str(payload.get("type") or "").strip()
        now = time.monotonic()

        if signal_type == "chat":
            text = str(payload.get("text") or "").strip()
            if not text:
                return
            client_turn_id = str(
                payload.get("clientTurnId")
                or payload.get("client_turn_id")
                or payload.get("turnId")
                or ""
            ).strip()
            input_type = str(payload.get("input_type") or payload.get("message_type") or "text").strip().lower() or "text"
            logger.info(
                "CHAT_MESSAGE_INPUT_CLASSIFIED source=%s input_type=%s client_turn_id=%s",
                str(payload.get("source") or "text").strip().lower() or "text",
                input_type,
                client_turn_id or "none",
            )
            accepted, accept_reason = should_accept_manual_user_input(text)
            if not accepted:
                logger.info("USER_INPUT_REJECTED_NOISE source=manual reason=%s text=%r", accept_reason, _safe_user_text_preview(text, max_chars=40))
                return
            logger.info(
                "UI_TEXT_DATA_CHANNEL_RECEIVED client_turn_id=%s type=chat text=%r",
                client_turn_id or "none",
                _safe_user_text_preview(text),
            )
            logger.info("CHAT_MESSAGE_RECEIVED source=data_channel text=%r", _safe_user_text_preview(text))
            logger.info("AGENT_TEXT_RECEIVED_FROM_UI text=%r", _safe_user_text_preview(text))
            try:  # texto escrito (inclusive pelo celular) também vale como o pedido atual para as tools de ação
                self._agent.record_user_activity(text, source="chat")
            except Exception:
                pass
            try:
                try:
                    from agent.runtime.media_autoshow import media_autoshow_for_text
                except ImportError:
                    from runtime.media_autoshow import media_autoshow_for_text
                media_autoshow_for_text(text)
            except Exception as exc:
                logger.debug("MEDIA_AUTOSHOW_TEXT_SKIPPED reason=%s", exc)
            if accept_reason == "short_allowed":
                logger.info("SHORT_COMMAND_DELIVERED_TO_AGENT")
            try:
                ui_panel_gate = getattr(self._agent, "try_handle_ui_panel_command", None)
                if callable(ui_panel_gate) and await ui_panel_gate(text, source="text", turn_id=None):
                    return
            except Exception as exc:
                logger.warning("UI_PANEL_COMMAND_GATE_ERROR source=text error=%s", exc)
            if is_whatsapp_intent(text):
                logger.info("[WHATSAPP_INTENT_PRIORITY] intent_detected=true source=chat")

            register_user_activity = getattr(self._speech_coordinator, "register_user_activity", None)
            if callable(register_user_activity):
                try:
                    register_user_activity(source="text_input")
                except Exception as exc:
                    logger.debug("[ACTIVITY_MONITOR] chat_activity_failed detail=%s", exc)
            await _notify_backend_economy_interaction(reason="data_channel_text")
            try:
                self._observer.observe_transcription(text, now=now)
            except Exception as exc:
                logger.warning("TEXT_CONTEXT_TASK_FAILED_NON_FATAL reason=observe_transcription_failed:%s", exc)
                logger.info("TEXT_TURN_CONTINUING_AFTER_CONTEXT_ERROR")
            is_visual_query = self._is_visual_query(text)
            if is_visual_query:
                logger.info("SCREEN_QUERY_DETECTED text=%r", text)
            classify_context = getattr(self._agent, "classify_user_context", None)
            user_ctx: dict[str, object] = {
                "tipo": "casual",
                "intensidade": 0.0,
                "palavras_chave_detectadas": [],
            }
            if callable(classify_context):
                try:
                    user_ctx = dict(classify_context(text))
                except Exception as exc:
                    logger.warning("[KIRA CONTEXT] classify_failed source=chat reason=%s", exc)
                else:
                    logger.info(
                        "[KIRA CONTEXT] input=%r tipo=%s intensidade=%.2f palavras=%s source=chat",
                        text,
                        str(user_ctx.get("tipo", "casual")),
                        float(user_ctx.get("intensidade", 0.0) or 0.0),
                        list(user_ctx.get("palavras_chave_detectadas", [])),
                    )
            record_user_activity = getattr(self._agent, "record_user_activity", None)
            if callable(record_user_activity):
                try:
                    await _call_with_optional_source(
                        record_user_activity,
                        text,
                        source="chat",
                    )
                except Exception as exc:
                    log_exception_safely(
                        logger,
                        f"Falha ao registrar atividade do usuario texto={text[:80]}",
                        exc,
                        level=logging.WARNING,
                    )
            visual_snapshot = None
            if self._visual_runtime is not None and _observer_visual_context_active(self._observer):
                if is_visual_query:
                    logger.info("SCREEN_QUERY_WAITING_FRESH_FRAME timeout_s=%.2f", VISUAL_QUERY_WAIT_TIMEOUT_SECONDS)
                    fresh = await self._visual_runtime.wait_for_fresh_frame(
                        timeout_seconds=VISUAL_QUERY_WAIT_TIMEOUT_SECONDS,
                    )
                    if not fresh:
                        state = self._visual_runtime.get_context_state()
                        logger.warning(
                            "SCREEN_QUERY_FAILED_NO_FRESH_FRAME track_sid=%s source=%s width=%s height=%s frame_ts=%.3f age_ms=%.1f",
                            state.get("track_sid") or "none",
                            state.get("source") or "visual_context",
                            state.get("width") or 0,
                            state.get("height") or 0,
                            float(state.get("frame_timestamp") or 0.0),
                            float(state.get("age_ms") or float("inf")),
                        )
                        await self._speech_coordinator.speak_proactive_message(
                            "Ainda nao tenho frame fresco da tela atual agora. Me chama de novo em um instante.",
                            label="KIRA",
                            add_to_chat_ctx=True,
                        )
                        return
                visual_snapshot = await self._refresh_visual_state(require_ui=True)
                if is_visual_query:
                    state = self._visual_runtime.get_context_state()
                    logger.info(
                        "SCREEN_QUERY_USING_FRAME track_sid=%s source=%s width=%s height=%s frame_ts=%.3f age_ms=%.1f",
                        state.get("track_sid") or "none",
                        state.get("source") or "visual_context",
                        state.get("width") or 0,
                        state.get("height") or 0,
                        float(state.get("frame_timestamp") or 0.0),
                        float(state.get("age_ms") or float("inf")),
                    )
            extra_instructions = ""
            if user_ctx:
                extra_instructions = build_voice_user_context_instruction(user_ctx)
            if is_visual_query:
                visual_summary = _observer_compact_summary(self._observer)
                extra_visual = (
                    "Pergunta visual explicita detectada. "
                    "Responda somente com base no frame visual mais recente e no contexto visual atual. "
                    "Nao use memoria visual antiga, nao use fallback generico. "
                    "Se nao houver detalhe suficiente no frame atual, diga isso claramente em uma frase curta. "
                    f"Contexto visual atual: {visual_summary}"
                )
                extra_instructions = (
                    f"{extra_instructions}\n\n{extra_visual}".strip()
                    if extra_instructions
                    else extra_visual
                )
            build_reply_instruction = getattr(self._agent, "build_proactive_reply_instruction", None)
            if callable(build_reply_instruction):
                try:
                    proactive_instruction = str(
                        await _await_if_needed(build_reply_instruction())
                    ).strip()
                    if proactive_instruction:
                        extra_instructions = (
                            f"{extra_instructions}\n\n{proactive_instruction}".strip()
                            if extra_instructions
                            else proactive_instruction
                        )
                except Exception as exc:
                    log_exception_safely(
                        logger,
                        f"Falha ao preparar instrucoes proativas texto={text[:80]}",
                        exc,
                        level=logging.WARNING,
                    )
            logger.info("Mensagem de chat recebida para resposta: %s", text)
            await dispatch_text_input_to_reply(
                text=text,
                speech_coordinator=self._speech_coordinator,
                observer=self._observer,
                agent=self._agent,
                handle_local_quick_command=lambda user_text: self._handle_local_quick_command(
                    user_text,
                    user_context=user_ctx,
                ),
                extra_instructions=extra_instructions,
                client_turn_id=client_turn_id,
            )
            if is_visual_query:
                logger.info("SCREEN_RESPONSE_GENERATED text=%r", text)
            return

        previous_visual_context_active = _observer_visual_context_active(self._observer)
        try:
            update = self._observer.observe_signal(payload, now=now)
        except Exception as exc:
            log_exception_safely(
                logger,
                f"Falha ao atualizar contexto visual para signal_type={signal_type or 'desconhecido'}",
                exc,
                level=logging.WARNING,
            )
            return
        current_visual_context_active = _observer_visual_context_active(self._observer)
        cancelled_watchers = self._agent.set_visual_context_active(current_visual_context_active)
        if self._visual_runtime is not None:
            self._visual_runtime.on_visual_context_active(current_visual_context_active)
        if cancelled_watchers:
            logger.info(
                "Modo visao desligado. %s watcher(s) visual(is) cancelado(s).",
                cancelled_watchers,
            )
        if (
            signal_type == "session_context"
            and previous_visual_context_active != current_visual_context_active
            and not current_visual_context_active
        ):
            await self._append_runtime_chat_message(
                role="assistant",
                content=build_visual_context_disabled_message(),
            )
            logger.info("Contexto visual desativado por session_context sem modo visao ativo.")
        if not update:
            return

        visual_snapshot = None
        if self._visual_runtime is not None and _observer_visual_context_active(self._observer):
            visual_snapshot = await self._refresh_visual_state(
                update=update,
                now=now,
            )

        await self._remember_context(update)

        if not _observer_visual_context_active(self._observer) and update.event_kind != "visual_context_started":
            return

        comment = self._commentary_engine.build_comment(
            update,
            visual_snapshot=visual_snapshot,
            now=now,
        )
        if not comment:
            return

        logger.info("Comentario contextual: %s", comment)
        await self._speech_coordinator.speak_proactive_message(comment, label="Contexto")

    def _on_data_received(self, packet: rtc.DataPacket) -> None:
        payload = parse_room_packet(packet)
        if not payload:
            return
        try:
            participant_identity = str(getattr(getattr(packet, "participant", None), "identity", "") or "").strip()
        except Exception:
            participant_identity = ""
        signal_type = str(payload.get("type") or "").strip()
        client_turn_id = str(payload.get("clientTurnId") or payload.get("client_turn_id") or payload.get("turnId") or "").strip()
        if signal_type == "chat":
            logger.info(
                "UI_TEXT_DATA_CHANNEL_PACKET_RECEIVED client_turn_id=%s participant=%s",
                client_turn_id or "none",
                participant_identity or "unknown",
            )
        self.track_task(asyncio.create_task(self._handle_signal_payload(payload)))

    async def _handle_local_quick_command(
        self,
        user_text: str,
        *,
        user_context: dict[str, object] | None = None,
    ) -> bool:
        text = str(user_text or "").strip()
        if self._whatsapp_processing:
            logger.info("[BLOCKED_BY_WHATSAPP] quick_command_blocked=true")
            return True

        async def _quick_safety_gate(text_value: str, _context: dict) -> dict:
            safety = check_safety_gate(text_value)
            if not safety.matched:
                return {"handled": False}
            publish_notice = getattr(self._agent, "publish_ui_panel_text_notice", None)
            if callable(publish_notice):
                await publish_notice(safety.response, turn_id=None)
            logger.warning("SAFETY_GATE_MATCHED severity=critical")
            return {"handled": True, "kind": "safety", "user_visible_text": safety.response}

        def _quick_agent_gate(name: str, method_name: str | None = None):
            async def _run(text_value: str, _context: dict) -> dict:
                gate = getattr(self._agent, method_name or f"try_handle_{name}_command", None)
                if not callable(gate):
                    return {"handled": False}
                try:
                    handled_value = await gate(text_value, source="text", turn_id=None)
                except Exception as exc:
                    logger.warning("%s_GATE_ERROR source=text error=%s", name.upper(), exc)
                    return {"handled": False}
                return {"handled": bool(handled_value)}
            return _run

        route_result = await route_user_command(
            text,
            source="text",
            gates=(
                CommandGate("safety", _quick_safety_gate),
                CommandGate("ui_panel", _quick_agent_gate("ui_panel")),
                CommandGate("local_command", _quick_agent_gate("local_command")),
                CommandGate("volume", _quick_agent_gate("volume")),
                CommandGate("creation_task", _quick_agent_gate("creation_task")),
                CommandGate("artifact_command", _quick_agent_gate("artifact_command", "try_handle_artifact_command")),
                CommandGate("whatsapp", _quick_agent_gate("whatsapp")),
                CommandGate("browser_task", _quick_agent_gate("browser_task")),
            ),
        )
        if bool(route_result.get("handled") or route_result.get("stop_routing")):
            gate_name = str(route_result.get("gate") or route_result.get("kind") or "unknown")
            logger.info("USER_TURN_NORMAL_REPLY_BLOCKED reason=gate_handled gate=%s", gate_name)
            return True

        try:
            ui_panel_gate = getattr(self._agent, "try_handle_ui_panel_command", None)
            if callable(ui_panel_gate) and await ui_panel_gate(text, source="text", turn_id=None):
                logger.info("UI_PANEL_LEGACY_ROUTE_BLOCKED route=runtime_local_quick_command reason=handled_by_ui_panel_gate")
                logger.info("UI_PANEL_VOICE_ROUTE_BLOCKED reason=ui_panel_is_text_only")
                logger.info("UI_PANEL_GEMINI_ROUTE_BLOCKED reason=ui_panel_command")
                logger.info("UI_PANEL_OPEN_TAB_BLOCKED reason=ui_panel_command")
                return True
        except Exception as exc:
            logger.warning("UI_PANEL_COMMAND_FAIL_OPEN_CONTINUE turn_id=none error=%s", exc)

        for gate_name in ("creation_task", "artifact_command"):
            gate = getattr(self._agent, f"try_handle_{gate_name}_command", None)
            if not callable(gate):
                continue
            try:
                if await gate(text, source="text", turn_id=None):
                    logger.info("WHATSAPP_ROUTE_BLOCKED reason=creation_or_artifact_command")
                    logger.info("FUZZY_ROUTE_BLOCKED reason=creation_or_artifact_command")
                    logger.info("GEMINI_ROUTE_BLOCKED reason=creation_or_artifact_command")
                    logger.info("NORMAL_REPLY_BLOCKED reason=creation_or_artifact_command")
                    return True
            except Exception as exc:
                logger.warning("%s_GATE_ERROR source=text error=%s", gate_name.upper(), exc)

        local_file_gate = getattr(self._agent, "try_handle_local_file_command", None)
        if callable(local_file_gate):
            try:
                if await local_file_gate(text, source="text", turn_id=None):
                    logger.info("FUZZY_ROUTE_BLOCKED reason=local_file_command")
                    logger.info("GEMINI_ROUTE_BLOCKED reason=local_file_command")
                    logger.info("NORMAL_REPLY_BLOCKED reason=local_file_command")
                    return True
            except Exception as exc:
                logger.warning("LOCAL_FILE_COMMAND_GATE_ERROR source=text error=%s", exc)

        # [REMOVED] PC_OPERATOR: desktop_operator gate removido

        try:
            from agent.runtime.fuzzy_command_matcher import handle_open_web
        except Exception:
            from runtime.fuzzy_command_matcher import handle_open_web
        open_web_result = handle_open_web(text)
        if open_web_result.get("handled"):
            notice = str(open_web_result.get("response") or "Nao identifiquei um site valido para abrir.")
            publish_notice = getattr(self._agent, "publish_ui_panel_text_notice", None)
            if callable(publish_notice):
                await publish_notice(notice, turn_id=None)
            try:
                from services.local_command_guard import mark_local_command
                mark_local_command("open_web_rejected")
            except Exception:
                pass
            logger.info("FUZZY_ROUTE_BLOCKED reason=open_web_rejected")
            logger.info("GEMINI_ROUTE_BLOCKED reason=open_web_rejected")
            logger.info("NORMAL_REPLY_BLOCKED reason=open_web_rejected")
            return True

        def _style_response(payload: str, *, personality_context: str = "default") -> str:
            if _LEGACY_FILE_MARKER in str(payload or "").upper():
                logger.warning(
                    "LEGACY_FILE_REPLY_BLOCKED reason=internal_marker_detected source=local_response"
                )
                return ""
            style_fn = getattr(self._agent, "apply_response_style", None)
            if callable(style_fn):
                return style_fn(
                    payload,
                    user_context=user_context,
                    personality_context=personality_context,
                )
            base = apply_kira_personality_style(payload, context=personality_context)
            tuned = apply_kira_tone(base, user_context)
            logger.info(
                "[KIRA TONE] tipo_contexto=%s modo_aplicado=%s",
                str((user_context or {}).get("tipo", "desconhecido")),
                resolve_kira_tone_mode(user_context),
            )
            return tuned

        assistant_whatsapp_active = getattr(self._agent, "is_whatsapp_tool_active", None)
        assistant_whatsapp_handler = getattr(self._agent, "handle_outbound_whatsapp_intent", None)
        if callable(assistant_whatsapp_active) and assistant_whatsapp_active() and callable(assistant_whatsapp_handler):
            logger.info("[AUTO_REPLY_BLOCKED] WhatsApp tool esta ativo, bloqueando auto-reply do Gemini")
            logger.info("[WHATSAPP_TOOL_ACTIVE_BLOCK] Bloqueando auto-reply normal porque WhatsApp tool esta ativo")
            try:
                whatsapp_result = await assistant_whatsapp_handler(text)
            except Exception as exc:
                log_exception_safely(
                    logger,
                    f"Falha ao processar follow-up de WhatsApp texto={text[:80]}",
                    exc,
                    level=logging.WARNING,
                )
            else:
                if getattr(whatsapp_result, "handled", False):
                    tool_result = getattr(whatsapp_result, "tool_result", None)
                    local_response = str(getattr(whatsapp_result, "local_response", "") or "").strip()
                    if tool_result is not None:
                        await self._speech_coordinator.speak_tool_action_result(tool_result, add_to_chat_ctx=True)
                    elif local_response:
                        await self._speech_coordinator.speak_post_action_reply(
                            local_response,
                            label="WHATSAPP_TOOL",
                            add_to_chat_ctx=True,
                        )
                    logger.info("NORMAL_REPLY_SUPPRESSED reason=whatsapp_tool_active")
                    logger.info("PROACTIVE_SUPPRESSED reason=whatsapp_tool_active")
                    return True

        logger.debug("[DEBUG] Verificando voice_profile_handler...")
        voice_profile_handler = getattr(self._agent, "handle_voice_profile_query", None)
        if callable(voice_profile_handler):
            try:
                voice_profile_response = voice_profile_handler(text)
            except Exception as exc:
                log_exception_safely(
                    logger,
                    f"Falha ao processar voice profile texto={text[:80]}",
                    exc,
                    level=logging.WARNING,
                )
            else:
                logger.debug(
                    "[DEBUG] voice_profile result: %r",
                    str(voice_profile_response)[:60] if voice_profile_response else None,
                )
                if voice_profile_response:
                    logger.info("[VOICE_PROFILE] Comando interceptado texto=%r", text[:80])
                    await self._speech_coordinator.speak_proactive_message(
                        str(voice_profile_response),
                        label="VOICE_PROFILES",
                        add_to_chat_ctx=True,
                    )
                    return True

        logger.debug("[DEBUG] Verificando simple_query_handler...")
        simple_query_handler = getattr(self._agent, "_simple_query_handler", None)
        if simple_query_handler is not None:
            try:
                simple_response = simple_query_handler.handle(text)
            except Exception as exc:
                log_exception_safely(
                    logger,
                    f"Falha ao processar simple query texto={text[:80]}",
                    exc,
                    level=logging.WARNING,
                )
            else:
                logger.debug("[DEBUG] simple_query result: %r", simple_response)
                if isinstance(simple_response, dict) and simple_response.get("source") == "system_vitals":
                    logger.info("VOICE_ROUTE_BLOCKED reason=system_vitals_text_only")
                    logger.info(
                        "SYSTEM_VITALS_COMMAND_HANDLED mode=text_only kind=%s",
                        str(simple_response.get("kind") or "unknown"),
                    )
                    return True
                if simple_response:
                    logger.info("[SIMPLE_QUERY] Respondido localmente texto=%r", text[:80])
                    await self._speech_coordinator.speak_proactive_message(
                        str(simple_response),
                        label="SIMPLE_QUERY",
                        add_to_chat_ctx=True,
                    )
                    return True

        logger.debug("[DEBUG] Verificando cache...")
        response_cache = getattr(self._agent, "_response_cache", None)
        if response_cache is not None:
            try:
                cached_response = response_cache.get(text)
            except Exception as exc:
                log_exception_safely(
                    logger,
                    f"Falha ao consultar cache texto={text[:80]}",
                    exc,
                    level=logging.WARNING,
                )
            else:
                if cached_response:
                    if _LEGACY_FILE_MARKER in str(cached_response).upper():
                        logger.warning("LEGACY_CACHE_ENTRY_SKIPPED reason=kira_file_marker")
                        logger.warning("LEGACY_CACHE_ENTRY_PURGED key=%r action=ignore_only", text[:80])
                        return True
                    logger.info("[CACHE] HIT texto=%r", text[:80])
                    await self._speech_coordinator.speak_proactive_message(
                        str(cached_response),
                        label="CACHE",
                        add_to_chat_ctx=True,
                    )
                    return True

        pending_file_edit_handler = getattr(self._agent, "handle_pending_file_edit", None)
        if callable(pending_file_edit_handler):
            try:
                file_edit_handled, file_edit_message = await pending_file_edit_handler(text)
            except Exception as exc:
                log_exception_safely(
                    logger,
                    f"Falha ao processar edicao pendente texto={text[:80]}",
                    exc,
                    level=logging.WARNING,
                )
            else:
                if file_edit_handled:
                    local_response = str(file_edit_message or "").strip()
                    if local_response:
                        local_response = _style_response(local_response, personality_context="pending")
                        logger.info("Resposta local de edicao pendente: %s", local_response)
                        await self._speech_coordinator.speak_proactive_message(
                            local_response,
                            label="KIRA EDIT",
                            add_to_chat_ctx=True,
                        )
                    return True

        pending_file_save_handler = getattr(self._agent, "handle_pending_file_save", None)
        if callable(pending_file_save_handler):
            try:
                file_save_handled, file_save_message = await pending_file_save_handler(text)
            except Exception as exc:
                log_exception_safely(
                    logger,
                    f"Falha ao processar salvamento pendente texto={text[:80]}",
                    exc,
                    level=logging.WARNING,
                )
            else:
                if file_save_handled:
                    local_response = str(file_save_message or "").strip()
                    if local_response:
                        if _LEGACY_FILE_MARKER in local_response.upper():
                            logger.warning(
                                "LEGACY_FILE_REPLY_BLOCKED reason=internal_marker_detected source=pending_file_save"
                            )
                            local_response = "Tarefa de arquivo registrada. Aguardando executor."
                        publish_notice = getattr(self._agent, "publish_ui_panel_text_notice", None)
                        if callable(publish_notice):
                            await publish_notice(local_response, turn_id=None)
                        logger.info("LOCAL_COMMAND_TEXT_ONLY source=legacy_file_blocked")
                        logger.info("LOCAL_COMMAND_VOICE_DISABLED reason=local_command")
                        logger.info("LOCAL_COMMAND_GEMINI_DISABLED reason=local_command")
                        logger.info("LOCAL_COMMAND_NO_SPEECH_STATE_CHANGE")
                        logger.info("LOCAL_COMMAND_PENDING_CLEARED")
                        logger.info("LOCAL_COMMAND_TURN_DONE")
                    return True

        whatsapp_confirmation_handler = getattr(
            self._agent,
            "process_whatsapp_confirmation",
            None,
        )
        if callable(whatsapp_confirmation_handler):
            try:
                confirmation_result = await whatsapp_confirmation_handler(text)
            except Exception as exc:
                log_exception_safely(
                    logger,
                    f"Falha ao processar confirmacao de WhatsApp texto={text[:80]}",
                    exc,
                    level=logging.WARNING,
                )
            else:
                if getattr(confirmation_result, "handled", False):
                    local_response = str(getattr(confirmation_result, "local_response", "") or "").strip()
                    tool_result = getattr(confirmation_result, "tool_result", None)
                    if tool_result is not None:
                        await self._speech_coordinator.speak_tool_action_result(tool_result, add_to_chat_ctx=True)
                    elif local_response:
                        logger.info("NORMAL_REPLY_SUPPRESSED reason=whatsapp_tool_handled")
                        logger.info("PROACTIVE_SUPPRESSED reason=whatsapp_tool_active")
                    return True

        whatsapp_delivery_status = getattr(
            self._agent,
            "maybe_resolve_whatsapp_delivery_status_query",
            None,
        )
        if callable(whatsapp_delivery_status):
            try:
                delivery_status_response = whatsapp_delivery_status(text)
            except Exception as exc:
                log_exception_safely(
                    logger,
                    f"Falha ao consultar status real de WhatsApp texto={text[:80]}",
                    exc,
                    level=logging.WARNING,
                )
            else:
                delivery_status_response = str(delivery_status_response or "").strip()
                if delivery_status_response:
                    from runtime.tool_action_result_manager import ToolActionResult
                    import time as _t
                    logger.info("WHATSAPP_STATUS_TOOL_RESULT_CREATED")
                    tool_result = ToolActionResult(
                        tool="whatsapp",
                        action="status_query",
                        status="success",
                        source="whatsapp_tool",
                        request_id=f"wa-status-{int(_t.time()*1000)}",
                        speak_text=delivery_status_response,
                        transcript_text=delivery_status_response,
                        should_speak=True,
                        should_publish_transcript=True,
                    )
                    await self._speech_coordinator.speak_tool_action_result(tool_result, add_to_chat_ctx=True)
                    logger.info("WHATSAPP_STATUS_TOOL_ACK_DISPATCHED")
                    logger.info("NORMAL_REPLY_SUPPRESSED reason=whatsapp_status_query")
                    logger.info("PROACTIVE_SUPPRESSED reason=whatsapp_tool_active")
                    return True

        has_pending_confirmation = getattr(self._agent, "has_pending_whatsapp_confirmation", None)
        if callable(has_pending_confirmation) and bool(has_pending_confirmation()):
            pending = getattr(self._agent, "_pending_whatsapp_confirmation", None)
            contact = str(getattr(pending, "contact", "") or "esse contato").strip() or "esse contato"
            clarification = (
                f"Ainda estou aguardando sua confirmacao para enviar a mensagem para {contact}. "
                "Me diz 'sim, manda' ou 'nao, cancela'."
            )
            logger.info("[WHATSAPP CONFIRM] pending_guard_reprompt contact=%s", contact)
            from runtime.tool_action_result_manager import ToolActionResult
            import time as _t
            tool_result = ToolActionResult(
                tool="whatsapp",
                action="confirmation_pending",
                status="pending",
                source="whatsapp_tool",
                request_id=f"wa-confirm-{int(_t.time()*1000)}",
                speak_text=clarification,
                transcript_text=clarification,
                should_speak=True,
                should_publish_transcript=True,
            )
            await self._speech_coordinator.speak_tool_action_result(tool_result, add_to_chat_ctx=True)
            return True

        outbound_intent_handler = getattr(
            self._agent,
            "handle_outbound_whatsapp_intent",
            None,
        )
        if callable(outbound_intent_handler) and (
            is_whatsapp_intent(text)
            or bool(getattr(self._agent, "is_whatsapp_tool_active", lambda: False)())
        ):
            self._whatsapp_processing = True
            try:
                outbound_result = await outbound_intent_handler(text)
            except Exception as exc:
                log_exception_safely(
                    logger,
                    f"Falha ao processar intencao de envio WhatsApp texto={text[:80]}",
                    exc,
                    level=logging.WARNING,
                )
            else:
                if getattr(outbound_result, "handled", False):
                    if getattr(outbound_result, "stop_normal_reply", False):
                        logger.info("WHATSAPP_TOOL_HANDLED stop_normal_reply=true")
                        logger.info("NORMAL_REPLY_SUPPRESSED reason=whatsapp_tool_handled")
                        logger.info("FALLBACK_SUPPRESSED reason=stop_normal_reply")
                    local_response = str(
                        getattr(outbound_result, "local_response", "") or ""
                    ).strip()
                    tool_result = getattr(outbound_result, "tool_result", None)
                    suppress_seconds = float(getattr(outbound_result, "suppress_proactive_seconds", 0.0) or 0.0)
                    if local_response:
                        voice_lane = str(
                            getattr(outbound_result, "voice_lane", "") or ""
                        ).strip().lower()
                        if voice_lane != "tool_ack":
                            local_response = _style_response(local_response)
                        logger.info(
                            "Resposta local de intencao WhatsApp: %s",
                            local_response,
                        )
                        if voice_lane == "tool_ack":
                            if tool_result is not None and hasattr(self._speech_coordinator, "speak_tool_action_result"):
                                await self._speech_coordinator.speak_tool_action_result(tool_result, add_to_chat_ctx=True)
                            else:
                                logger.info("NORMAL_REPLY_SUPPRESSED reason=whatsapp_tool_handled")
                        else:
                            await self._speech_coordinator.speak_proactive_message(
                                local_response,
                                label="WHATSAPP",
                                add_to_chat_ctx=True,
                            )
                    if suppress_seconds > 0:
                        suppressor = getattr(self._speech_coordinator, "suppress_proactive_for", None)
                        if callable(suppressor):
                            await _await_if_needed(suppressor(suppress_seconds, reason="whatsapp_tool_active"))
                            logger.info("PROACTIVE_SUPPRESSED reason=whatsapp_tool_active")
                    logger.info("[GEMINI_COMPLETELY_BLOCKED] reason=whatsapp_consumed")
                    return True
            finally:
                self._whatsapp_processing = False

        whatsapp_followup_handler = getattr(self._agent, "prepare_whatsapp_followup", None)
        if callable(whatsapp_followup_handler):
            try:
                followup_result = await whatsapp_followup_handler(text)
            except Exception as exc:
                log_exception_safely(
                    logger,
                    f"Falha ao processar follow-up de WhatsApp texto={text[:80]}",
                    exc,
                    level=logging.WARNING,
                )
            else:
                if getattr(followup_result, "handled_locally", False):
                    local_response = str(
                        getattr(followup_result, "local_response", "") or ""
                    ).strip()
                    if local_response:
                        local_response = _style_response(local_response)
                        logger.info(
                            "Resposta local de follow-up WhatsApp: %s",
                            local_response,
                        )
                        await self._speech_coordinator.speak_proactive_message(
                            local_response,
                            label="WHATSAPP",
                            add_to_chat_ctx=True,
                        )
                        return True
                if str(getattr(followup_result, "kind", "") or "").strip() not in {"", "none"}:
                    return False

        pending_handler = getattr(self._agent, "handle_pending_quick_command", None)
        if callable(pending_handler):
            try:
                pending_result = await _call_with_optional_app_origem(
                    pending_handler,
                    text,
                    app_origem="chat",
                )
            except Exception as exc:
                log_exception_safely(
                    logger,
                    f"Falha ao processar pendencias texto={text[:80]}",
                    exc,
                    level=logging.WARNING,
                )
            else:
                if getattr(pending_result, "handled", False):
                    local_response = str(
                        getattr(pending_result, "message", "") or ""
                    ).strip()
                    if local_response:
                        local_response = _style_response(local_response, personality_context="pending")
                        logger.info(
                            "Resposta local de pendencias: %s",
                            local_response,
                        )
                        await self._speech_coordinator.speak_proactive_message(
                            local_response,
                            label="PENDENCIAS",
                            add_to_chat_ctx=True,
                        )
                        return True

        op_memory_handler = getattr(self._agent, "maybe_answer_operational_memory_query", None)
        if callable(op_memory_handler):
            try:
                op_memory_response = str(op_memory_handler(text) or "").strip()
            except Exception as exc:
                log_exception_safely(
                    logger,
                    f"Falha ao consultar memoria operacional local texto={text[:80]}",
                    exc,
                    level=logging.WARNING,
                )
            else:
                if op_memory_response:
                    logger.info("OP_MEMORY_LOCAL_RESPONSE text=%r", text)
                    op_memory_response = _style_response(op_memory_response)
                    await self._speech_coordinator.speak_proactive_message(
                        op_memory_response,
                        label="KIRA MEM",
                        add_to_chat_ctx=True,
                    )
                    return True
                logger.info("OP_MEMORY_FALLBACK_TO_MODEL text=%r", text)

        if not self._looks_like_whatsapp_status_query(text):
            return False

        message = self._agent.describe_whatsapp_status()
        message = _style_response(message)
        logger.info("Resposta local de status do WhatsApp: %s", message)
        await self._speech_coordinator.speak_proactive_message(
            message,
            label="KIRA",
            add_to_chat_ctx=True,
        )
        return True

    async def _refresh_visual_state(self, *, update=None, require_ui: bool | None = None, now: float | None = None):
        if self._visual_runtime is None or not _observer_visual_context_active(self._observer):
            return None
        await get_voice_output_manager().wait_until_heavy_task_allowed("visual_refresh")

        now = now or time.monotonic()
        if require_ui is None:
            require_ui = self._should_probe_ui(update, now=now)
        snapshot = await self._visual_runtime.refresh_observer_state(require_ui=require_ui)
        if snapshot is not None:
            state = self._visual_runtime.get_context_state()
            logger.info(
                "SCREEN_CONTEXT_UPDATED track_sid=%s source=%s width=%s height=%s frame_ts=%.3f age_ms=%.1f",
                state.get("track_sid") or "none",
                state.get("source") or "visual_context",
                state.get("width") or 0,
                state.get("height") or 0,
                float(state.get("frame_timestamp") or 0.0),
                float(state.get("age_ms") or float("inf")),
            )
        if require_ui and snapshot is not None:
            self._last_visual_ui_refresh_at = now
        return snapshot

    def _should_probe_ui(self, update, *, now: float) -> bool:
        if update is None:
            return True
        if update.event_kind in {
            "visual_context_started",
            "app_detected",
            "app_changed",
            "domain_changed",
            "window_focus_changed",
        }:
            return True
        if update.event_kind == "screen_scene_changed":
            return True
        return (now - self._last_visual_ui_refresh_at) >= 8.0

    def _looks_like_whatsapp_status_query(self, text: str) -> bool:
        normalized = " ".join(str(text or "").lower().split())
        if not normalized:
            return False

        whatsapp_terms = ("whatsapp", "wpp", "zap", "whats", "what")
        if not any(term in normalized for term in whatsapp_terms):
            return False

        status_terms = (
            "status",
            "aberto",
            "aberta",
            "fechado",
            "monitor",
            "rodando",
            "ativo",
            "online",
            "ligado",
        )
        question_terms = ("esta", "tá", "ta", "como", "qual")
        return any(term in normalized for term in status_terms) or any(
            term in normalized for term in question_terms
        )

    def _is_visual_query(self, text: str) -> bool:
        normalized = " ".join(str(text or "").strip().split())
        if not normalized:
            return False
        return bool(VISUAL_QUERY_RE.search(normalized))
