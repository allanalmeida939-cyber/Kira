from __future__ import annotations

import logging
import time

import numpy as np

logger = logging.getLogger(__name__)


class AudioDenoiser:
    def __init__(self, sample_rate: int = 48000, enabled: bool = True) -> None:
        self.sample_rate = int(sample_rate)
        self.enabled = bool(enabled)
        self.frame_size = int(self.sample_rate * 0.01)
        self._stationary = True
        self._prop_decrease = 1.0
        self._frames_processed = 0
        self._total_process_ms = 0.0
        self._short_frame_passthrough_logged = False

        if not self.enabled:
            logger.info("AUDIO_DENOISER_CONFIG enabled=false fallback=passthrough")
            return
        logger.info("AUDIO_DENOISER_CONFIG enabled=true backend=noisereduce sample_rate=%d", self.sample_rate)

    def process_frame(self, audio_frame: np.ndarray) -> np.ndarray:
        if not self.enabled:
            return audio_frame

        original_shape = audio_frame.shape
        original_dtype = audio_frame.dtype
        work = audio_frame.reshape(-1)

        if len(work) != self.frame_size:
            return audio_frame
        if len(work) < 1024:
            if not self._short_frame_passthrough_logged:
                logger.info(
                    "AUDIO_DENOISER_PASSTHROUGH reason=frame_too_short frame_size=%s min_size=1024",
                    len(work),
                )
                self._short_frame_passthrough_logged = True
            return audio_frame

        try:
            import noisereduce as nr

            if original_dtype == np.int16:
                frame_float = work.astype(np.float32) / 32767.0
            else:
                frame_float = work.astype(np.float32)

            t0 = time.perf_counter()
            denoised = nr.reduce_noise(
                y=frame_float,
                sr=self.sample_rate,
                stationary=self._stationary,
                prop_decrease=self._prop_decrease,
            )
            self._total_process_ms += (time.perf_counter() - t0) * 1000.0
            self._frames_processed += 1

            out = np.asarray(denoised)
            if original_dtype == np.int16:
                out = np.clip(out * 32767.0, -32768, 32767).astype(np.int16)
            else:
                out = out.astype(original_dtype, copy=False)
            return out.reshape(original_shape)
        except Exception as exc:
            logger.error("[DENOISER] Erro ao processar frame: %s", exc)
            return audio_frame

    def process_chunk(self, audio_chunk: np.ndarray) -> np.ndarray:
        if not self.enabled:
            return audio_chunk

        original_shape = audio_chunk.shape
        original_dtype = audio_chunk.dtype
        flat = audio_chunk.reshape(-1)
        total = len(flat)
        if total == 0:
            return audio_chunk

        num_frames = total // self.frame_size
        remainder = total % self.frame_size
        processed_parts: list[np.ndarray] = []

        for i in range(num_frames):
            start = i * self.frame_size
            end = start + self.frame_size
            processed_parts.append(self.process_frame(flat[start:end]))

        if remainder:
            tail = flat[-self.frame_size:] if total >= self.frame_size else None
            if tail is None or len(tail) != self.frame_size:
                processed_parts.append(flat[-remainder:])
            else:
                clean_tail = self.process_frame(tail)
                processed_parts.append(clean_tail[-remainder:])

        if not processed_parts:
            return audio_chunk

        out = np.concatenate(processed_parts).astype(original_dtype, copy=False)
        return out.reshape(original_shape)

    def get_stats(self) -> dict[str, object]:
        avg_ms = (self._total_process_ms / self._frames_processed) if self._frames_processed else 0.0
        return {
            "enabled": self.enabled,
            "sample_rate": self.sample_rate,
            "frame_size": self.frame_size,
            "backend": "noisereduce",
            "frames_processed": self._frames_processed,
            "avg_process_ms": round(avg_ms, 4),
        }
