"""Cadastro de voz com barra de progresso: a Kira grava a pessoa ao longo de várias falas até ter voz suficiente.

Antes o cadastro usava só os últimos 8 s do turno; contando só os trechos com voz (sem as pausas) quase nunca dava os
6 s pedidos, e a Kira repetia "ainda é pouca voz" sem fim (sessão de 2026-10-23 05:53). Agora a fala vai somando
(a da própria Kira não entra) e a barra na tela mostra quanto falta; cheia, a voz é salva e a Kira dá o ok.
"""

from __future__ import annotations

from typing import Callable

import numpy as np

SAMPLE_RATE = 16000
# 30 s de fala variada (frase normal, comandos, mais alto, mais baixo, rápido), como o Google e a Alexa pedem várias
# frases: 14 janelas de 3 s cobrindo os jeitos de falar do dia a dia.
TARGET_SECONDS = 30.0
TIMEOUT_SECONDS = 150.0
PROGRESS_STEP = 0.05  # a barra na tela é atualizada a cada 5%


class VoiceEnrollment:
    def __init__(self, name: str, *, target_seconds: float = TARGET_SECONDS, on_progress: Callable[[float], None] | None = None) -> None:
        self.name = name
        self._target = int(target_seconds * SAMPLE_RATE)
        self._chunks: list[np.ndarray] = []
        self._samples = 0
        self._shown = -1.0
        self._on_progress = on_progress

    @property
    def progress(self) -> float:
        return min(1.0, self._samples / self._target)

    def feed(self, audio: np.ndarray) -> bool:
        """Mais um trecho com voz; True quando já tem o suficiente."""
        if audio.size:
            self._chunks.append(np.asarray(audio, dtype=np.float32).reshape(-1))
            self._samples += audio.size
        progress = self.progress
        if self._on_progress is not None and (progress >= 1.0 or progress - self._shown >= PROGRESS_STEP):
            self._shown = progress
            self._on_progress(progress)
        return progress >= 1.0

    def audio(self) -> np.ndarray:
        return np.concatenate(self._chunks) if self._chunks else np.array([], dtype=np.float32)


__all__ = ["TARGET_SECONDS", "TIMEOUT_SECONDS", "VoiceEnrollment"]
