from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

COMMAND_VERBS = frozenset(
    {
        "abra",
        "abre",
        "abrir",
        "feche",
        "fecha",
        "execute",
        "executa",
        "mostre",
        "mostra",
        "pesquise",
        "pesquisa",
        "toque",
        "toca",
        "pause",
        "pausa",
        "aumente",
        "aumenta",
        "diminua",
        "diminui",
        "navegue",
        "navega",
        "organize",
        "organiza",
        "ligue",
        "liga",
        "desligue",
        "desliga",
        "ative",
        "ativa",
        "desative",
        "desativa",
        "abaixa",
        "abaixe",
        "baixa",
        "sobe",
        "coloca",
        "bota",
        "renomeia",
        "renomeie",
        "muda",
        "mude",
        "troca",
        "troque",
        "move",
        "mova",
        "copia",
        "copie",
        "bloqueia", "bloqueie", "trava", "trave", "manda", "mande", "envia", "envie", "responde", "responda",
        "reinicia", "reinicie", "suspende", "suspenda", "fala", "diz", "escreve", "escreva", "preenche", "preencha",
        "entra", "entre", "faz", "faca", "minimiza", "minimize", "maximiza", "maximize", "silencia", "silencie",
        "abrai",  # fallback para ASR ruim
    }
)
# Infinitivo só vale como comando depois de um pedido: "pode renomear", "consegue abrir".
INFINITIVE_VERBS = frozenset(
    {
        "abrir", "fechar", "executar", "mostrar", "pesquisar", "tocar", "pausar", "aumentar", "diminuir", "navegar",
        "organizar", "ligar", "desligar", "ativar", "desativar", "renomear", "mudar", "trocar", "mover", "copiar",
        "bloquear", "travar", "mandar", "enviar", "responder", "reiniciar", "suspender", "preencher", "entrar", "fazer",
    }
)
# radical de verbo de ação mal transcrito ("bloqueu", "mandaa"): ainda é pedido de ação
_IMPERATIVE_STEMS = ("bloque", "manda", "mande", "envia", "envie", "aument", "diminu", "desblo", "reinici", "deslig")
_REQUEST_PREFIXES = ("voce pode", "voce consegue", "pode", "consegue", "da pra")

_POLITE_PREFIXES = (
    "por favor",
    "porfavor",
    "por fa",
    "porfa",
    "pfv",
)

# o reconhecimento de voz erra o nome ("Kiir bloqueu o computador"): variações comuns também contam
_WAKEWORDS = (
    "ei kira",
    "kiir",
    "kiira",
    "quira",
    "kyra",
    "hey kira",
    "ok kira",
    "okay kira",
    "kira",
    "ok",
    "okay",
)

_CASUAL_PREFIXES = (
    "vou ",
    "vou so ",
    "vou so dar ",
    "vou fazer ",
    "preciso ",
    "tenho que ",
    "depois eu ",
    "mais tarde eu ",
    "talvez ",
    "acho que ",
    "quero deixar ",
    "estou pensando em ",
    "to pensando em ",
)


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKD", str(text or ""))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = folded.lower().strip()
    folded = re.sub(r"[^a-z0-9\s,.:;!?-]+", " ", folded)
    folded = re.sub(r"\s+", " ", folded)
    return folded.strip()


def _strip_prefix(text: str, prefixes: tuple[str, ...]) -> tuple[str, str]:
    for prefix in prefixes:
        if text == prefix:
            return "", prefix
        if text.startswith(f"{prefix} "):
            return text[len(prefix):].strip(), prefix
        match = re.match(rf"^{re.escape(prefix)}[\s,.:;!?-]+(.+)$", text)
        if match:
            return match.group(1).strip(), prefix
    return text, ""


@dataclass(slots=True)
class CommandIntentResult:
    text: str
    normalized_text: str
    stripped_text: str
    first_word: str
    wakeword: str
    is_command: bool
    confidence: float
    reason: str


def classify_explicit_command(text: str) -> CommandIntentResult:
    normalized = _normalize(text)
    if not normalized:
        return CommandIntentResult(
            text=str(text or ""),
            normalized_text="",
            stripped_text="",
            first_word="",
            wakeword="",
            is_command=False,
            confidence=0.0,
            reason="empty",
        )

    if any(normalized.startswith(prefix) for prefix in _CASUAL_PREFIXES):
        return CommandIntentResult(
            text=str(text or ""),
            normalized_text=normalized,
            stripped_text=normalized,
            first_word=normalized.split()[0] if normalized else "",
            wakeword="",
            is_command=False,
            confidence=0.05,
            reason="casual_prefix",
        )

    stripped, wakeword = _strip_prefix(normalized, _WAKEWORDS)
    if wakeword:
        stripped, _ = _strip_prefix(stripped, _POLITE_PREFIXES)
        if any(stripped.startswith(prefix) for prefix in _CASUAL_PREFIXES):
            return CommandIntentResult(
                text=str(text or ""),
                normalized_text=normalized,
                stripped_text=stripped,
                first_word=stripped.split()[0] if stripped else "",
                wakeword=wakeword,
                is_command=False,
                confidence=0.05,
                reason="wakeword_plus_casual_prefix",
            )

    stripped, request = _strip_prefix(stripped, _REQUEST_PREFIXES)
    tokens = stripped.split()
    first_word = tokens[0] if tokens else ""
    if first_word in COMMAND_VERBS or (request and first_word in INFINITIVE_VERBS) or first_word.startswith(_IMPERATIVE_STEMS):
        return CommandIntentResult(
            text=str(text or ""),
            normalized_text=normalized,
            stripped_text=stripped,
            first_word=first_word,
            wakeword=wakeword,
            is_command=True,
            confidence=0.98 if wakeword else 0.93,
            reason="imperative_first_word",
        )

    return CommandIntentResult(
        text=str(text or ""),
        normalized_text=normalized,
        stripped_text=stripped,
        first_word=first_word,
        wakeword=wakeword,
        is_command=False,
        confidence=0.1,
        reason="missing_explicit_command_marker",
    )
