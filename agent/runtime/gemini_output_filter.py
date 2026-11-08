"""
Filtro de saída do Gemini - Remove conteúdo interno da resposta.

CRITICAL: Este filtro é a ÚNICA linha de defesa contra vazamento de
conteúdo técnico na UI. NUNCA publicar resposta do Gemini sem passar
por este filtro.
"""
from __future__ import annotations

import logging
import re

from runtime.response_sanitizer import extract_clean_speech

logger = logging.getLogger(__name__)


def strip_system_notes(text: str) -> str:
    """
    Compat wrapper para a etapa de remoção de SYSTEM_NOTE.
    """
    return extract_clean_speech(text)


def strip_technical_metadata(text: str) -> str:
    """
    Compat wrapper para a etapa de remoção de metadados técnicos.
    """
    return extract_clean_speech(text)


def extract_clean_response(raw_text: str) -> str:
    """
    Extrai resposta limpa da Kira removendo TODO conteúdo técnico.
    """
    if not raw_text:
        return ""

    original = str(raw_text or "")
    logger.debug(
        "[FILTER] Input - len=%d preview=%r",
        len(original),
        original[:200],
    )

    if "SYSTEM_NOTE" in original.upper():
        logger.warning(
            "[FILTER] BLOQUEANDO resposta com SYSTEM_NOTE - len=%d preview=%r",
            len(original),
            original[:150],
        )
        logger.debug("[FILTER] Texto bloqueado completo: %r", original)
        return ""

    cleaned = extract_clean_speech(original)
    if not cleaned:
        return ""

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned)
    cleaned = re.sub(r";\s*;", ";", cleaned)
    cleaned = re.sub(r"^;\s*", "", cleaned, flags=re.MULTILINE)
    lines = [line.strip(" ;") for line in cleaned.split("\n")]
    lines = [line for line in lines if line and line not in {".", ","}]
    if not lines:
        logger.warning("[FILTER] Resposta vazia após limpeza final - original=%r", original[:200])
        return ""

    final_speech = lines[-1].strip()
    if any(
        keyword in final_speech.lower()
        for keyword in ("system_note", "contexto_usuario", "intensidade=", "confidence=", "reasoning_only")
    ):
        logger.error(
            "[FILTER] FALHA - Conteúdo técnico ainda presente após limpeza: %r",
            final_speech[:200],
        )
        if len(lines) > 1:
            final_speech = lines[-2].strip()
        else:
            return ""
    logger.info(
        "[FILTER] Output limpo - len=%d texto=%r",
        len(final_speech),
        final_speech[:200],
    )
    return final_speech


__all__ = ["extract_clean_response", "strip_system_notes", "strip_technical_metadata"]
