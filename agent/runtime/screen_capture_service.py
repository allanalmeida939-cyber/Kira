from __future__ import annotations

import ctypes
import importlib
import logging
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ScreenFrame:
    image: np.ndarray
    timestamp: datetime
    resolution: tuple[int, int]
    monitor_id: int
    device_id: int = 0


@dataclass(slots=True)
class _MonitorBinding:
    monitor_id: int
    sort_key: tuple[int, int]
    display_name: str | None = None
    width: int = 0
    height: int = 0
    is_primary: bool = False
    camera: Any | None = None


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _MonitorInfoExW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _Rect),
        ("rcWork", _Rect),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


def _load_dxcam_module() -> Any:
    return importlib.import_module("dxcam")


def _enumerate_display_details() -> dict[str, dict[str, int | bool]]:
    details: dict[str, dict[str, int | bool]] = {}
    try:
        user32 = ctypes.windll.user32
    except Exception:
        return details

    monitor_enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(_Rect),
        wintypes.LPARAM,
    )

    def _callback(handle, hdc, rect_ptr, data):
        del hdc, rect_ptr, data
        info = _MonitorInfoExW()
        info.cbSize = ctypes.sizeof(_MonitorInfoExW)
        if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            details[str(info.szDevice)] = {
                "left": int(info.rcMonitor.left),
                "top": int(info.rcMonitor.top),
                "width": max(int(info.rcMonitor.right - info.rcMonitor.left), 0),
                "height": max(int(info.rcMonitor.bottom - info.rcMonitor.top), 0),
                "is_primary": bool(int(info.dwFlags) & 1),
            }
        return True

    callback_ref = monitor_enum_proc(_callback)
    try:
        user32.EnumDisplayMonitors(0, 0, callback_ref, 0)
    except Exception:
        return details
    return details


class ScreenCaptureService:
    """Captura a tela em background e mantem o ultimo frame em cache."""

    def __init__(
        self,
        monitor_id: int = 0,
        target_fps: int = 30,
        enabled: bool = False,
        *,
        device_id: int = 0,
        camera_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.monitor_id = max(int(monitor_id), 0)
        self.target_fps = max(int(target_fps), 1)
        self.enabled = bool(enabled)
        self.device_id = max(int(device_id), 0)
        self._camera_factory = camera_factory
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._latest_frame: ScreenFrame | None = None
        self._frame_count = 0
        self._last_error: str | None = None
        self.monitor_count = 0
        self.active_monitor_id: int | None = None
        self.cameras: list[Any] = []
        self._monitor_bindings: list[_MonitorBinding] = []
        self._refresh_monitor_inventory(create_cameras=bool(self._camera_factory))
        logger.info(
            "[SCREEN_CAPTURE] Inicializado - target_fps=%d enabled=%s monitors=%d",
            self.target_fps,
            self.enabled,
            self.monitor_count,
        )

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        logger.info("[SCREEN_CAPTURE] Enabled=%s", self.enabled)

    def is_active(self) -> bool:
        thread = self._capture_thread
        return bool(thread and thread.is_alive() and not self._stop_event.is_set())

    def get_latest_frame(self) -> ScreenFrame | None:
        with self._lock:
            return self._latest_frame

    def get_latest_image(self) -> np.ndarray | None:
        frame = self.get_latest_frame()
        return None if frame is None else frame.image

    def get_last_error(self) -> str | None:
        return self._last_error

    def get_capture_monitor_count(self) -> int:
        if self.active_monitor_id is not None:
            return 1
        return max(int(self.monitor_count), 1) if self.monitor_count else 0

    def list_monitors(self) -> list[dict[str, object]]:
        if not self._monitor_bindings:
            self._refresh_monitor_inventory(create_cameras=False)
        monitors: list[dict[str, object]] = []
        for binding in self._monitor_bindings:
            monitors.append(
                {
                    "id": int(binding.monitor_id),
                    "width": int(binding.width),
                    "height": int(binding.height),
                    "is_primary": bool(binding.is_primary),
                    "name": str(binding.display_name or f"Monitor {binding.monitor_id + 1}"),
                    "active": self.active_monitor_id == int(binding.monitor_id),
                }
            )
        return monitors

    def set_active_monitor(self, monitor_id: int | None) -> None:
        normalized: int | None
        if monitor_id is None:
            normalized = None
        else:
            normalized = int(monitor_id)
            if normalized < 0:
                raise ValueError("monitor_id_invalido")
            if not self._monitor_bindings:
                self._refresh_monitor_inventory(create_cameras=False)
            valid_ids = {binding.monitor_id for binding in self._monitor_bindings}
            if normalized not in valid_ids:
                raise ValueError(f"monitor_id_invalido:{normalized}")

        was_running = self.is_active()
        if was_running:
            self.stop()

        self.active_monitor_id = normalized
        logger.info("[SCREEN_CAPTURE] Monitor ativo definido - id=%s", self.active_monitor_id)

        if was_running and self.enabled:
            self.start()

    def start(self) -> bool:
        if not self.enabled:
            logger.warning("[SCREEN_CAPTURE] Tentou iniciar com o modo desabilitado")
            return False
        if self.is_active():
            logger.info("[SCREEN_CAPTURE] Captura ja estava ativa")
            return True

        self._stop_event.clear()
        if not self.cameras:
            self._refresh_monitor_inventory(create_cameras=True)
        if not self.cameras:
            if not self._last_error:
                self._last_error = "Nenhuma camera DXcam disponivel para captura."
            logger.error("[SCREEN_CAPTURE] Falha ao iniciar: %s", self._last_error)
            return False

        capture_bindings = self._capture_bindings()
        if not capture_bindings:
            self._last_error = (
                f"Monitor ativo {self.active_monitor_id} nao disponivel para captura."
                if self.active_monitor_id is not None
                else "Nenhum monitor disponivel para captura."
            )
            logger.error("[SCREEN_CAPTURE] Falha ao iniciar: %s", self._last_error)
            return False

        for binding in capture_bindings:
            if binding.camera is None:
                continue
            self._start_camera(binding)

        thread_name = (
            f"ScreenCaptureThread-Monitor-{self.active_monitor_id}"
            if self.active_monitor_id is not None
            else "ScreenCaptureThread-AllMonitors"
        )
        self._capture_thread = threading.Thread(target=self._capture_loop, name=thread_name, daemon=True)
        self._capture_thread.start()
        if self.active_monitor_id is not None:
            logger.info(
                "[SCREEN_CAPTURE] Captura iniciada - monitor=%d device=%d",
                self.active_monitor_id,
                self.device_id,
            )
        else:
            logger.info(
                "[SCREEN_CAPTURE] Captura iniciada - monitors=%d device=%d",
                len(capture_bindings),
                self.device_id,
            )
        return True

    def stop(self) -> bool:
        was_active = self.is_active()
        self._stop_event.set()

        thread = self._capture_thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._capture_thread = None

        self._release_all_cameras()

        with self._lock:
            self._latest_frame = None

        logger.info("[SCREEN_CAPTURE] Captura parada")
        return was_active

    def _refresh_monitor_inventory(self, *, create_cameras: bool) -> None:
        try:
            bindings = self._discover_monitor_bindings(create_cameras=create_cameras)
        except Exception as exc:
            self._last_error = str(exc)
            logger.error("[SCREEN_CAPTURE] Erro ao detectar monitores: %s", exc)
            bindings = []

        bindings.sort(key=lambda binding: binding.sort_key)
        self._monitor_bindings = bindings
        self.cameras = [binding.camera for binding in bindings if binding.camera is not None]
        self.monitor_count = len(bindings)
        logger.info("[SCREEN_CAPTURE] Detectados %d monitor(es)", self.monitor_count)

    def _discover_monitor_bindings(self, *, create_cameras: bool) -> list[_MonitorBinding]:
        if self._camera_factory is not None:
            return self._discover_factory_bindings()

        dxcam_module = _load_dxcam_module()
        display_details = _enumerate_display_details()
        output_names = self._detect_output_names(dxcam_module)
        bindings = [
            _MonitorBinding(
                monitor_id=monitor_index,
                display_name=display_name,
                sort_key=(
                    int(display_details.get(display_name or "", {}).get("left", monitor_index)),
                    int(display_details.get(display_name or "", {}).get("top", 0)),
                ),
                width=int(display_details.get(display_name or "", {}).get("width", 0)),
                height=int(display_details.get(display_name or "", {}).get("height", 0)),
                is_primary=bool(display_details.get(display_name or "", {}).get("is_primary", monitor_index == 0)),
            )
            for monitor_index, display_name in enumerate(output_names)
        ]

        if not bindings and create_cameras:
            return self._probe_bindings_by_creation(dxcam_module)

        if create_cameras:
            for binding in bindings:
                binding.camera = self._try_create_camera(dxcam_module, binding.monitor_id)

        return bindings

    def _discover_factory_bindings(self) -> list[_MonitorBinding]:
        bindings: list[_MonitorBinding] = []
        monitor_index = 0
        while True:
            camera = self._try_create_factory_camera(monitor_index)
            if camera is None:
                break
            bindings.append(
                _MonitorBinding(
                    monitor_id=monitor_index,
                    display_name=None,
                    sort_key=(monitor_index, 0),
                    is_primary=(monitor_index == 0),
                    camera=camera,
                )
            )
            monitor_index += 1
        return bindings

    def _detect_output_names(self, dxcam_module: Any) -> list[str | None]:
        metadata_reader = getattr(dxcam_module, "get_output_metadata", None)
        if callable(metadata_reader):
            try:
                metadata = metadata_reader() or {}
                if isinstance(metadata, dict) and metadata:
                    return [str(name) for name in metadata.keys()]
            except Exception:
                logger.debug(
                    "[SCREEN_CAPTURE] Falha ao ler get_output_metadata",
                    exc_info=True,
                )

        info_reader = getattr(dxcam_module, "output_info", None)
        if callable(info_reader):
            try:
                info_text = str(info_reader() or "").strip()
            except Exception:
                logger.debug("[SCREEN_CAPTURE] Falha ao ler output_info", exc_info=True)
                return []
            lines = [line for line in info_text.splitlines() if line.strip()]
            return [None for _ in lines]

        return []

    def _probe_bindings_by_creation(self, dxcam_module: Any) -> list[_MonitorBinding]:
        bindings: list[_MonitorBinding] = []
        monitor_index = 0
        while True:
            camera = self._try_create_camera(dxcam_module, monitor_index)
            if camera is None:
                break
            bindings.append(
                _MonitorBinding(
                    monitor_id=monitor_index,
                    display_name=None,
                    sort_key=(monitor_index, 0),
                    is_primary=(monitor_index == 0),
                    camera=camera,
                )
            )
            monitor_index += 1
        return bindings

    def _try_create_factory_camera(self, monitor_index: int) -> Any | None:
        if self._camera_factory is None:
            return None
        try:
            return self._camera_factory(
                monitor_id=monitor_index,
                device_id=self.device_id,
            )
        except TypeError:
            try:
                return self._camera_factory(monitor_index, self.device_id)
            except Exception as exc:
                self._last_error = str(exc)
                logger.error(
                    "[SCREEN_CAPTURE] Erro ao criar camera do monitor %d: %s",
                    monitor_index,
                    exc,
                )
                return None
        except Exception as exc:
            self._last_error = str(exc)
            logger.error(
                "[SCREEN_CAPTURE] Erro ao criar camera do monitor %d: %s",
                monitor_index,
                exc,
            )
            return None

    def _try_create_camera(self, dxcam_module: Any, monitor_index: int) -> Any | None:
        try:
            camera = dxcam_module.create(
                device_idx=self.device_id,
                output_idx=monitor_index,
            )
        except Exception as exc:
            self._last_error = str(exc)
            logger.error(
                "[SCREEN_CAPTURE] Erro ao criar camera do monitor %d: %s",
                monitor_index,
                exc,
            )
            return None
        return camera

    def _start_camera(self, binding: _MonitorBinding) -> None:
        camera = binding.camera
        if camera is None:
            return
        starter = getattr(camera, "start", None)
        if not callable(starter):
            return
        try:
            starter(target_fps=self.target_fps, video_mode=False)
        except TypeError:
            starter()
        logger.info(
            "[SCREEN_CAPTURE] Monitor %d iniciado - target_fps=%d",
            binding.monitor_id,
            self.target_fps,
        )

    def _capture_loop(self) -> None:
        frame_interval = 1.0 / float(self.target_fps)
        logger.info("[SCREEN_CAPTURE] Loop iniciado - target_fps=%d", self.target_fps)

        while not self._stop_event.is_set():
            started_at = time.perf_counter()
            try:
                capture_bindings = self._capture_bindings()
                frames: list[np.ndarray] = []
                for binding in capture_bindings:
                    if binding.camera is None:
                        continue
                    image = self._grab_image(binding.camera)
                    normalized = self._normalize_image(image)
                    if normalized is None:
                        continue
                    frames.append(normalized)
                    logger.debug(
                        "[SCREEN_CAPTURE] Monitor %d capturado - shape=%s",
                        binding.monitor_id,
                        normalized.shape,
                    )
                if frames:
                    combined = self._combine_frames(frames)
                    self._store_frame(combined)
                    if self.active_monitor_id is not None:
                        logger.debug(
                            "[SCREEN_CAPTURE] Frame unico - monitor=%d shape=%s",
                            self.active_monitor_id,
                            combined.shape,
                        )
                    else:
                        logger.debug(
                            "[SCREEN_CAPTURE] Frame combinado - monitors=%d shape=%s",
                            len(frames),
                            combined.shape,
                        )
            except Exception as exc:
                self._last_error = str(exc)
                logger.exception("[SCREEN_CAPTURE] Falha ao capturar frame: %s", exc)

            elapsed = time.perf_counter() - started_at
            remaining = frame_interval - elapsed
            if remaining > 0:
                self._stop_event.wait(remaining)

        logger.info(
            "[SCREEN_CAPTURE] Loop finalizado - frames=%d monitors=%d",
            self._frame_count,
            len(self._capture_bindings()),
        )

    def _capture_bindings(self) -> list[_MonitorBinding]:
        if self.active_monitor_id is None:
            return list(self._monitor_bindings)
        return [
            binding
            for binding in self._monitor_bindings
            if int(binding.monitor_id) == int(self.active_monitor_id)
        ]

    def _grab_image(self, camera: Any) -> np.ndarray | None:
        if camera is None:
            return None
        if hasattr(camera, "grab"):
            return camera.grab()
        if hasattr(camera, "get_latest_frame"):
            return camera.get_latest_frame()
        raise RuntimeError("Camera DXcam sem suporte a grab/get_latest_frame")

    def _normalize_image(self, image: np.ndarray | Any) -> np.ndarray | None:
        if image is None:
            return None
        array = np.asarray(image)
        if array.size == 0 or array.ndim not in (2, 3):
            return None
        if array.dtype != np.uint8:
            array = np.clip(array, 0, 255).astype(np.uint8)
        if array.ndim == 2:
            array = np.repeat(array[:, :, None], 3, axis=2)
        elif array.shape[2] == 1:
            array = np.repeat(array, 3, axis=2)
        elif array.shape[2] > 3:
            array = array[:, :, :3]
        return np.ascontiguousarray(array)

    def _combine_frames(self, frames: list[np.ndarray]) -> np.ndarray:
        normalized_frames = [frame for frame in frames if frame.size > 0]
        if not normalized_frames:
            raise RuntimeError("Nenhum frame valido disponivel para combinar.")
        if len(normalized_frames) == 1:
            return np.ascontiguousarray(normalized_frames[0].copy())

        max_height = max(int(frame.shape[0]) for frame in normalized_frames)
        total_width = sum(int(frame.shape[1]) for frame in normalized_frames)
        channel_count = int(normalized_frames[0].shape[2])
        combined = np.zeros((max_height, total_width, channel_count), dtype=np.uint8)

        x_offset = 0
        for frame in normalized_frames:
            height, width = int(frame.shape[0]), int(frame.shape[1])
            combined[:height, x_offset:x_offset + width] = frame
            x_offset += width

        return np.ascontiguousarray(combined)

    def _resize_frame_to_height(self, frame: np.ndarray, target_height: int) -> np.ndarray:
        if target_height <= 0 or int(frame.shape[0]) == target_height:
            return frame
        scale = float(target_height) / float(frame.shape[0])
        target_width = max(int(round(frame.shape[1] * scale)), 1)
        resized = Image.fromarray(frame).resize(
            (target_width, target_height),
            resample=Image.Resampling.BILINEAR,
        )
        return np.ascontiguousarray(np.asarray(resized))

    def _store_frame(self, image: np.ndarray) -> None:
        normalized = self._normalize_image(image)
        if normalized is None:
            return

        frame = ScreenFrame(
            image=normalized.copy(),
            timestamp=datetime.now(),
            resolution=(int(normalized.shape[1]), int(normalized.shape[0])),
            monitor_id=(
                int(self.active_monitor_id)
                if self.active_monitor_id is not None
                else (self.monitor_id if self.monitor_count <= 1 else -1)
            ),
            device_id=self.device_id,
        )
        with self._lock:
            self._latest_frame = frame
            self._frame_count += 1

    def _release_all_cameras(self) -> None:
        for binding in self._monitor_bindings:
            camera = binding.camera
            if camera is None:
                continue
            self._release_camera(camera)
            logger.info("[SCREEN_CAPTURE] Monitor %d parado", binding.monitor_id)
            binding.camera = None
        self.cameras = []

    def _release_camera(self, camera: Any | None) -> None:
        if camera is None:
            return
        method_name = "stop" if callable(getattr(camera, "stop", None)) else "release"
        method = getattr(camera, method_name, None)
        if callable(method):
            try:
                method()
            except Exception:
                logger.debug(
                    "[SCREEN_CAPTURE] Ignorando erro ao liberar camera via %s",
                    method_name,
                    exc_info=True,
                )


__all__ = ["ScreenCaptureService", "ScreenFrame"]
