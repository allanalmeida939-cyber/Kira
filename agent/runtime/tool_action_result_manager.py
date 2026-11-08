from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

if __package__ and __package__.startswith("agent"):
    from agent.core.personality_response import build_personality_response, format_whatsapp_send_ack
    from agent.core.tool_ack_templates import ToolAckTemplateBank
else:  # pragma: no cover - suporte a execucao direta em agent/
    from core.personality_response import build_personality_response, format_whatsapp_send_ack
    from core.tool_ack_templates import ToolAckTemplateBank

logger = logging.getLogger(__name__)

TOOL_WHATSAPP_SEND = "TOOL_WHATSAPP_SEND"
TOOL_WHATSAPP_STATUS_QUERY = "TOOL_WHATSAPP_STATUS_QUERY"
TOOL_WHATSAPP_PENDING = "TOOL_WHATSAPP_PENDING"
TOOL_WHATSAPP_ERROR = "TOOL_WHATSAPP_ERROR"

@dataclass
class ToolActionResult:
    tool: str
    action: str
    status: str
    source: str
    request_id: str
    contact_name: str | None = None
    contact_alias: str | None = None
    message: str | None = None
    error: str | None = None
    stage: str | None = None
    dry_run: bool = False
    created_at: float = field(default_factory=time.time)
    speak_text: str = ""
    transcript_text: str = ""
    dedupe_key: str = ""
    priority: str = "tool_ack"
    allow_contextual_comment: bool = True
    should_speak: bool = True
    should_publish_transcript: bool = True
    suppress_proactive_seconds: float = 4.0

class ToolActionResultManager:
    def __init__(self) -> None:
        self._templates = ToolAckTemplateBank()
        self._dedupe: dict[str, float] = {}

    def _style_final_text(self, result: ToolActionResult, text: str) -> str:
        tool = str(result.tool or "").strip().lower()
        action = str(result.action or "").strip().lower()
        status = str(result.status or "").strip().lower()
        if tool == "whatsapp":
            if action in {"pending_message", "pending_contact"}:
                return build_personality_response(
                    response_type="clarification_question",
                    raw_text=text,
                    source=result.source or "whatsapp_tool",
                ).text
            if action == "status_query":
                return build_personality_response(
                    response_type="whatsapp_status_ack",
                    raw_text=text,
                    source=result.source or "whatsapp_tool",
                ).text
            if action == "send_message" and status == "success" and not result.dry_run:
                return format_whatsapp_send_ack(
                    contact=result.contact_name or "contato",
                    message=result.message or "",
                    status=status,
                    raw_text=text,
                ).text
            if status in {"failed", "error"}:
                return build_personality_response(
                    response_type="generic_error",
                    raw_text=text,
                    source=result.source or "whatsapp_tool",
                ).text
            return build_personality_response(
                response_type="local_command_ack",
                raw_text=text,
                source=result.source or "whatsapp_tool",
            ).text
        return build_personality_response(
            response_type="local_command_ack",
            raw_text=text,
            source=result.source,
        ).text

    def _finalize(self, result: ToolActionResult, *, category: str, include_context_comment: bool = False) -> ToolActionResult:
        logger.info("TOOL_RESULT_BUILD_REQUESTED tool=%s action=%s", result.tool, result.action)
        template_id, template = self._templates.select(category)
        logger.info("TOOL_ACK_TEMPLATE_SELECTED category=%s template_id=%s", category, template_id)
        text = template.format(contact=result.contact_name or "contato", message=result.message or "", error=result.error or "erro")
        if include_context_comment and result.allow_contextual_comment and result.message and self._templates.should_add_contextual_comment(result.message, result.contact_name or ""):
            comment = self._templates.build_contextual_comment(result.message, result.contact_name or "")
            text = f"{text} {comment}".strip()
            logger.info("TOOL_ACK_CONTEXTUAL_COMMENT_ADDED")
        else:
            logger.info("TOOL_ACK_CONTEXTUAL_COMMENT_SKIPPED reason=policy")
        text = self._style_final_text(result, text)
        result.speak_text = text
        result.transcript_text = text
        result.dedupe_key = result.dedupe_key or f"{result.tool}:{result.action}:{result.status}:{result.request_id}"
        logger.info("TOOL_RESULT_BUILT status=%s source=%s", result.status, result.source)
        logger.info("TOOL_ACK_TEXT_FINAL speak_text=%s transcript_text=%s", result.speak_text, result.transcript_text)
        return result

    def build_whatsapp_send_success_result(self, *, request_id: str, contact_name: str, message: str, dry_run: bool = False) -> ToolActionResult:
        category = "whatsapp_dry_run_success" if dry_run else "whatsapp_send_success"
        return self._finalize(ToolActionResult(tool="whatsapp", action="send_message", status="success", source="whatsapp_tool", request_id=request_id, contact_name=contact_name, message=message, dry_run=dry_run), category=category, include_context_comment=not dry_run)

    def build_whatsapp_contact_not_found_result(
        self,
        *,
        request_id: str,
        contact_name: str,
        query: str,
        source: str,
    ) -> ToolActionResult:
        result = ToolActionResult(
            tool="whatsapp",
            action="send_message",
            status="failed",
            source=source or "whatsapp_tool",
            request_id=request_id,
            contact_name=contact_name or query,
            contact_alias=query,
            error="contact_not_found",
            stage="contact_lookup",
        )
        return self._finalize(result, category="whatsapp_contact_not_found")

    def build_whatsapp_pending_message_result(self, *, request_id: str, contact_name: str) -> ToolActionResult:
        return self._finalize(ToolActionResult(tool="whatsapp", action="pending_message", status="pending", source="whatsapp_tool", request_id=request_id, contact_name=contact_name), category="whatsapp_pending_message")

    def build_whatsapp_pending_contact_result(self, *, request_id: str) -> ToolActionResult:
        return self._finalize(ToolActionResult(tool="whatsapp", action="pending_contact", status="pending", source="whatsapp_tool", request_id=request_id), category="whatsapp_pending_contact")

    def build_whatsapp_last_send_result(self, *, request_id: str, contact_name: str, message: str) -> ToolActionResult:
        return self._finalize(ToolActionResult(tool="whatsapp", action="status_query", status="success", source="whatsapp_tool", request_id=request_id, contact_name=contact_name, message=message, allow_contextual_comment=False), category="whatsapp_last_send_success")

    def should_skip_duplicate(self, dedupe_key: str, window_s: float = 5.0) -> bool:
        now = time.time()
        last = self._dedupe.get(dedupe_key)
        if last and (now - last) <= window_s:
            logger.info("TOOL_ACK_DUPLICATE_SKIPPED key=%s", dedupe_key)
            return True
        self._dedupe[dedupe_key] = now
        logger.info("TOOL_ACK_DEDUPE_REGISTERED key=%s", dedupe_key)
        return False
