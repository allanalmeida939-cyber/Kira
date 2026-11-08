from __future__ import annotations

import logging
import time
from threading import RLock
from typing import Any

from services.feature_runtime_registry import (
    mark_feature_error,
    mark_feature_running,
    mark_feature_starting,
    mark_feature_stopped,
)

logger = logging.getLogger("kira.vision_livekit")

PROVIDER = "livekit"
_lock = RLock()
_state: dict[str, Any] = {
    "provider": PROVIDER,
    "vision_available": True,
    "vision_enabled": False,
    "vision_mode_enabled": False,
    "vision_status": "off",
    "state": "off",
    "active_source": "none",
    "selected_source": "none",
    "screen_share_requested": False,
    "screen_share_track_active": False,
    "webcam_track_active": False,
    "last_screen_frame_at": None,
    "last_webcam_frame_at": None,
    "vision_frames_recent": False,
    "vision_effective_active": False,
    "last_frame_at": None,
    "selected_monitor": None,
    "livekit_screen_active": False,
    "screen_capture_active": False,
    "screen_capture_enabled": False,
    "screen_capture_last_error": None,
    "dxcam_active": False,
    "legacy_providers_disabled": True,
    "requires_ui_publish": True,
    "synchronized": True,
    "monitor_count": 0,
    "captured_monitor_count": 0,
    "active_monitor_id": None,
    "latest_frame_age_ms": None,
    "latest_frame_size": None,
    "last_error": None,
    "updated_at": time.time(),
}


def _snapshot() -> dict[str, Any]:
    payload = dict(_state)
    payload["success"] = True
    return payload


def get_vision_livekit_state() -> dict[str, Any]:
    with _lock:
        state = _snapshot()
    logger.info("VISION_STATUS_REQUESTED provider=livekit state=%s", state.get("state"))
    return state


def start_vision_livekit(*, reason: str = "user_enabled") -> dict[str, Any]:
    logger.info("VISION_LIVEKIT_ENABLE_BEGIN")
    logger.info("VISION_ENABLE_BEGIN")
    try:
        mark_feature_starting("vision", reason=reason)
        with _lock:
            _state.update(
                {
                    "vision_enabled": True,
                    "vision_mode_enabled": True,
                    "vision_status": "waiting_frame",
                    "state": "waiting_frame",
                    "active_source": "livekit_screen_share",
                    "selected_source": "screen",
                    "screen_share_requested": True,
                    "screen_share_track_active": True,
                    "webcam_track_active": False,
                    "last_screen_frame_at": None,
                    "vision_frames_recent": False,
                    "vision_effective_active": False,
                    "livekit_screen_active": True,
                    "screen_capture_active": False,
                    "screen_capture_enabled": False,
                    "dxcam_active": False,
                    "last_error": None,
                    "updated_at": time.time(),
                }
            )
            state = _snapshot()
        mark_feature_running("vision", reason=reason)
        logger.info("VISION_LIVEKIT_ENABLE_OK")
        logger.info("VISION_ENABLE_OK")
        logger.info("VISION_SCREEN_SHARE_REQUESTED source=ui")
        return state
    except Exception as exc:
        logger.warning("VISION_LIVEKIT_ENABLE_FAILED_NON_FATAL reason=%s", exc)
        mark_feature_error("vision", reason=str(exc))
        with _lock:
            _state.update({"vision_status": "error", "state": "error", "last_error": str(exc), "updated_at": time.time()})
            return _snapshot()


def stop_vision_livekit(*, reason: str = "user_disabled") -> dict[str, Any]:
    logger.info("VISION_LIVEKIT_DISABLE_BEGIN")
    logger.info("VISION_DISABLED_BY_USER" if reason == "user_disabled" else "VISION_DISABLED_BY_TRACK_ENDED")
    with _lock:
        _state.update(
            {
                "vision_enabled": False,
                "vision_mode_enabled": False,
                "vision_status": "off",
                "state": "off",
                "active_source": "none",
                "selected_source": "none",
                "screen_share_requested": False,
                "screen_share_track_active": False,
                "webcam_track_active": False,
                "last_screen_frame_at": None,
                "last_webcam_frame_at": None,
                "vision_frames_recent": False,
                "vision_effective_active": False,
                "last_frame_at": None,
                "livekit_screen_active": False,
                "screen_capture_active": False,
                "screen_capture_enabled": False,
                "dxcam_active": False,
                "last_error": None,
                "updated_at": time.time(),
            }
        )
        state = _snapshot()
    mark_feature_stopped("vision", reason=reason)
    logger.info("VISION_LIVEKIT_DISABLE_OK")
    return state


def mark_livekit_frame_received(
    *,
    source: str = "screen_share",
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    now = time.time()
    selected = "screen" if "screen" in str(source or "").lower() else "webcam"
    is_screen = selected == "screen"
    with _lock:
        _state.update(
            {
                "vision_enabled": True,
                "vision_mode_enabled": True,
                "vision_status": "running",
                "state": "running",
                "active_source": "livekit_screen_share" if selected == "screen" else "livekit_camera",
                "selected_source": selected,
                "screen_share_requested": is_screen or bool(_state.get("screen_share_requested")),
                "screen_share_track_active": is_screen or bool(_state.get("screen_share_track_active")),
                "webcam_track_active": (not is_screen) or bool(_state.get("webcam_track_active")),
                "vision_frames_recent": True,
                "vision_effective_active": True,
                "livekit_screen_active": is_screen,
                "latest_frame_age_ms": 0,
                "latest_frame_size": f"{int(width or 0)}x{int(height or 0)}" if width and height else None,
                "last_frame_at": now,
                "last_screen_frame_at": now if is_screen else _state.get("last_screen_frame_at"),
                "last_webcam_frame_at": now if not is_screen else _state.get("last_webcam_frame_at"),
                "updated_at": now,
            }
        )
        state = _snapshot()
    # A cada quadro: em nível de depuração (eram 3 linhas por quadro no log).
    logger.debug("VISION_FRAME_RECEIVED source=%s selected=%s", source, selected)
    return state


def legacy_vision_blocked_response(*, reason: str = "vision_provider_livekit_only") -> dict[str, Any]:
    state = get_vision_livekit_state()
    state.update(
        {
            "legacy_provider_disabled": True,
            "legacy_reason": reason,
            "message": "Vision is LiveKit-only; use UI camera/screen publication.",
        }
    )
    return state
