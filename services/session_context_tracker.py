from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field


logger = logging.getLogger("kira.app")


PRONOUNS_PT = {
    "dela",
    "dele",
    "ela",
    "ele",
    "essa",
    "esse",
    "aquela",
    "aquele",
    "isso",
    "aquilo",
    "nela",
    "nele",
}
ENTITY_CONTEXT_MAX_AGE_SECONDS = 15 * 60

_KNOWN_PERSON_ALIASES = {
    "lari": "Larissa",
}


def _normalize(text: str | None) -> str:
    raw = str(text or "").strip().lower()
    decomposed = unicodedata.normalize("NFD", raw)
    ascii_text = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    ascii_text = re.sub(r"[^\w\s-]", " ", ascii_text)
    return re.sub(r"\s+", " ", ascii_text).strip()


def _extract_known_person(text: str) -> tuple[str | None, list[str]]:
    normalized = _normalize(text)
    for alias, canonical in _KNOWN_PERSON_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", normalized):
            return canonical, [alias.capitalize()]
        if re.search(rf"\b{re.escape(_normalize(canonical))}\b", normalized):
            return canonical, [alias.capitalize()]
    return None, []


def contains_reference_pronoun(text: str) -> tuple[bool, str]:
    normalized = _normalize(text)
    for pronoun in PRONOUNS_PT:
        if re.search(rf"\b{re.escape(pronoun)}\b", normalized):
            return True, pronoun
    return False, ""


def _replace_reference_with_entity(text: str, pronoun: str, entity: str) -> str:
    original = str(text or "")
    clean_entity = str(entity or "").strip()
    clean_pronoun = str(pronoun or "").strip().lower()
    if not original or not clean_entity or not clean_pronoun:
        return original
    replacements = {
        "dela": f"de {clean_entity}",
        "dele": f"de {clean_entity}",
        "nela": f"em {clean_entity}",
        "nele": f"em {clean_entity}",
        "ela": clean_entity,
        "ele": clean_entity,
        "essa": clean_entity,
        "esse": clean_entity,
        "aquela": clean_entity,
        "aquele": clean_entity,
    }
    replacement = replacements.get(clean_pronoun, clean_entity)
    if clean_pronoun in {"aquela", "aquele", "essa", "esse"}:
        pattern = re.compile(rf"\b{re.escape(clean_pronoun)}\s+(?:menina|garota|pessoa|cara|rapaz)\b", re.IGNORECASE)
        updated = pattern.sub(replacement, original, count=1)
        if updated != original:
            return updated
    pattern = re.compile(rf"\b{re.escape(clean_pronoun)}\b", re.IGNORECASE)
    return pattern.sub(replacement, original, count=1)


def apply_resolved_entity_to_turn(
    text: str,
    *,
    turn_id: str | None = None,
    source: str = "",
) -> dict[str, object]:
    tracker = get_session_context_tracker()
    resolved, aliases, pronoun = tracker.resolve_pronoun(text)
    if pronoun and not resolved:
        logger.info(
            'SESSION_CONTEXT_COREFERENCE_AMBIGUOUS pronoun="%s" turn_id=%s source=%s',
            pronoun,
            str(turn_id or "none"),
            str(source or "unknown"),
        )
        return {
            "applied": False,
            "ambiguous": True,
            "text": str(text or ""),
            "entity": "",
            "aliases": aliases,
            "pronoun": pronoun,
            "instruction": f'Voce esta falando da {tracker.last_person_name}?' if tracker.last_person_name else "",
        }
    if not pronoun or not resolved:
        return {
            "applied": False,
            "ambiguous": False,
            "text": str(text or ""),
            "entity": "",
            "aliases": aliases,
            "pronoun": pronoun or "",
            "instruction": "",
        }
    resolved_text = _replace_reference_with_entity(str(text or ""), pronoun, resolved)
    instruction = (
        "CONTEXTO DE SESSAO:\n"
        f"* \"{pronoun}\" neste turno se refere a {resolved}.\n"
        f"* Interprete internamente a pergunta como: {resolved_text}\n"
        "* Se nao houver memoria sobre o detalhe perguntado, diga que esse detalhe nao esta salvo. Nao pergunte \"ela quem\"."
    )
    logger.info(
        'SESSION_CONTEXT_COREFERENCE_APPLIED_TO_TURN pronoun="%s" value=%s turn_id=%s source=%s',
        pronoun,
        resolved,
        str(turn_id or "none"),
        str(source or "unknown"),
    )
    return {
        "applied": True,
        "ambiguous": False,
        "text": resolved_text,
        "entity": resolved,
        "aliases": aliases,
        "pronoun": pronoun,
        "instruction": instruction,
    }


@dataclass
class SessionContextTracker:
    """Short-lived context for the current conversation."""

    last_person_name: str | None = None
    last_person_aliases: list[str] = field(default_factory=list)
    last_person_context: str | None = None
    last_topic: str | None = None
    last_project: str | None = None
    last_artifact: str | None = None
    last_location: str | None = None
    last_memory_subject: str | None = None
    source_turn_id: str | None = None
    updated_at: float = 0.0

    def reset(self) -> None:
        self.last_person_name = None
        self.last_person_aliases = []
        self.last_person_context = None
        self.last_topic = None
        self.last_project = None
        self.last_artifact = None
        self.last_location = None
        self.last_memory_subject = None
        self.source_turn_id = None
        self.updated_at = 0.0

    def update_person(
        self,
        name: str,
        context: str | None = None,
        aliases: list[str] | None = None,
        turn_id: str | None = None,
        *,
        reason: str = "person_mentioned",
    ) -> None:
        clean_name = str(name or "").strip()
        if not clean_name:
            return
        clean_aliases = [str(alias or "").strip() for alias in list(aliases or []) if str(alias or "").strip()]
        self.last_person_name = clean_name
        self.last_person_aliases = list(dict.fromkeys(clean_aliases))
        if context:
            self.last_person_context = str(context).strip()
        self.last_memory_subject = clean_name
        self.source_turn_id = str(turn_id or "").strip() or self.source_turn_id
        self.updated_at = time.time()
        logger.info(
            "SESSION_CONTEXT_UPDATED person=%s reason=%s turn_id=%s",
            clean_name,
            reason,
            self.source_turn_id or "none",
        )
        logger.info(
            "SESSION_CONTEXT_ENTITY_SET type=person value=%s reason=%s turn_id=%s",
            clean_name,
            reason,
            self.source_turn_id or "none",
        )

    def update_from_text(self, text: str, *, turn_id: str | None = None, speaker: str = "user") -> bool:
        person, aliases = _extract_known_person(text)
        if not person:
            return False
        self.update_person(
            person,
            context=text,
            aliases=aliases,
            turn_id=turn_id,
            reason="person_mentioned",
        )
        return True

    def resolve_pronoun(self, text: str) -> tuple[str | None, list[str], str | None]:
        has_pronoun, pronoun = contains_reference_pronoun(text)
        if not has_pronoun:
            logger.info("SESSION_CONTEXT_COREFERENCE_SKIPPED reason=no_reference")
            return None, [], None
        if self.last_person_name:
            age_s = max(0.0, time.time() - float(self.updated_at or 0.0)) if self.updated_at else 0.0
            if self.updated_at and age_s > ENTITY_CONTEXT_MAX_AGE_SECONDS:
                logger.info(
                    'SESSION_CONTEXT_COREFERENCE_SKIPPED pronoun="%s" reason=stale_context age_s=%.1f',
                    pronoun,
                    age_s,
                )
                return None, [], pronoun
            logger.info(
                'SESSION_CONTEXT_PRONOUN_RESOLVED pronoun="%s" resolved_subject=%s',
                pronoun,
                self.last_person_name,
            )
            logger.info(
                'SESSION_CONTEXT_COREFERENCE_RESOLVED pronoun="%s" type=person value=%s',
                pronoun,
                self.last_person_name,
            )
            logger.info("SESSION_CONTEXT_USED_IN_MEMORY_QUERY subject=%s", self.last_person_name)
            return self.last_person_name, list(self.last_person_aliases), pronoun
        logger.info('SESSION_CONTEXT_AMBIGUOUS_PRONOUN pronoun="%s" reason=no_person_in_context', pronoun)
        logger.info('SESSION_CONTEXT_COREFERENCE_SKIPPED pronoun="%s" reason=no_person_in_context', pronoun)
        return None, [], pronoun


_SESSION_CONTEXT_TRACKER = SessionContextTracker()


def get_session_context_tracker() -> SessionContextTracker:
    return _SESSION_CONTEXT_TRACKER


__all__ = [
    "PRONOUNS_PT",
    "SessionContextTracker",
    "apply_resolved_entity_to_turn",
    "contains_reference_pronoun",
    "get_session_context_tracker",
]
