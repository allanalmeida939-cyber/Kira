from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from time_utils import now_brasilia


WhatsAppIntentKind = Literal["direct_message", "relay_message", "action_request"]
WhatsAppFollowupKind = Literal["none", "reply_now", "defer", "ignore", "topic_change"]

WHATSAPP_PENDING_TTL = timedelta(minutes=20)
WHATSAPP_FOLLOWUP_WINDOW = timedelta(minutes=4)

_ACCENT_TRANSLATION = str.maketrans(
    {
        "a": "a",
        "A": "A",
        "á": "a",
        "Á": "A",
        "à": "a",
        "À": "A",
        "ã": "a",
        "Ã": "A",
        "â": "a",
        "Â": "A",
        "e": "e",
        "E": "E",
        "é": "e",
        "É": "E",
        "ê": "e",
        "Ê": "E",
        "i": "i",
        "I": "I",
        "í": "i",
        "Í": "I",
        "o": "o",
        "O": "O",
        "ó": "o",
        "Ó": "O",
        "ô": "o",
        "Ô": "O",
        "õ": "o",
        "Õ": "O",
        "u": "u",
        "U": "U",
        "ú": "u",
        "Ú": "U",
        "ç": "c",
        "Ç": "C",
    }
)
_WHITESPACE_RE = re.compile(r"\s+")
_LEADING_ARTICLE_RE = re.compile(r"^(?:o|a|os|as)\s+", re.IGNORECASE)
_TRAILING_PUNCT_RE = re.compile(r"[\s,;:.!?-]+$")
_ASSISTANT_PREFIX_RE = re.compile(r"^(?:oi\s+|ei\s+)?kira[\s,:;-]*", re.IGNORECASE)

_ACTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "action_responde",
        re.compile(
            r"^(?:(?:oi|ei)\s+)?(?:kira[\s,:;-]*)?(?:responde|responder)\s+"
            r"(?:pra|pro|para)\s+(?P<target>[^,;:.!?]+?)\s+(?:que\s+)?(?P<content>.+)$"
        ),
    ),
    (
        "action_manda",
        re.compile(
            r"^(?:(?:oi|ei)\s+)?(?:kira[\s,:;-]*)?(?:manda(?:\s+mensagem)?|mandar)\s+"
            r"(?:pra|pro|para)\s+(?P<target>[^,;:.!?]+?)\s+(?:que\s+)?(?P<content>.+)$"
        ),
    ),
    (
        "action_envia",
        re.compile(
            r"^(?:(?:oi|ei)\s+)?(?:kira[\s,:;-]*)?(?:envia(?:\s+isso)?|enviar)\s+"
            r"(?:pra|pro|para)\s+(?P<target>[^,;:.!?]+?)\s+(?:que\s+)?(?P<content>.+)$"
        ),
    ),
    (
        "action_fala",
        re.compile(
            r"^(?:(?:oi|ei)\s+)?(?:kira[\s,:;-]*)?(?:fala|falar)\s+"
            r"(?:pra|pro|para)\s+(?P<target>[^,;:.!?]+?)\s+(?:que\s+)?(?P<content>.+)$"
        ),
    ),
)
_RELAY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "relay_avisa",
        re.compile(
            r"^(?:(?:oi|ei)\s+)?(?:kira[\s,:;-]*)?(?:avisa|avisar)\s+"
            r"(?:(?:pro|pra|para|ao|a|o)\s+)?(?P<target>[^,;:.!?]+?)\s+que\s+(?P<content>.+)$"
        ),
    ),
    (
        "relay_fala",
        re.compile(
            r"^(?:(?:oi|ei)\s+)?(?:kira[\s,:;-]*)?(?:fala|falar)\s+"
            r"(?:(?:pro|pra|para|ao|a|o)\s+)?(?P<target>[^,;:.!?]+?)\s+que\s+(?P<content>.+)$"
        ),
    ),
    (
        "relay_diz",
        re.compile(
            r"^(?:(?:oi|ei)\s+)?(?:kira[\s,:;-]*)?(?:diz|dizer)\s+"
            r"(?:(?:pro|pra|para|ao|a|o)\s+)?(?P<target>[^,;:.!?]+?)\s+que\s+(?P<content>.+)$"
        ),
    ),
    (
        "relay_manda_saber",
        re.compile(
            r"^(?:(?:oi|ei)\s+)?(?:kira[\s,:;-]*)?manda\s+"
            r"(?:(?:pro|pra|para|ao|a|o)\s+)?(?P<target>[^,;:.!?]+?)\s+saber\s+que\s+(?P<content>.+)$"
        ),
    ),
)
_IGNORE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:ignora|ignorar)\b"),
    re.compile(r"^(?:deixa\s+quieto|deixa\s+isso\s+quieto)\b"),
    re.compile(r"^(?:deixa\s+pra\s+la|deixa\s+pra\s+l[aá])\b"),
    re.compile(r"^(?:nem\s+responde|nao\s+responde|n[ãa]o\s+responde)\b"),
)
_DEFER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:depois\s+eu\s+(?:vejo|respondo))\b"),
    re.compile(r"^(?:deixa\s+pra\s+depois)\b"),
    re.compile(r"^(?:mais\s+tarde)\b"),
    re.compile(r"^(?:agora\s+nao|agora\s+n[ãa]o)\b"),
)
_REPLY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:responde|responder)\b"),
    re.compile(r"^(?:fala|falar)\b"),
    re.compile(r"^(?:diz|dizer)\b"),
    re.compile(r"^(?:manda|mandar)\b"),
    re.compile(r"^(?:envia|enviar)\b"),
    re.compile(r"^(?:pode\s+(?:responder|mandar|falar|dizer|enviar))\b"),
)
_FOLLOWUP_REFERENCE_TERMS = (
    "whatsapp",
    "wpp",
    "zap",
    "pra ele",
    "pra ela",
    "pro ",
    "para ",
    "responde",
    "manda",
    "fala",
    "diz",
    "envia",
)
_SECOND_PERSON_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bpra ele\b", re.IGNORECASE), "pra voce"),
    (re.compile(r"\bpara ele\b", re.IGNORECASE), "para voce"),
    (re.compile(r"\bpra ela\b", re.IGNORECASE), "pra voce"),
    (re.compile(r"\bpara ela\b", re.IGNORECASE), "para voce"),
    (re.compile(r"\bele\b", re.IGNORECASE), "voce"),
    (re.compile(r"\bela\b", re.IGNORECASE), "voce"),
)
_WHATSAPP_STICKER_PREVIEW_TERMS = frozenset(
    {
        "figurinha",
        "sticker",
    }
)
_WHATSAPP_GENERIC_PREVIEW_TERMS = frozenset(
    {
        "",
        "(nova mensagem)",
        "nova mensagem",
        "mensagem",
        "foto",
        "imagem",
        "gif",
        "video",
        "audio",
        "audio message",
        "voice message",
        "mensagem de voz",
        "nota de voz",
        "documento",
    }
)


@dataclass(slots=True)
class WhatsAppIntentResult:
    kind: WhatsAppIntentKind
    relay_target: str | None = None
    relay_content: str | None = None
    action_target: str | None = None
    action_content: str | None = None
    confidence: float = 0.0
    reason: str = ""


@dataclass(slots=True)
class PendingWhatsAppItem:
    id: str
    contact: str
    original_text: str
    normalized_text: str
    kind: WhatsAppIntentKind
    relay_target: str | None = None
    relay_content: str | None = None
    action_target: str | None = None
    action_content: str | None = None
    delivered_to_user: bool = False
    awaiting_user_response: bool = False
    created_at: datetime = field(default_factory=now_brasilia)
    expires_at: datetime = field(
        default_factory=lambda: now_brasilia() + WHATSAPP_PENDING_TTL
    )
    handled: bool = False

    def followup_contact(self) -> str | None:
        target = normalize_whatsapp_text(self.action_target or "")
        if target:
            return target
        contact = normalize_whatsapp_text(self.contact)
        return contact or None


@dataclass(slots=True)
class WhatsAppFollowupResult:
    kind: WhatsAppFollowupKind
    reply_target: str | None = None
    reply_text: str | None = None
    confidence: float = 0.0
    reason: str = ""
    handled_locally: bool = False
    local_response: str | None = None


def normalize_whatsapp_text(value: object) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "").strip())


def fold_whatsapp_text(value: object) -> str:
    text = normalize_whatsapp_text(value)
    return text.translate(_ACCENT_TRANSLATION).lower()


def classify_incoming_whatsapp_message(
    contact: str,
    text: str,
    *,
    user_name: str | None = None,
) -> WhatsAppIntentResult:
    del contact
    normalized_text = normalize_whatsapp_text(text)
    if not normalized_text:
        return WhatsAppIntentResult(
            kind="direct_message",
            confidence=0.0,
            reason="empty_text",
        )

    action_target, action_content, reason = _match_targeted_message(
        normalized_text,
        patterns=_ACTION_PATTERNS,
    )
    if action_target and action_content:
        return WhatsAppIntentResult(
            kind="action_request",
            action_target=action_target,
            action_content=action_content,
            confidence=0.96,
            reason=reason,
        )

    relay_target, relay_content, reason = _match_targeted_message(
        normalized_text,
        patterns=_RELAY_PATTERNS,
    )
    if relay_target and relay_content:
        if _looks_like_user_target(relay_target, user_name=user_name):
            return WhatsAppIntentResult(
                kind="relay_message",
                relay_target=relay_target,
                relay_content=adapt_relay_content_for_user(
                    relay_content,
                    target=relay_target,
                    user_name=user_name,
                ),
                confidence=0.91,
                reason=reason,
            )
        return WhatsAppIntentResult(
            kind="action_request",
            action_target=relay_target,
            action_content=relay_content,
            confidence=0.74,
            reason=f"{reason}_target_other_person",
        )

    return WhatsAppIntentResult(
        kind="direct_message",
        confidence=0.45,
        reason="fallback_direct_message",
    )


def build_relay_speech(
    contact: str,
    relay_content: str,
    tone_hint: str | None = None,
    *,
    user_name: str | None = None,
) -> str:
    intro = _user_prefix(user_name)
    base = f"{intro}{contact} pediu pra eu te avisar que {normalize_whatsapp_text(relay_content)}"
    if tone_hint:
        base = f"{base}. {normalize_whatsapp_text(tone_hint)}"
    return _ensure_sentence(base)


def build_whatsapp_direct_notification_speech(contact: str, text: str) -> str:
    normalized_contact = normalize_whatsapp_text(contact) or "Alguem"
    normalized_text = normalize_whatsapp_text(text)
    notification_kind = _classify_whatsapp_notification_text(normalized_text)
    if notification_kind == "sticker":
        return f"{normalized_contact} mandou uma figurinha."
    if notification_kind == "generic":
        return f"{normalized_contact} mandou uma mensagem."
    return f"Chefe, {normalized_contact} mandou: {normalized_text}"


def build_whatsapp_batch_notification_speech(
    contact: str,
    total_messages: int,
    latest_text: str,
) -> str:
    normalized_contact = normalize_whatsapp_text(contact) or "Alguem"
    try:
        message_count = max(int(total_messages), 1)
    except (TypeError, ValueError):
        message_count = 1
    if message_count <= 1:
        return build_whatsapp_direct_notification_speech(normalized_contact, latest_text)
    latest_preview = _build_whatsapp_batch_preview(latest_text)
    return (
        f"{normalized_contact} mandou {message_count} mensagens. "
        f"A ultima foi: {latest_preview}"
    )


def build_pending_whatsapp_speech(
    item: PendingWhatsAppItem,
    *,
    user_name: str | None = None,
) -> str:
    if item.kind == "relay_message":
        return build_relay_speech(
            item.contact,
            item.relay_content or item.original_text,
            user_name=user_name,
        )
    if item.kind == "action_request":
        intro = _user_prefix(user_name)
        target = normalize_whatsapp_text(item.action_target or "")
        content = normalize_whatsapp_text(item.action_content or item.original_text)
        if target:
            return _ensure_sentence(
                f"{intro}{item.contact} me pediu uma acao no WhatsApp para {target}: {content}"
            )
        return _ensure_sentence(
            f"{intro}{item.contact} me pediu uma acao no WhatsApp: {content}"
        )
    del user_name
    return build_whatsapp_direct_notification_speech(item.contact, item.original_text)


def detect_whatsapp_followup(
    user_text: str,
    *,
    active_item: PendingWhatsAppItem | None,
    user_name: str | None = None,
) -> WhatsAppFollowupResult:
    normalized_text = normalize_whatsapp_text(user_text)
    if not normalized_text:
        return WhatsAppFollowupResult(kind="none", reason="empty_user_text")
    if active_item is None:
        return WhatsAppFollowupResult(kind="none", reason="no_active_item")

    folded = fold_whatsapp_text(normalized_text)
    if _matches_any(folded, _IGNORE_PATTERNS):
        return WhatsAppFollowupResult(
            kind="ignore",
            confidence=0.95,
            reason="ignore_phrase",
            handled_locally=True,
            local_response="Tudo bem, vou deixar esse recado quieto por agora.",
        )
    if _matches_any(folded, _DEFER_PATTERNS):
        return WhatsAppFollowupResult(
            kind="defer",
            confidence=0.93,
            reason="defer_phrase",
            handled_locally=True,
            local_response="Beleza, deixei esse recado pra depois.",
        )
    if _looks_like_reply_now(
        folded,
        active_item=active_item,
        user_name=user_name,
    ):
        reply_target = active_item.followup_contact()
        return WhatsAppFollowupResult(
            kind="reply_now",
            reply_target=reply_target,
            reply_text=normalized_text,
            confidence=0.88,
            reason="reply_instruction",
        )
    if _looks_like_topic_change(
        folded,
        active_item=active_item,
        user_name=user_name,
    ):
        return WhatsAppFollowupResult(
            kind="topic_change",
            confidence=0.70,
            reason="topic_changed",
        )
    return WhatsAppFollowupResult(kind="none", confidence=0.0, reason="no_followup_match")


def build_delivery_context_note(item: PendingWhatsAppItem) -> str:
    target = item.followup_contact() or item.contact
    if item.kind == "relay_message":
        detail = (
            f"Ultimo recado entregue veio de {item.contact}. "
            f"Conteudo repassado ao usuario: {item.relay_content or item.original_text}."
        )
    elif item.kind == "action_request":
        detail = (
            f"Ultimo pedido de acao do WhatsApp veio de {item.contact}. "
            f"Alvo sugerido para envio: {target}. Conteudo base: {item.action_content or item.original_text}."
        )
    else:
        detail = (
            f"Ultima mensagem entregue no WhatsApp veio de {item.contact}. "
            f"Conteudo: {item.original_text}."
        )
    return (
        "[WHATSAPP CONTEXT] "
        f"{detail} "
        f"Se o usuario disser comandos curtos como 'fala que', 'responde ela' ou 'manda um kkk', "
        f"trate como resposta para {target}. "
        "Se ele ignorar, deixar pra depois ou mudar de assunto, encerre esse contexto sem insistir. "
        "Nunca envie WhatsApp diretamente sem confirmacao explicita para a mensagem proposta."
    )


def build_followup_context_note(
    item: PendingWhatsAppItem,
    followup: WhatsAppFollowupResult,
) -> str:
    if followup.kind == "reply_now":
        target = followup.reply_target or item.followup_contact() or item.contact
        return (
            "[WHATSAPP FOLLOWUP] "
            f"O usuario esta respondendo agora ao ultimo contexto do WhatsApp. "
            f"Use {target} como destino padrao se ele falar de forma abreviada. "
            f"Mensagem original recebida de {item.contact}: {item.original_text}. "
            "Monte uma proposta de resposta, mas so envie depois de confirmacao explicita."
        )
    if followup.kind == "ignore":
        return (
            "[WHATSAPP FOLLOWUP] O usuario decidiu ignorar o ultimo contexto do WhatsApp. "
            "Encerre esse assunto sem insistir."
        )
    if followup.kind == "defer":
        return (
            "[WHATSAPP FOLLOWUP] O usuario preferiu deixar o ultimo contexto do WhatsApp para depois. "
            "Encerre esse assunto por enquanto sem insistir."
        )
    if followup.kind == "topic_change":
        return (
            "[WHATSAPP FOLLOWUP] O usuario mudou de assunto depois do ultimo contexto do WhatsApp. "
            "Considere esse recado encerrado e siga a conversa normalmente."
        )
    return ""


def adapt_relay_content_for_user(
    content: str,
    *,
    target: str | None,
    user_name: str | None,
) -> str:
    normalized = normalize_whatsapp_text(content)
    if not normalized:
        return normalized
    if not _looks_like_user_target(target, user_name=user_name):
        return normalized

    rewritten = normalized
    replacement_targets: list[tuple[re.Pattern[str], str]] = list(_SECOND_PERSON_PATTERNS)
    clean_target = _clean_target(target or "")
    if clean_target:
        replacement_targets.insert(
            0,
            (
                re.compile(rf"\b{re.escape(clean_target)}\b", re.IGNORECASE),
                "voce",
            ),
        )
    for pattern, replacement in replacement_targets:
        rewritten = pattern.sub(replacement, rewritten)
    return normalize_whatsapp_text(rewritten)


def _match_targeted_message(
    text: str,
    *,
    patterns: tuple[tuple[str, re.Pattern[str]], ...],
) -> tuple[str | None, str | None, str]:
    folded = fold_whatsapp_text(text)
    for reason, pattern in patterns:
        match = pattern.match(folded)
        if not match:
            continue
        target = _extract_group_from_original(text, match, "target")
        content = _extract_group_from_original(text, match, "content")
        target = _clean_target(target)
        content = _clean_content(content)
        if target and content:
            return target, content, reason
    return None, None, ""


def _extract_group_from_original(text: str, match: re.Match[str], group_name: str) -> str:
    if group_name not in match.re.groupindex:
        return ""
    start, end = match.span(group_name)
    if start < 0 or end < 0:
        return ""
    return text[start:end]


def _clean_target(value: str) -> str:
    text = normalize_whatsapp_text(value)
    text = _LEADING_ARTICLE_RE.sub("", text)
    text = _TRAILING_PUNCT_RE.sub("", text)
    return normalize_whatsapp_text(text)


def _clean_content(value: str) -> str:
    text = normalize_whatsapp_text(value)
    text = _TRAILING_PUNCT_RE.sub("", text)
    return normalize_whatsapp_text(text)


def _ensure_sentence(text: str) -> str:
    normalized = normalize_whatsapp_text(text)
    if not normalized:
        return normalized
    if normalized[-1] not in ".!?":
        return f"{normalized}."
    return normalized


def _classify_whatsapp_notification_text(text: str) -> str:
    folded = fold_whatsapp_text(text)
    if folded in _WHATSAPP_STICKER_PREVIEW_TERMS:
        return "sticker"
    if folded in _WHATSAPP_GENERIC_PREVIEW_TERMS:
        return "generic"
    return "text"


def _build_whatsapp_batch_preview(text: str) -> str:
    normalized_text = normalize_whatsapp_text(text)
    notification_kind = _classify_whatsapp_notification_text(normalized_text)
    if notification_kind == "sticker":
        return "uma figurinha"
    if notification_kind == "generic":
        return "uma mensagem"
    return normalized_text or "uma mensagem"


def _user_prefix(user_name: str | None) -> str:
    user = normalize_whatsapp_text(user_name)
    if not user:
        return ""
    return f"{user}, "


def _matches_any(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _looks_like_reply_now(
    folded_text: str,
    *,
    active_item: PendingWhatsAppItem,
    user_name: str | None,
) -> bool:
    words = folded_text.split()
    starts_with_reply = _matches_any(folded_text, _REPLY_PATTERNS)
    if starts_with_reply:
        first_word = words[0] if words else ""
        if first_word in {"responde", "responder", "manda", "mandar", "envia", "enviar"}:
            if len(words) <= 10 or " que " in f" {folded_text} ":
                return True
        if first_word in {"fala", "falar", "diz", "dizer"}:
            if any(token in folded_text for token in (" que ", " pra ", " pro ", " para ")):
                return True

    target_terms = [fold_whatsapp_text(active_item.contact)]
    followup_target = active_item.followup_contact()
    if followup_target:
        target_terms.append(fold_whatsapp_text(followup_target))
    if user_name:
        target_terms.append(fold_whatsapp_text(user_name))

    if any(term and term in folded_text for term in target_terms):
        return any(token in folded_text for token in _FOLLOWUP_REFERENCE_TERMS)

    return False


def _looks_like_topic_change(
    folded_text: str,
    *,
    active_item: PendingWhatsAppItem,
    user_name: str | None,
) -> bool:
    del user_name
    if len(folded_text.split()) < 3:
        return False
    contact_terms = {
        fold_whatsapp_text(active_item.contact),
        fold_whatsapp_text(active_item.followup_contact() or ""),
    }
    if any(term and term in folded_text for term in contact_terms):
        return False
    return not any(token in folded_text for token in _FOLLOWUP_REFERENCE_TERMS)


def _looks_like_user_target(target: str | None, *, user_name: str | None) -> bool:
    folded_target = fold_whatsapp_text(_clean_target(target or ""))
    if not folded_target:
        return False
    if folded_target in {"ele", "ela", "voce", "vc", "tu"}:
        return True

    folded_user = fold_whatsapp_text(user_name or "")
    if not folded_user:
        return False
    target_tokens = {token for token in folded_target.split() if token}
    user_tokens = {token for token in folded_user.split() if token}
    if not target_tokens or not user_tokens:
        return False
    return bool(target_tokens & user_tokens)


__all__ = [
    "PendingWhatsAppItem",
    "WHATSAPP_FOLLOWUP_WINDOW",
    "WHATSAPP_PENDING_TTL",
    "WhatsAppFollowupResult",
    "WhatsAppIntentResult",
    "adapt_relay_content_for_user",
    "build_delivery_context_note",
    "build_followup_context_note",
    "build_whatsapp_batch_notification_speech",
    "build_whatsapp_direct_notification_speech",
    "build_pending_whatsapp_speech",
    "build_relay_speech",
    "classify_incoming_whatsapp_message",
    "detect_whatsapp_followup",
    "fold_whatsapp_text",
    "normalize_whatsapp_text",
]
