from __future__ import annotations

import re
from dataclasses import dataclass

if __package__ and __package__.startswith("agent"):
    from agent.services.whatsapp_data import fold_whatsapp_value, normalize_whatsapp_value
else:
    from services.whatsapp_data import fold_whatsapp_value, normalize_whatsapp_value


_SEND_VERBS = (
    "manda",
    "mande",
    "mandar",
    "envia",
    "envie",
    "enviar",
    "avisa",
    "avise",
    "avisar",
    "diz",
    "diga",
    "dizer",
)
_CHANNEL_TERMS = ("whatsapp", "zap", "wpp", "whats")
_STATUS_PATTERNS = (
    "mandou",
    "enviou",
    "deu certo",
    "foi",
    "conseguiu mandar",
)
_LEADING_COMMAND_RE = re.compile(
    r"^(?:(?:oi|ei)\s+)?(?:kira[\s,:;-]*)?"
    r"(?P<verb>manda|mande|mandar|envia|envie|enviar|avisa|avise|avisar|diz|diga|dizer)"
    r"(?:\s+uma)?(?:\s+mensagem)?(?:\s+no\s+(?:whatsapp|zap|wpp|whats))?\s*",
    re.IGNORECASE,
)
_LEADING_PREPOSITION_RE = re.compile(r"^(?:pra|para|pro|ao|a|à)\s+", re.IGNORECASE)
_MESSAGE_PREFIX_RE = re.compile(r"^(?:dizendo\s+que|dizendo|que)\s+", re.IGNORECASE)
_ARTICLE_RE = re.compile(r"^(?:o|a|os|as)\s+", re.IGNORECASE)
_SEPARATOR_RE = re.compile(r"^[\s,;:.-]+")
_CONTACT_PRONOUNS = {"ela", "ele"}


@dataclass(slots=True)
class ParsedWhatsAppIntent:
    classification: str
    contact: str = ""
    message: str = ""
    raw_text: str = ""
    normalized_text: str = ""
    status_query: bool = False


# ── FASE 2B/C6: inferencia de estilo da mensagem (sem envio) ──────────────────
# Estilo A (kira_speaking): Kira fala como intermediaria ("avisa fulano que...").
# Estilo B (user_speaking): mensagem literal como se fosse o usuario ("manda: ...").
# ambiguous: nao da pra decidir com seguranca -> Kira pergunta.

# Marcadores explicitos de ditado literal -> usuario falando (Estilo B).
_DICTATION_MARKERS = (
    "como eu",
    "como se fosse eu",
    "como se eu",
    "nas minhas palavras",
    "do meu jeito",
    "escreve assim",
    "escreve isso",
    "manda assim",
    "exatamente isso",
    "ao pe da letra",
)
# Verbos que indicam a Kira como intermediaria (Estilo A).
_INTERMEDIARY_VERBS = (
    "avisa", "avise", "avisar",
    "fala", "fale", "falar",
    "diz", "diga", "dizer",
    "pergunta", "pergunte", "perguntar",
    "chama", "chame", "chamar",
    "conta", "conte", "contar",
)
# Conectores de discurso indireto -> reforcam Estilo A.
_REPORTED_SPEECH_RE = re.compile(r"\b(que|se|pra que|para que)\b", re.IGNORECASE)
# Pronomes/expressoes vagas que indicam mensagem incompleta.
_VAGUE_MESSAGE_TOKENS = {
    "isso", "isto", "aquilo", "iss", "aquele negocio", "aquela coisa",
    "o negocio", "a coisa", "aquela parada", "isso ai",
}


@dataclass(slots=True)
class WhatsAppStyleInference:
    style: str  # "kira_speaking" | "user_speaking" | "ambiguous"
    reason: str
    needs_user_clarification: bool = False
    clarification_topic: str = ""  # "style" | "contact" | "message" | ""


_CLARIFICATION_QUESTIONS = {
    "style": (
        "Quer que eu mande como Kira (avisando que é você) ou como se fosse você mesmo?",
        "Mando essa no meu nome, dizendo que é da sua parte, ou escrevo como se fosse você falando?",
        "Prefere que eu fale como Kira ou que a mensagem pareça escrita por você?",
    ),
    "message": (
        "O que exatamente você quer que eu mande?",
        "Me diz o que devo escrever na mensagem?",
        "Qual é o recado? Não peguei o conteúdo.",
    ),
    "contact": (
        "Pra quem eu mando? Não achei esse contato.",
        "Qual contato exatamente? Não consegui identificar.",
        "Me confirma o nome do contato pra eu mandar certo?",
    ),
}


def suggest_clarification_question(topic: str) -> str:
    """Pergunta de esclarecimento VARIADA (nao fixa). A formulacao final pode ser
    refinada pela camada de personalidade da Kira em fases posteriores."""
    import random

    options = _CLARIFICATION_QUESTIONS.get(str(topic or "").strip(), _CLARIFICATION_QUESTIONS["style"])
    return random.choice(options)


def _message_is_vague(message: str) -> bool:
    folded = fold_whatsapp_value(message)
    if not folded:
        return True
    return folded in _VAGUE_MESSAGE_TOKENS


def infer_whatsapp_message_style(
    raw_text: str,
    message: str = "",
    *,
    had_literal_separator: bool = False,
) -> WhatsAppStyleInference:
    """Classifica o estilo da mensagem de WhatsApp SEM enviar.

    `had_literal_separator` deve ser True quando o parser ja detectou um separador
    literal ("contato: mensagem"), o que e um sinal forte de Estilo B.
    """
    folded_raw = fold_whatsapp_value(_strip_assistant_prefix(normalize_whatsapp_value(raw_text)))

    # Mensagem ausente/vaga -> precisa esclarecer o conteudo antes de qualquer estilo.
    if _message_is_vague(message):
        return WhatsAppStyleInference(
            style="ambiguous",
            reason="message_missing_or_vague",
            needs_user_clarification=True,
            clarification_topic="message",
        )

    # Sinais de ditado literal -> usuario falando (Estilo B).
    if had_literal_separator:
        return WhatsAppStyleInference(style="user_speaking", reason="literal_separator")
    # Dois-pontos no texto cru ("manda pra X: mensagem") tambem indica ditado literal.
    if ":" in str(raw_text or ""):
        return WhatsAppStyleInference(style="user_speaking", reason="literal_colon")
    if any(marker in folded_raw for marker in _DICTATION_MARKERS):
        return WhatsAppStyleInference(style="user_speaking", reason="dictation_marker")

    # Comando relatado ("manda/mandando ela/ele <fazer X>", "fala pra ela que ...") -> Kira intermediaria.
    if re.search(r"\bmand(?:a|ando|e)\s+(?:a\s+|o\s+)?(?:ela|ele|elas|eles)\b", folded_raw) \
       or re.search(r"\bmandando\b", folded_raw) \
       or re.search(r"\b(?:pra|para)\s+(?:ela|ele)\s+que\b", folded_raw):
        return WhatsAppStyleInference(style="kira_speaking", reason="reported_command")

    # Sinais de intermediacao -> Kira falando (Estilo A).
    has_intermediary = any(
        re.search(rf"\b{re.escape(verb)}\b", folded_raw) for verb in _INTERMEDIARY_VERBS
    )
    has_reported = bool(_REPORTED_SPEECH_RE.search(folded_raw))
    if has_intermediary and has_reported:
        return WhatsAppStyleInference(style="kira_speaking", reason="intermediary_verb_reported_speech")
    if has_intermediary:
        # Intermediacao sem discurso indireto claro -> ainda Estilo A, confianca menor.
        return WhatsAppStyleInference(style="kira_speaking", reason="intermediary_verb")

    # Sem sinais fortes -> perguntar ao usuario.
    return WhatsAppStyleInference(
        style="ambiguous",
        reason="no_strong_signal",
        needs_user_clarification=True,
        clarification_topic="style",
    )


def _strip_assistant_prefix(text: str) -> str:
    return re.sub(r"^(?:oi|ei)\s+kira[\s,:;-]*", "", text, flags=re.IGNORECASE).strip()


def is_whatsapp_intent(text: str) -> bool:
    raw = normalize_whatsapp_value(text)
    normalized = fold_whatsapp_value(_strip_assistant_prefix(raw))
    if not normalized:
        return False
    if any(re.search(rf"\b{re.escape(verb)}\b", normalized) for verb in _SEND_VERBS):
        if "mensagem" in normalized or any(term in normalized for term in _CHANNEL_TERMS):
            return True
        if re.search(r"\b(?:pra|para|pro)\b", normalized):
            return True
    return False


def _clean_message(text: str) -> str:
    value = normalize_whatsapp_value(text)
    value = _SEPARATOR_RE.sub("", value)
    return value.strip(" .,!?:;-")


def _match_longest_contact(rest: str, contacts: list[dict[str, object]] | None) -> tuple[str, str]:
    normalized_rest = normalize_whatsapp_value(rest)
    if not normalized_rest:
        return "", ""

    without_article = _ARTICLE_RE.sub("", normalized_rest)
    folded_rest = fold_whatsapp_value(without_article)

    best_contact = ""
    best_term = ""
    if contacts:
        candidates: list[tuple[int, str, str]] = []
        for item in contacts:
            name = normalize_whatsapp_value(item.get("name"))
            aliases = item.get("aliases") if isinstance(item.get("aliases"), list) else []
            terms = [name, *[normalize_whatsapp_value(alias) for alias in aliases]]
            for term in terms:
                folded_term = fold_whatsapp_value(term)
                if not folded_term:
                    continue
                if folded_rest == folded_term or folded_rest.startswith(folded_term + " "):
                    candidates.append((len(folded_term), name or term, term))
        if candidates:
            _, best_contact, best_term = sorted(candidates, key=lambda item: item[0], reverse=True)[0]

    if best_term:
        consumed = without_article[: len(best_term)]
        remainder = without_article[len(consumed) :]
        return best_contact or best_term, _clean_message(remainder)

    fallback_parts = without_article.split()
    if not fallback_parts:
        return "", ""
    if fold_whatsapp_value(fallback_parts[0]) in _CONTACT_PRONOUNS:
        remainder = " ".join(fallback_parts[1:]) if len(fallback_parts) > 1 else without_article
        return "", _clean_message(remainder)
    if len(fallback_parts) == 1:
        return fallback_parts[0], ""
    return fallback_parts[0], _clean_message(" ".join(fallback_parts[1:]))


def parse_whatsapp_outbound_intent(
    text: str,
    *,
    contacts: list[dict[str, object]] | None = None,
) -> ParsedWhatsAppIntent:
    raw_text = normalize_whatsapp_value(text)
    normalized_text = fold_whatsapp_value(_strip_assistant_prefix(raw_text))
    if not normalized_text:
        return ParsedWhatsAppIntent(
            classification="not_whatsapp",
            raw_text=raw_text,
            normalized_text=normalized_text,
        )

    if any(pattern in normalized_text for pattern in _STATUS_PATTERNS):
        return ParsedWhatsAppIntent(
            classification="status_query",
            raw_text=raw_text,
            normalized_text=normalized_text,
            status_query=True,
        )

    has_send_verb = any(re.search(rf"\b{re.escape(verb)}\b", normalized_text) for verb in _SEND_VERBS)
    has_channel_hint = any(term in normalized_text for term in _CHANNEL_TERMS) or "mensagem" in normalized_text
    if not has_send_verb and not has_channel_hint:
        return ParsedWhatsAppIntent(
            classification="not_whatsapp",
            raw_text=raw_text,
            normalized_text=normalized_text,
        )

    working = _strip_assistant_prefix(raw_text)
    command_match = _LEADING_COMMAND_RE.match(working)
    if not command_match:
        return ParsedWhatsAppIntent(
            classification="not_whatsapp",
            raw_text=raw_text,
            normalized_text=normalized_text,
        )
    rest = normalize_whatsapp_value(working[command_match.end() :])
    if not rest:
        return ParsedWhatsAppIntent(
            classification="missing_contact",
            raw_text=raw_text,
            normalized_text=normalized_text,
        )

    if rest.startswith(":"):
        return ParsedWhatsAppIntent(
            classification="missing_contact",
            message=_clean_message(rest[1:]),
            raw_text=raw_text,
            normalized_text=normalized_text,
        )

    if _MESSAGE_PREFIX_RE.match(rest):
        return ParsedWhatsAppIntent(
            classification="missing_contact",
            message=_clean_message(_MESSAGE_PREFIX_RE.sub("", rest, count=1)),
            raw_text=raw_text,
            normalized_text=normalized_text,
        )

    explicit_target = _LEADING_PREPOSITION_RE.match(rest)
    candidate_zone = rest
    if explicit_target:
        candidate_zone = rest[explicit_target.end() :]

    if ":" in candidate_zone:
        left, right = candidate_zone.split(":", 1)
        contact, _ = _match_longest_contact(left, contacts)
        message = _clean_message(right)
        classification = "ready_to_send" if contact and message else ("missing_message" if contact else "missing_contact")
        return ParsedWhatsAppIntent(
            classification=classification,
            contact=contact,
            message=message,
            raw_text=raw_text,
            normalized_text=normalized_text,
        )

    contact, message = _match_longest_contact(candidate_zone, contacts)
    message = _clean_message(_MESSAGE_PREFIX_RE.sub("", message, count=1))
    if contact and message:
        return ParsedWhatsAppIntent(
            classification="ready_to_send",
            contact=contact,
            message=message,
            raw_text=raw_text,
            normalized_text=normalized_text,
        )
    if contact:
        return ParsedWhatsAppIntent(
            classification="missing_message",
            contact=contact,
            raw_text=raw_text,
            normalized_text=normalized_text,
        )
    return ParsedWhatsAppIntent(
        classification="missing_contact",
        message=_clean_message(candidate_zone),
        raw_text=raw_text,
        normalized_text=normalized_text,
    )


# ── FASE 2C: orquestracao — reescrita por estilo + roteamento de confirmacao ──

def _ensure_sentence(text: str) -> str:
    t = normalize_whatsapp_value(text).strip()
    if not t:
        return t
    t = t[0].upper() + t[1:]
    if t[-1] not in ".!?":
        t += "."
    return t


# build_whatsapp_message_by_style REMOVIDO (V9): a assinatura/perspectiva da mensagem
# agora e decidida no WhatsappOrchestrator (_apply_perspective, em core/whatsapp). Esta
# funcao ja estava desabilitada (o re-style duplicava a mensagem) e nao tinha mais
# chamador ativo — so era citada em comentario. parse_whatsapp_outbound_intent e
# ParsedWhatsAppIntent (ainda usados pelo assistant) PERMANECEM neste arquivo.


# Confirmacao explicita de envio.
_CONFIRM_RE = re.compile(
    r"^\s*(sim|isso|claro|pode|confirmo|confirmar|manda|mande|mandar|envia|envie|enviar|"
    r"pode\s+mandar|pode\s+enviar|manda\s+sim|sim\s+manda|ok|beleza|isso\s+mesmo|"
    r"manda\s+ai|manda\s+a[ií])\b",
    re.IGNORECASE,
)
# Cancelamento explicito.
_CANCEL_RE = re.compile(
    r"\b(nao|n[aã]o|cancela|cancelar|deixa|esquece|para|n[aã]o\s+manda|n[aã]o\s+envia|"
    r"deixa\s+pra\s+l[aá]|melhor\s+nao)\b",
    re.IGNORECASE,
)
# Pergunta de status (NUNCA reenvia).
_STATUS_QUERY_RE = re.compile(
    r"\b(deu|foi|enviou|mandou|chegou|e\s*a[ií]|foi\s+a\s+mensagem|deu\s+certo|"
    r"ja\s+(foi|mandou|enviou))\b\??",
    re.IGNORECASE,
)
# "kira?" / chamado puro — nunca confirma envio.
_BARE_KIRA_RE = re.compile(
    r"^\s*(oi|ei|opa|e\s*a[ií])?\s*(kira|akira)\s*[?!.…]*\s*$|^\s*(ta|t[aá]|cad[eê]|voce|voc[eê])\s+(ai|a[ií])\s*\??\s*$",
    re.IGNORECASE,
)


def is_bare_kira_address(text: str) -> bool:
    """True para 'kira?', 'oi kira', 'ta ai?' — chamado puro, NUNCA confirma envio."""
    return bool(_BARE_KIRA_RE.match(normalize_whatsapp_value(text or "").strip()))


def is_whatsapp_status_query(text: str) -> bool:
    """True para 'deu?', 'foi?', 'enviou?' — consulta status, NUNCA reenvia."""
    t = normalize_whatsapp_value(text or "").strip()
    if not t or is_bare_kira_address(t):
        return False
    # status query so vale para frases curtas (evita falso positivo em comandos longos)
    if len(t.split()) > 6:
        return False
    return bool(_STATUS_QUERY_RE.search(t))


def is_whatsapp_send_confirmation(text: str) -> bool:
    """True somente para confirmacao explicita de envio (e nao chamado/cancelamento)."""
    t = normalize_whatsapp_value(text or "").strip()
    if not t or is_bare_kira_address(t):
        return False
    if _CANCEL_RE.search(t):
        return False
    if len(t.split()) > 6:
        return False
    return bool(_CONFIRM_RE.match(t))


def is_whatsapp_cancel(text: str) -> bool:
    t = normalize_whatsapp_value(text or "").strip()
    if not t or is_bare_kira_address(t):
        return False
    if len(t.split()) > 6:
        return False
    return bool(_CANCEL_RE.search(t))
