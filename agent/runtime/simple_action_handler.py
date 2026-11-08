from __future__ import annotations

import logging
import re
import unicodedata

if __package__ and __package__.startswith("agent"):
    from agent.runtime.action_controller import ActionController
else:  # pragma: no cover - suporte a execucao direta em agent/
    from runtime.action_controller import ActionController


logger = logging.getLogger(__name__)


def _normalize_text(text: str) -> str:
    raw = str(text or "").strip().lower()
    folded = unicodedata.normalize("NFD", raw)
    no_accents = "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", no_accents).strip()


class SimpleActionHandler:
    """Executa acoes simples sem precisar do Gemini."""

    SIMPLE_COMMANDS = {
        r"\b(?:aperta|pressiona)\s+enter\b|\benter\b": "enter",
        r"\b(?:aperta|pressiona)\s+esc\b|\b(?:aperta|pressiona)\s+escape\b|\besc\b|\bescape\b": "escape",
        r"\b(?:aperta|pressiona)\s+tab\b|\btab\b": "tab",
        r"\b(?:aperta|pressiona)\s+espaco\b|\bespaco\b|\bspace\b": "space",
        r"\b(?:aperta|pressiona)\s+backspace\b|\bbackspace\b|\bapaga\b": "backspace",
        r"\bctrl\+c\b|\bcontrol\+c\b|\bcopia\b": "ctrl+c",
        r"\bctrl\+v\b|\bcontrol\+v\b|\bcola\b": "ctrl+v",
        r"\bctrl\+x\b|\bcontrol\+x\b|\brecorta\b": "ctrl+x",
        r"\bctrl\+z\b|\bcontrol\+z\b|\bdesfaz\b": "ctrl+z",
        r"\bctrl\+a\b|\bcontrol\+a\b|\bseleciona tudo\b": "ctrl+a",
        r"\bfecha janela\b|\balt\+f4\b": "alt+f4",
        r"\bmaximiza janela\b|\bmaximizar janela\b": "win+up",
        r"\bminimiza janela\b|\bminimizar janela\b": "win+down",
    }

    def __init__(self, action_controller: ActionController) -> None:
        self.action_controller = action_controller
        self._patterns = {
            re.compile(pattern, re.IGNORECASE): action
            for pattern, action in self.SIMPLE_COMMANDS.items()
        }
        logger.info(
            "[SIMPLE_ACTION] Inicializado - commands=%d",
            len(self._patterns),
        )

    def try_execute(self, texto: str) -> bool:
        normalized = _normalize_text(texto)
        if not normalized:
            logger.debug("[SIMPLE_ACTION] Nao reconhecido - texto=%r", str(texto or "")[:50])
            return False

        for pattern, action in self._patterns.items():
            if not pattern.search(normalized):
                continue
            logger.info(
                "[SIMPLE_ACTION] Matched - texto=%r action=%r",
                str(texto or "")[:50],
                action,
            )
            success = self.action_controller.press_key(action)
            if success:
                logger.info("[SIMPLE_ACTION] Executado - action=%r", action)
                return True
            logger.warning("[SIMPLE_ACTION] Falhou - action=%r", action)
            return False

        logger.debug("[SIMPLE_ACTION] Nao reconhecido - texto=%r", str(texto or "")[:50])
        return False


__all__ = ["SimpleActionHandler"]
