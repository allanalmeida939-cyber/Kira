from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.memory_guard import is_sensitive, mask_sensitive_preview
from services.memory_store import (
    init_memory_db,
    search_learned_lessons,
    search_memories,
    search_people,
    search_preferences,
    search_project_facts,
)


logger = logging.getLogger("kira.app")
_TURN_MEMORY_CACHE_TTL_S = 30.0
_turn_memory_cache: dict[str, tuple[float, dict[str, Any]]] = {}

# ── PERF-HOTFIX-5: cache de memória de longa duração (2 min) ──────────────────
_MEMORY_LONG_CACHE_TTL = 120.0
_memory_long_cache: dict[str, tuple[float, list[Any]]] = {}

# Executor compartilhado p/ a busca mem0. NAO usar `with ThreadPoolExecutor` por
# busca: o __exit__ faz join e ANULA o timeout (esperaria o 429 inteiro, ~14s). Com
# um executor persistente, future.result(timeout) realmente corta em N s e a thread
# orfã termina sozinha em background (e ainda dispara o circuit-breaker do provider).
_MEM0_SEARCH_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="mem0-search"
)
_mem0_search_provider: Any | None = None
# Quanto a resposta espera pela memória de longo prazo antes de seguir sem ela.
_MEM0_REPLY_TIMEOUT_SECONDS = float(os.getenv("KIRA_MEM0_REPLY_TIMEOUT_SECONDS", "0.8") or 0.8)

# ── PERF-HOTFIX-5: padrões de mensagens que não precisam de busca pesada ──────
_MEMORY_SKIP_PATTERNS = [
    r'^(oi|olá|ola|hey|e aí|e ai|tudo bem|tudo certo|ok|pode|vai|claro|sim|não|nao)\b',
    r'^(abre|fecha|mostra|oculta|para|continua|cancela|confirma)\b',
    r'^(como você está|como voce esta|qual seu nome|me fala sobre você)\b',
]
_MEMORY_SKIP_RE = re.compile("|".join(_MEMORY_SKIP_PATTERNS), re.IGNORECASE)


def should_skip_heavy_memory(user_message: str) -> bool:
    """Retorna True se a mensagem não precisa de busca de memória pesada."""
    msg = user_message.strip()
    if len(msg) < 8:
        return True
    return bool(_MEMORY_SKIP_RE.match(msg.lower()))


def _safe_preview(text: str, *, max_chars: int = 120) -> str:
    preview = mask_sensitive_preview(str(text or "").strip())
    return preview[:max_chars]


def _mem0_primary_enabled() -> bool:
    return str(os.getenv("KIRA_MEMORY_PROVIDER", "mem0") or "mem0").strip().lower() in {"mem0", "auto"}


def _mem0_to_retrieved(item: dict[str, Any], *, index: int = 0) -> RetrievedMemory | None:
    memory = str(item.get("memory") or "").strip()
    if not memory:
        return None
    subject = str(item.get("metadata", {}).get("subject") if isinstance(item.get("metadata"), dict) else "").strip()
    if not subject:
        subject = _extract_memory_query_subject(memory) or "mem0"
    raw_score = item.get("score")
    try:
        score = float(raw_score if raw_score is not None else 1.0)
    except Exception:
        score = 1.0
    return RetrievedMemory(
        table="mem0",
        memory_id=index + 1,
        memory_type=str(item.get("metadata", {}).get("memory_type") or "mem0") if isinstance(item.get("metadata"), dict) else "mem0",
        subject=subject,
        text=memory,
        score=max(score, 0.1),
        sensitive=is_sensitive(memory),
        metadata={
            "mem0_id": item.get("id") or "",
            "metadata": item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
            "updated_at": item.get("updated_at") or "",
            "created_at": item.get("created_at") or "",
        },
    )


def _retrieve_mem0_context(
    text: str,
    *,
    source: str,
    max_items: int,
    timeout_seconds: float = _MEM0_REPLY_TIMEOUT_SECONDS,
) -> list[RetrievedMemory]:
    # PERF-HOTFIX-5: verificar cache longo antes de chamar a API
    cache_key = f"{source}|{_normalize(text)}"
    cache_entry = _memory_long_cache.get(cache_key)
    if cache_entry:
        ts, cached = cache_entry
        if (time.monotonic() - ts) < _MEMORY_LONG_CACHE_TTL:
            logger.info("MEM0_LONG_CACHE_HIT source=%s", source)
            return list(cached)
    # PERF-HOTFIX-5: skip de mensagens que não precisam de busca pesada
    if should_skip_heavy_memory(text):
        logger.info("MEM0_RETRIEVER_SKIPPED source=%s reason=short_or_trivial_message", source)
        return []

    from services.mem0_cloud_provider import Mem0CloudProvider, configured_top_k, mem0_is_rate_limited

    # Circuit-breaker: se um 429 recente tripou o breaker, pula sem nem chamar a API
    # (instantaneo) — evita o stall de ~14s por turno enquanto o rate-limit nao esfria.
    if mem0_is_rate_limited():
        logger.info("MEM0_RETRIEVER_SKIPPED source=%s reason=rate_limit_cooldown", source)
        return []

    def _do_search() -> list[Any]:
        global _mem0_search_provider
        # Provider compartilhado: criar e fechar um por turno custava DNS + TLS (~1 a 3 s por busca).
        if _mem0_search_provider is None:
            _mem0_search_provider = Mem0CloudProvider()
        provider = _mem0_search_provider
        top_k = max(max_items, configured_top_k(8))
        return provider.search(str(text or ""), user_id=provider.user_id, top_k=top_k)

    started = time.monotonic()
    logger.info("MEM0_RETRIEVER_BEGIN source=%s query_preview=%s", source, _safe_preview(text))
    try:
        future = _MEM0_SEARCH_EXECUTOR.submit(_do_search)
        rows = future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning("MEM0_RETRIEVER_TIMEOUT source=%s elapsed_ms=%s timeout_s=%s", source, elapsed_ms, timeout_seconds)
        return []
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.warning(
            "MEM0_RETRIEVER_FAILED source=%s error_type=%s elapsed_ms=%s safe_message=%s",
            source,
            exc.__class__.__name__,
            elapsed_ms,
            str(exc)[:300],
        )
        return []
    elapsed_ms = int((time.monotonic() - started) * 1000)
    results = [
        item
        for item in (_mem0_to_retrieved(row, index=index) for index, row in enumerate(rows))
        if item is not None
    ][: max(1, int(max_items))]
    logger.info("MEM0_RETRIEVER_RESULTS source=%s count=%s elapsed_ms=%s", source, len(results), elapsed_ms)

    # PERF-HOTFIX-5: guardar no cache longo
    _memory_long_cache[cache_key] = (time.monotonic(), list(results))
    # Limpar entradas expiradas (máx 50)
    if len(_memory_long_cache) > 50:
        now = time.monotonic()
        expired = [k for k, (ts, _) in _memory_long_cache.items() if (now - ts) >= _MEMORY_LONG_CACHE_TTL]
        for k in expired:
            _memory_long_cache.pop(k, None)
        if not expired:
            oldest = min(_memory_long_cache, key=lambda k: _memory_long_cache[k][0])
            _memory_long_cache.pop(oldest, None)

    return results


@dataclass(frozen=True)
class RetrievedMemory:
    table: str
    memory_id: int
    memory_type: str
    subject: str
    text: str
    score: float
    sensitive: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NaturalMemoryContext:
    text: str
    results: list[dict[str, Any]]
    source: str
    empty: bool
    reason: str = ""


_STOP_WORDS = {
    "a",
    "o",
    "as",
    "os",
    "um",
    "uma",
    "de",
    "da",
    "do",
    "das",
    "dos",
    "em",
    "no",
    "na",
    "nos",
    "nas",
    "para",
    "pra",
    "por",
    "que",
    "qual",
    "quais",
    "como",
    "onde",
    "voce",
    "você",
    "kira",
    "chefe",
    "isso",
    "esse",
    "essa",
    "aqui",
    "me",
    "minha",
    "meu",
}
_PREFERENCE_HINTS = {"explica", "explicar", "resume", "resumo", "prompt", "relatorio", "relatório", "resposta", "estilo", "fala", "simples"}
_PREFERENCE_ALWAYS_HINTS = {"larissa", "mensagem", "resposta", "preferencia", "preferencias", "preferência", "preferências", "lembra", "lembrar"}
_LESSON_HINTS = {"erro", "bug", "problema", "solucao", "solução", "voltou", "funcionou", "messages", "internet", "rota", "gate", "gemini", "livekit"}
_PROJECT_HINTS = {"kira", "projeto", "arquivo", "prompts.py", "personality_response.py", "boot", "livekit", "gemini", "manus", "messages", "internet", "memoria", "memória", "personalidade"}
_SENSITIVE_HINTS = {"senha", "token", "chave", "documento", "cpf", "rg", "segredo", "secret"}
_MEMORY_QUERY_ALIASES = {"lari": "Larissa"}
_MEMORY_QUERY_RE = re.compile(
    r"\b(?:lembra(?:r)?|recorda|sabe|conhece|quem\s+e|quem\s+eh|quem\s+é|o\s+que\s+voce\s+lembra|o\s+que\s+você\s+lembra)\b",
    re.IGNORECASE,
)


def _normalize(text: str | None) -> str:
    raw = str(text or "").strip().lower()
    decomposed = unicodedata.normalize("NFD", raw)
    ascii_text = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    ascii_text = re.sub(r"[^\w\s./@-]", " ", ascii_text)
    return re.sub(r"\s+", " ", ascii_text).strip()


def _cache_key(*, turn_id: str | None, source: str, text: str) -> str:
    normalized_turn_id = str(turn_id or "").strip()
    if not normalized_turn_id:
        return ""
    return f"{normalized_turn_id}|{str(source or '').strip().lower()}|{_normalize(text)}"


def clear_memory_turn_cache(turn_id: str | None = None) -> int:
    normalized_turn_id = str(turn_id or "").strip()
    now = time.time()
    removed = 0
    for key, (created_at, _) in list(_turn_memory_cache.items()):
        expired = (now - created_at) > _TURN_MEMORY_CACHE_TTL_S
        matches_turn = bool(normalized_turn_id and key.startswith(f"{normalized_turn_id}|"))
        if expired or matches_turn:
            _turn_memory_cache.pop(key, None)
            removed += 1
    return removed


def expand_memory_query_aliases(text: str) -> str:
    original = str(text or "")
    normalized = _normalize(original)
    additions: list[str] = []
    for alias, canonical in _MEMORY_QUERY_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", normalized) and _normalize(canonical) not in normalized:
            additions.append(canonical)
            logger.info('MEMORY_QUERY_ALIAS_EXPANDED original="%s" alias="%s"', alias.capitalize(), canonical)
    if not additions:
        return original
    return f"{original} {' '.join(additions)}".strip()


def _extract_memory_query_subject(text: str) -> str:
    expanded = expand_memory_query_aliases(text)
    normalized = _normalize(expanded)
    for canonical in _MEMORY_QUERY_ALIASES.values():
        if _normalize(canonical) in normalized:
            return canonical
    match = re.search(
        r"(?:sobre|da|do|de|quem\s+e|quem\s+eh|quem\s+é)\s+(?:a\s+|o\s+)?([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÀ-ÿ-]{2,})",
        expanded,
    )
    if match:
        return match.group(1).strip()
    for term in extract_memory_query_terms(expanded):
        if term not in {"lembra", "lembrar", "sobre", "sabe", "conhece", "recorda", "memoria", "memória"}:
            return term.capitalize()
    return ""


def _resolve_subject_with_session_context(text: str) -> tuple[str, list[str], str | None, bool]:
    try:
        from services.session_context_tracker import get_session_context_tracker
    except Exception:
        return _extract_memory_query_subject(text), [], None, False
    tracker = get_session_context_tracker()
    resolved, aliases, pronoun = tracker.resolve_pronoun(text)
    if pronoun and resolved:
        return resolved, aliases, pronoun, False
    if pronoun and not resolved:
        logger.info("MEMORY_SUBJECT_AMBIGUOUS_CONFIRMATION_REQUIRED pronoun=%s", pronoun)
        return "", [], pronoun, True
    raw_subject = _extract_memory_query_subject(text)
    logger.info("MEMORY_SUBJECT_RAW subject=%s", raw_subject or "none")
    normalized_subject = _normalize(raw_subject)
    logger.info("MEMORY_SUBJECT_NORMALIZED subject=%s", normalized_subject or "none")
    last_person = str(getattr(tracker, "last_person_name", "") or "").strip()
    if normalized_subject == "alegria" and _normalize(last_person) == "larissa":
        logger.info("MEMORY_SUBJECT_CONTEXT_CORRECTED raw=%s corrected=%s reason=recent_person", raw_subject, last_person)
        return last_person, list(getattr(tracker, "last_person_aliases", []) or []), None, False
    return raw_subject, [], None, False


_REMINDER_REQUEST_RE = re.compile(r"\b(?:me\s+lembr[ae]|lembr[ae]-me|me\s+avis[ae])\b")
_REMINDER_TIME_RE = re.compile(
    r"\d|\b(?:as|daqui|amanha|hoje|todo|toda|todos|todas|cada|depois|mais\s+tarde|noite|manha|tarde|minutos?|horas?|"
    r"semana|segunda|terca|quarta|quinta|sexta|sabado|domingo|meio\s+dia|meia\s+noite)\b"
)


# "Preciso lembrar de comprar carvão" é tarefa do Chefe, não pergunta sobre o que a Kira lembra.
_TASK_REMEMBER_RE = re.compile(
    r"\b(?:preciso|precisava|tenho\s+que|tenho\s+de|devo|vou|nao\s+posso\s+esquecer\s+de|quero)\s+(?:me\s+)?lembrar\b"
)


def is_reminder_request(text: str) -> bool:
    """"Me lembra às 11:19 de beber água" pede lembrete, não pergunta o que a Kira lembra."""
    normalized = _normalize(text)
    return bool(_REMINDER_REQUEST_RE.search(normalized) and _REMINDER_TIME_RE.search(normalized))


def is_memory_query(text: str) -> bool:
    normalized = _normalize(text)
    if not normalized or is_reminder_request(text) or _TASK_REMEMBER_RE.search(normalized):
        return False
    if re.search(r"\b(sabe|lembra) (sobre|de) (mim|eu)\b", normalized):
        # sobre o próprio Chefe: o Gemini responde com o núcleo de memória (cerebro 'o_que_sabe'); aqui virava
        # "Nao tenho nada salvo sobre a Mim"
        return False
    if re.match(r"^(?:kira\s+)?(?:lembra|lembre|lembre se|se lembra|guarda|grava|anota|memoriza|salva)\s+(?:que|disso)\b", normalized):
        # "lembra que meu time é o Fortaleza" é pedido para GUARDAR, não pergunta: o Gemini salva (cérebro); aqui
        # virava "não tenho nada salvo sobre a Time" e nada era guardado (teste de 2026-10-30)
        return False
    subject, _, pronoun, ambiguous = _resolve_subject_with_session_context(text)
    return bool(_MEMORY_QUERY_RE.search(str(text or "")) and (subject or pronoun or ambiguous))


def extract_memory_query_terms(text: str) -> list[str]:
    normalized = _normalize(text)
    terms: list[str] = []
    for token in normalized.split():
        if len(token) < 3 or token in _STOP_WORDS:
            continue
        terms.append(token)
    for match in re.findall(r"\b[A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wÀ-ÿ-]{2,}\b", str(text or "")):
        folded = _normalize(match)
        if folded and folded not in terms:
            terms.insert(0, folded)
    return list(dict.fromkeys(terms))[:12]


def _text_for_scoring(memory: dict[str, Any]) -> str:
    values = []
    for key in (
        "name",
        "aliases",
        "relation_to_alan",
        "notes",
        "subject",
        "content",
        "preference",
        "scope",
        "domain",
        "error",
        "symptom",
        "cause",
        "solution",
        "module",
        "fact",
        "file_path",
        "status",
        "category",
        "masked_preview",
    ):
        value = memory.get(key)
        if value:
            values.append(str(value))
    return " ".join(values)


def score_memory_relevance(memory: dict[str, Any], terms: list[str]) -> float:
    haystack = _normalize(_text_for_scoring(memory))
    if not haystack:
        return 0.0
    score = 0.0
    for term in terms:
        folded = _normalize(term)
        if not folded:
            continue
        if folded in haystack:
            score += 3.0 if len(folded) >= 6 else 1.5
    subject = _normalize(str(memory.get("subject") or memory.get("name") or memory.get("domain") or ""))
    if subject and subject in terms:
        score += 4.0
    return score


def sanitize_memory_for_prompt(memory: RetrievedMemory) -> str:
    text = str(memory.text or "").strip()
    if not text:
        return ""
    if memory.sensitive or is_sensitive(text):
        # Só o assunto, nunca o texto (nem mascarado): a frase "Valor oculto (...CPF falso do...)" com assunto "None"
        # entrava em toda resposta digitada e o Gemini Live derrubava a sessão (erro 1011, 2026-10-24 13:18-13:38).
        subject = str(memory.subject or "").strip()
        if not subject or subject.lower() in {"none", "null"}:
            return ""
        return f"Existe uma memoria sensivel sobre {subject}. Valor oculto."
    return text


def should_include_memory_in_prompt(memory: RetrievedMemory, source: str = "chat") -> bool:
    if not memory.text:
        return False
    return memory.score > 0  # sensível também só quando tem a ver com o pedido


def _result_key(memory: RetrievedMemory) -> tuple[str, int, str]:
    mem0_id = ""
    if isinstance(memory.metadata, dict):
        mem0_id = str(memory.metadata.get("mem0_id") or "")
    if mem0_id:
        return (memory.table, 0, mem0_id.lower())
    return (memory.table, memory.memory_id, memory.subject.lower())


def _dedupe(results: list[RetrievedMemory]) -> list[RetrievedMemory]:
    seen: set[tuple[str, int, str]] = set()
    deduped: list[RetrievedMemory] = []
    for item in sorted(results, key=lambda m: m.score, reverse=True):
        key = _result_key(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def retrieve_people_context(text: str, terms: list[str], *, db_path: str | Path | None = None) -> list[RetrievedMemory]:
    results: list[RetrievedMemory] = []
    for term in terms:
        for row in search_people(term, db_path=db_path, limit=5):
            name = str(row.get("name") or "").strip()
            relation = str(row.get("relation_to_alan") or "").strip()
            notes = str(row.get("notes") or "").strip()
            body = "; ".join(part for part in (relation, notes) if part)
            text_value = f"Pessoa: {name} - {body}".strip(" -")
            score = score_memory_relevance(row, terms) + 1.0
            results.append(RetrievedMemory("people", int(row.get("id") or 0), "person", name, text_value, score))
    return _dedupe(results)


def retrieve_preferences_context(text: str, terms: list[str], *, db_path: str | Path | None = None) -> list[RetrievedMemory]:
    normalized = _normalize(text)
    if not any(hint in normalized for hint in _PREFERENCE_HINTS | _PREFERENCE_ALWAYS_HINTS):
        # Preferences are useful, but keep them quiet for unrelated chatter.
        return []
    results: list[RetrievedMemory] = []
    for row in search_preferences(subject="Chefe", db_path=db_path, limit=5):
        preference = str(row.get("preference") or "").strip()
        score = score_memory_relevance(row, terms) + (2.0 if any(hint in normalized for hint in _PREFERENCE_HINTS) else 0.5)
        results.append(RetrievedMemory("preferences", int(row.get("id") or 0), "preference", "Chefe", f"Preferencia do Chefe: {preference}", score))
    return _dedupe(results)


def retrieve_experience_context(text: str, terms: list[str], *, db_path: str | Path | None = None) -> list[RetrievedMemory]:
    """Recupera só estratégias compatíveis, sem despejar o banco no Gemini."""
    try:
        from services.experience_memory import ExperienceMemory

        memory = ExperienceMemory(db_path=db_path)
        recommendation = memory.recommend(text, limit=4)
    except Exception as exc:
        logger.info("EXPERIENCE_MEMORY_RETRIEVAL_SKIPPED reason=%s", exc.__class__.__name__)
        return []

    results: list[RetrievedMemory] = []
    for index, row in enumerate(recommendation.get("preferences") or []):
        preference = str(row.get("preference") or "").strip()
        if not preference:
            continue
        scope = str(row.get("scope") or "geral").strip()
        # A preferência salva fica acima de procedimento e inferência no bloco
        # enviado ao modelo; a instrução atual continua sendo a autoridade
        # máxima porque vem fora deste bloco, na mensagem do usuário.
        score = 100.0 - float(index) - (0.0 if bool(row.get("explicit", True)) else 10.0)
        results.append(
            RetrievedMemory(
                "memory_preferences",
                int(row.get("id") or 0),
                "preference",
                "Chefe",
                f"Preferência contextual ({scope}): {preference}",
                score,
                metadata={"scope": scope, "source": row.get("source") or "structured_local_memory", "priority": "saved_explicit_preference"},
            )
        )
    for row in recommendation.get("procedures") or []:
        successes = int(row.get("success_count") or 0)
        failures = int(row.get("failure_count") or 0)
        explicit = bool(row.get("explicit"))
        if successes <= 0 and not explicit:
            # Falhas não viram instrução. A existência fica no histórico e o
            # procedimento só reaparece quando houver uma nova confirmação.
            continue
        steps = [str(step).strip() for step in row.get("steps") or [] if str(step).strip()]
        step_text = "; ".join(steps[:6]) or "seguir a estratégia registrada"
        if successes > 0:
            state = f"confirmado {successes} vez(es), {failures} falha(s)"
        else:
            state = "orientação explícita ainda sem confirmação de execução"
        confidence = float(row.get("confidence") or 0.0)
        score = 40.0 + min(10.0, float(row.get("recommendation_score") or confidence * 5.0)) + (2.0 if successes else 0.0)
        results.append(
            RetrievedMemory(
                "memory_procedures",
                int(row.get("id") or 0),
                "procedure",
                str(row.get("goal") or "tarefa"),
                f"Procedimento para {row.get('goal')}: {step_text}. Estado: {state}; confiança {confidence:.2f}.",
                score,
                metadata={"procedure_id": row.get("id"), "successes": successes, "failures": failures, "confirmed": successes > 0, "priority": "proven_procedure"},
            )
        )
    return _dedupe(results)


def retrieve_learned_lessons_context(text: str, terms: list[str], *, db_path: str | Path | None = None) -> list[RetrievedMemory]:
    normalized = _normalize(text)
    if not any(hint in normalized for hint in _LESSON_HINTS):
        return []
    results: list[RetrievedMemory] = []
    for term in terms:
        for row in search_learned_lessons(term, db_path=db_path, limit=5):
            error = str(row.get("error") or row.get("symptom") or "").strip()
            solution = str(row.get("solution") or "").strip()
            domain = str(row.get("domain") or "").strip()
            body = f"Aprendizado tecnico: {error} foi resolvido com {solution}".strip()
            score = score_memory_relevance(row, terms) + 2.0
            results.append(RetrievedMemory("learned_lessons", int(row.get("id") or 0), "learned_lesson", domain, body, score))
    for row in search_memories(memory_type="learned_lesson", db_path=db_path, limit=8):
        score = score_memory_relevance(row, terms)
        if score > 0:
            subject = str(row.get("subject") or "").strip()
            content = str(row.get("content") or "").strip()
            results.append(RetrievedMemory("memories", int(row.get("id") or 0), "learned_lesson", subject, f"Aprendizado tecnico: {content}", score))
    return _dedupe(results)


def retrieve_project_context(text: str, terms: list[str], *, db_path: str | Path | None = None) -> list[RetrievedMemory]:
    normalized = _normalize(text)
    if not any(hint in normalized for hint in _PROJECT_HINTS):
        return []
    results: list[RetrievedMemory] = []
    for term in terms:
        for row in search_project_facts(term, db_path=db_path, limit=5):
            module = str(row.get("module") or "projeto").strip()
            fact = str(row.get("fact") or "").strip()
            file_path = str(row.get("file_path") or "").strip()
            suffix = f" ({file_path})" if file_path and file_path not in fact else ""
            score = score_memory_relevance(row, terms) + 1.5
            results.append(RetrievedMemory("project_knowledge", int(row.get("id") or 0), "project", module, f"Projeto: {fact}{suffix}", score))
    for row in search_memories(memory_type="project", db_path=db_path, limit=8):
        score = score_memory_relevance(row, terms)
        if score > 0:
            subject = str(row.get("subject") or "projeto").strip()
            content = str(row.get("content") or "").strip()
            results.append(RetrievedMemory("memories", int(row.get("id") or 0), "project", subject, f"Projeto: {content}", score))
    return _dedupe(results)


def retrieve_sensitive_context_safe(text: str, terms: list[str], *, db_path: str | Path | None = None) -> list[RetrievedMemory]:
    normalized = _normalize(text)
    if not any(hint in normalized for hint in _SENSITIVE_HINTS):
        return []
    init_memory_db(db_path)
    from services.memory_store import _connect  # Local import keeps the public store API small for now.

    results: list[RetrievedMemory] = []
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, subject, category, masked_preview, active
            FROM sensitive_memories
            WHERE active = 1
            ORDER BY updated_at DESC, id DESC
            LIMIT 20
            """
        ).fetchall()
    for row in rows:
        payload = dict(row)
        score = score_memory_relevance(payload, terms)
        if score <= 0 and not any(hint in _normalize(_text_for_scoring(payload)) for hint in _SENSITIVE_HINTS):
            continue
        subject = str(payload.get("subject") or "sensivel").strip()
        preview = str(payload.get("masked_preview") or "").strip()
        results.append(RetrievedMemory("sensitive_memories", int(payload.get("id") or 0), "sensitive", subject, preview, score + 2.0, sensitive=True))
    return _dedupe(results)


def _retrieve_generic_memories(text: str, terms: list[str], *, db_path: str | Path | None = None) -> list[RetrievedMemory]:
    results: list[RetrievedMemory] = []
    for term in terms:
        for row in search_memories(term, db_path=db_path, limit=5):
            memory_type = str(row.get("type") or "memory").strip()
            score = score_memory_relevance(row, terms)
            if score <= 0:
                continue
            subject = str(row.get("subject") or memory_type).strip()
            content = str(row.get("content") or "").strip()
            sensitive = str(row.get("sensitivity") or "").lower() == "sensitive" or is_sensitive(content)
            results.append(RetrievedMemory("memories", int(row.get("id") or 0), memory_type, subject, content, score, sensitive=sensitive))
    return _dedupe(results)


def build_real_context_block(results: list[RetrievedMemory], *, max_items: int = 5) -> str:
    safe_items = [item for item in results if should_include_memory_in_prompt(item)]
    if not safe_items:
        return ""
    lines: list[str] = ["CONTEXTO REAL DISPONIVEL:"]
    for item in safe_items[: max(1, int(max_items))]:
        safe_text = sanitize_memory_for_prompt(item)
        if safe_text:
            lines.append(f"- {safe_text}")
    if len(lines) > 1:
        lines.extend(
            [
                "",
                "REGRAS:",
                "Use somente se for relevante para a pergunta.",
                "Hierarquia: instrução explícita da mensagem atual; depois preferência salva; depois procedimento comprovado; por último qualquer inferência.",
                "Nao invente memoria que nao esteja aqui.",
                "Se nao houver informacao suficiente, diga que nao encontrou essa memoria.",
            ]
        )
    return "\n".join(lines).strip() if len(lines) > 1 else ""


def _memory_prompt_label(memory: RetrievedMemory) -> str:
    memory_type = str(memory.memory_type or "").strip().lower()
    if memory_type == "person":
        return "Pessoa"
    if memory_type == "project":
        return "Projeto Kira"
    if memory_type == "preference":
        return "Preferencia"
    if memory_type == "learned_lesson":
        return "Aprendizado"
    if memory_type == "procedure":
        return "Procedimento comprovado"
    if memory_type == "sensitive":
        return "Item sensivel"
    return "Memoria"


def build_natural_memory_context_block(results: list[RetrievedMemory], *, max_items: int = 5) -> str:
    safe_items = [item for item in results if should_include_memory_in_prompt(item)]
    if not safe_items:
        return ""
    lines: list[str] = ["CONTEXTO REAL DISPONIVEL:"]
    for item in safe_items[: max(1, int(max_items))]:
        safe_text = sanitize_memory_for_prompt(item)
        if safe_text:
            lines.append(f"* {_memory_prompt_label(item)}: {safe_text}")
    if len(lines) <= 1:
        return ""
    lines.extend(
        [
            "",
            "REGRAS:",
            "* Use essas informacoes somente se forem relevantes para a resposta atual.",
            "* Hierarquia: instrução explícita da mensagem atual; depois preferência salva; depois procedimento comprovado; por último qualquer inferência.",
            "* A resposta final deve ser natural, como Akira falando com Chefe.",
            "* Nao diga que recebeu um bloco tecnico ou um sistema de memoria.",
            "* Nunca responda com SYSTEM_NOTE_FOR_REASONING_ONLY, SYSTEM_NOTE ou metadados internos.",
            "* Nao invente detalhes. Se a memoria nao responder tudo, diga apenas o que sabe.",
            "* Se houver memoria sensivel, nunca exponha valores brutos.",
            "* Perfil de voz ou identificacao vocal nao e memoria real sobre pessoa/projeto.",
        ]
    )
    return "\n".join(lines).strip()


def _structured_results_for_log(results: list[RetrievedMemory]) -> list[dict[str, Any]]:
    structured: list[dict[str, Any]] = []
    for item in results:
        safe_text = sanitize_memory_for_prompt(item)
        structured.append(
            {
                "table": item.table,
                "memory_id": item.memory_id,
                "memory_type": item.memory_type,
                "subject": item.subject,
                "score": round(float(item.score or 0.0), 3),
                "sensitive": bool(item.sensitive),
                "preview": mask_sensitive_preview(safe_text)[:160],
            }
        )
    return structured


def _coerce_retrieved_memory(raw: Any) -> RetrievedMemory | None:
    if isinstance(raw, RetrievedMemory):
        return raw
    if isinstance(raw, dict):
        try:
            return RetrievedMemory(**raw)
        except TypeError:
            return None
    return None


def _item_mentions_subject(item: RetrievedMemory | dict[str, Any], subject: str, aliases: list[str] | None = None) -> bool:
    clean_subject = str(subject or "").strip()
    if not clean_subject:
        return True
    subject_terms = {_normalize(clean_subject)}
    subject_terms.update(_normalize(alias) for alias in list(aliases or []) if str(alias or "").strip())
    subject_terms.discard("")
    if not subject_terms:
        return True
    if isinstance(item, RetrievedMemory):
        payload: dict[str, Any] = {
            "subject": item.subject,
            "text": item.text,
            "content": item.text,
            "person": item.metadata.get("person") if isinstance(item.metadata, dict) else "",
            "entities": item.metadata.get("entities") if isinstance(item.metadata, dict) else [],
        }
    else:
        payload = dict(item)
    direct_subject = _normalize(str(payload.get("subject") or payload.get("name") or payload.get("person") or ""))
    if direct_subject in subject_terms:
        return True
    entities = payload.get("entities")
    if isinstance(entities, (list, tuple, set)) and any(_normalize(str(entity)) in subject_terms for entity in entities):
        return True
    haystack = _normalize(
        " ".join(
            str(payload.get(key) or "")
            for key in ("text", "content", "notes", "relation_to_alan", "aliases")
        )
    )
    return any(term and re.search(rf"\b{re.escape(term)}\b", haystack) for term in subject_terms)


def _filter_relevant_items(
    items: list[RetrievedMemory],
    subject: str,
    aliases: list[str] | None = None,
) -> list[RetrievedMemory]:
    before = len(items)
    filtered = [item for item in items if _item_mentions_subject(item, subject, aliases)]
    logger.info("MEMORY_RELEVANCE_FILTER_APPLIED subject=%s before=%s after=%s", subject or "none", before, len(filtered))
    return filtered


def _fallback_relevant_items(
    subject: str,
    *,
    aliases: list[str] | None = None,
    source: str = "speech",
    max_items: int = 3,
    db_path: str | Path | None = None,
) -> list[RetrievedMemory]:
    logger.info("MEMORY_FALLBACK_BEGIN reason=normal_context_empty subject=%s", subject or "none")
    terms = list(dict.fromkeys([_normalize(subject), *(_normalize(alias) for alias in list(aliases or []))]))
    terms = [term for term in terms if term]
    if not terms:
        logger.info("MEMORY_FALLBACK_RESULT found=false")
        return []
    attempts: list[tuple[str, Any]] = [
        ("people", retrieve_people_context),
        ("preferences", retrieve_preferences_context),
        ("experience", retrieve_experience_context),
        ("project_knowledge", retrieve_project_context),
        ("learned_lessons", retrieve_learned_lessons_context),
        ("sensitive_memories", retrieve_sensitive_context_safe),
        ("memories", _retrieve_generic_memories),
    ]
    found: list[RetrievedMemory] = []
    query_text = " ".join([subject, *(aliases or [])]).strip()
    for name, func in attempts:
        started = time.monotonic()
        try:
            candidates = func(query_text, terms, db_path=db_path)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.warning("MEMORY_FALLBACK_SOURCE_TRIED source=%s count=0 elapsed_ms=%s error=%s", name, elapsed_ms, exc)
            continue
        relevant = _filter_relevant_items(candidates, subject, aliases)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info("MEMORY_FALLBACK_SOURCE_TRIED source=%s count=%s elapsed_ms=%s", name, len(relevant), elapsed_ms)
        found.extend(relevant)
        if len(found) >= max(1, int(max_items)):
            break
    found = _dedupe(found)[: max(1, int(max_items))]
    logger.info("MEMORY_FALLBACK_RESULT found=%s", str(bool(found)).lower())
    return found


def build_memory_context_for_reply(
    user_text: str,
    *,
    source: str = "voice",
    max_items: int = 5,
    db_path: str | Path | None = None,
    turn_id: str | None = None,
) -> dict[str, Any]:
    normalized_turn_id = str(turn_id or "").strip()
    if normalized_turn_id:
        logger.info("MEMORY_NATURAL_CONTEXT_BEGIN source=%s turn_id=%s", source, normalized_turn_id)
    else:
        logger.info("MEMORY_NATURAL_CONTEXT_BEGIN source=%s", source)
    query_text = expand_memory_query_aliases(user_text)
    payload = retrieve_context_for_text(
        query_text,
        source=source,
        max_items=max_items,
        db_path=db_path,
        turn_id=turn_id,
    )
    results: list[RetrievedMemory] = []
    for raw in list(payload.get("results") or []):
        item = _coerce_retrieved_memory(raw)
        if item is not None:
            results.append(item)
    if not results:
        reason = "no_relevant_memory" if payload.get("empty") else "no_safe_results"
        if normalized_turn_id:
            logger.info("MEMORY_NATURAL_CONTEXT_EMPTY reason=%s source=%s turn_id=%s", reason, source, normalized_turn_id)
        else:
            logger.info("MEMORY_NATURAL_CONTEXT_EMPTY reason=%s source=%s", reason, source)
        return {
            "ok": True,
            "source": source,
            "text": "",
            "results": [],
            "empty": True,
            "reason": reason,
        }
    block = build_natural_memory_context_block(results, max_items=max_items)
    if not block:
        if normalized_turn_id:
            logger.info("MEMORY_NATURAL_CONTEXT_EMPTY reason=no_prompt_block source=%s turn_id=%s", source, normalized_turn_id)
        else:
            logger.info("MEMORY_NATURAL_CONTEXT_EMPTY reason=no_prompt_block source=%s", source)
        return {
            "ok": True,
            "source": source,
            "text": "",
            "results": [],
            "empty": True,
            "reason": "no_prompt_block",
        }
    structured = _structured_results_for_log(results[: max(1, int(max_items))])
    if normalized_turn_id:
        logger.info("MEMORY_NATURAL_CONTEXT_RESULTS count=%s source=%s turn_id=%s", len(structured), source, normalized_turn_id)
        logger.info("MEMORY_NATURAL_CONTEXT_INJECTED items=%s source=%s turn_id=%s", len(structured), source, normalized_turn_id)
    else:
        logger.info("MEMORY_NATURAL_CONTEXT_RESULTS count=%s source=%s", len(structured), source)
        logger.info("MEMORY_NATURAL_CONTEXT_INJECTED items=%s source=%s", len(structured), source)
    return {
        "ok": True,
        "source": source,
        "text": block,
        "results": structured,
        "raw_results": [item.__dict__ for item in results[: max(1, int(max_items))]],
        "empty": False,
        "reason": "",
    }


def _natural_memory_line(text: str) -> str:
    clean = re.sub(r"^\s*(Pessoa|Projeto|Preferencia do Chefe|Preferencia|Aprendizado tecnico|Memoria)\s*:\s*", "", str(text or "").strip(), flags=re.IGNORECASE)
    clean = re.sub(r"^\s*Larissa\s*-\s*", "", clean, flags=re.IGNORECASE)
    return clean.strip(" .")


def build_memory_query_gate_response(
    user_text: str,
    *,
    source: str = "speech",
    max_items: int = 3,
    db_path: str | Path | None = None,
    turn_id: str | None = None,
) -> dict[str, Any]:
    if not is_memory_query(user_text):
        return {"handled": False, "matched": False}
    subject, context_aliases, pronoun, ambiguous_pronoun = _resolve_subject_with_session_context(user_text)
    if ambiguous_pronoun:
        response = "Dela quem, Chefe? Pode me dizer?"
        logger.info("MEMORY_QUERY_GATE_MATCHED subject=ambiguous_pronoun source=%s", source)
        logger.info("MEMORY_QUERY_GATE_RESULT found=false reason=ambiguous_pronoun")
        logger.info("MEMORY_QUERY_GATE_HANDLED voice=%s text=%s", str(source == "speech").lower(), str(source == "text").lower())
        return {
            "handled": True,
            "matched": True,
            "found": False,
            "subject": "",
            "count": 0,
            "response": response,
            "reply_instruction": f"Responda exatamente, de forma natural: {response}",
            "turn_id": str(turn_id or "").strip(),
            "ambiguous_pronoun": True,
            "voice_route": "generate_reply",
        }
    subject = subject or "isso"
    logger.info("MEMORY_QUERY_GATE_MATCHED subject=%s source=%s", subject, source)
    expanded_text = expand_memory_query_aliases(user_text)
    if pronoun and subject and _normalize(subject) not in _normalize(expanded_text):
        expanded_text = f"{expanded_text} {subject}".strip()
    natural_payload = build_memory_context_for_reply(
        expanded_text,
        source=source,
        max_items=max_items,
        db_path=db_path,
        turn_id=turn_id,
    )
    raw_results: list[RetrievedMemory] = []
    raw_source = natural_payload.get("raw_results") or natural_payload.get("results") or []
    for raw in list(raw_source):
        item = _coerce_retrieved_memory(raw)
        if item is not None:
            raw_results.append(item)
    aliases = list(context_aliases)
    normalized_subject = _normalize(subject)
    for alias, canonical in _MEMORY_QUERY_ALIASES.items():
        if _normalize(canonical) == normalized_subject:
            aliases.append(alias)
            aliases.append(canonical)
    relevant_results = _filter_relevant_items(raw_results, subject, aliases)
    if not relevant_results:
        relevant_results = _fallback_relevant_items(
            subject,
            aliases=aliases,
            source=source,
            max_items=max_items,
            db_path=db_path,
        )
    safe_lines = [
        _natural_memory_line(sanitize_memory_for_prompt(item))
        for item in relevant_results[: max(1, int(max_items))]
    ]
    safe_lines = [line for line in safe_lines if line]
    found = bool(safe_lines)
    if found:
        logger.info("MEMORY_QUERY_GATE_RESULT found=true count=%s", len(safe_lines))
        joined = "; ".join(dict.fromkeys(safe_lines))
        response = f"Sobre a {subject}, eu lembro que {joined}."
        reply_instruction = (
            f"Pergunta de memoria real sobre {subject}. Responda pela rota normal de voz. "
            f"Use somente estes itens relevantes: {joined}. "
            "Nao diga que lembra de algo fora desses itens."
        )
    else:
        logger.info("MEMORY_QUERY_GATE_RESULT found=false reason=no_subject_relevant_memory")
        response = f"Nao tenho nada salvo sobre a {subject} ainda, Chefe."
        reply_instruction = (
            f"Pergunta de memoria real sobre {subject}. "
            f"Responda de forma natural, sem inventar memoria: {response}"
        )
    logger.info("MEMORY_QUERY_GATE_HANDLED voice=%s text=%s", str(source == "speech").lower(), str(source == "text").lower())
    return {
        "handled": True,
        "matched": True,
        "found": found,
        "subject": subject,
        "count": len(safe_lines),
        "response": response,
        "reply_instruction": reply_instruction,
        "voice_route": "generate_reply",
        "turn_id": str(turn_id or "").strip(),
    }


def retrieve_context_for_text(
    text: str,
    *,
    source: str = "chat",
    max_items: int = 5,
    db_path: str | Path | None = None,
    turn_id: str | None = None,
) -> dict[str, Any]:
    clear_memory_turn_cache()
    normalized_turn_id = str(turn_id or "").strip()
    key = _cache_key(turn_id=normalized_turn_id, source=source, text=text)
    if key and key in _turn_memory_cache:
        _, cached_payload = _turn_memory_cache[key]
        cached_results = list(cached_payload.get("results") or [])
        logger.info("MEMORY_TURN_CACHE_HIT turn_id=%s subject=%s count=%s", normalized_turn_id, _extract_memory_query_subject(text) or "none", len(cached_results))
        return dict(cached_payload)
    logger.info("MEMORY_RETRIEVER_BEGIN source=%s", source)
    terms = extract_memory_query_terms(text)
    if not terms:
        logger.info("MEMORY_CONTEXT_EMPTY source=%s", source)
        payload = {"ok": True, "source": source, "terms": [], "results": [], "text": "", "empty": True}
        if key:
            _turn_memory_cache[key] = (time.time(), dict(payload))
            logger.info("MEMORY_TURN_CACHE_STORE turn_id=%s subject=%s count=0", normalized_turn_id, "none")
        return payload

    results: list[RetrievedMemory] = []
    if _mem0_primary_enabled():
        results.extend(_retrieve_mem0_context(text, source=source, max_items=max_items))
    else:
        logger.info("MEM0_RETRIEVER_SKIPPED source=%s reason=provider_disabled", source)
    # Procedimentos e preferências contextuais são locais e entram mesmo quando
    # o provider semântico externo está ativo; o banco inteiro nunca é enviado.
    results.extend(retrieve_experience_context(text, terms, db_path=db_path))
    if not results and not _mem0_primary_enabled():
        logger.info("MEMORY_PROVIDER_FALLBACK_USED provider=local reason=mem0_disabled")
        results.extend(retrieve_people_context(text, terms, db_path=db_path))
        results.extend(retrieve_preferences_context(text, terms, db_path=db_path))
        results.extend(retrieve_learned_lessons_context(text, terms, db_path=db_path))
        results.extend(retrieve_project_context(text, terms, db_path=db_path))
        results.extend(retrieve_sensitive_context_safe(text, terms, db_path=db_path))
        results.extend(_retrieve_generic_memories(text, terms, db_path=db_path))
    results = _dedupe(results)
    filtered = [item for item in results if item.score > 0 or item.sensitive][: max(1, int(max_items))]
    logger.info("MEMORY_RETRIEVER_RESULTS source=%s count=%s", source, len(filtered))
    block = build_real_context_block(filtered, max_items=max_items)
    if block:
        logger.info("MEMORY_CONTEXT_BLOCK_BUILT source=%s items=%s", source, len(filtered))
        logger.info("MEM0_CONTEXT_BLOCK_BUILT source=%s memories_count=%s", source, len(filtered))
        logger.info("MEM0_CONTEXT_INJECTED source=%s", source)
        payload = {
            "ok": True,
            "source": source,
            "terms": terms,
            "results": [item.__dict__ for item in filtered],
            "text": block,
            "empty": False,
        }
        if key:
            _turn_memory_cache[key] = (time.time(), dict(payload))
            logger.info("MEMORY_TURN_CACHE_STORE turn_id=%s subject=%s count=%s", normalized_turn_id, _extract_memory_query_subject(text) or "none", len(filtered))
        return payload
    logger.info("MEMORY_CONTEXT_EMPTY source=%s", source)
    logger.info("MEM0_CONTEXT_EMPTY source=%s", source)
    payload = {"ok": True, "source": source, "terms": terms, "results": [], "text": "", "empty": True}
    if key:
        _turn_memory_cache[key] = (time.time(), dict(payload))
        logger.info("MEMORY_TURN_CACHE_STORE turn_id=%s subject=%s count=0", normalized_turn_id, _extract_memory_query_subject(text) or "none")
    return payload


__all__ = [
    "NaturalMemoryContext",
    "RetrievedMemory",
    "build_memory_query_gate_response",
    "build_memory_context_for_reply",
    "build_natural_memory_context_block",
    "clear_memory_turn_cache",
    "expand_memory_query_aliases",
    "build_real_context_block",
    "extract_memory_query_terms",
    "is_memory_query",
    "retrieve_context_for_text",
    "should_skip_heavy_memory",
    "_filter_relevant_items",
    "retrieve_learned_lessons_context",
    "retrieve_people_context",
    "retrieve_preferences_context",
    "retrieve_experience_context",
    "retrieve_project_context",
    "retrieve_sensitive_context_safe",
    "sanitize_memory_for_prompt",
    "score_memory_relevance",
    "should_include_memory_in_prompt",
]
