from __future__ import annotations

from dataclasses import dataclass
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScreenshotCommand:
    mode: str
    raw_text: str
    normalized_text: str
    monitor_id: int | None = None
    all_monitors: bool = False
    confidence_score: float = 0.0
    reason: str = ""
    handled: bool = True
    speak: bool = False


def normalize_screenshot_text(text: str) -> str:
    value = str(text or "").strip().lower()
    value = unicodedata.normalize("NFD", value)
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"^(?:ei\s+|hey\s+|ok\s+)?(?:kira\s+ai|kira|akira|kiri)\s+", "", value).strip()


def _monitor_id(text: str) -> int | None:
    word_ids = {"um": 1, "dois": 2, "tres": 3, "quatro": 4}
    match = re.search(r"\b(?:monitor|tela)\s+(um|dois|tres|quatro|[1-9]\d*)\b", text)
    if not match:
        return None
    value = match.group(1)
    return word_ids.get(value, int(value) if value.isdigit() else None)


def detect_screenshot_command(text: str) -> ScreenshotCommand | None:
    normalized = normalize_screenshot_text(text)
    if not normalized:
        return None
    screenshot_terms = (
        "print",
        "screenshot",
        "printscreen",
        "print screen",
        "captura a tela",
        "captura de tela",
    )
    observe_patterns = (
        r"\bo\s+que\s+(?:voce|vc|ce)\s+(?:esta|ta)\s+vendo\b",
        r"\bo\s+que\s+e\s+que\s+(?:voce|vc|ce)\s+(?:esta|ta)\s+vendo\b",
        r"\bo\s+que\s+(?:voce|vc|ce)\s+ve\b",
        r"\bo\s+que\s+(?:esta|ta)\s+vendo\b",
        r"\bo\s+que\s+e\s+que\s+(?:esta|ta)\s+vendo\b",
        r"\b(?:voce|vc|ce)\s+(?:esta|ta)\s+vendo\s+a\s+tela\b",
        r"\bo\s+que\s+aparece\s+na\s+tela\b",
        r"\bo\s+que\s+tem\s+na\s+tela\b",
        r"\b(?:le|ler)\s+a\s+tela\b",
        r"\bdescrev[ea]\s+a\s+tela\b",
        r"\bve\s+o\s+bloco\s+de\s+notas\b",
        r"\bo\s+que\s+esta\s+escrito\s+(?:no\s+bloco\s+de\s+notas|ai)\b",
        r"\b(?:voce|vc|ce)\s+consegue\s+ver\s+o\s+que\s+esta\s+escrito\b",
        r"\bme\s+mostra\s+a\s+tela\b",
        r"\bobserva\s+a\s+tela\b",
        r"\bve\s+minha\s+tela\b",
        r"\bve\s+o\s+que\s+tem\s+na\s+tela\b",
        r"\bolha\s+minha\s+tela\b",
        r"\bme\s+diz\s+o\s+que\s+(?:aparece|tem)\s+na\s+tela\b",
        r"\bmostra\s+(?:o\s+)?monitor\b",
        r"\bmostra\s+meus\s+monitores\b",
    )
    has_screenshot = any(term in normalized for term in screenshot_terms)
    has_observe = any(re.search(pattern, normalized) for pattern in observe_patterns)
    if not has_screenshot and not has_observe:
        return None
    all_monitors = bool(re.search(r"\b(?:monitores|todos\s+os\s+monitores|todas\s+as\s+telas)\b", normalized))
    monitor_id = _monitor_id(normalized)
    if has_observe:
        mode = "observe_only"
        reason = "observe_phrase"
    elif re.search(r"\b(?:salva|salvar|guarda|guardar)\b", normalized):
        mode = "capture_and_save"
        reason = "save_requested"
    elif re.search(r"\b(?:copiar|copia|clipboard|area\s+de\s+transferencia|deixa\s+no\s+copiar)\b", normalized):
        mode = "capture_and_clipboard"
        reason = "clipboard_requested"
    elif all_monitors:
        mode = "capture_all_monitors"
        reason = "all_monitors_requested"
    elif monitor_id is not None:
        mode = "capture_monitor"
        reason = "monitor_requested"
    else:
        mode = "capture_only"
        reason = "capture_requested"
    command = ScreenshotCommand(
        mode=mode,
        raw_text=str(text or ""),
        normalized_text=normalized,
        monitor_id=monitor_id,
        all_monitors=all_monitors,
        confidence_score=0.95 if has_screenshot else 0.88,
        reason=reason,
    )
    logger.info("SCREENSHOT_GATE_MATCHED reason=%s confidence=%.2f", command.reason, command.confidence_score)
    logger.info("SCREENSHOT_COMMAND_PARSED mode=%s monitor=%s", command.mode, command.monitor_id or "default")
    return command


__all__ = ["ScreenshotCommand", "detect_screenshot_command", "normalize_screenshot_text"]
