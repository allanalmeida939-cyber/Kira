from __future__ import annotations

import re


_INTERNAL_LINE_PATTERNS = (
    re.compile(r"^\s*\[KIRA\s+CONTEXT\].*$", re.IGNORECASE),
    re.compile(r"^\s*\[KIRA\s+EDIT(?:\s+DIFF)?\].*$", re.IGNORECASE),
    re.compile(r"^\s*(?:tipo|intensidade|humor|ctrl\d+|backup_created|edited|file_type|target|changed|summary|error)\s*=", re.IGNORECASE),
    re.compile(r"^\s*system[_\s-]?note\b.*$", re.IGNORECASE),
    re.compile(r"^\s*internal[_\s-]?meta\b.*$", re.IGNORECASE),
)

_INLINE_INTERNAL_PATTERNS = (
    re.compile(r"\bctrl\d+>", re.IGNORECASE),
    re.compile(r"\[KIRA\s+CONTEXT\][^\n]*", re.IGNORECASE),
)


def _preview(text: str, limit: int = 160) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3]}..."


def sanitize_user_visible_kira_output(text: str) -> str:
    raw = str(text or "")
    if not raw.strip():
        return ""

    cleaned_lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append("")
            continue
        updated = line
        for pattern in _INLINE_INTERNAL_PATTERNS:
            updated = pattern.sub("", updated)
        updated = updated.strip()
        if any(pattern.search(updated) for pattern in _INTERNAL_LINE_PATTERNS):
            continue
        if updated:
            cleaned_lines.append(updated)

    cleaned = "\n".join(cleaned_lines).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def sanitize_or_fallback_user_visible_output(text: str) -> tuple[str, dict[str, object]]:
    raw = str(text or "")
    sanitized = sanitize_user_visible_kira_output(raw)
    blocked = sanitized != raw.strip()
    if sanitized:
        return sanitized, {
            "blocked_internal_leak": blocked,
            "empty_after_sanitize": False,
            "raw_preview": _preview(raw),
            "sanitized_preview": _preview(sanitized),
        }

    fallback = ""
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if any(pattern.search(candidate) for pattern in _INTERNAL_LINE_PATTERNS):
            continue
        fallback = candidate
        break
    if not fallback:
        fallback = "Beleza. Ajustei isso por aqui."
    return fallback, {
        "blocked_internal_leak": True,
        "empty_after_sanitize": True,
        "raw_preview": _preview(raw),
        "sanitized_preview": _preview(fallback),
    }
