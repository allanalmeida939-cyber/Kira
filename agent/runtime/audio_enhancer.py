from __future__ import annotations

import logging
from collections import deque

import numpy as np


logger = logging.getLogger(__name__)


class AudioEnhancer:
    """Pipeline leve para captacao mais sensivel sem quebrar o fluxo atual."""

    def __init__(
        self,
        sample_rate: int = 48000,
        target_rms: float = 0.1,
        agc_attack: float = 0.6,
        agc_release: float = 0.15,
        noise_gate_threshold_db: float = -50.0,
        ring_buffer_seconds: float = 2.0,
        pre_emphasis: float = 0.35,
        max_gain: float = 8.0,
        enabled: bool = True,
    ) -> None:
        self.sample_rate = max(int(sample_rate), 1)
        self.target_rms = max(float(target_rms), 0.001)
        self.agc_attack = max(float(agc_attack), 0.001)
        self.agc_release = max(float(agc_release), 0.001)
        self.noise_gate_threshold_db = float(noise_gate_threshold_db)
        self.noise_gate_threshold = float(10 ** (self.noise_gate_threshold_db / 20.0))
        self.ring_buffer_seconds = max(float(ring_buffer_seconds), 0.1)
        self.pre_emphasis = float(pre_emphasis)
        self.max_gain = max(float(max_gain), 1.0)
        self.enabled = bool(enabled)

        self._current_gain = 1.0
        self._noise_floor = max(self.noise_gate_threshold * 0.5, 1e-5)
        self._frames_processed = 0
        buffer_size = max(int(self.sample_rate * self.ring_buffer_seconds), 1)
        self._ring_buffer: deque[float] = deque(maxlen=buffer_size)

        if not self.enabled:
            logger.warning("[AUDIO_ENHANCER] Desabilitado - enabled=False")
            return

        logger.info(
            "[AUDIO_ENHANCER] Inicializado - sr=%d target_rms=%.2f gate=%.1fdB lookback=%.1fs",
            self.sample_rate,
            self.target_rms,
            self.noise_gate_threshold_db,
            self.ring_buffer_seconds,
        )

    def _to_float32(self, audio: np.ndarray) -> np.ndarray:
        work = np.asarray(audio).reshape(-1)
        if work.dtype == np.int16:
            return work.astype(np.float32) / 32767.0
        return work.astype(np.float32, copy=False)

    def _restore_dtype(
        self,
        audio_float: np.ndarray,
        *,
        dtype: np.dtype | type[np.generic],
        shape: tuple[int, ...] | None = None,
    ) -> np.ndarray:
        if np.issubdtype(np.dtype(dtype), np.integer):
            restored = np.clip(audio_float * 32767.0, -32768.0, 32767.0).astype(dtype)
        else:
            restored = audio_float.astype(dtype, copy=False)
        if shape is not None:
            return restored.reshape(shape)
        return restored

    def _update_noise_floor(self, rms: float) -> None:
        if rms <= 0.0:
            return
        if rms < self._noise_floor * 1.5:
            alpha = 0.01
        else:
            alpha = 0.001
        self._noise_floor = (1.0 - alpha) * self._noise_floor + alpha * rms

    def _compute_gain(self, rms: float) -> float:
        if rms <= 1e-6:
            return self._current_gain
        target_gain = min(self.target_rms / rms, self.max_gain)
        smoothing = self.agc_attack if target_gain > self._current_gain else self.agc_release
        self._current_gain += (target_gain - self._current_gain) * smoothing
        return self._current_gain

    def process_frame(self, audio_frame: np.ndarray) -> np.ndarray:
        if not self.enabled:
            return audio_frame

        audio = np.asarray(audio_frame)
        if audio.size == 0:
            return audio_frame

        original_dtype = audio.dtype
        original_shape = audio.shape
        audio_float = self._to_float32(audio)

        rms = float(np.sqrt(np.mean(np.square(audio_float)))) if audio_float.size else 0.0
        self._update_noise_floor(rms)
        gate_threshold = max(self.noise_gate_threshold, self._noise_floor * 2.0)

        if rms < gate_threshold:
            audio_float = audio_float * 0.25

        gain = self._compute_gain(max(rms, 1e-6))
        enhanced = np.clip(audio_float * gain, -1.0, 1.0)

        if enhanced.size > 1 and self.pre_emphasis != 0.0:
            emphasized = enhanced.copy()
            emphasized[1:] = enhanced[1:] - (self.pre_emphasis * enhanced[:-1])
            enhanced = np.clip(emphasized, -1.0, 1.0)

        self._ring_buffer.extend(float(sample) for sample in enhanced)
        self._frames_processed += 1
        return self._restore_dtype(enhanced, dtype=original_dtype, shape=original_shape)

    def process_chunk(self, audio_chunk: np.ndarray) -> np.ndarray:
        return self.process_frame(audio_chunk)

    def get_lookback_audio(
        self,
        *,
        dtype: np.dtype | type[np.generic] = np.int16,
    ) -> np.ndarray:
        if not self._ring_buffer:
            return np.array([], dtype=dtype)
        buffered = np.asarray(self._ring_buffer, dtype=np.float32)
        return self._restore_dtype(buffered, dtype=dtype)

    def get_stats(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "sample_rate": self.sample_rate,
            "target_rms": self.target_rms,
            "ring_buffer_seconds": self.ring_buffer_seconds,
            "ring_buffer_samples": len(self._ring_buffer),
            "noise_floor": round(self._noise_floor, 6),
            "current_gain": round(self._current_gain, 4),
            "frames_processed": self._frames_processed,
        }


__all__ = ["AudioEnhancer"]
