from __future__ import annotations

import logging
import re
import unicodedata

if __package__ and __package__.startswith("agent"):
    from agent.runtime.gemini_output_filter import extract_clean_response
    from agent.user_visible_output import sanitize_user_visible_kira_output
else:  # pragma: no cover - suporte a execucao direta em agent/
    from runtime.gemini_output_filter import extract_clean_response
    from user_visible_output import sanitize_user_visible_kira_output

logger = logging.getLogger(__name__)

_USER_SOURCES = frozenset({"user", "usuario", "voice", "mic", "speech", "transcript"})

_INTERNAL_MARKERS = frozenset(
    {
        "SYSTEM_NOTE",
        "SYSTEM_NOTE_FOR_REASONING_ONLY",
        "RUNTIME_EXCEPTION",
        "INTERNAL_NOTE",
        "REASONING_ONLY",
        "DO_NOT_SHOW",
        "INTERNAL_CONTEXT",
        "INSTRUCTION:",
        "PROMPT:",
        "<THINKING>",
        "</THINKING>",
        "<SYSTEM>",
        "</SYSTEM>",
    }
)

_INTERNAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"contexto_usuario[_\w]*\s*=",
        r"contexto_usuario[_\w]*\s*:",
        r"intensidade[_\w]*\s*=",
        r"confidence[_\w]*\s*=",
        r"<\|.*?\|>",
        r"\[INTERNAL:.*?\]",
        r"\[DEBUG:.*?\]",
        r"__\w+__\s*=",
    )
)

_INLINE_CLEANUP_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"</?thinking>",
        r"</?system>",
        r"</?internal>",
        r"<\|.*?\|>",
    )
)

_FALLBACK_PHRASES = frozenset(
    {
        "deu um branco",
        "tenta mais uma vez",
        "não consegui fechar essa resposta",
        "nao consegui fechar essa resposta",
        "me manda de novo",
        "to aqui, chefe. pode repetir",
        "tô aqui, chefe. pode repetir",
        "nao consegui processar",
        "erro ao gerar",
        "falha na geracao",
    }
)

_EMPTY_INPUT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^\s*$",
        r"^\s*[\.,;:!?\-_/\\|@#$%^&*()\[\]{}\"']+\s*$",
    )
)

_NOISE_INPUT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^\s*<\s*noise\s*>\s*$",
        r"^\s*\(\s*noise\s*\)\s*$",
        r"^\s*\[\s*noise\s*\]\s*$",
    )
)



_FILLER_INPUTS = frozenset(
    {
        "ah",
        "ahn",
        "eh",
        "hm",
        "hum",
        "humm",
        "hmmm",
        "hmmmm",
        "uh",
        "uhum",
        "um",
        "umm",
    }
)

# ── Respostas / confirmacoes / negacoes / reconhecimentos curtos ───────────────
# Lista GENEROSA de falas curtas do dia a dia que devem SEMPRE contar como fala
# legitima (nao-ruido) e como portugues. Qualquer uma destas, sozinha, passa pelo
# filtro e chega ao roteamento/gate (inclui as confirmacoes de envio do WhatsApp:
# "manda", "pode mandar", "vai", "isso", "aham sei"...). Unida tanto na whitelist
# de comandos curtos quanto nas palavras-sinal de portugues.
# OBS: nao incluir aqui os fillers puros de ruido (ah, ahn, eh, hm, hum, uh, um,
# uhum) — esses continuam sendo descartados de proposito.
_AFFIRMATION_AND_REPLY_WORDS = frozenset(
    {
        # Afirmativas / confirmacoes
        "sim", "isso", "exato", "exatamente", "certo", "correto", "claro",
        "obvio", "óbvio", "certeza", "positivo", "afirmativo", "concordo",
        "aprovado", "aprova", "aprovo", "autorizo", "autorizado", "libera",
        "liberado", "pode", "podes", "manda", "mandar", "mande", "mando",
        "envia", "enviar", "envie", "confirma", "confirmar", "confirmo",
        "confirmado", "vai", "vamos", "vamo", "bora", "partiu", "dale",
        "dahora", "daora", "fechado", "fechou", "fecho", "beleza", "bele",
        "belê", "blz", "tranquilo", "suave", "ok", "okay", "okey", "oka",
        "ta", "tá", "otimo", "ótimo", "perfeito", "show", "valeu", "vlw",
        "segue", "segura", "agora", "ja", "já", "feito", "faz", "faça", "faca",
        "simbora", "uhul",
        # Reconhecimentos ("entendi" / "sei")
        "aham", "ahã", "aha", "sei", "saquei", "captei", "entendi",
        "entendido", "anotado", "boa", "show",
        # Negativas / cancelamento
        "nao", "não", "nops", "nunca", "jamais", "cancela", "cancelar",
        "deixa", "esquece", "aborta", "negativo",
        # Saudacoes / chamadas curtas
        "opa", "eai", "salve", "alo", "alô", "fala",
        # Frases curtas comuns (entram inteiras e por token)
        "pode mandar", "manda logo", "manda ai", "manda aí", "isso mesmo",
        "ta bom", "tá bom", "ta certo", "tá certo", "pode ser", "com certeza",
        "aham sei", "sei sim", "vai la", "vai lá", "bora la", "bora lá",
        "claro que sim", "pode sim", "sim manda", "deixa pra la", "deixa pra lá",
        "deixa quieto", "nao precisa", "não precisa", "pode crer", "ta certo isso",
    }
)

_SHORT_TRANSCRIPT_WHITELIST = frozenset(
    {
        "sim", "nao", "não", "ok", "pode", "cancela", "cancelar", "pausa", "pause",
        "parar", "para", "continua", "continuar", "volta", "voltar", "abre", "abrir",
        "fecha", "fechar", "muta", "mudo", "desmuta", "volume", "print", "screenshot",
        "oi", "ola", "olá", "kira", "akira", "ajuda",
        # FASE 4 — comandos de controle do navegador (aba aberta)
        "desce", "descer", "sobe", "subir", "proximo", "próximo", "proxima", "próxima",
        "anterior", "clica", "clique", "pula", "pular", "legenda", "play",
    }
    # Respostas/confirmacoes/negacoes curtas (lista generosa compartilhada).
    | _AFFIRMATION_AND_REPLY_WORDS
)

_PORTUGUESE_SIGNAL_WORDS = frozenset(
    {
        "o", "a", "os", "as", "um", "uma", "de", "do", "da", "para", "pra", "por",
        "com", "eu", "voce", "você", "kira", "abre", "mostra", "oculta", "fecha",
        "cria", "faz", "manda", "mensagem", "pasta", "arquivo", "pdf", "texto",
        "volume", "status", "sistema", "transcricao", "transcrição", "print", "tela",
        "sim", "nao", "não", "pausa", "continua", "cancelar", "ajuda",
        "bom", "boa", "dia", "tarde", "noite", "chefe", "qual", "capital", "brasil",
        "quero", "preciso", "sobre", "me", "diz", "fala", "obrigado", "tudo", "bem",
        "como", "que", "e", "vai", "vou", "opa", "ta", "ai",
        # preposições comuns + âncoras de comando/navegador (issue 4: comandos válidos como
        # "Coloca Marília Mendonça no YouTube" eram rejeitados por não ter token PT reconhecido).
        "no", "na", "nos", "nas", "em",
        "coloca", "coloque", "poe", "põe", "bota", "toca", "toque", "pesquisa", "pesquise",
        "procura", "procure", "busca", "abrir", "navegador", "site", "google", "internet",
        "youtube", "video", "vídeo", "musica", "música", "canal",
        "desce", "desça", "sobe", "sobre", "clica", "clique", "proximo", "próximo", "anterior",
    }
    # Respostas/confirmacoes/negacoes curtas contam como portugues (senao
    # "Pode mandar."/"Manda."/"Sei." caiam em no_portuguese_tokens).
    | _AFFIRMATION_AND_REPLY_WORDS
)


def _fold_text(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text or "").strip().lower())
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def _word_tokens(text: str) -> list[str]:
    return re.findall(r"[a-zA-ZÀ-ÿ0-9]+", str(text or "").lower())


def is_short_transcript_allowed(text: str) -> bool:
    folded = _fold_text(text)
    tokens = _word_tokens(folded)
    if len(tokens) >= 3:
        return True
    if "kira" in tokens:
        return True
    # FASE 4 — casa por TOKEN (ignora pontuacao do STT: "Fecha." -> token "fecha").
    # Antes era `folded in whitelist`, que falhava com ponto final.
    wl_tokens = {tok for item in _SHORT_TRANSCRIPT_WHITELIST for tok in _word_tokens(_fold_text(item))}
    if any(tok in wl_tokens for tok in tokens):
        return True
    return folded in {_fold_text(item) for item in _SHORT_TRANSCRIPT_WHITELIST}


def has_suspicious_non_latin_chars(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped:
        return False
    if _non_latin_char_ratio(stripped) > 0.20:
        return True
    useful = _useful_char_count(stripped)
    if useful <= 2 and _fold_text(stripped) not in {_fold_text(item) for item in _SHORT_TRANSCRIPT_WHITELIST}:
        return True
    return False


def has_portuguese_signal(text: str) -> bool:
    folded_tokens = {_fold_text(token) for token in _word_tokens(text)}
    if not folded_tokens:
        return False
    if _fold_text(text) in {_fold_text(item) for item in _SHORT_TRANSCRIPT_WHITELIST}:
        return True
    folded_pt = {_fold_text(item) for item in _PORTUGUESE_SIGNAL_WORDS}
    if folded_tokens & folded_pt:
        return True
    return any(token in {"pdf", "txt", "ok"} for token in folded_tokens)


def _preview(text: str, *, limit: int = 100) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3]}..."


def _normalize_channel(value: str | None, *, fallback: str) -> str:
    return str(value or fallback).strip().lower() or fallback


def _is_user_channel(*, source: str | None = None, speaker: str | None = None) -> bool:
    normalized_source = _normalize_channel(source, fallback="")
    normalized_speaker = _normalize_channel(speaker, fallback="")
    if normalized_speaker in {"user", "voce", "você"}:
        return True
    return normalized_source in _USER_SOURCES


def is_internal_content(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped:
        return False
    upper = stripped.upper()
    if any(marker in upper for marker in _INTERNAL_MARKERS):
        return True
    return any(pattern.search(stripped) for pattern in _INTERNAL_PATTERNS)


def is_fallback_message(text: str, source: str | None = None) -> bool:
    stripped = str(text or "").strip()
    if not stripped:
        return False
    normalized_source = _normalize_channel(source, fallback="")
    if normalized_source in {"fallback", "error_recovery", "voice_failure", "audio_confirmation_failed"}:
        return True
    lowered = stripped.lower()
    return any(phrase in lowered for phrase in _FALLBACK_PHRASES)


def is_empty_user_input(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped:
        return True
    if any(pattern.match(stripped) for pattern in _NOISE_INPUT_PATTERNS):
        return True
    if any(pattern.match(stripped) for pattern in _EMPTY_INPUT_PATTERNS):
        return True
    lowered = stripped.lower()
    if lowered in _FILLER_INPUTS:
        return True
    if len(stripped) == 1 and not stripped.isalnum():
        return True
    if stripped and not any(c.isalnum() for c in stripped):
        return True
    return False


_REAL_SHORT_WORDS = frozenset(
    {
        "sim", "nao", "não", "ok", "oi", "e", "é", "ta", "tá", "pode",
        "manda", "deu", "vai", "vem", "faz", "bom", "boa", "né", "ne",
        "aqui", "ali", "ja", "já", "ai", "aí", "isso", "nao",
    }
)

_NON_LATIN_SCRIPT_RANGES: tuple[tuple[int, int], ...] = (
    (0x0400, 0x04FF),  # Cyrillic
    (0x0590, 0x05FF),  # Hebrew
    (0x0600, 0x06FF),  # Arabic
    (0x3040, 0x309F),  # Hiragana
    (0x30A0, 0x30FF),  # Katakana
    (0x3400, 0x4DBF),  # CJK Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xAC00, 0xD7AF),  # Hangul syllables
)


def _useful_char_count(text: str) -> int:
    return sum(1 for c in text if c.isalpha() or c.isdigit())


def _is_latin_like_char(char: str) -> bool:
    if not char:
        return False
    try:
        name = char.encode("ascii", "ignore")
    except Exception:
        name = b""
    if name:
        return char.isalpha()
    codepoint = ord(char)
    return (
        ("\u00C0" <= char <= "\u024F")
        or ("\u1E00" <= char <= "\u1EFF")
        or codepoint in {0x00AA, 0x00BA}
    )


def _non_latin_char_ratio(text: str) -> float:
    useful_chars = [char for char in str(text or "") if char.isalpha()]
    if not useful_chars:
        return 0.0
    non_latin = 0
    for char in useful_chars:
        codepoint = ord(char)
        if any(start <= codepoint <= end for start, end in _NON_LATIN_SCRIPT_RANGES):
            non_latin += 1
            continue
        if not _is_latin_like_char(char):
            non_latin += 1
    return non_latin / max(len(useful_chars), 1)


def _looks_like_wrong_language_transcript(text: str) -> bool:
    stripped = str(text or "").strip()
    useful = _useful_char_count(stripped)
    if useful < 3:
        return False
    lowered = stripped.lower()
    if lowered in _REAL_SHORT_WORDS:
        return False
    return _non_latin_char_ratio(stripped) > 0.30


def is_empty_user_input_with_reason(text: str) -> tuple[bool, str]:
    """Return (is_empty, reason). Use for diagnostic logging."""
    stripped = str(text or "").strip()
    if not stripped:
        return True, "empty"
    if any(pattern.match(stripped) for pattern in _NOISE_INPUT_PATTERNS):
        return True, "noise_transcript"
    if any(pattern.match(stripped) for pattern in _EMPTY_INPUT_PATTERNS):
        return True, "punctuation_only"
    lowered = stripped.lower()
    if lowered in _FILLER_INPUTS:
        return True, "filler_word"
    if len(stripped) == 1 and not stripped.isalnum():
        return True, "single_symbol"
    if not any(c.isalnum() for c in stripped):
        return True, "no_signal_chars"
    useful = _useful_char_count(stripped)
    if useful < 2 and lowered not in _REAL_SHORT_WORDS:
        return True, "too_short"
    if _looks_like_wrong_language_transcript(stripped):
        return True, "wrong_language_non_latin"
    if has_suspicious_non_latin_chars(stripped):
        return True, "suspicious_non_latin"
    if not is_short_transcript_allowed(stripped):
        return True, "short_not_whitelisted"
    if not has_portuguese_signal(stripped):
        return True, "no_portuguese_tokens"
    return False, ""


def log_user_transcript_decision(text: str, *, accepted: bool, reason: str) -> None:
    preview = _preview(text, limit=60)
    if accepted:
        useful = _useful_char_count(str(text or "").strip())
        logger.info(
            "USER_TRANSCRIPT_ACCEPTED text_len=%d useful_chars=%d preview=%r",
            len(str(text or "").strip()),
            useful,
            preview,
        )
    elif reason == "punctuation_only":
        logger.info("USER_TRANSCRIPT_IGNORED_EMPTY reason=punctuation_only text=%r", preview)
    elif reason in ("empty", "single_symbol", "no_signal_chars"):
        logger.info("USER_TRANSCRIPT_IGNORED_EMPTY reason=%s text=%r", reason, preview)
    elif reason == "noise_transcript":
        logger.info("USER_TURN_DROPPED reason=noise_transcript text=%r", preview)
    elif reason in ("filler_word",):
        logger.info("USER_TRANSCRIPT_IGNORED_NO_SIGNAL reason=filler_word text=%r", preview)
    elif reason == "too_short":
        logger.info("USER_TRANSCRIPT_IGNORED_TOO_SHORT reason=too_short text=%r", preview)
    elif reason == "wrong_language_non_latin":
        logger.warning("USER_TRANSCRIPT_REJECTED_WRONG_LANGUAGE text_preview=%r reason=non_latin_detected", preview)
    elif reason == "short_not_whitelisted":
        logger.info("VOICE_INPUT_SHORT_REJECTED reason=not_in_short_command_whitelist text=%r", preview)
    elif reason == "no_portuguese_tokens":
        logger.info("VOICE_INPUT_LANGUAGE_REJECTED reason=no_portuguese_tokens text=%r", preview)
    elif reason == "suspicious_non_latin":
        logger.info("VOICE_INPUT_CHARSET_REJECTED reason=suspicious_non_latin text=%r", preview)
    else:
        logger.info("USER_TRANSCRIPT_IGNORED_EMPTY reason=%s text=%r", reason, preview)


def sanitize_for_transcript(
    text: str,
    *,
    source: str | None = None,
    speaker: str | None = None,
    block_fallback: bool = True,
) -> str | None:
    stripped = str(text or "").strip()
    if not stripped:
        return None

    if _is_user_channel(source=source, speaker=speaker):
        empty, reason = is_empty_user_input_with_reason(stripped)
        if empty:
            logger.info("[INPUT_FILTER] Bloqueando entrada vazia reason=%s texto=%r", reason, _preview(stripped))
            return None
        return stripped

    if is_internal_content(stripped):
        logger.warning(
            "INTERNAL_NOTE_OUTPUT_BLOCKED_EARLY source=%s text=%r",
            _normalize_channel(source, fallback="unknown"),
            _preview(stripped),
        )
        logger.warning(
            "INTERNAL_NOTE_MODEL_VIOLATION source=%s marker=internal_note",
            _normalize_channel(source, fallback="unknown"),
        )
        return None

    stripped = extract_clean_response(stripped)
    if not stripped:
        logger.warning(
            "[FILTER] Conteudo vazio apos sanitizacao source=%s",
            _normalize_channel(source, fallback="unknown"),
        )
        return None

    if block_fallback and is_fallback_message(stripped, source=source):
        logger.warning(
            "[FILTER] Bloqueando fallback source=%s texto=%r",
            _normalize_channel(source, fallback="unknown"),
            _preview(stripped),
        )
        return None

    sanitized = sanitize_user_visible_kira_output(stripped)
    cleaned_lines: list[str] = []
    removed_internal = False
    for raw_line in sanitized.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for pattern in _INLINE_CLEANUP_PATTERNS:
            line = pattern.sub("", line)
        line = line.strip()
        if not line:
            removed_internal = True
            continue
        if is_internal_content(line):
            removed_internal = True
            continue
        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    if not cleaned:
        logger.warning(
            "[FILTER] Bloqueando conteudo interno sem sobra source=%s texto=%r",
            _normalize_channel(source, fallback="unknown"),
            _preview(stripped),
        )
        return None
    if is_internal_content(cleaned):
        logger.warning(
            "[FILTER] Bloqueando conteudo tecnico residual source=%s texto=%r",
            _normalize_channel(source, fallback="unknown"),
            _preview(cleaned),
        )
        return None
    if removed_internal:
        logger.warning(
            "[FILTER] Conteudo interno removido source=%s texto=%r",
            _normalize_channel(source, fallback="unknown"),
            _preview(cleaned),
        )
    return cleaned


__all__ = [
    "is_empty_user_input",
    "is_empty_user_input_with_reason",
    "is_short_transcript_allowed",
    "has_suspicious_non_latin_chars",
    "has_portuguese_signal",
    "is_fallback_message",
    "is_internal_content",
    "log_user_transcript_decision",
    "sanitize_for_transcript",
]
