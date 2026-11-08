from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
import logging


SAFETY_RESPONSE_PT_BR = (
    "Eu sinto muito que voce esteja passando por isso. Se houver risco imediato, ligue 188 no CVV "
    "ou 192/190 agora. Fique perto de alguem de confianca e me diga: voce esta em perigo neste momento?"
)
ADULT_SAFE_RESPONSE_PT_BR = (
    "Entendi, chefe. Se voce quiser conversar sobre relacionamento, consentimento ou algo mais pessoal "
    "de forma respeitosa, eu posso te ouvir."
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SafetyGateResult:
    matched: bool
    reason: str = "no_crisis_pattern"
    response: str = ""


def _normalize(text: str) -> str:
    lowered = str(text or "").strip().lower()
    decomposed = unicodedata.normalize("NFKD", lowered)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


_CRISIS_PATTERNS = (
    re.compile(r"\b(eu\s+)?(vou|quero|queria|preciso)\s+(me\s+)?(matar|morrer|suicidar)\b"),
    re.compile(r"\bnao\s+quero\s+mais\s+viver\b"),
    re.compile(r"\bquero\s+sumir\s+para\s+sempre\b"),
    re.compile(r"\bpretendo\s+(me\s+)?(matar|suicidar)\b"),
    re.compile(r"\bminha\s+vida\s+nao\s+vale\s+(nada|a\s+pena)\b"),
)
_ADULT_SAFE_PATTERNS = (
    re.compile(r"\b(eu\s+)?quero\s+transar\b"),
    re.compile(r"\bquero\s+fazer\s+sexo\b"),
)


def check_safety_gate(text: str) -> SafetyGateResult:
    normalized = _normalize(text)
    if not normalized:
        return SafetyGateResult(False, "empty_text", "")
    for pattern in _CRISIS_PATTERNS:
        if pattern.search(normalized):
            return SafetyGateResult(True, "crisis_pattern", SAFETY_RESPONSE_PT_BR)
    for pattern in _ADULT_SAFE_PATTERNS:
        if pattern.search(normalized):
            logger.info("ADULT_SAFE_RESPONSE_ROUTE reason=clear_adult_non_explicit")
            return SafetyGateResult(True, "adult_safe_response", ADULT_SAFE_RESPONSE_PT_BR)
    return SafetyGateResult(False, "no_crisis_pattern", "")
