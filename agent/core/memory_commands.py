from __future__ import annotations

import logging
import os
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.core.personality_response import build_personality_response
from services.memory_guard import classify_memory_text, is_sensitive, mask_sensitive_preview
from services.memory_store import (
    _connect,
    add_audit_event,
    add_learned_lesson,
    add_memory,
    add_person,
    add_preference,
    add_project_fact,
    add_sensitive_memory,
    init_memory_db,
    search_learned_lessons,
    search_memories,
    search_people,
    search_preferences,
    search_project_facts,
)


logger = logging.getLogger("kira.app")
_PENDING_SENSITIVE_REVEAL: dict[str, dict[str, str]] = {}


def _mem0_primary_enabled() -> bool:
    return str(os.getenv("KIRA_MEMORY_PROVIDER", "mem0") or "mem0").strip().lower() in {"mem0", "auto"}


def _mem0_provider():
    from services.mem0_cloud_provider import Mem0CloudProvider

    return Mem0CloudProvider()


def _mem0_metadata(cmd: MemoryCommand, *, source: str) -> dict[str, Any]:
    return {
        "source": "explicit_memory_command",
        "command_source": source,
        "memory_type": cmd.memory_type,
        "subject": cmd.subject,
        "action": cmd.action,
    }


def _mem0_summary_line(row: dict[str, Any]) -> str:
    return str(row.get("memory") or "").strip()


@dataclass(frozen=True)
class MemoryCommand:
    action: str
    memory_type: str = "generic"
    subject: str = ""
    content: str = ""
    relation: str = ""
    sensitive: bool = False
    raw_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryCommandResult:
    handled: bool
    action: str = ""
    memory_type: str = "generic"
    subject: str = ""
    reply: str = ""
    memory_id: int | None = None
    result_count: int = 0
    sensitive: bool = False
    reason: str = "memory_command"
    metadata: dict[str, Any] = field(default_factory=dict)


_SAVE_PATTERNS = (
    r"\b(?:kira,\s*)?lembra(?:r)?\s+que\s+(.+)",
    r"\b(?:kira,\s*)?lembre\s+que\s+(.+)",
    r"\b(?:kira,\s*)?guarda\s+que\s+(.+)",
    r"\b(?:kira,\s*)?guarda\s+(.+)",
    r"\b(?:kira,\s*)?anota\s+que\s+(.+)",
    r"\b(?:kira,\s*)?grava\s+(?:isso\s+)?(?:que\s+)?(.+)",
    r"\b(?:kira,\s*)?aprendeu\s+que\s+(.+)",
    r"\b(?:kira,\s*)?aprende\s+que\s+(.+)",
    r"\b(?:kira,\s*)?nao\s+esquece\s+que\s+(.+)",
    r"\b(?:kira,\s*)?não\s+esquece\s+que\s+(.+)",
    r"\b(?:kira,\s*)?salva\s+(?:isso\s+)?(?:na\s+memoria\s+)?(?:na\s+memória\s+)?(?:que\s+)?(.+)",
    r"\b(?:kira,\s*)?salva\s+como\s+(?:preferencia|preferência|solucao|solução)\s+(.+)",
    r"\b(?:kira,\s*)?salva\s+(.+)",
    r"\b(?:kira,\s*)?isso\s+funcionou,\s*lembra\s+(.+)",
    r"\b(?:kira,\s*)?essa\s+foi\s+a\s+solucao\s+(.+)",
    r"\b(?:kira,\s*)?essa\s+foi\s+a\s+solução\s+(.+)",
)
_QUERY_PATTERNS = (
    r"\bo\s+que\s+voce\s+lembra\s+sobre\s+(.+)",
    r"\bo\s+que\s+você\s+lembra\s+sobre\s+(.+)",
    r"\bo\s+que\s+voce\s+sabe\s+sobre\s+(.+)",
    r"\bo\s+que\s+você\s+sabe\s+sobre\s+(.+)",
    r"\bo\s+que\s+eu\s+te\s+falei\s+sobre\s+(.+)",
    r"\blembra\s+(?:de|da|do)\s+(.+)",
    r"\bo\s+que\s+voce\s+aprendeu\s+sobre\s+(.+)",
    r"\bo\s+que\s+você\s+aprendeu\s+sobre\s+(.+)",
)
_FORGET_PATTERNS = (
    r"\besquece\s+(?:o\s+que\s+voce\s+lembra\s+sobre\s+|o\s+que\s+você\s+lembra\s+sobre\s+|o\s+que\s+voce\s+sabe\s+sobre\s+|o\s+que\s+você\s+sabe\s+sobre\s+)?(.+)",
    r"\bapaga\s+a\s+memoria\s+sobre\s+(.+)",
    r"\bapaga\s+a\s+memória\s+sobre\s+(.+)",
    r"\bremove\s+(?:essa\s+memoria|essa\s+memória|a\s+memoria\s+sobre|a\s+memória\s+sobre)\s*(.*)",
    r"\bnao\s+lembra\s+mais\s+(?:de|da|do)\s+(.+)",
    r"\bnão\s+lembra\s+mais\s+(?:de|da|do)\s+(.+)",
)
_UPDATE_PATTERNS = (
    r"\batualiza\s*:\s*(.+)",
    r"\batualiza\s+(?:isso\s*)?(.+)",
    r"\bcorrige\s+essa\s+memoria\s+(.+)",
    r"\bcorrige\s+essa\s+memória\s+(.+)",
    r"\bna\s+verdade\s+(.+)",
    r"\btroca\s+essa\s+informacao\s+(.+)",
    r"\btroca\s+essa\s+informação\s+(.+)",
)
_CORRECTION_PATTERNS = (
    r"\bn[aã]o\s+faz\s+assim[,.]?\s*(?:fa[cç]a?|faz|use|usa)\s+(.+)",
    r"\bn[aã]o\s+fa[cç]a?\s+assim[,.]?\s*(?:fa[cç]a?|faz|use|usa)\s+(.+)",
    r"\b(?:fa[cç]a|faz)\s+desse\s+outro\s+jeito[,:]?\s*(.+)",
    r"\b(?:o\s+jeito\s+certo|a\s+forma\s+certa)\s+[eé]\s+(.+)",
)
_PROCEDURE_QUERY_PATTERNS = (
    "como voce aprendeu a fazer isso",
    "como você aprendeu a fazer isso",
    "qual procedimento voce usa",
    "qual procedimento você usa",
    "qual procedimento usa",
    "como voce faz isso",
    "como você faz isso",
    "qual estrategia voce usa",
    "qual estratégia você usa",
)
_REUSE_PROCEDURE_PATTERNS = (
    "faz como da ultima vez",
    "faz como da última vez",
    "use o procedimento anterior",
    "usa o procedimento anterior",
    "faz do jeito que funcionou",
)
_MEMORY_VERBS_RE = re.compile(r"\b(lembra|lembre|guarda|anota|salva|grava|esquece|apaga|remove|atualiza|corrige|aprendeu|aprende|solucao|resolvemos)\b", re.IGNORECASE)
_STOP_SUBJECTS = {"isso", "essa", "esse", "memoria", "memória", "sobre", "que", "voce", "você"}
_REVEAL_CONFIRM_RE = re.compile(r"^\s*pode\s+mostrar\s*[.!?]?\s*$", re.IGNORECASE)
_SENSITIVE_QUERY_RE = re.compile(r"\b(?:voce|você)\s+(?:tem|lembra|sabe).*\b(?:minha|meu)\s+(senha|cpf|token|api key|chave api|endereco|endereço|email|e-mail|login|credencial|pix|cartao|cartão|documento)\b", re.IGNORECASE)
_SENSITIVE_REVEAL_RE = re.compile(r"\b(?:mostra|revela|exibe|me\s+mostra).*\b(?:minha|meu)\s+(senha|cpf|token|api key|chave api|endereco|endereço|email|e-mail|login|credencial|pix|cartao|cartão|documento)\b", re.IGNORECASE)


def _normalize(text: str | None) -> str:
    raw = str(text or "").strip().lower()
    decomposed = unicodedata.normalize("NFD", raw)
    ascii_text = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    ascii_text = re.sub(r"[^\w\s:/.@-]", " ", ascii_text)
    return re.sub(r"\s+", " ", ascii_text).strip()


def _clean_fragment(text: str | None) -> str:
    clean = str(text or "").strip()
    clean = re.sub(r"^[\s:,-]+", "", clean)
    clean = re.sub(r"[?!.]+$", "", clean).strip()
    return re.sub(r"\s+", " ", clean)


def _sensitive_vault_command(raw: str) -> MemoryCommand | None:
    clean = _clean_fragment(raw)
    normalized = _normalize(clean)
    sensitive_terms = (
        "senha",
        "cpf",
        "token",
        "api key",
        "chave api",
        "endereco",
        "email",
        "e mail",
        "login",
        "credencial",
        "pix",
        "cartao",
        "documento",
    )
    has_owner = any(term in normalized for term in ("meu ", "minha ", "meus ", "minhas "))
    has_sensitive_term = any(term in normalized for term in sensitive_terms)
    if has_owner and has_sensitive_term and any(term in normalized for term in ("lembra que", "lembrar que", "guarda ", "salva ", "anota ", "grava ")):
        return None
    if _REVEAL_CONFIRM_RE.match(clean) or normalized == "pode mostrar":
        return MemoryCommand(action="vault_reveal_confirm", memory_type="sensitive", subject="Chefe", content=clean, raw_text=raw, sensitive=True)
    if _SENSITIVE_REVEAL_RE.search(clean) or (has_owner and has_sensitive_term and any(term in normalized for term in ("mostra", "revela", "exibe"))):
        return MemoryCommand(action="vault_reveal_request", memory_type="sensitive", subject="Chefe", content=clean, raw_text=raw, sensitive=True)
    if _SENSITIVE_QUERY_RE.search(clean) or (has_owner and has_sensitive_term and any(term in normalized for term in ("tem", "lembra", "sabe"))):
        return MemoryCommand(action="vault_query", memory_type="sensitive", subject="Chefe", content=clean, raw_text=raw, sensitive=True)
    return None


def _capitalize_name(value: str) -> str:
    words = []
    for word in str(value or "").split():
        if word.lower() in {"da", "de", "do", "das", "dos"}:
            words.append(word.lower())
        else:
            words.append(word[:1].upper() + word[1:])
    return " ".join(words).strip()


def _match_first(patterns: tuple[str, ...], text: str) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return _clean_fragment(match.group(1))
    return ""


def _is_preference_query(normalized: str) -> bool:
    return any(
        phrase in normalized
        for phrase in (
            "minhas preferencias",
            "preferencias de resposta",
            "quais sao minhas preferencias",
            "como eu gosto que voce responda",
            "como eu prefiro que voce responda",
            "meu jeito de resposta",
            "meu estilo de resposta",
        )
    )


def _is_learned_lesson_query(normalized: str) -> bool:
    return any(
        phrase in normalized
        for phrase in (
            "o que voce aprendeu sobre",
            "o que voce lembra sobre essa solucao",
            "o que voce lembra sobre o bug",
            "qual foi a solucao do erro",
            "como resolvemos o erro",
        )
    )


def _is_procedure_query(normalized: str) -> bool:
    return any(phrase in normalized for phrase in _PROCEDURE_QUERY_PATTERNS)


def _is_reuse_procedure(normalized: str) -> bool:
    return any(phrase in normalized for phrase in _REUSE_PROCEDURE_PATTERNS)


def _correction_content(text: str) -> str:
    clean = _clean_fragment(text)
    for pattern in _CORRECTION_PATTERNS:
        match = re.search(pattern, clean, flags=re.IGNORECASE)
        if match:
            return _clean_fragment(match.group(1))
    return clean


def _is_correction(normalized: str) -> bool:
    return bool(
        any(marker in normalized for marker in ("nao faz assim", "nao faca assim", "faz desse outro jeito", "o jeito certo e", "a forma certa e"))
        and any(marker in normalized for marker in ("faz ", "faca ", "faça ", "use ", "usa "))
    )


def _learned_lesson_query_text(text: str) -> str:
    clean = _clean_fragment(text)
    patterns = (
        r"\bo\s+que\s+voc[eê]\s+aprendeu\s+sobre\s+(.+)",
        r"\bo\s+que\s+voce\s+aprendeu\s+sobre\s+(.+)",
        r"\bqual\s+foi\s+a\s+solu[cç][aã]o\s+(?:do|de|da)\s+(.+)",
        r"\bcomo\s+resolvemos\s+(.+)",
        r"\bo\s+que\s+voc[eê]\s+lembra\s+sobre\s+(.+)",
        r"\bo\s+que\s+voce\s+lembra\s+sobre\s+(.+)",
    )
    for pattern in patterns:
        match = re.search(pattern, clean, flags=re.IGNORECASE)
        if match:
            return _clean_fragment(match.group(1))
    return clean


def _extract_subject_from_text(text: str, *, default: str = "") -> str:
    clean = _clean_fragment(text)
    patterns = (
        r"\b(?:minha|meu)\s+(?:amiga|amigo|mae|mãe|pai|irma|irmã|irmao|irmão|cliente|contato)\s+([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÀ-ÿ-]+)",
        r"\bsobre\s+(?:a\s+|o\s+)?([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÀ-ÿ-]+)",
        r"^([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÀ-ÿ-]+)\s+(?:agora|e|é|fala|prefere|gosta|nao|não)",
    )
    for pattern in patterns:
        match = re.search(pattern, clean)
        if match:
            return _capitalize_name(match.group(1))
    normalized = _normalize(clean)
    if "sobre mim" in normalized or "sobre alan" in normalized or "meu perfil" in normalized:
        return "Chefe"
    if "meu projeto" in normalized or normalized == "projeto":
        return "projeto"
    for token in clean.split():
        stripped = token.strip(" ,.:;!?")
        if stripped.lower() in _STOP_SUBJECTS:
            continue
        if stripped[:1].isupper() and len(stripped) > 2:
            return _capitalize_name(stripped)
    return default


def normalize_memory_subject(text: str) -> str:
    subject = _extract_subject_from_text(text)
    if subject:
        return subject
    clean = _clean_fragment(text)
    clean = re.sub(r"^(a|o|as|os|minha|meu)\s+", "", clean, flags=re.IGNORECASE)
    return _capitalize_name(clean.split()[0]) if clean.split() else ""


def route_memory_type(text: str) -> str:
    if is_sensitive(text):
        return "sensitive"
    normalized = _normalize(text)
    if any(term in normalized for term in ("prefiro", "preferencia", "gosto de", "nao gosto", "resposta leiga", "me responde", "quando eu pedir", "quando eu falar", "sempre use", "sempre usa")):
        return "preference"
    if any(term in normalized for term in ("erro", "bug", "solucao", "funcionou", "causa", "resolvido", "corrigido")):
        return "learned_lesson"
    if any(term in normalized for term in ("projeto", "kira", "agent/", "livekit", "gemini", "rota", "arquivo", "modulo", "sqlite")):
        return "project"
    if any(term in normalized for term in ("minha amiga", "meu amigo", "amiga", "amigo", "cliente", "contato")):
        return "person"
    classification = classify_memory_text(text)
    if classification.memory_type in {"alan_profile", "person", "project", "preference", "learned_lesson", "sensitive", "temporary"}:
        return classification.memory_type
    if _extract_subject_from_text(text):
        return "person"
    return "generic"


def _relation_from_text(text: str) -> str:
    normalized = _normalize(text)
    if "minha amiga" in normalized:
        return "amiga do Chefe"
    if "meu amigo" in normalized:
        return "amigo do Chefe"
    if "cliente" in normalized:
        return "cliente"
    if "contato" in normalized:
        return "contato"
    return ""


def detect_memory_command(text: str) -> MemoryCommand | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    normalized = _normalize(raw)
    if re.match(r"^(e )?o que (voce|vc) (sabe|lembra) (sobre|de) (mim|eu)\b", normalized):
        # sobre o próprio Chefe: o Gemini responde com o núcleo de memória (cerebro 'o_que_sabe'); aqui virava
        # "nao tenho nada salvo sobre a Mim"
        return None
    vault_cmd = _sensitive_vault_command(raw)
    if vault_cmd is not None:
        return vault_cmd
    if _is_procedure_query(normalized):
        return MemoryCommand(action="procedure_query", memory_type="procedure", content=raw, raw_text=raw)
    if _is_reuse_procedure(normalized):
        return MemoryCommand(action="reuse_procedure", memory_type="procedure", content=raw, raw_text=raw)
    if _is_correction(normalized):
        correction = _correction_content(raw)
        return MemoryCommand(
            action="correct_procedure",
            memory_type="procedure",
            content=correction or raw,
            raw_text=raw,
            metadata={"correction": correction or raw},
        )
    if normalized in {"esquece essa preferencia", "apaga essa preferencia"}:
        return MemoryCommand(action="forget_preference", memory_type="preference", subject="Chefe", content="", raw_text=raw)
    if (
        not _MEMORY_VERBS_RE.search(normalized)
        and not normalized.startswith("o que voce sabe sobre")
        and not _is_preference_query(normalized)
        and not _is_learned_lesson_query(normalized)
    ):
        return None

    forget_content = _match_first(_FORGET_PATTERNS, raw)
    if forget_content:
        subject = normalize_memory_subject(forget_content)
        return MemoryCommand(action="forget", subject=subject, content=forget_content, raw_text=raw)

    update_content = _match_first(_UPDATE_PATTERNS, raw)
    if update_content:
        subject = normalize_memory_subject(update_content)
        memory_type = route_memory_type(update_content)
        return MemoryCommand(action="update", memory_type=memory_type, subject=subject, content=update_content, sensitive=is_sensitive(update_content), raw_text=raw)

    if _is_preference_query(normalized):
        return MemoryCommand(action="query", memory_type="preference", subject="Chefe", content="preferencias de resposta", raw_text=raw)

    if _is_learned_lesson_query(normalized):
        content = _learned_lesson_query_text(raw)
        return MemoryCommand(action="query", memory_type="learned_lesson", subject=_lesson_domain(content), content=content, raw_text=raw)

    query_content = _match_first(_QUERY_PATTERNS, raw)
    if query_content:
        subject = normalize_memory_subject(query_content)
        memory_type = route_memory_type(query_content)
        return MemoryCommand(action="query", memory_type=memory_type, subject=subject, content=query_content, raw_text=raw)

    if normalized in {"esquece isso", "remove essa memoria", "remove essa memória"}:
        return MemoryCommand(action="forget", subject="", content="", raw_text=raw)

    save_content = _match_first(_SAVE_PATTERNS, raw)
    if save_content:
        memory_type = route_memory_type(save_content)
        subject = "Chefe" if memory_type == "preference" else normalize_memory_subject(save_content)
        if memory_type == "learned_lesson" and not subject:
            subject = "aprendizado"
        if memory_type == "project" and not subject:
            subject = "projeto"
        return MemoryCommand(
            action="save",
            memory_type=memory_type,
            subject=subject,
            content=save_content,
            relation=_relation_from_text(save_content),
            sensitive=is_sensitive(save_content),
            raw_text=raw,
        )
    return None


def parse_memory_save(text: str) -> MemoryCommand | None:
    cmd = detect_memory_command(text)
    return cmd if cmd and cmd.action == "save" else None


def parse_memory_query(text: str) -> MemoryCommand | None:
    cmd = detect_memory_command(text)
    return cmd if cmd and cmd.action == "query" else None


def parse_memory_forget(text: str) -> MemoryCommand | None:
    cmd = detect_memory_command(text)
    return cmd if cmd and cmd.action == "forget" else None


def parse_memory_update(text: str) -> MemoryCommand | None:
    cmd = detect_memory_command(text)
    return cmd if cmd and cmd.action == "update" else None


def _person_content(cmd: MemoryCommand) -> str:
    subject = cmd.subject or "essa pessoa"
    content = cmd.content
    content = re.sub(r"\bminha amiga\s+", "", content, flags=re.IGNORECASE)
    content = re.sub(r"\bmeu amigo\s+", "", content, flags=re.IGNORECASE)
    content = re.sub(rf"^{re.escape(subject)}\s+", "", content, flags=re.IGNORECASE)
    return _clean_fragment(content) or cmd.content


def _learned_lesson_parts(content: str) -> dict[str, str]:
    clean = _clean_fragment(content)
    lower = _normalize(clean)
    error = clean
    solution = clean
    cause = ""
    if " foi resolvido " in lower:
        parts = re.split(r"\bfoi\s+resolvido\s+(?:colocando|com|por|ao)?\s*", clean, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            error = parts[0].strip()
            solution = parts[1].strip()
    elif " solucao " in lower or " solução " in lower:
        parts = re.split(r"\b(?:solucao|solução)\b", clean, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            error = parts[0].strip()
            solution = parts[1].strip()
    return {"domain": _lesson_domain(clean), "error": error, "cause": cause, "solution": solution or clean}


def _lesson_domain(content: str) -> str:
    normalized = _normalize(content)
    if "whatsapp" in normalized and "browser" in normalized:
        return "WhatsApp/browser routing"
    if "whatsapp" in normalized:
        return "WhatsApp"
    if "browser" in normalized:
        return "browser"
    if "memoria" in normalized or "memory" in normalized:
        return "memory"
    return "general"


def _ack(text: str, *, source: str = "memory", response_type: str = "local_command_ack") -> str:
    return build_personality_response(
        response_type=response_type,
        raw_text=text,
        source=source,
        should_speak=False,
        text_only=True,
    ).text


def _summary_line(row: dict[str, Any]) -> str:
    table = str(row.get("_table") or "")
    if table == "people":
        name = str(row.get("name") or "").strip()
        relation = str(row.get("relation_to_alan") or "").strip()
        notes = str(row.get("notes") or "").strip()
        pieces = []
        if relation:
            pieces.append(f"{name} é {relation}")
        if notes:
            pieces.append(notes)
        return " e ".join(pieces) if pieces else name
    if table == "preferences":
        return str(row.get("preference") or "").strip()
    if table == "learned_lessons":
        error = str(row.get("error") or "").strip()
        solution = str(row.get("solution") or "").strip()
        if error and solution and error != solution:
            return f"{error}: {solution}"
        return solution or error
    if table == "project_knowledge":
        return str(row.get("fact") or "").strip()
    return str(row.get("content") or row.get("masked_preview") or "").strip()


def _collect_query_results(subject: str, query: str, *, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    needle = subject or query
    results: list[dict[str, Any]] = []
    for row in search_people(needle, db_path=db_path, limit=5):
        row["_table"] = "people"
        results.append(row)
    for row in search_memories(needle, subject=subject or None, db_path=db_path, limit=8):
        row["_table"] = "memories"
        results.append(row)
    for row in search_preferences(needle, subject=subject if subject == "Chefe" else None, db_path=db_path, limit=5):
        row["_table"] = "preferences"
        results.append(row)
    for row in search_project_facts(needle, db_path=db_path, limit=5):
        row["_table"] = "project_knowledge"
        results.append(row)
    for row in search_learned_lessons(needle, db_path=db_path, limit=5):
        row["_table"] = "learned_lessons"
        results.append(row)
    return results


def _collect_preference_results(*, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for row in search_preferences(None, subject="Chefe", db_path=db_path, limit=8):
        row["_table"] = "preferences"
        results.append(row)
    for row in search_memories(None, memory_type="preference", subject="Chefe", db_path=db_path, limit=8):
        row["_table"] = "memories"
        results.append(row)
    return results


def _collect_learned_lesson_results(query: str, subject: str, *, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    def _add(rows: list[dict[str, Any]], table: str) -> None:
        for row in rows:
            key = (table, int(row.get("id") or 0))
            if key in seen:
                continue
            seen.add(key)
            row["_table"] = table
            results.append(row)

    if subject:
        _add(search_learned_lessons(domain=subject, db_path=db_path, limit=8), "learned_lessons")
        _add(search_memories(subject=subject, memory_type="learned_lesson", db_path=db_path, limit=8), "memories")
    if query:
        _add(search_learned_lessons(query, db_path=db_path, limit=8), "learned_lessons")
        _add(search_memories(query, memory_type="learned_lesson", db_path=db_path, limit=8), "memories")
    normalized = _normalize(f"{subject} {query}")
    for keyword in ("WhatsApp", "browser", "erro", "solucao"):
        if keyword.lower() in normalized:
            _add(search_learned_lessons(keyword, db_path=db_path, limit=8), "learned_lessons")
            _add(search_memories(keyword, memory_type="learned_lesson", db_path=db_path, limit=8), "memories")
    return results


def _handle_save(cmd: MemoryCommand, *, source: str, db_path: str | Path | None = None) -> MemoryCommandResult:
    _mirror_save_to_local_priority(cmd, source=source)
    # Preferências orientadas a contexto precisam sobreviver mesmo quando o
    # provider Mem0 está ativo ou indisponível. O espelho local é a fonte de
    # verdade para precedência, correção e esquecimento.
    if cmd.memory_type == "preference":
        try:
            from services.experience_memory import ExperienceMemory

            saved = ExperienceMemory(db_path=db_path).remember_preference(
                cmd.content,
                source=f"explicit:{source}",
                explicit=True,
                confidence=1.0,
            )
        except ValueError as exc:
            return MemoryCommandResult(True, "save", "preference", "Chefe", _ack(f"Não salvei essa preferência: {exc}.", source="memory", response_type="generic_error"))
        logger.info("STRUCTURED_PREFERENCE_SAVED id=%s scope=%s", saved.get("id"), saved.get("scope"))
        reply = _ack("Pronto, Chefe. Vou aplicar essa preferência quando o contexto combinar.", source="memory")
        return MemoryCommandResult(True, "save", "preference", "Chefe", reply, int(saved.get("id") or 0), metadata={"structured": True, "scope": saved.get("scope"), "context": saved.get("context")})
    if _mem0_primary_enabled():
        if cmd.sensitive:
            from services.sensitive_vault import SensitiveVault, classify_sensitive_text, third_party_sensitive_mem0_allowed

            classification = classify_sensitive_text(cmd.content)
            if classification.sensitive and classification.owner == "alan":
                logger.info("SENSITIVE_VAULT_ROUTE owner=alan category=%s action=local_vault", classification.category)
                logger.info("MEM0_SAVE_BLOCKED_PERSONAL_SENSITIVE reason=alan_sensitive")
                try:
                    from services.sensitive_vault import _extract_value

                    raw_value = _extract_value(cmd.content)
                except Exception:
                    raw_value = cmd.content
                saved = SensitiveVault().save(
                    raw_value=raw_value,
                    category=classification.category,
                    label=classification.label,
                    context=cmd.content,
                    source=source,
                )
                return MemoryCommandResult(
                    True,
                    "save",
                    "sensitive",
                    classification.label,
                    "Pronto, guardei isso no cofre local.",
                    sensitive=True,
                    metadata={"sensitive_vault": True, "item_id": saved.get("item_id")},
                )
            if classification.sensitive and classification.owner == "third_party":
                if third_party_sensitive_mem0_allowed():
                    logger.info("SENSITIVE_THIRD_PARTY_POLICY action=allow_mem0")
                else:
                    logger.info("SENSITIVE_THIRD_PARTY_POLICY action=blocked_or_confirm_required")
                    reply = "Esse dado parece sensivel e de outra pessoa. Nao vou salvar sem confirmacao."
                    return MemoryCommandResult(True, "save", "sensitive", cmd.subject, reply, sensitive=True, metadata={"third_party_blocked": True})
        logger.info("MEM0_EXPLICIT_SAVE_BEGIN type=%s subject=%s", cmd.memory_type, cmd.subject or "")
        provider = _mem0_provider()
        try:
            result = provider.add_messages(
                [{"role": "user", "content": cmd.content}],
                user_id=provider.user_id,
                metadata=_mem0_metadata(cmd, source=source),
            )
        finally:
            provider.close()
        if bool(result.get("blocked_sensitive")):
            logger.info("MEM0_SAVE_BLOCKED_SENSITIVE source=explicit_memory_command subject=%s", cmd.subject or "")
            reply = _ack("Isso parece sensivel, Chefe. Nao vou mandar para a memoria em nuvem.", source="memory")
            return MemoryCommandResult(True, "save", cmd.memory_type, cmd.subject, reply, sensitive=True, metadata={"blocked_sensitive": True})
        if not bool(result.get("ok")):
            logger.warning("MEM0_EXPLICIT_SAVE_FAILED error_type=%s", result.get("error_type") or "unknown")
            reply = _ack("Nao consegui salvar essa memoria no Mem0 agora, chefe.", source="memory", response_type="generic_error")
            return MemoryCommandResult(True, "save", cmd.memory_type, cmd.subject, reply, metadata={"mem0_error": result})
        if bool(result.get("pending")):
            logger.info("MEM0_EXPLICIT_SAVE_PENDING event_id=%s", result.get("event_id") or "")
        else:
            logger.info("MEM0_EXPLICIT_SAVE_OK status=%s", result.get("status") or "ok")
        reply = _ack("Beleza, vou guardar isso.", source="memory")
        return MemoryCommandResult(True, "save", cmd.memory_type, cmd.subject, reply, metadata={"mem0": True, "event_id": result.get("event_id")})

    if cmd.sensitive:
        memory_id = add_sensitive_memory(
            subject=cmd.subject or "sensivel",
            value=cmd.content,
            category=cmd.memory_type,
            source=source,
            db_path=db_path,
        )
        logger.info("MEMORY_COMMAND_SAVED type=sensitive subject=%s masked=true id=%s", cmd.subject or "", memory_id)
        reply = _ack("Fechado, chefe. Salvei isso como memoria sensivel local.", source="memory")
        return MemoryCommandResult(True, "save", "sensitive", cmd.subject, reply, memory_id, sensitive=True)

    if cmd.memory_type == "person":
        person_id = add_person(
            name=cmd.subject or "Pessoa",
            relation_to_alan=cmd.relation or None,
            notes=_person_content(cmd),
            db_path=db_path,
        )
        memory_id = add_memory(
            memory_type="person",
            category="person",
            subject=cmd.subject or "Pessoa",
            content=cmd.content,
            source=source,
            db_path=db_path,
        )
        logger.info("MEMORY_COMMAND_SAVED id=%s type=person subject=%s person_id=%s", memory_id, cmd.subject, person_id)
        reply = _ack(f"Pronto, Chefe. Vou lembrar disso sobre {cmd.subject or 'essa pessoa'}.", source="memory")
        return MemoryCommandResult(True, "save", "person", cmd.subject, reply, memory_id)

    if cmd.memory_type == "preference":
        memory_id = add_preference(subject="Chefe", preference=cmd.content, scope="geral", db_path=db_path)
        add_memory(memory_type="preference", category="preference", subject="Chefe", content=cmd.content, source=source, db_path=db_path)
        logger.info("MEMORY_COMMAND_SAVED id=%s type=preference subject=Chefe", memory_id)
        reply = _ack("Pronto, Chefe. Vou lembrar essa preferencia.", source="memory")
        return MemoryCommandResult(True, "save", "preference", "Chefe", reply, memory_id)

    if cmd.memory_type == "learned_lesson":
        parts = _learned_lesson_parts(cmd.content)
        memory_id = add_learned_lesson(
            domain=parts["domain"],
            error=parts["error"],
            cause=parts["cause"],
            solution=parts["solution"],
            validation="salvo por comando textual",
            result="pendente de uso futuro",
            db_path=db_path,
        )
        add_memory(memory_type="learned_lesson", category="learned_lesson", subject=parts["domain"], content=cmd.content, source=source, db_path=db_path)
        logger.info("MEMORY_COMMAND_SAVED id=%s type=learned_lesson subject=%s", memory_id, parts["domain"])
        reply = _ack("Fechado, chefe. Salvei isso como solucao aprendida.", source="memory")
        return MemoryCommandResult(True, "save", "learned_lesson", parts["domain"], reply, memory_id)

    if cmd.memory_type == "project":
        memory_id = add_project_fact(module=cmd.subject or "projeto", fact=cmd.content, status="active", db_path=db_path)
        add_memory(memory_type="project", category="project", subject=cmd.subject or "projeto", content=cmd.content, source=source, db_path=db_path)
        logger.info("MEMORY_COMMAND_SAVED id=%s type=project subject=%s", memory_id, cmd.subject or "projeto")
        reply = _ack("Fechado, chefe. Salvei isso como memoria do projeto.", source="memory")
        return MemoryCommandResult(True, "save", "project", cmd.subject or "projeto", reply, memory_id)

    memory_id = add_memory(
        memory_type=cmd.memory_type if cmd.memory_type != "generic" else "generic",
        category=cmd.memory_type if cmd.memory_type != "generic" else "generic",
        subject=cmd.subject or None,
        content=cmd.content,
        source=source,
        db_path=db_path,
    )
    logger.info("MEMORY_COMMAND_SAVED id=%s type=%s subject=%s", memory_id, cmd.memory_type, cmd.subject)
    reply = _ack("Fechado, chefe. Vou lembrar disso.", source="memory")
    return MemoryCommandResult(True, "save", cmd.memory_type, cmd.subject, reply, memory_id)


def _mirror_save_to_local_priority(cmd: MemoryCommand, *, source: str) -> None:
    """Mantém fatos explícitos no índice local, sem duplicar dados sensíveis."""
    if bool(getattr(cmd, "sensitive", False)):
        return
    try:
        from services.local_memory_priority import get_local_priority_memory

        store = get_local_priority_memory()
        memory_type = str(getattr(cmd, "memory_type", "general") or "general")
        subject = str(getattr(cmd, "subject", "") or "Chefe").strip() or "Chefe"
        content = str(getattr(cmd, "content", "") or "").strip()
        if not content:
            return
        if memory_type == "person":
            store.remember_person(
                name=subject,
                details={"relation": getattr(cmd, "relation", "") or "", "notes": content, "source": source},
            )
        elif memory_type == "preference" and subject.casefold() in {"alan", "user", "usuario"}:
            store.remember_user_profile(key="preference", value=content, source=source)
        else:
            store.remember_fact(category=memory_type, subject=subject, predicate="observacao", object_value=content, source=source)
        logger.info("LOCAL_PRIORITY_EXPLICIT_MEMORY_SAVED type=%s subject=%s", memory_type, subject)
    except Exception as exc:
        logger.warning("LOCAL_PRIORITY_EXPLICIT_MEMORY_FAILED error_type=%s", exc.__class__.__name__)


def _vault_category_from_text(text: str) -> str:
    try:
        from services.sensitive_vault import classify_sensitive_text

        classification = classify_sensitive_text(text)
        if classification.category != "normal":
            return classification.category
    except Exception:
        pass
    return "secret"


def _handle_sensitive_vault_query(cmd: MemoryCommand, *, reveal: bool) -> MemoryCommandResult:
    from services.sensitive_vault import SensitiveVault, reveal_requires_confirmation

    category = _vault_category_from_text(cmd.content)
    vault = SensitiveVault()
    rows = vault.find(query=cmd.content, category=category if category != "secret" else None, limit=3)
    if not rows and category:
        rows = vault.find(query=category, limit=3)
    if not rows:
        return MemoryCommandResult(
            True,
            "query",
            "sensitive",
            "Chefe",
            "Nao encontrei esse dado no cofre local.",
            result_count=0,
            sensitive=True,
        )
    row = rows[0]
    label = str(row.get("label") or category or "dado sensivel")
    masked = str(row.get("masked_preview") or "")
    if reveal:
        if reveal_requires_confirmation():
            _PENDING_SENSITIVE_REVEAL["default"] = {"item_id": str(row.get("id") or ""), "label": label}
            logger.info("SENSITIVE_VAULT_REVEAL_CONFIRM_REQUIRED")
            return MemoryCommandResult(
                True,
                "query",
                "sensitive",
                label,
                'Esse e um dado sensivel. Quer revelar agora? Responda: "pode mostrar".',
                result_count=1,
                sensitive=True,
                metadata={"confirm_required": True},
            )
        revealed = vault.reveal(item_id=str(row.get("id") or ""))
        return MemoryCommandResult(True, "query", "sensitive", label, str(revealed.get("value") or ""), result_count=1, sensitive=True, metadata={"revealed": True})
    reply = f'Tenho sim, esta salvo no cofre local como "{label}" ({masked}). Por seguranca, nao vou mostrar inteiro sem confirmacao.'
    return MemoryCommandResult(True, "query", "sensitive", label, reply, result_count=len(rows), sensitive=True)


def _handle_sensitive_vault_reveal_confirm(cmd: MemoryCommand) -> MemoryCommandResult:
    pending = _PENDING_SENSITIVE_REVEAL.pop("default", None)
    if not pending:
        return MemoryCommandResult(
            True,
            "query",
            "sensitive",
            "Chefe",
            "Nao tenho uma revelacao sensivel pendente agora.",
            result_count=0,
            sensitive=True,
        )
    from services.sensitive_vault import SensitiveVault

    vault = SensitiveVault()
    revealed = vault.reveal(item_id=str(pending.get("item_id") or ""))
    if not bool(revealed.get("ok")):
        return MemoryCommandResult(
            True,
            "query",
            "sensitive",
            str(pending.get("label") or ""),
            "Nao consegui revelar esse dado agora.",
            result_count=0,
            sensitive=True,
        )
    return MemoryCommandResult(
        True,
        "query",
        "sensitive",
        str(revealed.get("label") or pending.get("label") or ""),
        str(revealed.get("value") or ""),
        result_count=1,
        sensitive=True,
        metadata={"revealed": True},
    )


def _handle_query(cmd: MemoryCommand, *, db_path: str | Path | None = None) -> MemoryCommandResult:
    if cmd.action == "vault_query":
        return _handle_sensitive_vault_query(cmd, reveal=False)
    if cmd.action == "vault_reveal_request":
        return _handle_sensitive_vault_query(cmd, reveal=True)
    if cmd.action == "vault_reveal_confirm":
        return _handle_sensitive_vault_reveal_confirm(cmd)
    if _mem0_primary_enabled():
        logger.info("MEM0_EXPLICIT_QUERY_BEGIN type=%s subject=%s", cmd.memory_type, cmd.subject or cmd.content)
        provider = _mem0_provider()
        try:
            query = " ".join(part for part in (cmd.subject, cmd.content) if part).strip() or cmd.raw_text
            results = provider.search(query, user_id=provider.user_id, top_k=5)
        finally:
            provider.close()
        logger.info("MEM0_EXPLICIT_QUERY_RESULTS count=%s", len(results))
        if not results:
            subject_text = f" sobre {cmd.subject}" if cmd.subject else ""
            return MemoryCommandResult(
                True,
                "query",
                cmd.memory_type,
                cmd.subject,
                _ack(f"Nao achei memoria no Mem0{subject_text} ainda, chefe.", source="memory"),
                result_count=0,
            )
        lines = []
        seen = set()
        for row in results:
            line = _mem0_summary_line(row)
            key = line.lower()
            if line and key not in seen:
                seen.add(key)
                lines.append(line)
            if len(lines) >= 3:
                break
        joined = "; ".join(lines)
        return MemoryCommandResult(
            True,
            "query",
            cmd.memory_type,
            cmd.subject,
            _ack(f"Lembro sim, chefe: {joined}", source="memory"),
            result_count=len(results),
            metadata={"mem0": True},
        )

    if cmd.memory_type == "preference":
        results = _collect_preference_results(db_path=db_path)
    elif cmd.memory_type == "learned_lesson":
        results = _collect_learned_lesson_results(cmd.content, cmd.subject, db_path=db_path)
    else:
        results = _collect_query_results(cmd.subject, cmd.content, db_path=db_path)
    logger.info("MEMORY_COMMAND_QUERY subject=%s results=%s", cmd.subject or cmd.content, len(results))
    if not results:
        if cmd.memory_type == "preference":
            return MemoryCommandResult(
                True,
                "query",
                cmd.memory_type,
                "Chefe",
                _ack("Nao achei preferencia de resposta salva ainda, chefe.", source="memory"),
                result_count=0,
            )
        if cmd.memory_type == "learned_lesson":
            return MemoryCommandResult(
                True,
                "query",
                cmd.memory_type,
                cmd.subject,
                _ack("Nao achei solucao aprendida sobre esse erro ainda, chefe.", source="memory"),
                result_count=0,
            )
        subject_text = f" sobre {cmd.subject}" if cmd.subject else ""
        return MemoryCommandResult(
            True,
            "query",
            cmd.memory_type,
            cmd.subject,
            _ack(f"Nao achei nada salvo{subject_text} ainda, chefe.", source="memory"),
            result_count=0,
        )
    lines = []
    seen = set()
    for row in results:
        line = _summary_line(row)
        key = line.lower()
        if line and key not in seen:
            seen.add(key)
            lines.append(line)
        if len(lines) >= 3:
            break
    joined = "; ".join(lines)
    if cmd.memory_type == "learned_lesson":
        return MemoryCommandResult(
            True,
            "query",
            cmd.memory_type,
            cmd.subject,
            _ack(f"Aprendi isto, chefe: {joined}", source="memory"),
            result_count=len(results),
        )
    if cmd.memory_type == "preference":
        return MemoryCommandResult(
            True,
            "query",
            cmd.memory_type,
            "Chefe",
            _ack(f"Lembro que voce prefere: {joined}", source="memory"),
            result_count=len(results),
        )
    return MemoryCommandResult(
        True,
        "query",
        cmd.memory_type,
        cmd.subject,
        _ack(f"Lembro sim, chefe: {joined}", source="memory"),
        result_count=len(results),
    )


def _deactivate_rows(subject: str, *, source: str, db_path: str | Path | None = None) -> int:
    init_memory_db(db_path)
    if not subject:
        return 0
    like = f"%{subject}%"
    count = 0
    specs = (
        ("memories", "subject LIKE ? OR content LIKE ? OR tags LIKE ?", (like, like, like)),
        ("people", "name LIKE ? OR aliases LIKE ? OR notes LIKE ?", (like, like, like)),
        ("preferences", "subject LIKE ? OR preference LIKE ? OR scope LIKE ?", (like, like, like)),
        ("project_knowledge", "module LIKE ? OR fact LIKE ? OR file_path LIKE ?", (like, like, like)),
        ("sensitive_memories", "subject LIKE ? OR category LIKE ? OR masked_preview LIKE ?", (like, like, like)),
    )
    audit_rows: list[tuple[str, int]] = []
    with _connect(db_path) as conn:
        for table, where, params in specs:
            rows = conn.execute(f"SELECT id FROM {table} WHERE active = 1 AND ({where})", params).fetchall()
            ids = [int(row["id"]) for row in rows]
            if not ids:
                continue
            placeholders = ",".join("?" for _ in ids)
            conn.execute(f"UPDATE {table} SET active = 0 WHERE id IN ({placeholders})", ids)
            for row_id in ids:
                audit_rows.append((table, row_id))
            count += len(ids)
    for table, row_id in audit_rows:
        add_audit_event(
            action="deactivate",
            table_name=table,
            memory_id=row_id,
            summary=f"subject={subject}",
            source=source,
            db_path=db_path,
        )
    return count


def _handle_forget(cmd: MemoryCommand, *, source: str, db_path: str | Path | None = None) -> MemoryCommandResult:
    if _mem0_primary_enabled():
        logger.info("MEM0_EXPLICIT_FORGET_BEGIN subject=%s", cmd.subject or cmd.content)
        provider = _mem0_provider()
        deleted = 0
        try:
            query = cmd.subject or cmd.content or cmd.raw_text
            results = provider.search(query, user_id=provider.user_id, top_k=8)
            for row in results:
                memory_id = str(row.get("id") or "").strip()
                if not memory_id:
                    continue
                delete_result = provider.delete_memory(memory_id)
                if bool(delete_result.get("ok")):
                    deleted += 1
        finally:
            provider.close()
        logger.info("MEM0_EXPLICIT_FORGET_RESULT subject=%s count=%s", cmd.subject or cmd.content, deleted)
        reply = _ack(
            f"Pronto, chefe. Removi {deleted} memoria(s) do Mem0 sobre {cmd.subject or 'isso'}." if deleted else f"Nao encontrei memoria no Mem0 sobre {cmd.subject or 'isso'}.",
            source="memory",
        )
        return MemoryCommandResult(True, "forget", "generic", cmd.subject, reply, result_count=deleted, metadata={"mem0": True})

    count = _deactivate_rows(cmd.subject, source=source, db_path=db_path)
    logger.info("MEMORY_COMMAND_FORGOT subject=%s count=%s", cmd.subject, count)
    if count <= 0:
        reply = _ack(f"Nao encontrei memoria ativa sobre {cmd.subject or 'isso'}.", source="memory")
    else:
        reply = _ack(f"Pronto, chefe. Desativei as memorias sobre {cmd.subject or 'isso'}.", source="memory")
    return MemoryCommandResult(True, "forget", "generic", cmd.subject, reply, result_count=count)


def _handle_update(cmd: MemoryCommand, *, source: str, db_path: str | Path | None = None) -> MemoryCommandResult:
    if _mem0_primary_enabled():
        logger.info("MEM0_EXPLICIT_UPDATE_BEGIN subject=%s", cmd.subject or "")
        save_cmd = MemoryCommand(
            action="save",
            memory_type=cmd.memory_type,
            subject=cmd.subject,
            content=cmd.content,
            relation=_relation_from_text(cmd.content),
            sensitive=cmd.sensitive,
            raw_text=cmd.raw_text,
        )
        return _handle_save(save_cmd, source=source, db_path=db_path)

    save_cmd = MemoryCommand(
        action="save",
        memory_type=cmd.memory_type,
        subject=cmd.subject,
        content=cmd.content,
        relation=_relation_from_text(cmd.content),
        sensitive=cmd.sensitive,
        raw_text=cmd.raw_text,
    )
    result = _handle_save(save_cmd, source=source, db_path=db_path)
    add_audit_event(
        action="update_command",
        table_name="memories",
        memory_id=result.memory_id,
        summary=mask_sensitive_preview(cmd.content),
        source=source,
        db_path=db_path,
    )
    logger.info("MEMORY_COMMAND_UPDATED subject=%s id=%s", cmd.subject, result.memory_id)
    reply = _ack(f"Atualizado, chefe. Vou lembrar que {cmd.content}.", source="memory")
    return MemoryCommandResult(True, "update", cmd.memory_type, cmd.subject, reply, result.memory_id, sensitive=cmd.sensitive)


def _procedure_reply(details: dict[str, Any], *, reuse: bool = False) -> str:
    best = details.get("best") or {}
    if not best:
        return "Ainda não tenho um procedimento comprovado para isso. Posso registrar a próxima tentativa quando o resultado for confirmado."
    steps = [str(step).strip() for step in best.get("steps") or [] if str(step).strip()]
    description = "; ".join(steps[:5]) or "a estratégia registrada"
    successes = int(best.get("success_count") or 0)
    failures = int(best.get("failure_count") or 0)
    prefix = "Vou priorizar" if reuse else "Aprendi"
    return f"{prefix} este procedimento para {best.get('goal')}: {description}. Ele foi confirmado {successes} vez(es), teve {failures} falha(s) e está com confiança {float(best.get('confidence') or 0):.2f}."


def _handle_procedure_command(cmd: MemoryCommand, *, reuse: bool = False, db_path: str | Path | None = None) -> MemoryCommandResult:
    try:
        from services.experience_memory import ExperienceMemory, get_experience_memory

        store = ExperienceMemory(db_path=db_path) if db_path is not None else get_experience_memory()
        details = store.explain("")
    except Exception as exc:
        logger.warning("PROCEDURE_MEMORY_QUERY_FAILED error_type=%s", exc.__class__.__name__)
        return MemoryCommandResult(True, "procedure_query", "procedure", "", "Ainda não consegui consultar os procedimentos aprendidos.")
    action = "reuse_procedure" if reuse else "procedure_query"
    return MemoryCommandResult(True, action, "procedure", "", _ack(_procedure_reply(details, reuse=reuse), source="memory"), result_count=len(details.get("procedures") or []), metadata=details)


def _handle_correction_command(cmd: MemoryCommand, *, source: str, db_path: str | Path | None = None) -> MemoryCommandResult:
    try:
        from services.experience_memory import ExperienceMemory, get_experience_memory

        store = ExperienceMemory(db_path=db_path) if db_path is not None else get_experience_memory()
        result = store.record_correction(
            correction=cmd.content,
            source="voice_correction" if source == "speech" else "text_correction",
        )
    except Exception as exc:
        logger.warning("PROCEDURE_CORRECTION_FAILED error_type=%s", exc.__class__.__name__)
        return MemoryCommandResult(True, "correct_procedure", "procedure", "", "Não consegui registrar essa correção agora.")
    if not result.get("ok"):
        return MemoryCommandResult(True, "correct_procedure", "procedure", "", "Não salvei essa correção porque ela parece conter dado sensível.")
    return MemoryCommandResult(
        True,
        "correct_procedure",
        "procedure",
        "",
        _ack("Entendi. Atualizei o procedimento e vou priorizar esse jeito; ainda vou confirmar o resultado numa próxima execução.", source="memory"),
        result_count=1,
        metadata=result,
    )


def _handle_forget_preference(*, source: str, db_path: str | Path | None = None) -> MemoryCommandResult:
    try:
        from services.experience_memory import ExperienceMemory, get_experience_memory

        store = ExperienceMemory(db_path=db_path) if db_path is not None else get_experience_memory()
        count = store.forget(kind="preference")
    except Exception as exc:
        logger.warning("STRUCTURED_PREFERENCE_FORGET_FAILED error_type=%s", exc.__class__.__name__)
        count = 0
    message = "Pronto. Esqueci a preferência mais recente." if count else "Não encontrei uma preferência ativa para esquecer."
    return MemoryCommandResult(True, "forget_preference", "preference", "Chefe", _ack(message, source=source), result_count=count)


def build_memory_response(result: MemoryCommandResult) -> str:
    return result.reply


def handle_memory_command(text: str, source: str = "chat", *, db_path: str | Path | None = None) -> MemoryCommandResult:
    cmd = detect_memory_command(text)
    if cmd is None:
        return MemoryCommandResult(False)
    safe_subject = "<sensitive>" if cmd.sensitive else cmd.subject
    safe_type = "sensitive" if cmd.sensitive else cmd.memory_type
    logger.info("MEMORY_COMMAND_DETECTED action=%s type=%s subject=%s", cmd.action, safe_type, safe_subject)
    try:
        if cmd.action == "procedure_query":
            return _handle_procedure_command(cmd, db_path=db_path)
        if cmd.action == "reuse_procedure":
            return _handle_procedure_command(cmd, reuse=True, db_path=db_path)
        if cmd.action == "correct_procedure":
            return _handle_correction_command(cmd, source=source, db_path=db_path)
        if cmd.action == "forget_preference":
            return _handle_forget_preference(source=source, db_path=db_path)
        if cmd.action == "save":
            return _handle_save(cmd, source=source, db_path=db_path)
        if cmd.action in {"vault_query", "vault_reveal_request", "vault_reveal_confirm"}:
            return _handle_query(cmd, db_path=db_path)
        if cmd.action == "query":
            return _handle_query(cmd, db_path=db_path)
        if cmd.action == "forget":
            return _handle_forget(cmd, source=source, db_path=db_path)
        if cmd.action == "update":
            return _handle_update(cmd, source=source, db_path=db_path)
    except sqlite3.Error as exc:
        logger.warning("MEMORY_COMMAND_SQLITE_FAILED action=%s type=%s subject=%s error=%s", cmd.action, safe_type, safe_subject, exc)
        reply = _ack(f"Nao consegui mexer nessa memoria agora, chefe. O erro foi: {exc}", source="memory", response_type="generic_error")
        return MemoryCommandResult(True, cmd.action, safe_type, safe_subject, reply, sensitive=cmd.sensitive)
    except Exception as exc:
        logger.warning("MEMORY_COMMAND_FAILED action=%s type=%s subject=%s error=%s", cmd.action, safe_type, safe_subject, exc)
        reply = _ack(f"Nao consegui mexer nessa memoria agora, chefe. O erro foi: {exc}", source="memory", response_type="generic_error")
        return MemoryCommandResult(True, cmd.action, safe_type, safe_subject, reply, sensitive=cmd.sensitive)
    return MemoryCommandResult(False)


__all__ = [
    "MemoryCommand",
    "MemoryCommandResult",
    "build_memory_response",
    "detect_memory_command",
    "handle_memory_command",
    "normalize_memory_subject",
    "parse_memory_forget",
    "parse_memory_query",
    "parse_memory_save",
    "parse_memory_update",
    "route_memory_type",
]
