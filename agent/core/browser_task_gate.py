from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

logger = logging.getLogger("kira.browser_agent.gate")


@dataclass
class BrowserTaskMatch:
    matched: bool
    reason: str = ""
    task: str = ""
    query: str = ""
    action: str = ""
    requires_confirmation: bool = False
    sensitive_action: str = ""


@dataclass
class BrowserIntentClassification:
    intent: str
    action: str = ""
    confidence: float = 0.0
    reason: str = ""
    query: str = ""
    requires_confirmation: bool = False
    sensitive_action: str = ""


def _norm(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text or "").lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[?.!,;:]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


BLOCKED_PATTERNS = (
    r"\b(whatsapp|zap|wpp)\b",
    r"\b(cria|criar|gera|gerar|fazer)\b.*\b(pdf|txt|slide|arquivo|codigo)\b",
    r"\b(abre|abrir)\b.*\b(pasta|documentos|downloads|arquivo local)\b",
    r"\b(volume|aumenta o volume|abaixa o volume|status do sistema|sistema vital|transcricao)\b",
    r"\b(oculta|ocultar|mostra|mostrar|abre|abrir)\b.*\b(api|apis|painel|aba|tab|configuracoes|configuracao)\b",
    r"\b(mouse|teclado|monitor 1|monitor 2|bloco de notas|notepad|vs code|vscode|copiar|colar|ctrl\+c|ctrl\+v)\b",
)

SEARCH_VERB_PATTERN = r"\b(pesquisa|pesquisar|pesquise|procura|procurar|procure|busca|buscar|busque|googla|google)\b"
BROWSER_SEARCH_CONTEXT_PATTERN = r"\b(no navegador|na internet|na web|no google)\b"
SUMMARY_CUE_PATTERN = (
    r"\b(resume|resuma|resumir|resumo|primeiro resultado|principais pontos|topicos|pontos principais|"
    r"me diga|me diz|diga|fala|fale|explique|explica|me conte|o que encontrou)\b"
)
OPEN_SITE_PATTERN = (
    r"\b(entra|entrar|abre|abrir|abri|acessa|acessar|acesse|vai|va|ir|navega|navegue|me leva|mostra)\b.*\b("
    r"site|portal|google|instagram|facebook|gmail|amazon|youtube|internet|web|http|www|"
    r"twitter|linkedin|reddit|netflix|spotify|mercado\s*livre|chatgpt|wikipedia|github|"
    r"whatsapp\s*web|receita\s*federal"
    r")\b"
)
OPEN_SITE_PATTERN_BROAD = (
    # Captura "abre/vai/entra + artigo + qualquer coisa" sem exigir site conhecido
    r"\b(abre|abrir|abri|acessa|acessar|acesse|vai\s+(?:para|pro|pra|ao|a)|va\s+(?:para|pro|pra|ao|a)|entra?\s+n[oa]?|entra|entrar|navega?\s+(?:para|pro|pra|ao|a)|navegue|ir\s+(?:para|pro|pra|ao|a)|me\s+leva\s+(?:para|pro|pra|ao|a))\b\s+(?:o\s+|a\s+)?[a-zA-Z0-9]"
)
WEB_CONTEXT_PATTERN = (
    r"\b(site|portal|pagina|resultado|google|internet|web|formulario|compra|pedido|pagamento|login|senha)\b|https?://"
)
SENSITIVE_ACTION_PATTERNS = (
    (
        r"\b(enviar|envia|submeter|publicar|postar|comprar|pagar|apagar|deletar|excluir|alterar senha|aceitar termos|confirmar pedido)\b",
        "confirmation_required",
    ),
    (r"\b(enviar email|mandar email|enviar mensagem|publicar story|publicar post)\b", "confirmation_required"),
)


def _extract_search_query(text: str) -> str:
    raw = str(text or "").strip()
    raw = re.sub(r"(?i)^\s*(kira|kiara|cara)\s*,?\s*", "", raw).strip()
    match = re.search(
        r"(?i)\b(?:pesquisa|pesquisar|pesquise|procura|procurar|procure|busca|buscar|busque)\b\s+(.+)$",
        raw,
    )
    if not match:
        return ""
    query = str(match.group(1) or "").strip(" .!?")
    query = re.sub(r"(?i)^(?:por|sobre|no google|na internet|na web|na amazon|no mercado livre)\s+", "", query).strip()
    query = re.sub(r"(?i)\s+(?:no navegador|na internet|na web|no google)$", "", query).strip()
    query = re.split(
        r"(?i)\s+e\s+(?:me\s+)?(?:resume|resuma|resumir|diga|diz|fala|fale|explique|explica|conte|leia|ler)\b",
        query,
        maxsplit=1,
    )[0].strip(" .!?")
    query = re.sub(r"(?i)\b(?:o\s+)?primeiro\s+resultado\b", "", query).strip(" .!?")
    query = re.sub(r"(?i)\b(?:principais pontos|pontos principais|topicos)\b.*$", "", query).strip(" .!?")
    return re.sub(r"\s+", " ", query).strip()


def _sensitive_action(normalized: str) -> str:
    for sensitive_pattern, action in SENSITIVE_ACTION_PATTERNS:
        if re.search(sensitive_pattern, normalized):
            return action
    return ""


def classify_browser_intent(text: str) -> BrowserIntentClassification:
    normalized = _norm(text)
    if not normalized:
        return BrowserIntentClassification("none", reason="empty")
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, normalized):
            return BrowserIntentClassification("blocked", reason="blocked_non_browser_domain")

    sensitive_action = _sensitive_action(normalized)
    has_web_context = bool(re.search(WEB_CONTEXT_PATTERN, normalized) or re.search(OPEN_SITE_PATTERN, normalized))
    if sensitive_action and has_web_context:
        return BrowserIntentClassification(
            "browser_task",
            action="sensitive_action_confirmation",
            confidence=0.95,
            reason="sensitive_browser_action",
            requires_confirmation=True,
            sensitive_action=sensitive_action,
        )

    # If explicit open verb + site pattern present, treat as open_site before search routing.
    # Prevents "abre o Google" from being mis-routed as browser_search.
    _OPEN_VERB_EARLY = r"\b(abre|abra|abrir|abri|acessa|acesse|acessar|entra|entre|entrar|vai\s+(?:para|pro|pra|ao|a)|va\s+(?:para|pro|pra|ao|a)|navega?|navegue|me\s+leva)\b"
    if re.search(_OPEN_VERB_EARLY, normalized) and (
        re.search(OPEN_SITE_PATTERN, normalized) or re.search(OPEN_SITE_PATTERN_BROAD, normalized)
    ):
        return BrowserIntentClassification(
            "browser_task",
            action="open_site",
            confidence=0.88,
            reason="open_site_verb_priority",
        )

    has_search = bool(re.search(SEARCH_VERB_PATTERN, normalized))
    has_summary_cue = bool(re.search(SUMMARY_CUE_PATTERN, normalized))
    if has_search and has_summary_cue:
        query = _extract_search_query(text)
        return BrowserIntentClassification(
            "browser_task",
            action="search_and_summarize",
            confidence=0.95 if query else 0.75,
            reason="search_with_answer_request",
            query=query,
        )
    if has_search and (re.search(BROWSER_SEARCH_CONTEXT_PATTERN, normalized) or "navegador" in normalized):
        return BrowserIntentClassification(
            "browser_task",
            action="browser_search",
            confidence=0.90,
            reason="browser_search_pattern_matched",
            query=_extract_search_query(text),
        )
    if has_search:
        return BrowserIntentClassification(
            "browser_task",
            action="browser_search",
            confidence=0.82,
            reason="search_without_summary_request",
            query=_extract_search_query(text),
        )
    if re.search(r"\b(faz?\s+login|fazer\s+login|logar\s+n[ao]|loga\s+n[ao]|acessa?\s+minha\s+conta|entra?\s+com\s+minha\s+conta|sign\s*in)\b", normalized):
        return BrowserIntentClassification(
            "browser_task",
            action="assisted_login",
            confidence=0.90,
            reason="assisted_login_requested",
        )
    if re.search(OPEN_SITE_PATTERN, normalized) or re.search(OPEN_SITE_PATTERN_BROAD, normalized):
        return BrowserIntentClassification(
            "browser_task",
            action="open_site",
            confidence=0.85,
            reason="open_site_browser_action",
        )
    return BrowserIntentClassification("none", reason="not_browser_research")


def match_browser_task(text: str) -> BrowserTaskMatch:
    classification = classify_browser_intent(text)
    matched = classification.intent == "browser_task"
    logger.info(
        "BROWSER_INTENT_CLASSIFIED intent=%s action=%s matched=%s confidence=%.2f reason=%s query_chars=%s",
        classification.intent,
        classification.action or "none",
        str(matched).lower(),
        float(classification.confidence or 0.0),
        classification.reason,
        len(classification.query or ""),
    )
    logger.info(
        "BROWSER_ROUTING_DECISION_FINAL intent=%s action=%s matched=%s reason=%s",
        classification.intent,
        classification.action or "none",
        str(matched).lower(),
        classification.reason,
    )
    if not matched:
        if classification.reason and classification.intent != "none":
            logger.info("BROWSER_TASK_GATE_SKIPPED reason=%s", classification.reason)
        return BrowserTaskMatch(False, reason=classification.reason)

    logger.info("BROWSER_TASK_GATE_MATCHED reason=%s action=%s", classification.reason, classification.action)
    if classification.requires_confirmation:
        logger.info("BROWSER_TASK_WAITING_CONFIRMATION action=%s", classification.sensitive_action or "confirmation_required")
        logger.info("BROWSER_TASK_SENSITIVE_ACTION_BLOCKED reason=confirmation_required")
    return BrowserTaskMatch(
        True,
        reason=classification.reason,
        task=str(text or "").strip(),
        query=classification.query,
        action=classification.action or "",
        requires_confirmation=classification.requires_confirmation,
        sensitive_action=classification.sensitive_action,
    )


def is_browser_task(text: str) -> bool:
    return match_browser_task(text).matched


def is_browser_search_task(text: str) -> bool:
    return match_browser_task(text).matched
