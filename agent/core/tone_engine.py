from __future__ import annotations

import re


ToneMode = str

_CONTEXT_TO_TONE: dict[str, ToneMode] = {
    "tecnico": "serio",
    "comando": "foco",
    "casual": "leve",
    "emocional": "companheira",
    "provocacao": "leve",
    "brincadeira": "leve",
}


def resolve_kira_tone_mode(contexto: dict[str, object] | None) -> ToneMode:
    tipo = str((contexto or {}).get("tipo", "") or "").strip().lower()
    return _CONTEXT_TO_TONE.get(tipo, "neutro")


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def apply_kira_tone(texto_resposta: str, contexto: dict[str, object] | None) -> str:
    text = _normalize_spaces(texto_resposta)
    if not text:
        return text

    mode = resolve_kira_tone_mode(contexto)
    if mode == "serio":
        tuned = text
        tuned = re.sub(r"\b[Bb]eleza,?\s*", "Certo, ", tuned)
        tuned = re.sub(r"\b[Ff]echado,?\s*", "Certo, ", tuned)
        tuned = re.sub(r"\b[hH]omi,?\s*", "", tuned)
        tuned = re.sub(r"\b[cC]hefe,?\s*", "", tuned)
        tuned = _normalize_spaces(tuned).strip(" ,")
        if tuned and tuned[0].islower():
            tuned = tuned[0].upper() + tuned[1:]
        return tuned

    if mode == "foco":
        tuned = text
        tuned = re.sub(r"^(Chefe|Chefe|Homi),\s*", "", tuned, flags=re.IGNORECASE)
        tuned = re.sub(r"^(Fechado|Certo|Beleza),?\s*", "", tuned, flags=re.IGNORECASE)
        tuned = _normalize_spaces(tuned)
        parts = re.split(r"(?<=[.!?])\s+", tuned)
        return parts[0] if parts else tuned

    if mode == "companheira":
        tuned = text
        if not re.match(r"^(Chefe|Chefe|Homi),", tuned, flags=re.IGNORECASE):
            tuned = f"Chefe, {tuned[0].lower() + tuned[1:] if len(tuned) > 1 else tuned.lower()}"
        return _normalize_spaces(tuned)

    if mode == "leve":
        return text

    return text
