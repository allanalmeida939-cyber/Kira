from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SafetyNetDecision:
    triggered: bool
    target: str = ""
    confidence_score: float = 0.0
    keywords: tuple[str, ...] = ()
    reason: str = ""


_TARGET_KEYWORDS = {
    "ui_panel": ("sistema vital", "status do sistema", "transcricao", "painel", "mostrar painel", "ocultar painel"),
    "artifact": ("pdf", "arquivo", "pasta", "paste", "parte do pdf", "onde salvou", "onde esta", "abre a pasta", "abre o arquivo"),
    "desktop_operator": (
        "bloco de notas",
        "bloco de nota",
        "bloco notas",
        "notepad",
        "copiar",
        "copia",
        "colar",
        "cola",
        "digitar",
        "digita",
        "escrever",
        "escreve",
        "monitor",
        "tela",
        "janela",
        "clicar",
        "clica",
        "mouse",
        "teclado",
    ),
    "screenshot": ("print", "screenshot", "captura de tela", "printscreen", "print screen"),
    "volume": ("volume", "som", "audio", "aumentar", "aumenta", "diminuir", "diminui", "mutar", "muta", "desmutar"),
    "browser": ("navegador", "site", "aba", "abrir google", "abre google", "pesquisar"),
}

_QUESTION_ALLOWLIST = (
    r"\b(?:me\s+)?explica\b",
    r"\bo\s+que\s+e\b",
    r"\bqual\s+e\b",
    r"\bcuriosidade\b",
)


def normalize_operational_text(text: str) -> str:
    value = str(text or "").strip().lower()
    value = unicodedata.normalize("NFD", value)
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"^(?:ei\s+|hey\s+|ok\s+)?(?:kira\s+ai|kira|akira|kiri)\s+", "", value).strip()


def _matched_keywords(normalized: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    padded = f" {normalized} "
    for target, keywords in _TARGET_KEYWORDS.items():
        matches = [keyword for keyword in keywords if f" {keyword} " in padded or keyword in normalized]
        if matches:
            found[target] = matches
    return found


def detect_operational_safety_net(text: str) -> SafetyNetDecision:
    normalized = normalize_operational_text(text)
    if not normalized:
        return SafetyNetDecision(False)
    matches = _matched_keywords(normalized)
    if not matches:
        return SafetyNetDecision(False)
    if any(re.search(pattern, normalized) for pattern in _QUESTION_ALLOWLIST):
        weak_only = set(matches) <= {"artifact", "desktop_operator"}
        if weak_only and not re.search(r"\b(?:abre|abrir|cola|copiar|copia|digita|escreve|clica|print|captura|volume)\b", normalized):
            return SafetyNetDecision(False)
    priority = ("screenshot", "ui_panel", "volume", "artifact", "desktop_operator", "browser")
    target = next((item for item in priority if item in matches), "")
    keywords = tuple(matches.get(target, ()))
    actionish = bool(re.search(r"\b(?:abre|abrir|mostra|oculta|cola|copiar|copia|digita|escreve|clica|tira|faz|captura|aumenta|diminui|muta|pesquisa)\b", normalized))
    confidence = 0.72 if actionish else 0.42
    if target == "screenshot":
        confidence = max(confidence, 0.82)
    logger.info("KEYWORD_SAFETY_NET_TRIGGERED keywords=%s", list(keywords))
    logger.info("KEYWORD_SAFETY_NET_ROUTE target=%s confidence=%.2f", target, confidence)
    return SafetyNetDecision(True, target, confidence, keywords, "operational_keyword")


def log_normal_reply_anchor_alert(text: str) -> None:
    decision = detect_operational_safety_net(text)
    if decision.triggered:
        logger.warning(
            "KEYWORD_ANCHOR_NORMAL_REPLY_ALERT text=%r anchors=%s route=gemini_normal",
            str(text or "")[:180],
            list(decision.keywords),
        )


__all__ = ["SafetyNetDecision", "detect_operational_safety_net", "log_normal_reply_anchor_alert"]
