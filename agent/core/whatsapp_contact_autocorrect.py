"""
Autocorrection and fuzzy matching for WhatsApp contact resolution.

Statuses:
  exact              - folded query matches a name/alias exactly
  high_confidence    - single candidate with score >= HIGH_CONFIDENCE_THRESHOLD
  needs_confirmation - single candidate with score in [CONFIRM_THRESHOLD, HIGH_CONFIDENCE_THRESHOLD)
  ambiguous          - multiple candidates with close scores
  not_found          - no candidate above CONFIRM_THRESHOLD
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Score thresholds (0.0 – 1.0)
HIGH_CONFIDENCE_THRESHOLD = 0.80
CONFIRM_THRESHOLD = 0.45
AMBIGUOUS_GAP = 0.15  # If top-2 scores differ by less than this, it's ambiguous


def normalize_contact_text(text: str) -> str:
    """Lowercase + strip accents + collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", str(text or "").lower())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(stripped.split())


def build_auto_aliases(contact_name: str) -> set[str]:
    """
    Generate plausible spoken variants for a contact name.
    E.g. "Amye" -> {"amye", "amy", "amie", "ami"}
    """
    base = normalize_contact_text(contact_name)
    if not base:
        return set()

    aliases: set[str] = {base}
    tokens = base.split()

    # First token (first name) variants
    first = tokens[0]
    aliases.add(first)

    # Common suffix substitutions for feminine names ending in 'e' or 'y'
    for suffix_from, suffix_to in [
        ("ye", "y"), ("ye", "ie"), ("ye", "i"),
        ("ey", "y"), ("ey", "ie"),
        ("ie", "y"), ("ie", "i"),
        ("y", "ie"), ("y", "i"),
        ("a", "ah"),
    ]:
        if first.endswith(suffix_from):
            aliases.add(first[: -len(suffix_from)] + suffix_to)

    # Drop trailing silent 'e'
    if len(first) > 3 and first.endswith("e"):
        aliases.add(first[:-1])

    # Initials-only if multi-token
    if len(tokens) >= 2:
        initials = "".join(t[0] for t in tokens if t)
        aliases.add(initials)

    return aliases


def _lcs_length(a: str, b: str) -> int:
    """Longest common subsequence length (char level)."""
    if not a or not b:
        return 0
    la, lb = len(a), len(b)
    prev = [0] * (lb + 1)
    for i in range(1, la + 1):
        curr = [0] * (lb + 1)
        for j in range(1, lb + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev = curr
    return prev[lb]


def _prefix_score(query: str, term: str) -> float:
    """Score based on how much of query is a prefix of term (or vice-versa)."""
    if not query or not term:
        return 0.0
    shorter, longer = (query, term) if len(query) <= len(term) else (term, query)
    if longer.startswith(shorter):
        return len(shorter) / len(longer)
    return 0.0


def _char_overlap(a: str, b: str) -> float:
    """Fraction of characters in query that appear (in order) in term."""
    if not a or not b:
        return 0.0
    lcs = _lcs_length(a, b)
    return lcs / max(len(a), len(b))


def score_contact_candidate(query: str, contact: dict) -> dict:
    """
    Score a single contact against the query.
    Returns {"score": float, "matched_term": str, "reason": str}
    """
    q = normalize_contact_text(query)
    if not q:
        return {"score": 0.0, "matched_term": "", "reason": "empty_query"}

    # Collect all terms: name, whatsapp_name, stored aliases, auto-aliases
    name = normalize_contact_text(contact.get("name") or "")
    wname = normalize_contact_text(contact.get("whatsapp_name") or "")
    stored_aliases = [normalize_contact_text(a) for a in (contact.get("aliases") or []) if a]
    auto_aliases = build_auto_aliases(name)

    all_terms: list[str] = []
    for t in [name, wname] + stored_aliases:
        if t and t not in all_terms:
            all_terms.append(t)
    for t in sorted(auto_aliases):
        if t and t not in all_terms:
            all_terms.append(t)

    best_score = 0.0
    best_term = ""
    best_reason = "no_match"

    for term in all_terms:
        if not term:
            continue

        # Exact match
        if q == term:
            return {"score": 1.0, "matched_term": term, "reason": "exact"}

        # Query is contained in term or term in query
        score = 0.0
        reason = ""
        if q in term:
            score = len(q) / len(term)
            reason = "substring_query_in_term"
        elif term in q:
            score = len(term) / len(q) * 0.9
            reason = "substring_term_in_query"
        else:
            prefix = _prefix_score(q, term)
            overlap = _char_overlap(q, term)
            score = max(prefix * 0.85, overlap * 0.75)
            reason = "prefix" if prefix >= overlap else "lcs"

        if score > best_score:
            best_score = score
            best_term = term
            best_reason = reason

    return {"score": best_score, "matched_term": best_term, "reason": best_reason}


@dataclass
class ContactResolution:
    status: str  # exact | high_confidence | needs_confirmation | ambiguous | not_found
    query: str
    selected_contact: dict | None
    candidates: list[dict] = field(default_factory=list)
    score: float = 0.0
    reason: str = ""


def resolve_contact_candidate(query: str, contacts: list[dict]) -> ContactResolution:
    """
    Resolve a spoken contact name against the contacts list using fuzzy matching.

    Returns a ContactResolution describing the best match and confidence level.
    """
    q = normalize_contact_text(query)
    logger.info("WHATSAPP_CONTACT_AUTOCORRECT_START query=%r contacts_count=%d", q, len(contacts))

    if not q:
        return ContactResolution(
            status="not_found",
            query=query,
            selected_contact=None,
            reason="empty_query",
        )

    scored: list[dict] = []
    for contact in contacts:
        result = score_contact_candidate(q, contact)
        if result["score"] > 0.0:
            scored.append({
                "contact": contact,
                "score": result["score"],
                "matched_term": result["matched_term"],
                "reason": result["reason"],
            })
        logger.info(
            "WHATSAPP_CONTACT_AUTOCORRECT_CANDIDATE name=%r score=%.3f term=%r reason=%r",
            normalize_contact_text(contact.get("name") or ""),
            result["score"],
            result["matched_term"],
            result["reason"],
        )

    # Sort descending by score
    scored.sort(key=lambda x: x["score"], reverse=True)

    if not scored or scored[0]["score"] < CONFIRM_THRESHOLD:
        logger.info(
            "WHATSAPP_CONTACT_AUTOCORRECT_RESULT status=not_found query=%r",
            q,
        )
        return ContactResolution(
            status="not_found",
            query=query,
            selected_contact=None,
            candidates=[],
            score=0.0,
            reason="below_threshold",
        )

    top = scored[0]
    top_score = top["score"]
    top_contact = top["contact"]

    # Exact
    if top["reason"] == "exact" or top_score >= 1.0:
        logger.info(
            "WHATSAPP_CONTACT_AUTOCORRECT_RESULT status=exact query=%r name=%r score=%.3f",
            q,
            normalize_contact_text(top_contact.get("name") or ""),
            top_score,
        )
        return ContactResolution(
            status="exact",
            query=query,
            selected_contact=top_contact,
            candidates=[c["contact"] for c in scored],
            score=top_score,
            reason=top["reason"],
        )

    # Check ambiguity: multiple candidates with close scores
    if len(scored) >= 2:
        second_score = scored[1]["score"]
        if second_score >= CONFIRM_THRESHOLD and (top_score - second_score) < AMBIGUOUS_GAP:
            logger.info(
                "WHATSAPP_CONTACT_AMBIGUOUS query=%r top=%r score=%.3f second=%r score=%.3f",
                q,
                normalize_contact_text(scored[0]["contact"].get("name") or ""),
                top_score,
                normalize_contact_text(scored[1]["contact"].get("name") or ""),
                second_score,
            )
            return ContactResolution(
                status="ambiguous",
                query=query,
                selected_contact=None,
                candidates=[c["contact"] for c in scored if c["score"] >= CONFIRM_THRESHOLD],
                score=top_score,
                reason="ambiguous_candidates",
            )

    if top_score >= HIGH_CONFIDENCE_THRESHOLD:
        logger.info(
            "WHATSAPP_CONTACT_AUTOCORRECT_RESULT status=high_confidence query=%r name=%r score=%.3f",
            q,
            normalize_contact_text(top_contact.get("name") or ""),
            top_score,
        )
        return ContactResolution(
            status="high_confidence",
            query=query,
            selected_contact=top_contact,
            candidates=[c["contact"] for c in scored],
            score=top_score,
            reason=top["reason"],
        )

    # needs_confirmation
    logger.info(
        "WHATSAPP_CONTACT_CONFIRMATION_REQUIRED query=%r name=%r score=%.3f",
        q,
        normalize_contact_text(top_contact.get("name") or ""),
        top_score,
    )
    return ContactResolution(
        status="needs_confirmation",
        query=query,
        selected_contact=top_contact,
        candidates=[c["contact"] for c in scored],
        score=top_score,
        reason=top["reason"],
    )


# ---------------------------------------------------------------------------
# Confirmation / rejection phrase sets (folded, no accents)
# ---------------------------------------------------------------------------

_CONFIRMATION_PHRASES: frozenset[str] = frozenset({
    "sim", "e", "isso", "isso mesmo", "pode", "pode sim", "manda",
    "manda sim", "confirma", "confirmado", "certo", "correto",
    "e ela", "e ele", "essa mesmo", "esse mesmo", "essa", "esse",
    "vai", "tudo bem", "ok", "okay",
})

_REJECTION_PHRASES: frozenset[str] = frozenset({
    "nao", "nao e", "errado", "cancela", "cancelar", "nao manda",
    "outra pessoa", "nao e essa", "nao e esse", "errada",
    "diferente", "outro", "outra",
})


def is_contact_confirmation(text: str) -> bool:
    folded = normalize_contact_text(text)
    return folded in _CONFIRMATION_PHRASES


def is_contact_rejection(text: str) -> bool:
    folded = normalize_contact_text(text)
    return folded in _REJECTION_PHRASES
