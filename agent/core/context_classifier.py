from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


ContextType = str


@dataclass(frozen=True, slots=True)
class UserContext:
    tipo: ContextType
    intensidade: float
    palavras_chave_detectadas: list[str]


_TYPE_PRIORITY: dict[str, int] = {
    "emocional": 6,
    "tecnico": 5,
    "comando": 4,
    "provocacao": 3,
    "brincadeira": 2,
    "casual": 1,
}
_TYPE_PRIORITY_ORDER: tuple[str, ...] = (
    "emocional",
    "tecnico",
    "comando",
    "provocacao",
    "brincadeira",
    "casual",
)

_KEYWORDS: dict[str, tuple[str, ...]] = {
    "tecnico": (
        "erro",
        "bug",
        "codigo",
        "sistema",
        "falha",
        "stacktrace",
        "exception",
        "traceback",
        "deploy",
    ),
    "comando": (
        "faz",
        "faca",
        "cria",
        "organiza",
        "abre",
        "resolve",
        "executa",
    ),
    "casual": (
        "e ai",
        "como voce ta",
        "como vc ta",
        "o que ta rolando",
        "tudo bem",
        "oi",
    ),
    "emocional": (
        "to cansado",
        "estou cansado",
        "to frustrado",
        "estou frustrado",
        "ta dificil",
        "nao aguento mais",
        "sem tempo",
    ),
    "brincadeira": (
        "kkkk",
        "haha",
        "zoeira",
        "brincando",
        "kkk",
    ),
    "provocacao": (
        "tu e lenta",
        "voce e lenta",
        "tu erra muito",
        "voce erra muito",
        "tu nao sabe nada",
        "burra",
        "lenta",
    ),
}

_STRONG_WORDS = {"muito", "demais"}


def _fold_text(value: str) -> str:
    lowered = str(value or "").strip().lower()
    normalized = unicodedata.normalize("NFKD", lowered)
    no_accents = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", no_accents).strip()


def _has_exclamation_repetition(text: str) -> bool:
    return "!!" in text or bool(re.search(r"!\s*!", text))


def classify_user_context(texto_usuario: str) -> dict[str, object]:
    folded = _fold_text(texto_usuario)
    if not folded:
        return {
            "tipo": "casual",
            "intensidade": 0.0,
            "palavras_chave_detectadas": [],
        }

    starts_with_command = any(
        folded.startswith(prefix)
        for prefix in ("faz", "faca", "cria", "organiza", "abre", "resolve", "executa")
    )

    hits: dict[str, list[str]] = {key: [] for key in _KEYWORDS}
    for tipo, keywords in _KEYWORDS.items():
        for keyword in keywords:
            if keyword in folded:
                hits[tipo].append(keyword)

    if starts_with_command and "comando" not in hits:
        hits["comando"] = []
    if starts_with_command and not hits["comando"]:
        hits["comando"].append("inicio_comando")

    if hits["provocacao"]:
        hits["brincadeira"] = []

    best_tipo = "casual"
    for tipo in _TYPE_PRIORITY_ORDER:
        if hits.get(tipo):
            best_tipo = tipo
            break

    strongest = set(hits.get(best_tipo, []))
    has_strong_word = any(token in folded for token in _STRONG_WORDS)
    has_repetition = _has_exclamation_repetition(folded)
    keyword_count = len(strongest)
    intensity = 0.3
    if has_strong_word:
        intensity += 0.3
    if has_repetition:
        intensity += 0.2
    intensity += (keyword_count * 0.1)
    intensity = max(0.0, min(1.0, round(intensity, 2)))
    return {
        "tipo": best_tipo,
        "intensidade": intensity,
        "palavras_chave_detectadas": sorted(strongest),
    }
