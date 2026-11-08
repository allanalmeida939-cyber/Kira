"""Leva a tela compartilhada para a conversa do Gemini Live como imagem.

O modelo de áudio nativo ignora os quadros de vídeo em tempo real que o LiveKit encaminha (medido em 2026-10-21: "não
estou recebendo nenhuma imagem de vídeo"), mas enxerga uma imagem enviada dentro da conversa. Então, com a visão
ligada: quando a tela muda de verdade e já passaram alguns segundos, o quadro reduzido vai para o contexto — só com a
Kira quieta, para não cortar fala.
"""

from __future__ import annotations

import asyncio
import os
import time

import cv2
import numpy as np

MIN_INTERVAL_SECONDS = max(float(os.getenv("KIRA_SCREEN_FEED_INTERVAL_SECONDS", "6") or 6), 2.0)
MAX_WIDTH = 1024
CHANGE_THRESHOLD = 6.0  # diferença média (0-255) na miniatura cinza; abaixo disso é a mesma tela


def signature(frame_bgr: np.ndarray) -> np.ndarray:
    """Miniatura cinza 48x27: barata e suficiente para saber se a tela mudou."""
    small = cv2.resize(frame_bgr, (48, 27), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.int16)


def changed(previous: np.ndarray | None, current: np.ndarray) -> bool:
    return previous is None or float(np.abs(current - previous).mean()) >= CHANGE_THRESHOLD


def encode(frame_bgr: np.ndarray) -> bytes:
    height, width = frame_bgr.shape[:2]
    if width > MAX_WIDTH:
        frame_bgr = cv2.resize(frame_bgr, (MAX_WIDTH, round(height * MAX_WIDTH / width)), interpolation=cv2.INTER_AREA)
    ok, data = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
    return data.tobytes() if ok else b""


class ScreenContextFeed:
    def __init__(self, session_provider, *, clock=time.monotonic) -> None:
        self._session_provider = session_provider
        self._clock = clock
        self._last_sent_at = float("-inf")
        self._last_signature: np.ndarray | None = None
        self._busy = False

    def reset(self) -> None:
        """Compartilhamento novo: o primeiro quadro vai logo."""
        self._last_sent_at = float("-inf")
        self._last_signature = None

    async def offer(self, frame_bgr: np.ndarray) -> bool:
        if self._busy or self._clock() - self._last_sent_at < MIN_INTERVAL_SECONDS:
            return False
        session = self._session_provider()
        if session is None:
            return False
        current = signature(frame_bgr)
        if float(current.std()) < 2.0 or not changed(self._last_signature, current):
            return False  # tela lisa (quadro preto ao parar o compartilhamento) ou igual à última enviada
        self._busy = True
        try:
            jpeg = await asyncio.to_thread(encode, frame_bgr)
            if jpeg and session.send_screen_image(jpeg):
                self._last_sent_at = self._clock()
                self._last_signature = current
                return True
            return False
        finally:
            self._busy = False


__all__ = ["ScreenContextFeed", "changed", "encode", "signature"]
