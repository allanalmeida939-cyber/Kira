"""
Interceptor de comandos - executa ANTES do LLM.
"""
from __future__ import annotations

import logging
import time
from typing import Tuple

if __package__ and __package__.startswith("agent"):
    from agent.core.browser_task_gate import classify_browser_intent
    from agent.core.command_intent import classify_explicit_command
else:  # pragma: no cover - suporte a execucao direta em agent/
    from core.browser_task_gate import classify_browser_intent
    from core.command_intent import classify_explicit_command

logger = logging.getLogger(__name__)

_LAST_SIGNATURE = ""
_LAST_AT = 0.0
_DEDUP_SECONDS = 20.0


def should_intercept(user_input: str) -> bool:
    """Verifica se input e um comando que deve ser interceptado."""
    text = str(user_input or "").strip()
    if not text:
        return False
    text_lower = text.lower()
    voice_profile_keywords = (
        "quem você conhece",
        "quem voce conhece",
        "quem vc conhece",
        "cadastra minha voz",
        "salva minha voz",
        "grava minha voz",
        "registra minha voz",
        "lista perfis",
        "lista os perfis",
        "esquece minha voz",
        "deleta meu perfil",
        "volta a falar",
        "reseta a voz",
        "resetar voz",
        "voz de volta",
    )
    if any(kw in text_lower for kw in voice_profile_keywords):
        logger.info("[INTERCEPT] Comando de voice profile detectado")
        return True
    decision = classify_explicit_command(text)
    logger.info(
        "[INTENT] text=%r is_command=%s confidence=%.2f action=browser_intercept reason=%s",
        text[:120],
        decision.is_command,
        decision.confidence,
        decision.reason,
    )
    return decision.is_command


def execute_intercepted_command(user_input: str) -> Tuple[bool, str]:
    """
    Executa comando interceptado.

    Returns:
        (executado: bool, mensagem_de_confirmacao: str)
    """
    global _LAST_SIGNATURE, _LAST_AT

    text = str(user_input or "").strip()
    decision = classify_explicit_command(text)
    logger.info("[INTERCEPT] checking_command input=%r", text[:50])
    logger.info(
        "[INTENT] text=%r is_command=%s confidence=%.2f action=browser_execute reason=%s",
        text[:120],
        decision.is_command,
        decision.confidence,
        decision.reason,
    )
    if not decision.is_command:
        logger.info("[INTERCEPT] ignored_non_explicit_command")
        return False, ""

    lowered = text.lower()
    browser_cmd = any(word in lowered for word in ("site", "google", "internet", "navegador", "pesquisa", "pesquisar"))
    if browser_cmd:
        browser_decision = classify_browser_intent(text)
        if browser_decision.intent == "browser_task":
            logger.info(
                "BROWSER_ROUTER_SKIPPED_ALREADY_DECIDED intent=%s action=%s reason=%s",
                browser_decision.intent,
                browser_decision.action,
                browser_decision.reason,
            )
            logger.info("FUZZY_BROWSER_BLOCKED_BY_FINAL_DECISION action=%s", browser_decision.action)
            return False, ""
        logger.info("[INTERCEPT] browser_agent_command_detected")
        signature = f"browser-agent:{text.lower()[:80]}"
        now = time.monotonic()
        if signature == _LAST_SIGNATURE and (now - _LAST_AT) <= _DEDUP_SECONDS:
            logger.warning(
                "[INTERCEPT] duplicate_command_suppressed cmd=browser_agent elapsed_s=%.1f",
                now - _LAST_AT,
            )
            return True, ""

        _LAST_SIGNATURE = signature
        _LAST_AT = now
        message = "Fechado, chefe. Reconheci a pesquisa e vou encaminhar para o navegador da Kira."
        logger.info("BROWSER_ACTION_REQUESTED source=browser_intercept text=%r", text[:120])
        logger.info("BROWSER_ACTION_STARTED source=browser_intercept status=ack_only")
        logger.info("BROWSER_ACTION_COMPLETED source=browser_intercept status=ack_dispatched")
        logger.info("REPLY_START_DELAY_SOURCE source=browser_intercept reason=ack_before_browser_execution")
        logger.info("[INTERCEPT] browser_agent_routed message=%r", message)
        return True, message

    logger.info("[INTERCEPT] not_a_command proceeding_normally")
    return False, ""
