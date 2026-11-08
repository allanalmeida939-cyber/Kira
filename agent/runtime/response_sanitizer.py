from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_SYSTEM_NOTE_MARKERS = (
    "SYSTEM_NOTE_FOR_REASONING_ONLY:",
    "SYSTEM_NOTE:",
)

_INLINE_METADATA_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"contexto_usuario[_\w]*\s*[=:]\s*[^\n]+",
        r"\b(?:intensidade|confidence|categoria|score|tipo)\s*[=:]\s*[\d\w\.\-]+",
        r"</?thinking>",
        r"</?system>",
        r"</?internal>",
        r"<\|.*?\|>",
    )
)

# Etiqueta de nota do sistema imitada pelo Gemini no começo da fala ("[WHATSAPP ENVIADO] {...}", "[VOZ SALVA
# para Chefe]", sessões de 2026-10-23/24) e o JSON que às vezes vem junto.
# Sem IGNORECASE: só etiqueta em MAIÚSCULAS ("[Racionais]", "[2 x 1]" na fala ficam).
_SYSTEM_TAG_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"\[(?:[A-ZÁÉÍÓÚÂÊÔÃÕÇ][A-ZÁÉÍÓÚÂÊÔÃÕÇ_ ]{2,30})(?:[^\]\n]{0,60})\]",
    r"\{\s*\"[a-z_]+\"\s*:[^{}\n]{0,400}\}",
))

_LEADING_INTERNAL_SENTENCE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^\s*system_note(?:_for_reasoning_only)?[:\s]",
        r"^\s*contexto_usuario[_\w]*\s*[=:]",
        r"^\s*nao ha screen ?share ativo\b.*$",
        r"^\s*o usuario\b.*$",
        r"^\s*voce apenas\b.*$",
        r"^\s*vou\b.*$",
        r"^\s*manter\b.*$",
        r"^\s*nao verbalizar\b.*$",
        r"^\s*use isso apenas\b.*$",
    )
)

# Negrito de markdown que o Gemini escreve na transcrição ("**Clima:** em Fortaleza..."): a tela mostrava os asteriscos.
_MARKDOWN_BOLD_RE = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")

_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_MULTIBREAK_RE = re.compile(r"\n{3,}")


def _normalize_text(raw_text: str) -> str:
    return str(raw_text or "").replace("\r\n", "\n").replace("\r", "\n")


def _looks_like_internal_sentence(sentence: str) -> bool:
    stripped = str(sentence or "").strip()
    if not stripped:
        return False
    return any(pattern.search(stripped) for pattern in _LEADING_INTERNAL_SENTENCE_PATTERNS)


def _strip_leading_internal_sentences(block: str) -> str:
    stripped = str(block or "").strip()
    if not stripped:
        return ""
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    kept_lines: list[str] = []
    dropping_lines = True
    for line in lines:
        if dropping_lines and _looks_like_internal_sentence(line):
            continue
        dropping_lines = False
        kept_lines.append(line)
    if kept_lines:
        return " ".join(kept_lines).strip()
    sentences = re.split(r"(?<=[.!?])\s+", stripped)
    kept: list[str] = []
    dropping = True
    for sentence in sentences:
        candidate = sentence.strip()
        if not candidate:
            continue
        if dropping and _looks_like_internal_sentence(candidate):
            continue
        dropping = False
        kept.append(candidate)
    return " ".join(kept).strip()


def _strip_system_note_blocks(text: str) -> str:
    cleaned = text
    upper_cleaned = cleaned.upper()
    while True:
        marker_pos = -1
        marker_text = ""
        for marker in _SYSTEM_NOTE_MARKERS:
            pos = upper_cleaned.find(marker.upper())
            if pos != -1 and (marker_pos == -1 or pos < marker_pos):
                marker_pos = pos
                marker_text = marker
        if marker_pos == -1:
            return cleaned

        prefix = cleaned[:marker_pos].rstrip()
        suffix = cleaned[marker_pos + len(marker_text):].lstrip()
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", suffix) if part.strip()]

        replacement_parts: list[str] = []
        if paragraphs:
            first_visible = _strip_leading_internal_sentences(paragraphs[0])
            if first_visible:
                replacement_parts.append(first_visible)
            if len(paragraphs) > 1:
                replacement_parts.extend(paragraphs[1:])
        replacement = "\n\n".join(part for part in replacement_parts if part).strip()

        if prefix and replacement:
            cleaned = f"{prefix}\n\n{replacement}"
        elif prefix:
            cleaned = prefix
        else:
            cleaned = replacement
        upper_cleaned = cleaned.upper()


def sanitize_gemini_response(raw_text: str) -> str:
    """
    Remove conteudo interno/tecnico e preserva apenas a fala visivel da Kira.
    """
    normalized = _normalize_text(raw_text)
    if not normalized.strip():
        return ""

    cleaned = _strip_system_note_blocks(normalized)
    for pattern in _INLINE_METADATA_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    for pattern in _SYSTEM_TAG_PATTERNS:
        cleaned = pattern.sub("", cleaned)

    lines = [_MULTISPACE_RE.sub(" ", line.strip()) for line in cleaned.split("\n")]
    cleaned = "\n".join(line for line in lines if line).strip()
    cleaned = _MULTIBREAK_RE.sub("\n\n", cleaned)

    if cleaned != normalized.strip():
        logger.warning(
            "[SANITIZE] Conteudo interno removido original=%d final=%d removido=%d",
            len(normalized),
            len(cleaned),
            max(len(normalized) - len(cleaned), 0),
        )
        logger.debug("[SANITIZE] Original=%r", normalized[:300])
        logger.debug("[SANITIZE] Limpo=%r", cleaned[:300])

    return cleaned


def extract_clean_speech(raw_text: str) -> str:
    """
    Extrai apenas a fala final de uma resposta mista do Gemini.
    """
    normalized = _normalize_text(raw_text).strip()
    sanitized = sanitize_gemini_response(raw_text)
    if not sanitized:
        logger.warning("[SANITIZE] Resposta vazia apos sanitizacao")
        return ""

    if sanitized == normalized:
        return _MARKDOWN_BOLD_RE.sub(r"\2", sanitized)

    paragraphs = [part.strip() for part in sanitized.split("\n\n") if part.strip()]
    if len(paragraphs) > 1:
        final_speech = paragraphs[-1]
        logger.info(
            "[SANITIZE] Multiplos paragrafos apos limpeza=%d extraindo_ultimo",
            len(paragraphs),
        )
        return _MARKDOWN_BOLD_RE.sub(r"\2", final_speech)
    return _MARKDOWN_BOLD_RE.sub(r"\2", sanitized)


__all__ = [
    "sanitize_gemini_response",
    "extract_clean_speech",
]
