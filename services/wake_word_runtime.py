from __future__ import annotations

import re
import unicodedata


_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_wake_text(text: str) -> str:
    raw = str(text or "").strip().lower()
    decomposed = unicodedata.normalize("NFKD", raw)
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    ascii_text = re.sub(r"[^a-z0-9\s]", " ", ascii_text)
    return " ".join(ascii_text.split())


def _normalized_token_spans(text: str) -> list[tuple[str, int, int]]:
    tokens: list[tuple[str, int, int]] = []
    for match in _TOKEN_RE.finditer(str(text or "")):
        normalized = normalize_wake_text(match.group(0))
        if normalized:
            tokens.append((normalized, match.start(), match.end()))
    return tokens


def _wake_candidates(wake_words: list[str] | tuple[str, ...]) -> list[tuple[str, list[str]]]:
    candidates: list[tuple[str, list[str]]] = []
    for word in wake_words:
        raw_word = str(word or "")
        normalized_tokens = normalize_wake_text(raw_word).split()
        if normalized_tokens:
            candidates.append((raw_word, normalized_tokens))
    return sorted(candidates, key=lambda item: (len(item[1]), len(" ".join(item[1]))), reverse=True)


def _strip_query_prefix(text: str) -> str:
    return re.sub(r"^[\s,;:.!?-]+", "", str(text or "")).strip()


def extract_wake_query(text: str, wake_words: list[str] | tuple[str, ...]) -> dict[str, object]:
    raw_text = str(text or "")
    tokens = _normalized_token_spans(raw_text)
    if not tokens:
        return {
            "detected": False,
            "wake_word": "",
            "word": "",
            "remaining_user_query": "",
            "wake_with_query": False,
        }

    normalized_tokens = [token for token, _, _ in tokens]
    for wake_word, wake_tokens in _wake_candidates(wake_words):
        wake_len = len(wake_tokens)
        for start_index in range(0, len(tokens) - wake_len + 1):
            if normalized_tokens[start_index : start_index + wake_len] != wake_tokens:
                continue
            end_index = start_index + wake_len - 1
            query = _strip_query_prefix(raw_text[tokens[end_index][2] :])
            return {
                "detected": True,
                "wake_word": wake_word,
                "word": wake_word,
                "remaining_user_query": query,
                "wake_with_query": bool(query),
            }

    return {
        "detected": False,
        "wake_word": "",
        "word": "",
        "remaining_user_query": "",
        "wake_with_query": False,
    }


def detect_wake_word(text: str, wake_words: list[str] | tuple[str, ...]) -> str | None:
    result = extract_wake_query(text, wake_words)
    return str(result.get("wake_word") or "") or None
