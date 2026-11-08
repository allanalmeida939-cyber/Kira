"""Gatilho de seguranca: abre midia quando o usuario pede ou a Kira promete mostrar."""

from __future__ import annotations

import asyncio
import re
import time

from services.media_topics import clean_topic, detect_user_media_request, fold

try:
    from agent.runtime.media_tools import request_media
except ImportError:  # pragma: no cover - execucao com agent/ no sys.path
    from runtime.media_tools import request_media


_PROMISE_RE = re.compile(
    r"\b(deixa\s+eu\s+(?:te\s+)?(?:mostrar|abrir)|vou\s+(?:te\s+)?(?:mostrar|abrir)|"
    r"(?:da|d[aá])\s+uma\s+olhada|olha\s+(?:so\s+)?(?:as|os|na|nas)|painei?s?|separei\s+(?:umas?|algumas?)|"
    r"abri\s+(?:uns?|alguns?)\s+painei?s?|imagens\s+e\s+artigos)\b",
)
_REPLY_STOP_PROPER = frozenset({"claro", "alan", "chefe", "chef", "saquei", "bom", "boa", "olha", "deixa", "tudo", "kira"})


def reply_promises_visuals(reply_text: str) -> bool:
    return bool(_PROMISE_RE.search(fold(reply_text)))


def topic_from_reply(reply_text: str) -> str:
    """Primeiro nome proprio no meio de uma frase da resposta (ex.: Chernobyl)."""
    for sentence in re.split(r"(?<=[.!?])\s+", reply_text):
        words = re.findall(r"[\w'-]+", sentence)
        for index, word in enumerate(words[1:], start=1):
            if word[:1].isupper() and len(word) > 3 and fold(word) not in _REPLY_STOP_PROPER:
                return word
    return ""


def derive_topic(user_text: str, reply_text: str, recent: str = "") -> str:
    """Tema pedido pelo usuario, senao o da conversa recente, senao um nome proprio da resposta."""
    return detect_user_media_request(user_text) or recent or topic_from_reply(reply_text)


RECENT_TOPIC_SECONDS = 600.0


class MediaAutoShow:
    """Observa as falas finais e dispara request_media sem bloquear a conversa."""

    def __init__(self) -> None:
        self._last_user_text = ""
        self._recent_topic = ""
        self._recent_at = 0.0
        self._tasks: set[asyncio.Task[object]] = set()

    def _spawn(self, topic: str) -> None:
        task = asyncio.ensure_future(self._run(topic))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _run(topic: str) -> None:
        try:
            await asyncio.wait_for(request_media(topic), timeout=2.5)
        except Exception:
            return

    def _recent(self) -> str:
        return self._recent_topic if time.monotonic() - self._recent_at < RECENT_TOPIC_SECONDS else ""

    def on_user_final(self, text: str) -> None:
        self._last_user_text = str(text or "")
        topic = detect_user_media_request(self._last_user_text)
        if topic:
            self._recent_topic, self._recent_at = topic, time.monotonic()
            self._spawn(topic)

    def on_kira_final(self, text: str) -> None:
        if not reply_promises_visuals(text):
            return
        topic = derive_topic(self._last_user_text, text, self._recent())
        if topic:
            self._spawn(topic)


__all__ = ["MediaAutoShow", "derive_topic", "detect_user_media_request", "reply_promises_visuals", "topic_from_reply"]


_TEXT_AUTOSHOW: MediaAutoShow | None = None


def media_autoshow_for_text(text: str) -> None:
    """Mensagem digitada (canal de dados da UI) aciona o visualizador pela mesma regra da voz."""
    global _TEXT_AUTOSHOW
    if _TEXT_AUTOSHOW is None:
        _TEXT_AUTOSHOW = MediaAutoShow()
    _TEXT_AUTOSHOW.on_user_final(text)
