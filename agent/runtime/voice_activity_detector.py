from __future__ import annotations

import logging
from collections import deque
from typing import Optional

import numpy as np

torch = None
AF = None


logger = logging.getLogger(__name__)


class VoiceActivityDetector:
    def __init__(
        self,
        sample_rate: int = 48000,
        threshold_start: float = 0.30,
        threshold_continue: float = 0.20,
        min_speech_duration_ms: int = 150,
        min_silence_duration_ms: int = 600,
        speech_pad_ms: int = 800,
        enabled: bool = True,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.vad_sample_rate = 16000
        self.threshold_start = float(threshold_start)
        self.threshold_continue = float(threshold_continue)
        self.threshold = self.threshold_start
        self.min_speech_duration_ms = int(min_speech_duration_ms)
        self.min_silence_duration_ms = int(min_silence_duration_ms)
        self.speech_pad_ms = int(speech_pad_ms)
        self.enabled = bool(enabled)
        self._model = None

        self.pre_buffer_size = int(self.sample_rate * (self.speech_pad_ms / 1000.0))
        self._pre_buffer: deque[float] = deque(maxlen=max(self.pre_buffer_size, 1))
        self._vad_tail = np.zeros(512, dtype=np.float32)
        self._state = "idle"
        self._state_duration_ms = 0.0
        self._speech_duration_ms = 0.0
        self._silence_duration_ms = 0.0
        self._is_speaking = False

        if self.enabled:
            self._load_model()

    def _load_model(self) -> None:
        global torch, AF
        try:
            logger.info("[VAD] Carregando modelo Silero VAD...")
            import torch

            try:
                import torchaudio.functional as AF
            except Exception:  # pragma: no cover - fallback quando torchaudio nao estiver disponivel
                AF = None
            model, _utils = torch.hub.load(
                "snakers4/silero-vad",
                "silero_vad",
                force_reload=False,
                onnx=False,
                trust_repo=True,
            )
            self._model = model.eval()
            logger.info(
                "[VAD] Modelo carregado - threshold_start=%.2f threshold_continue=%.2f speech_pad=%dms",
                self.threshold_start,
                self.threshold_continue,
                self.speech_pad_ms,
            )
        except Exception as exc:
            logger.error("[VAD] Falha ao carregar modelo Silero: %s", exc)
            self.enabled = False
            self._model = None

    def _set_state(self, state: str, *, duration_ms: float = 0.0) -> None:
        if self._state != state:
            logger.debug("[VAD] state %s -> %s", self._state, state)
        self._state = state
        self._state_duration_ms = max(duration_ms, 0.0)

    def _resample_audio(self, audio_float: np.ndarray) -> np.ndarray:
        if audio_float.size == 0:
            return audio_float.astype(np.float32, copy=False)
        if self.sample_rate == self.vad_sample_rate:
            return audio_float.astype(np.float32, copy=False)

        if AF is not None:
            tensor = torch.from_numpy(audio_float).view(1, -1)
            resampled = AF.resample(tensor, self.sample_rate, self.vad_sample_rate)
            return resampled.squeeze(0).contiguous().cpu().numpy().astype(np.float32, copy=False)

        duration_s = len(audio_float) / float(self.sample_rate)
        target_len = max(int(round(duration_s * self.vad_sample_rate)), 1)
        source_x = np.linspace(0.0, 1.0, num=len(audio_float), endpoint=False)
        target_x = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
        return np.interp(target_x, source_x, audio_float).astype(np.float32, copy=False)

    def _build_vad_window(self, audio_resampled: np.ndarray) -> np.ndarray:
        if audio_resampled.size >= 512:
            vad_window = audio_resampled[-512:]
        else:
            needed = 512 - audio_resampled.size
            vad_window = np.concatenate([self._vad_tail[-needed:], audio_resampled])
        self._vad_tail = vad_window.astype(np.float32, copy=False)
        return self._vad_tail

    def _infer_speech_probability(self, audio_resampled: np.ndarray) -> float:
        vad_window = self._build_vad_window(audio_resampled)
        vad_input = torch.from_numpy(vad_window)
        with torch.no_grad():
            return float(self._model(vad_input, self.vad_sample_rate).item())

    def process_chunk(self, audio_chunk: np.ndarray) -> tuple[Optional[np.ndarray], bool, bool]:
        if not self.enabled or self._model is None:
            return audio_chunk, True, False

        audio = np.asarray(audio_chunk)
        audio_flat = audio.reshape(-1)
        if audio_flat.size == 0:
            return None, False, False

        for sample in audio_flat:
            self._pre_buffer.append(float(sample))

        if audio_flat.dtype == np.int16:
            audio_float = audio_flat.astype(np.float32) / 32767.0
        else:
            audio_float = audio_flat.astype(np.float32, copy=False)

        audio_resampled = self._resample_audio(audio_float)
        chunk_duration_ms = (len(audio_flat) / self.sample_rate) * 1000.0

        try:
            speech_prob = self._infer_speech_probability(audio_resampled)
        except Exception as exc:
            logger.error("[VAD] Erro na inferencia: %s", exc)
            return None, False, False

        self._state_duration_ms += chunk_duration_ms
        speech_started = False

        if self._state == "idle":
            if speech_prob >= self.threshold_start:
                self._speech_duration_ms = chunk_duration_ms
                self._silence_duration_ms = 0.0
                self._set_state("maybe_speech", duration_ms=chunk_duration_ms)
            return None, False, False

        if self._state == "maybe_speech":
            if speech_prob >= self.threshold_continue:
                self._speech_duration_ms += chunk_duration_ms
                if self._speech_duration_ms >= self.min_speech_duration_ms:
                    self._is_speaking = True
                    self._silence_duration_ms = 0.0
                    self._set_state("speech", duration_ms=chunk_duration_ms)
                    speech_started = True
                    pre = np.array(self._pre_buffer, dtype=audio_flat.dtype)
                    logger.info(
                        "[VAD] FALA INICIADA - prob=%.2f pre_buffer=%d samples",
                        speech_prob,
                        len(pre),
                    )
                    return pre, True, True
                return None, False, False

            self._speech_duration_ms = 0.0
            self._set_state("idle")
            return None, False, False

        if self._state == "speech":
            if speech_prob >= self.threshold_continue:
                self._silence_duration_ms = 0.0
                self._state_duration_ms = chunk_duration_ms
                return audio_chunk, True, False
            self._silence_duration_ms = chunk_duration_ms
            self._set_state("maybe_silence", duration_ms=chunk_duration_ms)
            return audio_chunk, True, False

        if self._state == "maybe_silence":
            if speech_prob >= self.threshold_continue:
                self._silence_duration_ms = 0.0
                self._set_state("speech", duration_ms=chunk_duration_ms)
                return audio_chunk, True, False

            self._silence_duration_ms += chunk_duration_ms
            if self._silence_duration_ms >= self.min_silence_duration_ms:
                silence_ms = self._silence_duration_ms
                self._is_speaking = False
                self._speech_duration_ms = 0.0
                self._silence_duration_ms = 0.0
                self._set_state("idle")
                logger.info("[VAD] FALA FINALIZADA - silence_duration=%.0fms", silence_ms)
                return None, False, False
            return audio_chunk, True, False

        self._set_state("idle")
        return None, False, False

    def get_stats(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "sample_rate": self.sample_rate,
            "vad_sample_rate": self.vad_sample_rate,
            "threshold": self.threshold,
            "threshold_start": self.threshold_start,
            "threshold_continue": self.threshold_continue,
            "state": self._state,
            "is_speaking": self._is_speaking,
            "pre_buffer_size": len(self._pre_buffer),
            "speech_pad_ms": self.speech_pad_ms,
        }


__all__ = ["VoiceActivityDetector"]
