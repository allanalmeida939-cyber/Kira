"""confirmation.py — classificador de resposta de confirmacao do WhatsApp.

Porta `_classify_whatsapp_confirmation_reply` + `_normalize_confirmation_reply`
(e as listas de frases/tokens) que estavam inline no assistant.py. Isolado e
testavel. Retorna "confirm" | "cancel" | "unrelated".

Regra do fluxo: so "confirm" envia; "cancel" e "unrelated" derrubam o job
pendente e seguem a conversa normal (a Kira nao re-pergunta).
"""

from __future__ import annotations

import re

from ..whatsapp_context import fold_whatsapp_text

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)

_POSITIVE_PHRASES = {
    "sim", "sim manda", "sim manda sim", "confirmo", "confirmo pode enviar",
    "confirmo pode mandar", "manda", "manda sim", "pode", "pode mandar",
    "pode enviar", "pode ir", "pode prosseguir", "envia", "enviar", "confirma",
    "confirmado", "ok", "ok entao", "okay", "beleza", "blz", "certo", "fechado",
    "isso", "vai", "bora", "manda logo", "manda ai", "manda ja", "pode ser",
    "ta certo", "isso mesmo", "e essa", "essa", "e essa mesma", "envia sim",
    "pode enviar sim", "pode mandar sim", "manda essa",
}
_NEGATIVE_PHRASES = {
    "nao", "n", "nao manda", "nao enviar", "nao envia", "cancela", "cancelar",
    "para", "para isso", "deixa", "deixa pra la", "deixa quieto", "esquece",
    "aborta", "negativo",
}
_AMBIGUOUS_PHRASES = {"talvez", "pera", "calma", "deixa eu pensar", "nao sei"}

_POSITIVE_TOKENS = {
    "sim", "s", "ok", "okay", "blz", "beleza", "certo", "fechado", "manda",
    "envia", "enviar", "confirmo", "confirma", "confirmado", "bora", "vai",
    "prosseguir", "pode",
}
_NEGATIVE_TOKENS = {
    "nao", "n", "cancela", "cancelar", "para", "deixa", "esquece", "aborta",
    "negativo",
}
_AMBIGUOUS_TOKENS = {"talvez", "pera", "calma", "pensar", "duvida"}

# negacoes explicitas (multi-palavra) que sempre cancelam
_EXPLICIT_CANCEL_SUBSTRINGS = (
    "nao manda", "nao enviar", "nao envia", "deixa pra la", "deixa quieto",
)


def normalize_reply(value: str) -> str:
    folded = fold_whatsapp_text(value)
    folded = _PUNCT_RE.sub(" ", folded)
    return " ".join(folded.split())


def classify(value: str) -> str:
    """Retorna "confirm" | "cancel" | "unrelated" (ambiguo -> unrelated)."""
    normalized = normalize_reply(value)
    if not normalized:
        return "unrelated"

    if normalized in _NEGATIVE_PHRASES:
        return "cancel"
    if normalized in _POSITIVE_PHRASES:
        return "confirm"
    if normalized in _AMBIGUOUS_PHRASES:
        return "unrelated"

    # prioridade para cancelamento quando ha termo de negacao explicito
    if any(sub in normalized for sub in _EXPLICIT_CANCEL_SUBSTRINGS):
        return "cancel"

    tokens = set(normalized.split())
    if tokens & _NEGATIVE_TOKENS:
        return "cancel"
    if tokens & _POSITIVE_TOKENS:
        return "confirm"
    if tokens & _AMBIGUOUS_TOKENS:
        return "unrelated"
    return "unrelated"
