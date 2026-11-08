from __future__ import annotations

import re
import unicodedata


def _norm(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text or "").strip().lower())
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def user_requested_foreign_language(text: str) -> bool:
    normalized = _norm(text)
    return bool(
        re.search(r"\b(traduza|traduz|responda|responde|fale|fala|como fala|em ingles|em espanhol|em italiano|para ingles|para espanhol|para italiano)\b", normalized)
        and re.search(r"\b(ingles|english|espanhol|spanish|italiano|italian|traducao|traduzir)\b", normalized)
    )


_FOREIGN_PATTERNS = (
    re.compile(r"\b(ciao|allora|posso aiutarti|va bene|come posso|certamente)\b", re.I),
    re.compile(r"\b(claro que si|puedo ayudarte|vale|entendido|como puedo ayudarte)\b", re.I),
    re.compile(r"\b(sure|i can help|of course|let me|how can i help)\b", re.I),
)


def looks_like_foreign_output(text: str, *, user_text: str = "") -> bool:
    if user_requested_foreign_language(user_text):
        return False
    normalized = _norm(text)
    hits = sum(1 for pattern in _FOREIGN_PATTERNS if pattern.search(normalized))
    return hits >= 1


def enforce_ptbr_output(text: str, *, user_text: str = "") -> tuple[str, bool]:
    if not looks_like_foreign_output(text, user_text=user_text):
        return str(text or ""), False
    return "Desculpa, chefe. Corrigi aqui. Vou responder em português.", True
