from __future__ import annotations

import re


_UNCERTAIN_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bacho que\b", re.IGNORECASE), ""),
    (re.compile(r"\btalvez\b", re.IGNORECASE), ""),
    (re.compile(r"\bpode ser que\b", re.IGNORECASE), ""),
)

_DRY_MAP: dict[str, str] = {
    "tarefa registrada.": "Fechado, chefe. Ja deixei isso registrado.",
    "pendencia registrada em background.": "Fechado, chefe. Ja deixei isso registrado.",
    "ok.": "Fechado, chefe.",
    "feito.": "Pronto, chefe.",
}


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _choose_address(text: str, *, context: str) -> str:
    folded = text.lower()
    if context == "light" and any(token in folded for token in ("hahaha", "kk", "de boa", "tranquilo")):
        return "homi"
    # Varia sem estado global usando hash do texto: alterna entre Chefe e Chefe.
    return "alan" if (sum(ord(ch) for ch in text) % 5 == 0) else "chefe"


def apply_kira_personality_style(text: str, context: str = "default") -> str:
    raw = _normalize_spaces(str(text or ""))
    if not raw:
        return raw
    if raw.startswith("[") and "]" in raw[:24]:
        # Notas de sistema/contexto interno nao devem ser estilizadas.
        return raw

    normalized_key = raw.lower()
    if normalized_key in _DRY_MAP:
        return _DRY_MAP[normalized_key]

    styled = raw
    for pattern, replacement in _UNCERTAIN_REPLACEMENTS:
        styled = pattern.sub(replacement, styled)
    styled = _normalize_spaces(styled)
    if not styled:
        styled = raw

    lower = styled.lower()
    if any(tag in lower for tag in ("chefe,", "alan,", "homi,")):
        return styled

    address = _choose_address(styled, context=context)
    if address == "alan":
        return f"Chefe, {styled[0].lower() + styled[1:] if len(styled) > 1 else styled.lower()}"
    if address == "homi":
        return f"Homi, {styled[0].lower() + styled[1:] if len(styled) > 1 else styled.lower()}"
    return f"Chefe, {styled[0].lower() + styled[1:] if len(styled) > 1 else styled.lower()}"
