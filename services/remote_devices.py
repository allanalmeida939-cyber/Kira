"""Qual aparelho o Chefe está usando agora (PC ou celular), para a Kira responder lá.

O celular e o PC ficam os dois ligados à mesma conversa. O aparelho usado por último (escreveu, apertou o microfone,
falou) é o "ativo": a voz da Kira toca nele (o outro fica mudo, só com a transcrição) e os painéis abrem nele. Cada
fala na transcrição leva de onde veio ("TEXTO · CELULAR", "VOZ · CELULAR").
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections import deque
from typing import Any, Callable

logger = logging.getLogger("kira.app.remote")

DEVICES = ("pc", "celular")
PHONE_TEXT_MATCH_S = 120.0     # texto escrito no celular que volta na transcrição em até 2 min leva o rótulo
PHONE_VOICE_IDLE_S = 90.0      # microfone do celular sem sinal de vida por 90 s: a voz volta para o PC


def _fold(text: object) -> str:
    plain = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(plain.split())


class DeviceTracker:
    def __init__(self, clock: Callable[[], float] = time.monotonic, *, on_change: Callable[[str], None] | None = None) -> None:
        self._clock = clock
        self._on_change = on_change
        self._active = "pc"
        self._changed_at = clock()
        self._phone_texts: deque[tuple[float, str, str]] = deque(maxlen=20)
        self._phone_voice_at = float("-inf")
        self._last_user_text = ""
        self._last_user_at = float("-inf")

    @property
    def active(self) -> str:
        if self._active == "celular" and self._phone_voice_at > float("-inf") and self._clock() - self._phone_voice_at > PHONE_VOICE_IDLE_S \
                and self._clock() - self._changed_at > PHONE_VOICE_IDLE_S:
            # celular sumiu com o microfone aberto (tela apagou, sem sinal): não deixa a Kira muda no PC para sempre
            self._phone_voice_at = float("-inf")
            self.set_active("pc", reason="phone_voice_idle")
        return self._active

    @property
    def phone_voice(self) -> bool:
        return self._clock() - self._phone_voice_at <= PHONE_VOICE_IDLE_S

    def set_active(self, device: str, *, reason: str = "") -> bool:
        if device not in DEVICES:
            raise ValueError(device)
        changed = device != self._active
        self._active = device
        self._changed_at = self._clock()
        if device == "pc":
            self._phone_voice_at = float("-inf")
        if changed:
            logger.info("REMOTE_ACTIVE_DEVICE device=%s reason=%s", device, reason or "-")
            if self._on_change is not None:
                try:
                    self._on_change(device)
                except Exception as exc:  # avisar a tela é extra
                    logger.info("REMOTE_ACTIVE_NOTIFY_FAILED reason=%s", type(exc).__name__)
        return changed

    def note_phone_text(self, text: str, turn_id: str = "") -> None:
        self._phone_texts.append((self._clock(), _fold(text), str(turn_id or "")))
        self._phone_voice_at = float("-inf")  # microfone ainda aberto volta a avisar em até 30 s
        self.set_active("celular", reason="phone_text")

    def note_phone_voice(self, on: bool) -> None:
        """Microfone do celular ligado (ou ainda ligado: o celular repete a cada 30 s) ou desligado."""
        if on:
            self._phone_voice_at = self._clock()
            self.set_active("celular", reason="phone_voice")
        else:
            self._phone_voice_at = float("-inf")

    def device_of(self, payload: dict[str, Any]) -> str | None:
        """De onde veio esta fala do usuário na transcrição: 'celular' ou None (PC)."""
        role = str(payload.get("speaker") or payload.get("role") or "").lower()
        if role not in {"user", "usuario", "voce", "você", "alan"}:
            return None
        now = self._clock()
        turn = str(payload.get("turn_id") or payload.get("client_turn_id") or "")
        text = _fold(payload.get("text"))
        for at, phone_text, phone_turn in self._phone_texts:
            if now - at <= PHONE_TEXT_MATCH_S and ((turn and turn == phone_turn) or (text and text == phone_text)):
                return "celular"
        kind = str(payload.get("message_type") or payload.get("source") or "").lower()
        is_voice = any(word in kind for word in ("voice", "voz", "speech", "audio", "transcript_final", "realtime", "livekit"))
        if is_voice and self._active == "celular" and self.phone_voice:
            return "celular"
        return None

    def wanted_device(self, within_s: float = 90.0) -> str | None:
        """Lugar dito no último pedido ("toca no computador", "abre no celular"); None = o aparelho em uso."""
        if self._clock() - self._last_user_at > within_s:
            return None
        return said_device(self._last_user_text)

    def tag(self, payload: dict[str, Any]) -> dict[str, Any]:
        role = str(payload.get("speaker") or payload.get("role") or "").lower()
        if role in {"user", "usuario", "voce", "você", "alan"} and payload.get("text"):
            self._last_user_text = str(payload.get("text") or "")
            self._last_user_at = self._clock()
        device = self.device_of(payload)
        return {**payload, "device": device} if device else payload

    def snapshot(self) -> dict[str, Any]:
        return {"device": self.active, "phone_voice": self.phone_voice}


_PC_WORDS = re.compile(r"\b(n[oa] (computador|pc|notebook|tela do pc|tela grande|monitor)|pelo (computador|pc))\b")
_PHONE_WORDS = re.compile(r"\b(n[oa] (celular|telefone|smartphone)|pelo celular)\b")


def said_device(text: str) -> str | None:
    """'pc' se o pedido diz o computador, 'celular' se diz o celular, None se não diz lugar."""
    folded = _fold(text)
    if _PC_WORDS.search(folded):
        return "pc"
    if _PHONE_WORDS.search(folded):
        return "celular"
    return None


__all__ = ["DEVICES", "DeviceTracker", "said_device"]
