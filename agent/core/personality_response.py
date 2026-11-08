from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any

try:
    from agent.prompts import KIRA_LOCAL_RESPONSE_TEMPLATES, get_kira_style_rules
except Exception:  # pragma: no cover - worker can run from agent/
    from prompts import KIRA_LOCAL_RESPONSE_TEMPLATES, get_kira_style_rules


ResponseType = str
logger = logging.getLogger("kira.app")


@dataclass(slots=True)
class PersonalityResponse:
    text: str
    response_type: ResponseType
    should_speak: bool | None = None
    source: str = ""
    style: str = "agent/prompts.py"
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


_SPACE_RE = re.compile(r"\s+")


def _clean_text(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^\s*Interface:\s*", "", text, flags=re.IGNORECASE).strip()
    text = _SPACE_RE.sub(" ", text)
    return text


def _interface_stripped(value: object) -> bool:
    return bool(re.match(r"^\s*Interface:\s*", str(value or ""), flags=re.IGNORECASE))


def _public_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return {str(k): v for k, v in dict(metadata or {}).items() if not str(k).startswith("_")}


def _template_available(response_type: ResponseType) -> bool:
    return str(response_type or "") in KIRA_LOCAL_RESPONSE_TEMPLATES


def _safe_text_preview(result: PersonalityResponse, raw_text: object = "") -> str:
    source = str(result.source or result.metadata.get("source") or "").strip().lower()
    response_type = str(result.response_type or "").strip().lower()
    if "whatsapp" in source or response_type.startswith("whatsapp"):
        return "<masked_whatsapp_text>"
    return _clean_text(result.text)[:120]


def _log_response_applied(
    result: PersonalityResponse,
    *,
    raw_text: object = "",
    fallback: bool | None = None,
) -> PersonalityResponse:
    if bool(result.metadata.pop("_defer_personality_log", False)):
        return result
    clean_raw = _clean_text(raw_text if raw_text != "" else result.text)
    text_hash = hashlib.sha256(clean_raw.encode("utf-8", errors="ignore")).hexdigest()[:12]
    template = _template_available(result.response_type)
    logger.info(
        "PERSONALITY_RESPONSE_APPLIED type=%s source=%s text_only=%s should_speak=%s interface_stripped=%s template=%s fallback=%s text_len=%d text_hash=%s preview=%s",
        result.response_type or "unknown",
        result.source or result.metadata.get("source") or "",
        str(bool(result.metadata.get("text_only"))).lower(),
        "none" if result.should_speak is None else str(bool(result.should_speak)).lower(),
        str(_interface_stripped(raw_text)).lower(),
        str(template).lower(),
        str((not template) if fallback is None else bool(fallback)).lower(),
        len(clean_raw),
        text_hash,
        _safe_text_preview(result, raw_text),
    )
    return result


def _sentence(value: object) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    if text[-1] not in ".!?":
        text += "."
    return text


def _lower_first(value: str) -> str:
    text = str(value or "")
    if len(text) <= 1:
        return text.lower()
    return text[0].lower() + text[1:]


def _strip_duplicate_address(value: str) -> str:
    text = _clean_text(value)
    return re.sub(r"^(chefe|alan|homi|patrao|senhor)[,.]?\s+", "", text, flags=re.IGNORECASE).strip()


def _already_personalized(value: str) -> bool:
    lowered = _clean_text(value).lower()
    prefixes = (
        "pronto, chefe",
        "fechado, chefe",
        "certo, chefe",
        "chefe,",
        "alan,",
        "nao consegui",
        "não consegui",
        "mandei para",
    )
    return any(lowered.startswith(prefix) for prefix in prefixes)


def _apply_template(response_type: ResponseType, message: str, **kwargs: Any) -> str:
    template = KIRA_LOCAL_RESPONSE_TEMPLATES.get(response_type) or "{message}"
    payload = {"message": message, **kwargs}
    return _sentence(template.format(**payload))


def _panel_name(panel: object) -> str:
    normalized = str(panel or "").strip().lower()
    names = {
        "system_vitals": "Sistema Vital",
        "session_status": "Status do Sistema",
        "api_identity": "painel de APIs",
        "transcription": "transcricao",
        "all": "os paineis",
    }
    return names.get(normalized, str(panel or "painel").strip() or "painel")


def _panel_message(action: str, panel: object, *, already: bool = False) -> str:
    target = _panel_name(panel)
    if already:
        if action == "show":
            return f"{target} ja estava visivel"
        if action == "hide":
            return f"{target} ja estava oculto"
        return f"{target} ja estava nesse estado"
    if action == "show":
        return f"mostrei {target} na tela"
    if action == "hide":
        return f"tirei {target} da tela"
    if action == "toggle":
        return f"alternei {target} na tela"
    return f"atualizei {target}"


def format_ack(message: object, *, response_type: ResponseType = "local_command_ack", **metadata: Any) -> PersonalityResponse:
    meta = _public_metadata(metadata)
    if _already_personalized(str(message or "")):
        result = PersonalityResponse(text=_sentence(message), response_type=response_type, metadata=meta)
        return result if bool(metadata.get("_defer_personality_log")) else _log_response_applied(result, raw_text=message)
    clean = _strip_duplicate_address(str(message or "comando executado"))
    text = _apply_template(response_type, _lower_first(clean), **metadata)
    result = PersonalityResponse(text=text, response_type=response_type, metadata=meta)
    return result if bool(metadata.get("_defer_personality_log")) else _log_response_applied(result, raw_text=message)


def format_error(message: object, *, response_type: ResponseType = "generic_error", stage: str = "", **metadata: Any) -> PersonalityResponse:
    meta = _public_metadata(metadata)
    if _already_personalized(str(message or "")):
        result = PersonalityResponse(text=_sentence(message), response_type=response_type, reason=stage, metadata=meta)
        return result if bool(metadata.get("_defer_personality_log")) else _log_response_applied(result, raw_text=message)
    clean = _strip_duplicate_address(str(message or "erro nao informado"))
    if stage and stage.lower() not in clean.lower():
        clean = f"o erro foi na etapa {stage}: {clean}"
    text = _apply_template(response_type, _lower_first(clean), **metadata)
    result = PersonalityResponse(text=text, response_type=response_type, reason=stage, metadata=meta)
    return result if bool(metadata.get("_defer_personality_log")) else _log_response_applied(result, raw_text=message)


def format_question(message: object, *, response_type: ResponseType = "clarification_question", **metadata: Any) -> PersonalityResponse:
    meta = _public_metadata(metadata)
    clean = _strip_duplicate_address(str(message or "me diga o dado que falta"))
    text = _apply_template(response_type, _lower_first(clean), **metadata)
    result = PersonalityResponse(text=text, response_type=response_type, metadata=meta)
    return result if bool(metadata.get("_defer_personality_log")) else _log_response_applied(result, raw_text=message)


def format_completion(message: object, *, response_type: ResponseType = "manus_task_done", **metadata: Any) -> PersonalityResponse:
    meta = _public_metadata(metadata)
    clean = _strip_duplicate_address(str(message or "tarefa concluida"))
    text = _apply_template(response_type, _lower_first(clean), **metadata)
    result = PersonalityResponse(text=text, response_type=response_type, metadata=meta)
    return result if bool(metadata.get("_defer_personality_log")) else _log_response_applied(result, raw_text=message)


def format_text_only(message: object, *, source: str = "", response_type: ResponseType = "local_command_ack", **metadata: Any) -> PersonalityResponse:
    result = build_personality_response(response_type=response_type, raw_text=message, source=source, **metadata)
    result.should_speak = False
    result.metadata["text_only"] = True
    _log_response_applied(result, raw_text=message)
    return result


def format_tool_result(
    message: object,
    *,
    tool: str = "",
    action: str = "",
    status: str = "",
    source: str = "",
    **metadata: Any,
) -> PersonalityResponse:
    tool_name = str(tool or source or "").strip().lower()
    status_name = str(status or "").strip().lower()
    if tool_name == "whatsapp":
        if status_name in {"failed", "error"}:
            return format_error(message, response_type="generic_error", stage=action or "WhatsApp", tool=tool, source=source, **metadata)
        return build_personality_response(response_type="whatsapp_send_ack", raw_text=message, source=source, **metadata)
    if tool_name in {"manus", "creation_task"}:
        response_type = "manus_task_done" if status_name in {"done", "completed", "completed_saved"} else "manus_task_ack"
        return build_personality_response(response_type=response_type, raw_text=message, source=source, **metadata)
    if tool_name == "browser":
        return build_personality_response(response_type="browser_ack", raw_text=message, source=source, **metadata)
    return build_personality_response(response_type="local_command_ack", raw_text=message, source=source, **metadata)


def format_ui_panel_ack(*, action: str, panel: object, already: bool = False, raw_text: object = "") -> PersonalityResponse:
    message = _panel_message(str(action or ""), panel, already=already)
    if raw_text:
        raw = _clean_text(raw_text)
        if raw and "painel atualizado" not in raw.lower() and "ja esta" not in raw.lower():
            message = raw
    return format_ack(message, response_type="ui_panel_ack", action=action, panel=str(panel or ""), already=already)


def format_volume_ack(*, action: str, value: int | None = None, raw_text: object = "", ok: bool = True) -> PersonalityResponse:
    if not ok:
        return format_error(raw_text or "nao consegui alterar o volume pelo backend atual", response_type="local_command_error", stage="volume")
    clean = _clean_text(raw_text)
    match = re.search(r"(\d{1,3})\s*%", clean)
    percent = int(match.group(1)) if match else value
    if action == "mute":
        message = "mutei o som"
    elif action == "unmute":
        message = "ativei o som"
    elif percent is not None:
        message = f"deixei o volume em {max(0, min(100, int(percent)))}%"
    else:
        message = "ajustei o volume"
    return format_ack(message, response_type="volume_ack", action=action, value=percent)


def format_browser_ack(*, action: str, raw_text: object = "", ok: bool = True) -> PersonalityResponse:
    if not ok:
        return format_error(raw_text or "nao consegui executar o comando do navegador agora", response_type="generic_error", stage="navegador")
    clean = _strip_duplicate_address(str(raw_text or "navegador atualizado"))
    defaults = {
        "search_and_summarize": "vou pesquisar e resumir isso para voce",
        "search_google": "vou pesquisar isso para voce",
        "open_site": "abri o site",
        "open_search_result": "abri esse resultado",
        "click": "cliquei no alvo que encontrei",
        "summarize_page": "li a pagina atual",
        "structured_summary": "li a pagina atual",
        "list_links": "levantei os links principais",
        "start": "iniciei o navegador da Kira",
        "stop": "fechei o navegador da Kira",
        "open_blank": "abri uma pagina em branco",
    }
    message = clean if clean and clean.lower() not in {"navegador atualizado"} else defaults.get(str(action or ""), clean)
    result = PersonalityResponse(text=_sentence(message), response_type="browser_ack", metadata={"action": action})
    return _log_response_applied(result, raw_text=message)


def format_whatsapp_send_ack(*, contact: object, message: object, status: str = "success", raw_text: object = "") -> PersonalityResponse:
    if str(status or "").lower() in {"failed", "error"}:
        return format_error(raw_text or f"nao consegui enviar para {contact}", response_type="generic_error", stage="WhatsApp")
    clean_message = _clean_text(message)
    clean_contact = _clean_text(contact) or "contato"
    text = _apply_template("whatsapp_send_ack", clean_message, contact=clean_contact)
    return _log_response_applied(PersonalityResponse(text=text, response_type="whatsapp_send_ack", source="whatsapp"), raw_text=raw_text or clean_message)


def format_manus_task_ack(*, kind: object = "", destination_mode: str = "", waiting_for_details: bool = False, raw_text: object = "") -> PersonalityResponse:
    label = _clean_text(kind).upper() or "arquivo"
    if waiting_for_details:
        message = f"vou preparar essa criacao de {label} e aguardar os detalhes"
    elif destination_mode == "default":
        message = f"enviei {label} para o Manus e vou salvar na pasta padrao quando terminar"
    else:
        message = f"enviei {label} para o Manus e te aviso quando ficar pronto"
    if raw_text:
        clean = _clean_text(raw_text)
        if clean:
            message = clean
    return format_ack(message, response_type="manus_task_ack", kind=label, destination_mode=destination_mode)


def format_artifact_ack(message: object, *, ok: bool = True, **metadata: Any) -> PersonalityResponse:
    if not ok:
        return format_error(message, response_type="artifact_error", stage="artefato", **metadata)
    return format_ack(message, response_type="artifact_ack", **metadata)


def build_personality_response(
    *,
    response_type: ResponseType,
    raw_text: object = "",
    source: str = "",
    should_speak: bool | None = None,
    **metadata: Any,
) -> PersonalityResponse:
    _ = get_kira_style_rules("local_response")
    normalized_type = str(response_type or "local_command_ack").strip().lower()
    clean = _strip_duplicate_address(str(raw_text or ""))
    if not clean:
        clean = "feito"
    if normalized_type.endswith("_error") or normalized_type == "generic_error":
        result = format_error(clean, response_type=normalized_type, _defer_personality_log=True, **metadata)
    elif normalized_type == "clarification_question":
        result = format_question(clean, _defer_personality_log=True, **metadata)
    elif normalized_type == "manus_task_done":
        result = format_completion(clean, response_type="manus_task_done", _defer_personality_log=True, **metadata)
    elif normalized_type == "browser_ack":
        result = PersonalityResponse(text=_sentence(clean), response_type=normalized_type, source=source, metadata=_public_metadata(metadata))
    elif normalized_type in {"startup_or_ready_message", "normal_chat"}:
        result = PersonalityResponse(text=_sentence(clean), response_type=normalized_type, source=source, metadata=_public_metadata(metadata))
    else:
        result = format_ack(clean, response_type=normalized_type, _defer_personality_log=True, **metadata)
    result.source = source
    result.should_speak = should_speak
    return _log_response_applied(result, raw_text=raw_text)


__all__ = [
    "PersonalityResponse",
    "build_personality_response",
    "format_ack",
    "format_artifact_ack",
    "format_browser_ack",
    "format_completion",
    "format_error",
    "format_manus_task_ack",
    "format_question",
    "format_text_only",
    "format_tool_result",
    "format_ui_panel_ack",
    "format_volume_ack",
    "format_whatsapp_send_ack",
    "get_kira_style_rules",
]
