from __future__ import annotations

import builtins
import hashlib
import logging
import threading
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np


logger = logging.getLogger(__name__)

_GLOBAL_VISION_FRAME_STORE: "VisionFrameStore | None" = None
_BUILTINS_STORE_ATTR = "_kira_global_vision_frame_store"
_GLOBAL_STORE_LOCK = threading.RLock()


def set_vision_frame_store(store: "VisionFrameStore | None") -> None:
    global _GLOBAL_VISION_FRAME_STORE
    with _GLOBAL_STORE_LOCK:
        _GLOBAL_VISION_FRAME_STORE = store
        setattr(builtins, _BUILTINS_STORE_ATTR, store)


def get_vision_frame_store() -> "VisionFrameStore | None":
    global _GLOBAL_VISION_FRAME_STORE
    with _GLOBAL_STORE_LOCK:
        shared = getattr(builtins, _BUILTINS_STORE_ATTR, None)
        if shared is not None:
            _GLOBAL_VISION_FRAME_STORE = shared
            return shared
        return _GLOBAL_VISION_FRAME_STORE


def ensure_vision_frame_store(debug_dir: str = "debug_vision") -> "VisionFrameStore":
    store = get_vision_frame_store()
    if store is not None:
        return store
    with _GLOBAL_STORE_LOCK:
        store = get_vision_frame_store()
        if store is None:
            store = VisionFrameStore(debug_dir=debug_dir)
            set_vision_frame_store(store)
            logger.info("VISION_FRAME_STORE_BOUND source=ensure_vision_frame_store")
        return store


class VisionFrameStore:
    """Armazena frames de LiveKit e DXCam com prioridade e telemetria simples."""

    _SOURCE_PRIORITY = ("livekit_screen_share", "livekit_camera", "dxcam_local")
    VALID_SOURCES = {"livekit_screen_share", "livekit_camera", "dxcam_local"}

    def __init__(self, debug_dir: str = "debug_vision") -> None:
        self._frames: dict[str, dict[str, Any] | None] = {
            "livekit_screen_share": None,
            "livekit_camera": None,
            "dxcam_local": None,
        }
        self._track_active: dict[str, dict[str, Any]] = {
            source: {"active": False, "track_sid": "", "label": "", "updated_at": 0.0}
            for source in self.VALID_SOURCES
        }
        self._native_video_active: dict[str, bool] = {source: False for source in self.VALID_SOURCES}
        self._warming_up: dict[str, bool] = {source: False for source in self.VALID_SOURCES}
        self._warming_started_at: dict[str, float] = {}
        self._lock = threading.RLock()
        self._debug_dir = Path(debug_dir)
        self._debug_dir.mkdir(parents=True, exist_ok=True)
        self._last_signatures: dict[str, bytes] = {}
        self._last_frame_log_at: dict[str, float] = {}
        self._first_frame_received_at_by_source: dict[str, float] = {}
        self._light_frame_metadata: dict[str, dict[str, Any]] = {}
        self._light_visual_context: dict[str, Any] = {}
        self._adaptive_fps: float | None = None
        self.frames_available: bool = False
        self.first_frame_received_at: float | None = None
        logger.info("[VISION_STORE] Inicializado debug_dir=%s", self._debug_dir)

    @property
    def latest_frame_age_ms(self) -> int | None:
        latest = self.get_latest(max_age_seconds=60.0)
        if not latest:
            return None
        try:
            return int(latest.get("age_ms") or 0)
        except Exception:
            return None

    def set_track_active(self, source: str, active: bool, *, track_sid: str = "", label: str = "") -> None:
        normalized_source = str(source or "").strip().lower()
        if normalized_source not in self.VALID_SOURCES:
            logger.warning("[VISION_STORE] Fonte invalida para track: %s", source)
            return
        with self._lock:
            self._track_active[normalized_source] = {
                "active": bool(active),
                "track_sid": str(track_sid or "").strip(),
                "label": str(label or "").strip(),
                "updated_at": time.time(),
            }
            if active:
                self._warming_up[normalized_source] = True
                self._warming_started_at[normalized_source] = time.monotonic()
            else:
                self._warming_up[normalized_source] = False
                self._warming_started_at.pop(normalized_source, None)
        logger.info(
            "VISION_TRACK_STATE_UPDATED source=%s active=%s track_sid=%s",
            normalized_source,
            str(bool(active)).lower(),
            str(track_sid or "").strip() or "none",
        )
        if bool(active):
            logger.info("VISION_STATE_SYNCED_WITH_LIVEKIT_TRACK source=%s", normalized_source)
            logger.info("VISION_WARMING_UP_BEGIN source=%s", normalized_source)

    def set_warming_up(self, source: str, active: bool) -> None:
        normalized_source = str(source or "").strip().lower()
        if normalized_source not in self.VALID_SOURCES:
            return
        with self._lock:
            self._warming_up[normalized_source] = bool(active)
            if active:
                self._warming_started_at[normalized_source] = time.monotonic()
            else:
                self._warming_started_at.pop(normalized_source, None)
        if active:
            logger.info("VISION_WARMING_UP_BEGIN source=%s", normalized_source)

    def set_native_video_active(self, source: str, active: bool) -> None:
        normalized_source = str(source or "").strip().lower()
        if normalized_source not in self.VALID_SOURCES:
            logger.warning("[VISION_STORE] Fonte invalida para native video: %s", source)
            return
        with self._lock:
            old_active = bool(self._native_video_active.get(normalized_source))
            self._native_video_active[normalized_source] = bool(active)
        if active and not old_active:
            logger.info("VISION_NATIVE_VIDEO_ACTIVE source=%s", normalized_source)

    def record_light_frame(
        self,
        source: str,
        *,
        width: int = 0,
        height: int = 0,
        track_sid: str = "",
    ) -> None:
        normalized_source = str(source or "").strip().lower()
        if normalized_source not in self.VALID_SOURCES:
            return
        now = time.time()
        with self._lock:
            previous = dict(self._light_frame_metadata.get(normalized_source) or {})
            self._light_frame_metadata[normalized_source] = {
                "timestamp": now,
                "width": max(int(width or 0), 0),
                "height": max(int(height or 0), 0),
                "track_sid": str(track_sid or "").strip(),
                "count": int(previous.get("count") or 0) + 1,
            }
            self._native_video_active[normalized_source] = True

    def update_light_visual_context(self, *, source: str, summary: str, age_ms: int = 0) -> None:
        normalized_source = str(source or "").strip().lower()
        if normalized_source not in self.VALID_SOURCES:
            return
        text = " ".join(str(summary or "").strip().split())
        if not text:
            return
        with self._lock:
            self._light_visual_context = {
                "source": normalized_source,
                "summary": text[:240],
                "timestamp": time.time(),
                "source_frame_age_ms": max(int(age_ms or 0), 0),
            }

    def set_adaptive_fps(self, fps: float) -> None:
        with self._lock:
            self._adaptive_fps = max(float(fps), 0.0)

    def get_light_visual_context(self, *, max_age_seconds: float = 10.0) -> dict[str, Any] | None:
        with self._lock:
            payload = dict(self._light_visual_context or {})
        if not payload:
            return None
        age_seconds = max(time.time() - float(payload.get("timestamp") or 0.0), 0.0)
        if age_seconds > max(float(max_age_seconds), 0.0):
            return None
        payload["age_ms"] = int(age_seconds * 1000.0)
        return payload

    def update(
        self,
        *,
        image_bytes: bytes,
        source: str,
        width: int,
        height: int,
        metadata: dict[str, Any] | None = None,
        frame_array: np.ndarray | None = None,
        save_debug: bool = True,
    ) -> None:
        normalized_source = str(source or "").strip().lower()
        if normalized_source not in self.VALID_SOURCES:
            logger.warning("[VISION_STORE] Fonte invalida ignorada: %s", source)
            return
        if int(width or 0) <= 0 or int(height or 0) <= 0:
            logger.info("VISION_FRAME_INVALID_SKIPPED reason=zero_size")
            logger.info("VISION_CONTEXT_SKIPPED reason=invalid_frame")
            return
        if not image_bytes:
            logger.info("VISION_FRAME_INVALID_SKIPPED reason=empty_bytes")
            logger.info("VISION_CONTEXT_SKIPPED reason=invalid_frame")
            return

        if frame_array is None:
            frame_array = self._decode_image_bytes(image_bytes)

        payload = {
            "bytes": bytes(image_bytes),
            "frame": frame_array,
            "width": max(int(width), 0),
            "height": max(int(height), 0),
            "timestamp": time.time(),
            "monotonic_timestamp": time.monotonic(),
            "metadata": dict(metadata or {}),
        }

        with self._lock:
            source_had_frame = self._frames.get(normalized_source) is not None
            self._frames[normalized_source] = payload
            if not source_had_frame:
                self._first_frame_received_at_by_source[normalized_source] = time.monotonic()
                logger.info("VISION_FIRST_FRAME_STORED source=%s", normalized_source)
            if not self.frames_available:
                self.frames_available = True
                self.first_frame_received_at = time.monotonic()
            if self._warming_up.get(normalized_source):
                self._warming_up[normalized_source] = False
                self._warming_started_at.pop(normalized_source, None)
                logger.info("VISION_WARMING_UP_FRAME_READY source=%s", normalized_source)

        if save_debug:
            self._save_debug_snapshot(normalized_source, image_bytes)

        now = time.monotonic()
        if now - float(self._last_frame_log_at.get(normalized_source, 0.0) or 0.0) >= 5.0:
            self._last_frame_log_at[normalized_source] = now
            logger.info("VISION_FRAME_STORED source=%s", normalized_source)
            logger.info(
                "[VISION_STORE] Frame atualizado source=%s size=%sx%s bytes=%s",
                normalized_source,
                payload["width"],
                payload["height"],
                len(image_bytes),
            )

    def update_from_ndarray(
        self,
        *,
        frame: np.ndarray,
        source: str,
        metadata: dict[str, Any] | None = None,
        jpeg_quality: int = 85,
        save_debug: bool = True,
    ) -> bool:
        if frame is None or getattr(frame, "size", 0) <= 0:
            return False
        try:
            ok, encoded = cv2.imencode(
                ".jpg",
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), max(min(int(jpeg_quality), 100), 30)],
            )
        except Exception as exc:
            logger.warning("[VISION_STORE] Falha ao codificar frame ndarray: %s", exc)
            return False
        if not ok:
            return False
        height, width = frame.shape[:2]
        self.update(
            image_bytes=encoded.tobytes(),
            source=source,
            width=int(width),
            height=int(height),
            metadata=metadata,
            frame_array=frame.copy(),
            save_debug=save_debug,
        )
        return True

    def get_latest(
        self,
        *,
        max_age_seconds: float = 5.0,
        preferred_source: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            now = time.time()
            if preferred_source:
                sources = (str(preferred_source).strip().lower(),)
            else:
                sources = self._SOURCE_PRIORITY

            for source in sources:
                frame = self._frames.get(source)
                if not frame:
                    continue
                age_seconds = max(now - float(frame.get("timestamp") or 0.0), 0.0)
                if age_seconds > max(max_age_seconds, 0.0):
                    continue
                payload = dict(frame)
                payload["source"] = source
                payload["age_ms"] = int(age_seconds * 1000.0)
                return payload
        return None

    def clear_source(self, source: str) -> None:
        normalized_source = str(source or "").strip().lower()
        with self._lock:
            if normalized_source in self._frames:
                self._frames[normalized_source] = None
                self._last_signatures.pop(normalized_source, None)
                self._first_frame_received_at_by_source.pop(normalized_source, None)
                self._light_frame_metadata.pop(normalized_source, None)
                if str(self._light_visual_context.get("source") or "") == normalized_source:
                    self._light_visual_context = {}
                self._native_video_active[normalized_source] = False
                self._warming_up[normalized_source] = False
                self._warming_started_at.pop(normalized_source, None)
            self.frames_available = any(frame is not None for frame in self._frames.values())
            if not self.frames_available:
                self.first_frame_received_at = None
        logger.info("[VISION_STORE] Fonte limpa: %s", normalized_source)

    def frame_changed_significantly(
        self,
        current_bytes: bytes,
        *,
        source: str = "livekit_screen_share",
        threshold: float = 8.0,
    ) -> tuple[bool, float]:
        normalized_source = str(source or "livekit_screen_share").strip().lower()
        frame = self._decode_image_bytes(current_bytes)
        if frame is None:
            return True, 999.0

        try:
            small = cv2.resize(frame, (320, 180))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            signature = gray.tobytes()
        except Exception as exc:
            logger.warning("[VISION_STORE] Falha ao calcular assinatura de frame: %s", exc)
            return True, 999.0

        with self._lock:
            previous = self._last_signatures.get(normalized_source)
            self._last_signatures[normalized_source] = signature

        if previous is None:
            logger.info("[VISION_STORE] Primeiro frame registrado para comparacao source=%s", normalized_source)
            return True, 999.0

        current_arr = np.frombuffer(signature, dtype=np.uint8)
        previous_arr = np.frombuffer(previous, dtype=np.uint8)
        if current_arr.shape != previous_arr.shape or current_arr.size == 0:
            return True, 999.0

        score = float(np.mean(np.abs(current_arr.astype(np.int16) - previous_arr.astype(np.int16))))
        changed = score >= float(threshold)
        if changed:
            logger.info(
                "[VISION_STORE] Mudanca detectada source=%s score=%.2f threshold=%.2f",
                normalized_source,
                score,
                threshold,
            )
        else:
            logger.debug(
                "[VISION_STORE] Frame semelhante source=%s score=%.2f threshold=%.2f",
                normalized_source,
                score,
                threshold,
            )
        return changed, score

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            sources: dict[str, Any] = {}
            for source, frame in self._frames.items():
                track_state = dict(self._track_active.get(source) or {})
                light_state = dict(self._light_frame_metadata.get(source) or {})
                first_frame_received = source in self._first_frame_received_at_by_source
                if not frame:
                    sources[source] = {
                        "track_active": bool(track_state.get("active")),
                        "native_video_active": bool(self._native_video_active.get(source)),
                        "vision_warming_up": bool(self._warming_up.get(source)),
                        "first_frame_received": bool(first_frame_received),
                        "frames_available": False,
                        "recent_frame": False,
                        "light_frame_available": bool(light_state),
                        "light_frame_age_ms": (
                            int(max(now - float(light_state.get("timestamp") or 0.0), 0.0) * 1000.0)
                            if light_state
                            else None
                        ),
                    }
                    continue
                age_seconds = max(now - float(frame.get("timestamp") or 0.0), 0.0)
                width = int(frame.get("width") or 0)
                height = int(frame.get("height") or 0)
                sources[source] = {
                    "track_active": bool(track_state.get("active")),
                    "native_video_active": bool(self._native_video_active.get(source)),
                    "vision_warming_up": bool(self._warming_up.get(source)),
                    "first_frame_received": bool(first_frame_received),
                    "frames_available": True,
                    "recent_frame": age_seconds <= 5.0,
                    "active": age_seconds <= 5.0,
                    "age_ms": int(age_seconds * 1000.0),
                    "light_frame_available": bool(light_state),
                    "size": f"{width}x{height}" if width > 0 and height > 0 else None,
                    "timestamp": float(frame.get("timestamp") or 0.0),
                }

        latest = self.get_latest(max_age_seconds=5.0)
        active_source = str(latest.get("source")) if latest else "none"
        livekit_screen = sources.get("livekit_screen_share", {})
        livekit_camera = sources.get("livekit_camera", {})
        visual_context = self.get_light_visual_context(max_age_seconds=60.0)
        return {
            "active_source": active_source,
            "track_active": any(bool(item.get("track_active")) for item in sources.values()),
            "native_video_active": any(bool(item.get("native_video_active")) for item in sources.values()),
            "vision_warming_up": any(bool(item.get("vision_warming_up")) for item in sources.values()),
            "frames_available": bool(self.frames_available),
            "first_frame_received": any(bool(item.get("first_frame_received")) for item in sources.values()),
            "first_frame_received_at": self.first_frame_received_at,
            "recent_frame_available": bool(latest),
            "livekit_screen_track_active": bool(livekit_screen.get("track_active")),
            "livekit_screen_native_video_active": bool(livekit_screen.get("native_video_active")),
            "livekit_camera_track_active": bool(livekit_camera.get("track_active")),
            "livekit_camera_native_video_active": bool(livekit_camera.get("native_video_active")),
            "livekit_screen_active": active_source == "livekit_screen_share",
            "livekit_camera_active": active_source == "livekit_camera",
            "dxcam_active": bool(sources.get("dxcam_local", {}).get("active")),
            "latest_frame_age_ms": latest.get("age_ms") if latest else None,
            "adaptive_fps": self._adaptive_fps,
            "visual_context_age_ms": visual_context.get("age_ms") if visual_context else None,
            "visual_context_summary": visual_context.get("summary") if visual_context else "",
            "latest_frame_size": (
                f"{latest.get('width')}x{latest.get('height')}"
                if latest and latest.get("width") and latest.get("height")
                else None
            ),
            "sources": sources,
        }

    @staticmethod
    def compute_frame_digest(frame: np.ndarray) -> str:
        if frame is None or getattr(frame, "size", 0) <= 0:
            return ""
        try:
            digest = hashlib.sha1(memoryview(frame).tobytes()).hexdigest()
        except Exception:
            digest = ""
        return digest

    @staticmethod
    def _decode_image_bytes(image_bytes: bytes) -> np.ndarray | None:
        if not image_bytes:
            return None
        try:
            arr = np.frombuffer(image_bytes, dtype=np.uint8)
            decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        except Exception:
            return None
        return decoded

    def _save_debug_snapshot(self, source: str, image_bytes: bytes) -> None:
        debug_path = self._debug_dir / f"latest_{source}.jpg"
        try:
            debug_path.write_bytes(image_bytes)
        except Exception as exc:
            logger.debug("[VISION_STORE] Falha ao salvar debug source=%s detail=%s", source, exc)


__all__ = [
    "VisionFrameStore",
    "ensure_vision_frame_store",
    "get_vision_frame_store",
    "set_vision_frame_store",
]
