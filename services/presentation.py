"""Contrato de apresentação Kira -> UI: um objeto validado, atualizado por seção.

Ação "present": a apresentação inteira. Ação "present_patch": só o que mudou (status, seção ativa, seções por id).
Conteúdo externo é dado: texto limitado, links só https públicos, imagens só pelo proxy local.
"""

from __future__ import annotations

import re

from services.ui_actions_hub import _is_plain_object, _safe_string, _safe_url, _validate_id

STATES = ("preparing_query", "researching", "collecting_media", "organizing", "ready", "partial", "error", "cancelled")
SECTION_TYPES = ("text", "image_gallery", "timeline", "video", "chart", "sources", "weather", "agenda", "email", "news", "election", "briefing", "health", "recipe", "instagram")
SECTION_STATES = ("pending", "ready", "unavailable")
MAX_SECTIONS = 12
MAX_SOURCES = 20
MAX_ITEMS = 40
MAX_DEPTH = 6
DATA_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
URL_KEY_RE = re.compile(r"(?:url|href|link|image|thumbnail|poster|src)$", re.IGNORECASE)


class InvalidPresentation(ValueError):
    pass


def _text(value: object, limit: int, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    text = _safe_string(value, limit)
    if text is None or (required and not text.strip()):
        raise InvalidPresentation("texto_invalido")
    return text


def _ident(value: object, *, required: bool = True) -> str | None:
    result = _validate_id(value, required=required)
    if result == "" or (required and result is None):
        raise InvalidPresentation("id_invalido")
    return result


def _data(value: object, key: str = "", depth: int = 0) -> object:
    if depth > MAX_DEPTH:
        raise InvalidPresentation("profundidade")
    if isinstance(value, str):
        if URL_KEY_RE.search(key):
            url = _safe_url(value, key)
            if url is None:
                raise InvalidPresentation("url_invalida")
            return url
        return _text(value, 4000)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
            raise InvalidPresentation("numero_invalido")
        return value
    if isinstance(value, list):
        if len(value) > MAX_ITEMS:
            raise InvalidPresentation("lista_grande")
        return [_data(item, key, depth + 1) for item in value]
    if not _is_plain_object(value):
        raise InvalidPresentation("objeto_invalido")
    result: dict[str, object] = {}
    for child_key, child in value.items():
        if not isinstance(child_key, str) or not DATA_KEY_RE.fullmatch(child_key):
            raise InvalidPresentation("chave_invalida")
        result[child_key] = _data(child, child_key, depth + 1)
    return result


def _section(raw: object, *, partial: bool = False) -> dict[str, object]:
    if not _is_plain_object(raw):
        raise InvalidPresentation("secao_invalida")
    section: dict[str, object] = {"id": _ident(raw.get("id"))}
    if not partial or "type" in raw:
        if raw.get("type") not in SECTION_TYPES:
            raise InvalidPresentation("tipo_de_secao")
        section["type"] = raw["type"]
    if raw.get("heading") is not None:
        section["heading"] = _text(raw.get("heading"), 120)
    if raw.get("status") is not None:
        if raw["status"] not in SECTION_STATES:
            raise InvalidPresentation("estado_de_secao")
        section["status"] = raw["status"]
    if raw.get("reason") is not None:
        section["reason"] = _text(raw.get("reason"), 200)
    if raw.get("data") is not None:
        data = _data(raw["data"])
        if not isinstance(data, dict):
            raise InvalidPresentation("dados_invalidos")
        section["data"] = data
    return section


def _source(raw: object) -> dict[str, object]:
    if not _is_plain_object(raw):
        raise InvalidPresentation("fonte_invalida")
    url = _safe_url(raw.get("url"), "url")
    if url is None:
        raise InvalidPresentation("fonte_sem_url")
    source: dict[str, object] = {"id": _ident(raw.get("id")), "name": _text(raw.get("name"), 120, required=True), "url": url}
    for key in ("accessedAt", "publishedAt"):
        if raw.get(key) is not None:
            source[key] = _text(raw.get(key), 40)
    return source


# "stage": uma seção por vez acompanhando a fala (briefing); "summary": todas reunidas no fim.
PRESENTATION_MODES = frozenset({"stage"})
PRESENTATION_STAGES = frozenset({"summary"})


def _common(raw: dict) -> dict[str, object]:
    result: dict[str, object] = {"presentationId": _ident(raw.get("presentationId")), "requestId": _ident(raw.get("requestId"))}
    if raw.get("turnId") is not None:
        result["turnId"] = _ident(raw.get("turnId"))
    if raw.get("status") is not None:
        if raw["status"] not in STATES:
            raise InvalidPresentation("estado_invalido")
        result["status"] = raw["status"]
    if raw.get("activeSectionId") is not None:
        result["activeSectionId"] = _ident(raw.get("activeSectionId"))
    if raw.get("mode") is not None:
        if raw["mode"] not in PRESENTATION_MODES:
            raise InvalidPresentation("modo_invalido")
        result["mode"] = raw["mode"]
    if raw.get("stage") is not None:
        if raw["stage"] not in PRESENTATION_STAGES:
            raise InvalidPresentation("fase_invalida")
        result["stage"] = raw["stage"]
    return result


def validate_present(raw: object) -> dict[str, object] | None:
    try:
        if not _is_plain_object(raw):
            return None
        result = {"type": "present", **_common(raw)}
        result["title"] = _text(raw.get("title"), 120, required=True)
        result.setdefault("status", "preparing_query")
        sections = raw.get("sections") or []
        sources = raw.get("sources") or []
        if not isinstance(sections, list) or not isinstance(sources, list) or len(sections) > MAX_SECTIONS or len(sources) > MAX_SOURCES:
            return None
        result["sections"] = [_section(s) for s in sections]
        result["sources"] = [_source(s) for s in sources]
        if len({s["id"] for s in result["sections"]}) != len(result["sections"]):
            return None
        return result
    except InvalidPresentation:
        return None


def validate_present_patch(raw: object) -> dict[str, object] | None:
    try:
        if not _is_plain_object(raw):
            return None
        result = {"type": "present_patch", **_common(raw)}
        if raw.get("sections") is not None:
            if not isinstance(raw["sections"], list) or len(raw["sections"]) > MAX_SECTIONS:
                return None
            result["sections"] = [_section(s, partial=True) for s in raw["sections"]]
        if raw.get("sources") is not None:
            if not isinstance(raw["sources"], list) or len(raw["sources"]) > MAX_SOURCES:
                return None
            result["sources"] = [_source(s) for s in raw["sources"]]
        return result
    except InvalidPresentation:
        return None


__all__ = ["SECTION_TYPES", "STATES", "validate_present", "validate_present_patch"]
