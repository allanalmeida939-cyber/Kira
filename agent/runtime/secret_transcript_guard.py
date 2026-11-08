"""Senha falada para o navegador da Kira não aparece em lugar nenhum.

Com o navegador esperando uma senha, a transcrição da fala do Chefe é trocada por SECRET_PLACEHOLDER no próprio evento
da sessão do Gemini, antes de qualquer ouvinte: logs, transcrição na tela, histórico, roteador e memórias só veem o
marcador. O texto de verdade fica guardado na memória (browser_task_state) até o atalho do navegador mandá-lo para a
tarefa. "Cancela" e "eu mesmo digito" passam normalmente. O próprio Gemini ouviu o áudio: isso não dá para evitar.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import unicodedata

try:  # mesma ordem do resto do agente: "core" e "agent.core" seriam módulos (e estados) diferentes
    from agent.core import browser_task_state as bts
except ImportError:  # pragma: no cover - execução de dentro de agent/ sem o pacote
    from core import browser_task_state as bts

logger = logging.getLogger(__name__)

EVENT = "input_audio_transcription_completed"
_PASS_THROUGH = re.compile(
    r"\b(cancela\w*|esquece|deixa (pra|para) la|para a tarefa|desiste|eu (mesmo )?digito|vou digitar|digito eu|"
    r"digitar (eu mesmo|na tela))\b"
)


# Ordem para a página no lugar da senha ("aperta em esqueceu a senha", "não sei a senha", "clica no anúncio"):
# passa como fala normal e o navegador deixa de esperar senha, senão a ordem virava a "senha" digitada.
_PAGE_COMMAND = re.compile(
    r"\b(esquec\w*|nao sei (a |minha )?senha|nao lembro|aperta\w*|clica\w*|clique|toca (em|no|na|ali)|seleciona\w*|"
    r"rola\w*|desce|volta\w*|recuper\w*|redefin\w*|troca\w* (a )?senha|muda\w* (a )?senha)\b"
)


def _fold(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").lower())
    return " ".join("".join(ch for ch in raw if not unicodedata.combining(ch)).split())


def mask_event(event):
    """Evento com a transcrição trocada pelo marcador (cópia; o original não muda)."""
    if dataclasses.is_dataclass(event) and not isinstance(event, type):
        return dataclasses.replace(event, transcript=bts.SECRET_PLACEHOLDER)
    setattr(event, "transcript", bts.SECRET_PLACEHOLDER)
    return event


def guard_transcription_event(event):
    """Devolve o evento que os ouvintes devem ver. Sem senha esperada, é o próprio evento."""
    if not bts.is_browser_secret_expected():
        return event
    text = str(getattr(event, "transcript", "") or "")
    if not text.strip():
        return event
    if bool(getattr(event, "is_final", False)):
        if _PASS_THROUGH.search(_fold(text)):
            return event
        if _PAGE_COMMAND.search(_fold(text)):
            bts.set_browser_task_waiting(True, secret=False)
            logger.info("BROWSER_SECRET_SKIPPED_PAGE_COMMAND")
            return event
        bts.stash_spoken_secret(text)
        logger.info("BROWSER_SECRET_SPOKEN_CAPTURED")
    return mask_event(event)


def install_secret_transcript_guard(realtime_session) -> bool:
    """Envolve o emit da sessão do Gemini (uma vez por sessão). Falha aqui nunca derruba a conversa."""
    if realtime_session is None or getattr(realtime_session, "_kira_secret_guard", False):
        return False
    original_emit = getattr(realtime_session, "emit", None)
    if not callable(original_emit):
        return False

    def guarded_emit(event, *args):
        if event == EVENT and args:
            try:
                args = (guard_transcription_event(args[0]), *args[1:])
            except Exception as exc:
                logger.warning("BROWSER_SECRET_GUARD_FAILED reason=%s", type(exc).__name__)
        return original_emit(event, *args)

    try:
        realtime_session.emit = guarded_emit
        realtime_session._kira_secret_guard = True
    except Exception as exc:
        logger.warning("BROWSER_SECRET_GUARD_NOT_INSTALLED reason=%s", type(exc).__name__)
        return False
    logger.info("BROWSER_SECRET_GUARD_INSTALLED")
    return True


__all__ = ["guard_transcription_event", "install_secret_transcript_guard", "mask_event"]
