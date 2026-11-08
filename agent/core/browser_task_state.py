"""Tarefa no navegador da Kira esperando resposta do Chefe: enquanto isso, a próxima fala dele vai para o navegador
(atalho de voz em try_handle_browser_task_command; "cancela" para a tarefa). Quem liga e desliga: runtime/browser_tools.py.

Pergunta de senha: a fala seguinte do Chefe é a senha. O guarda da transcrição (runtime/secret_transcript_guard.py) a
guarda aqui, só na memória, e o resto do agente (logs, transcrição, roteador, memórias) recebe SECRET_PLACEHOLDER.

Um estado só no processo: importado como "core.browser_task_state" e como "agent.core.browser_task_state", o Python
cria dois módulos; com o estado em cada um, a resposta falada nunca chegava ao navegador (2026-10-25).
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import types
import unicodedata

logger = logging.getLogger("kira.browser_task_state")

SECRET_PLACEHOLDER = "senha falada, enviada ao navegador"

_STATE = sys.modules.setdefault("_kira_browser_task_state", types.ModuleType("_kira_browser_task_state"))
if not hasattr(_STATE, "lock"):
    _STATE.lock = threading.Lock()
    _STATE.waiting = False
    _STATE.secret_expected = False
    _STATE.spoken_secret = ""
    _STATE.answered_at = 0.0
    _STATE.answered_text = ""


def _fold(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").lower())
    return " ".join("".join(ch for ch in raw if not unicodedata.combining(ch) and (ch.isalnum() or ch.isspace())).split())


def set_browser_task_waiting(value: bool, *, secret: bool = False) -> None:
    with _STATE.lock:
        changed = _STATE.waiting != bool(value) or _STATE.secret_expected != bool(value and secret)
        _STATE.waiting = bool(value)
        _STATE.secret_expected = bool(value and secret)
        if not _STATE.waiting:
            _STATE.spoken_secret = ""
    if changed:
        logger.info("BROWSER_TASK_WAITING_SET value=%s secret=%s", str(bool(value)).lower(), str(bool(value and secret)).lower())


def is_browser_task_waiting() -> bool:
    with _STATE.lock:
        return _STATE.waiting


def is_browser_secret_expected() -> bool:
    with _STATE.lock:
        return _STATE.secret_expected


def stash_spoken_secret(text: str) -> None:
    with _STATE.lock:
        _STATE.spoken_secret = str(text or "")


def pop_spoken_secret() -> str:
    with _STATE.lock:
        value, _STATE.spoken_secret = _STATE.spoken_secret, ""
        return value


def note_answered(text: str) -> None:
    """Resposta que acabou de ir para o navegador (senha: o marcador)."""
    with _STATE.lock:
        _STATE.answered_at, _STATE.answered_text = time.monotonic(), _fold(text)


def answered_recently(text: str, *, within: float = 8.0) -> bool:
    """A mesma fala já foi respondida há pouco pelo outro caminho: não responder de novo."""
    with _STATE.lock:
        return bool(_STATE.answered_text) and _STATE.answered_text == _fold(text) and time.monotonic() - _STATE.answered_at <= within


__all__ = [
    "SECRET_PLACEHOLDER",
    "answered_recently",
    "is_browser_secret_expected",
    "is_browser_task_waiting",
    "note_answered",
    "pop_spoken_secret",
    "set_browser_task_waiting",
    "stash_spoken_secret",
]
