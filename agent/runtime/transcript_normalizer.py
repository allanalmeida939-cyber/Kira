from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranscriptNormalizationResult:
    text: str
    changed: bool
    reason: str = ""
    confidence: float = 0.0


def _fold(text: str | None) -> str:
    raw = str(text or "").strip().lower()
    decomposed = unicodedata.normalize("NFD", raw)
    ascii_text = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    ascii_text = re.sub(r"[^\w\s]", " ", ascii_text)
    return re.sub(r"\s+", " ", ascii_text).strip()


def _replace_panel_aliases(text: str) -> tuple[str, str, float]:
    folded = _fold(text)
    has_panel_action = bool(
        re.search(r"\b(?:mostra|mostrar|mostre|oculta|ocultar|oculte|esconde|esconder|abre|abrir)\b", folded)
    )
    if re.search(r"\bpanel\s+de\s+ativos\b", folded):
        return re.sub(r"(?i)\bpanel\s+de\s+ativos\b", "painel de ativos", text), "panel_de_ativos", 0.96
    if has_panel_action and re.search(r"\bpa[ií]s\s+ativas\b", text, flags=re.IGNORECASE):
        return re.sub(r"(?i)\bpa[ií]s\s+ativas\b", "painel de ativos", text), "pais_ativas_panel", 0.88
    if has_panel_action and re.search(r"\bpais\s+ativas\b", folded):
        return re.sub(r"(?i)\bpais\s+ativas\b", "painel de ativos", text), "pais_ativas_panel", 0.86
    return text, "", 0.0


def _replace_common_stt(text: str) -> tuple[str, str, float]:
    updated = text
    reason = ""
    confidence = 0.0
    if re.search(r"(?i)^\s*questi[\s,]+", updated):
        updated = re.sub(r"(?i)^\s*questi([\s,]+)", r"Kira\1", updated)
        reason = "questi_prefix"
        confidence = max(confidence, 0.82)
    if re.search(r"(?i)\bdalla\b", updated):
        updated = re.sub(r"(?i)\bdalla\b", "dela", updated)
        reason = "dalla_dela" if not reason else f"{reason}+dalla_dela"
        confidence = max(confidence, 0.84)
    if re.search(r"(?i)^\s*s[iìí]\s*,\s*", updated):
        updated = re.sub(r"(?i)^\s*s[iìí]\s*,\s*", "sim, ", updated)
        reason = "si_sim" if not reason else f"{reason}+si_sim"
        confidence = max(confidence, 0.78)
    return updated, reason, confidence


def _replace_contextual_person(text: str, *, tracker: object | None) -> tuple[str, str, float]:
    folded = _fold(text)
    if "alegria" not in folded:
        return text, "", 0.0
    last_person = str(getattr(tracker, "last_person_name", "") or "").strip()
    if _fold(last_person) != "larissa":
        return text, "", 0.0
    memory_like = bool(re.search(r"\b(?:lembra|lembrar|sabe|falou|falei|ela|menina)\b", folded))
    if not memory_like:
        return text, "", 0.0
    updated = re.sub(r"(?i)\balegria\b", "Larissa", text)
    return updated, "contextual_person_larissa", 0.91


def normalize_transcript_contextual(
    text: str,
    *,
    tracker: object | None = None,
    source: str = "speech",
    turn_id: str | None = None,
) -> TranscriptNormalizationResult:
    original = str(text or "").strip()
    logger.info("TRANSCRIPT_NORMALIZER_INPUT source=%s turn_id=%s text=%r", source, turn_id or "none", original[:160])
    if not original:
        logger.info("TRANSCRIPT_NORMALIZER_SKIPPED reason=empty")
        return TranscriptNormalizationResult(original, False, "empty", 0.0)

    candidate, reason, confidence = _replace_panel_aliases(original)
    candidate, common_reason, common_confidence = _replace_common_stt(candidate)
    if common_reason:
        reason = f"{reason}+{common_reason}" if reason else common_reason
        confidence = max(confidence, common_confidence)
    candidate, person_reason, person_confidence = _replace_contextual_person(candidate, tracker=tracker)
    if person_reason:
        reason = f"{reason}+{person_reason}" if reason else person_reason
        confidence = max(confidence, person_confidence)

    if candidate != original and confidence >= 0.75:
        logger.info(
            "TRANSCRIPT_NORMALIZER_CANDIDATE reason=%s confidence=%.2f text=%r",
            reason,
            confidence,
            candidate[:160],
        )
        if "panel" in reason or "ativos" in reason:
            logger.info("PANEL_LIKE_STT_CORRECTED reason=%s text=%r", reason, candidate[:160])
        logger.info("TRANSCRIPT_NORMALIZER_APPLIED reason=%s confidence=%.2f", reason, confidence)
        return TranscriptNormalizationResult(candidate.strip(), True, reason, confidence)

    logger.info("TRANSCRIPT_NORMALIZER_SKIPPED reason=no_high_confidence_candidate")
    return TranscriptNormalizationResult(original, False, "no_high_confidence_candidate", confidence)


__all__ = ["TranscriptNormalizationResult", "normalize_transcript_contextual"]
