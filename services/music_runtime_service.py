from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass
from threading import RLock
from typing import Any, Awaitable, Callable

from services.feature_runtime_registry import (
    get_feature_state,
    mark_feature_error,
    mark_feature_running,
    mark_feature_starting,
    mark_feature_state,
    mark_feature_stopped,
)


logger = logging.getLogger("kira.music_runtime")

PlayerCallback = Callable[[str | None], Awaitable[dict[str, Any]] | dict[str, Any]]
CommandCallback = Callable[[], Awaitable[dict[str, Any]] | dict[str, Any]]
VolumeCallback = Callable[[int], Awaitable[dict[str, Any]] | dict[str, Any]]


@dataclass
class MusicRuntimeState:
    state: str = "off"
    playing: bool = False
    tab_active: bool = False
    track: dict[str, Any] | None = None
    volume: int = 70
    last_error: str | None = None
    last_activity_at: float | None = None
    idle_shutdown_seconds: int = 300


_lock = RLock()
_state = MusicRuntimeState()


def reset_for_tests() -> None:
    with _lock:
        _state.state = "off"
        _state.playing = False
        _state.tab_active = False
        _state.track = None
        _state.volume = 70
        _state.last_error = None
        _state.last_activity_at = None


def _snapshot() -> dict[str, Any]:
    feature = get_feature_state("music")
    with _lock:
        return {
            "enabled": bool(feature.enabled),
            "lazy": bool(feature.lazy),
            "autostart": bool(feature.autostart),
            "state": _state.state,
            "playing": bool(_state.playing),
            "track": dict(_state.track) if isinstance(_state.track, dict) else None,
            "volume": int(_state.volume),
            "tab_active": bool(_state.tab_active),
            "last_error": _state.last_error,
            "idle_shutdown_seconds": _state.idle_shutdown_seconds,
            "updated_at": time.time(),
        }


def get_music_status() -> dict[str, Any]:
    logger.info("MUSIC_STATUS_REQUESTED light=true")
    logger.info("MUSIC_STATUS_NO_WAKEUP")
    return _snapshot()


def touch_idle() -> None:
    with _lock:
        _state.last_activity_at = time.time()
    logger.info("MUSIC_IDLE_TIMER_TOUCH")


async def _maybe_await(value: object) -> object:
    if hasattr(value, "__await__"):
        return await value  # type: ignore[misc]
    return value


async def ensure_music_started(reason: str = "user_requested") -> dict[str, Any]:
    with _lock:
        if _state.state in {"running", "playing", "paused"}:
            touch_idle()
            return _snapshot()
        _state.state = "starting"
        _state.last_error = None
    mark_feature_starting("music", reason=reason)
    mark_feature_running("music", reason=reason)
    with _lock:
        _state.state = "running"
    touch_idle()
    return _snapshot()


async def play_music(query: str | None = None, *, player: PlayerCallback | None = None) -> dict[str, Any]:
    logger.info("MUSIC_TASK_ROUTE lazy=true")
    logger.info("MUSIC_PLAY_REQUEST_BEGIN")
    await ensure_music_started("user_requested")
    try:
        if player is None:
            raise RuntimeError("player_unavailable")
        result = await _maybe_await(player(query))
        if not isinstance(result, dict) or not bool(result.get("ok", result.get("success"))):
            reason = str(result.get("error") if isinstance(result, dict) else "player_failed") or "player_failed"
            raise RuntimeError(reason)
        track = result.get("track") if isinstance(result.get("track"), dict) else {"query": query or "musica"}
        with _lock:
            _state.state = "playing"
            _state.playing = True
            _state.track = track
            _state.last_error = None
        mark_feature_state("music", "playing", reason="play_started")
        logger.info("MUSIC_PLAY_STARTED_OK")
        touch_idle()
        return {"ok": True, "state": _snapshot(), "track": track}
    except Exception as exc:
        with _lock:
            _state.state = "error"
            _state.playing = False
            _state.last_error = str(exc)
        mark_feature_error("music", reason=str(exc))
        logger.info("MUSIC_PLAY_FAILED_NON_FATAL reason=%s", exc)
        return {"ok": False, "error": str(exc), "state": _snapshot()}


async def pause_music(*, player: CommandCallback | None = None) -> dict[str, Any]:
    if player is not None:
        result = await _maybe_await(player())
        if isinstance(result, dict) and not bool(result.get("ok", result.get("success"))):
            return {"ok": False, "error": result.get("error") or "pause_failed", "state": _snapshot()}
    with _lock:
        _state.state = "paused"
        _state.playing = False
    mark_feature_state("music", "paused", reason="pause")
    logger.info("MUSIC_PAUSE_OK")
    touch_idle()
    return {"ok": True, "state": _snapshot()}


async def resume_music(*, player: CommandCallback | None = None) -> dict[str, Any]:
    if player is not None:
        result = await _maybe_await(player())
        if isinstance(result, dict) and not bool(result.get("ok", result.get("success"))):
            return {"ok": False, "error": result.get("error") or "resume_failed", "state": _snapshot()}
    with _lock:
        _state.state = "playing"
        _state.playing = True
    mark_feature_state("music", "playing", reason="resume")
    logger.info("MUSIC_RESUME_OK")
    touch_idle()
    return {"ok": True, "state": _snapshot()}


async def stop_music(*, player: CommandCallback | None = None) -> dict[str, Any]:
    if player is not None:
        result = await _maybe_await(player())
        if isinstance(result, dict) and not bool(result.get("ok", result.get("success"))):
            return {"ok": False, "error": result.get("error") or "stop_failed", "state": _snapshot()}
    with _lock:
        _state.state = "paused"
        _state.playing = False
    mark_feature_state("music", "paused", reason="stop")
    logger.info("MUSIC_STOP_OK")
    touch_idle()
    return {"ok": True, "state": _snapshot()}


async def next_track(*, player: CommandCallback | None = None) -> dict[str, Any]:
    if player is None:
        return {"ok": False, "error": "next_not_supported", "state": _snapshot()}
    result = await _maybe_await(player())
    return {"ok": bool(isinstance(result, dict) and result.get("ok", result.get("success"))), "state": _snapshot(), "raw": result}


async def previous_track(*, player: CommandCallback | None = None) -> dict[str, Any]:
    if player is None:
        return {"ok": False, "error": "previous_not_supported", "state": _snapshot()}
    result = await _maybe_await(player())
    return {"ok": bool(isinstance(result, dict) and result.get("ok", result.get("success"))), "state": _snapshot(), "raw": result}


async def set_volume(value: int, *, player: VolumeCallback | None = None) -> dict[str, Any]:
    vol = max(0, min(100, int(value)))
    if player is not None:
        result = await _maybe_await(player(vol))
        if isinstance(result, dict) and not bool(result.get("ok", result.get("success"))):
            return {"ok": False, "error": result.get("error") or "volume_failed", "state": _snapshot()}
    with _lock:
        _state.volume = vol
    touch_idle()
    return {"ok": True, "state": _snapshot()}


async def shutdown_music(reason: str = "not_playing") -> dict[str, Any]:
    with _lock:
        _state.state = "off"
        _state.playing = False
    mark_feature_stopped("music", reason=reason)
    return {"ok": True, "state": _snapshot()}


def set_music_tab_active(active: bool) -> dict[str, Any]:
    with _lock:
        _state.tab_active = bool(active)
    touch_idle()
    return _snapshot()


def run_idle_shutdown_check(*, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    with _lock:
        if _state.playing:
            logger.info("MUSIC_IDLE_SHUTDOWN_SKIPPED reason=playing")
            return _snapshot()
        if _state.tab_active:
            logger.info("MUSIC_IDLE_SHUTDOWN_SKIPPED reason=tab_active")
            return _snapshot()
        last = _state.last_activity_at or now
        if _state.state == "off" or now - last < _state.idle_shutdown_seconds:
            return _snapshot()
    logger.info("MUSIC_IDLE_SHUTDOWN reason=not_playing")
    mark_feature_stopped("music", reason="not_playing")
    with _lock:
        _state.state = "off"
        _state.playing = False
    return _snapshot()

