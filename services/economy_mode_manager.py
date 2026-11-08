from __future__ import annotations

import logging
import inspect
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from services.economy_mode_config import (
    CONFIG_PATH,
    load_economy_mode_config,
    save_economy_mode_config,
)
from services.economy_mode_state import EconomyModeState
from services.session_transport_manager import provider_kind_from_snapshot
from services.standby_wake_audio_cache import get_standby_wake_audio_cache
from services.wake_word_runtime import detect_wake_word, extract_wake_query


logger = logging.getLogger("kira.app")


@dataclass
class EconomyModeManager:
    config_path: str = str(CONFIG_PATH)
    _config: dict[str, Any] = field(default_factory=dict)
    _state: EconomyModeState = EconomyModeState.DISABLED
    _last_interaction_at: float = field(default_factory=time.time)
    _last_log_at: float = 0.0
    _logged_thresholds: set[str] = field(default_factory=set)
    _wake_session_message_count: int = 0
    _last_error: str = ""
    _transport_switch_in_progress: bool = False
    _current_transport: str = "unknown"
    _next_handoff_allowed_at: float = 0.0
    _last_switch_error: str = ""
    _gemini_active: bool = True
    _gemini_ready: bool = True
    _economy_standby: bool = False
    _last_standby_at: float = 0.0
    _last_wake_at: float = 0.0
    _active_after_wake_grace_until: float = 0.0
    _active_after_wake_grace_end_logged: bool = True
    _last_standby_error: str = ""
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        self.reload_config()
        logger.info("ECONOMY_TIMER_STARTED enabled=%s", str(bool(self._config.get("enabled"))).lower())

    def reload_config(self) -> dict[str, Any]:
        with self._lock:
            self._config = load_economy_mode_config(self.config_path)
            self._state = self._initial_active_state(self._config)
            self._logged_thresholds.clear()
            return deepcopy(self._config)

    def get_config(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._config)

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            next_config = deepcopy(self._config)
            next_config.update(dict(updates or {}))
            self._config = save_economy_mode_config(next_config, self.config_path)
            self._state = self._initial_active_state(self._config)
            if self._state not in {EconomyModeState.ECONOMY_LOCAL_WAKE_ONLY, EconomyModeState.ECONOMY_LOCAL_GEMINI_STANDBY}:
                self._gemini_active = True
                self._gemini_ready = True
                self._economy_standby = False
            self._logged_thresholds.clear()
            logger.info("ECONOMY_CONFIG_UPDATED enabled=%s", str(bool(self._config.get("enabled"))).lower())
            return deepcopy(self._config)

    def real_gemini_standby_allowed(self) -> bool:
        with self._lock:
            config = deepcopy(self._config)
        return self._real_gemini_standby_allowed(config)

    def real_gemini_standby_block_reason(self) -> str:
        with self._lock:
            config = deepcopy(self._config)
        return self._real_gemini_standby_block_reason(config)

    def enable(self) -> dict[str, Any]:
        config = self.update_config({"enabled": True})
        with self._lock:
            self._state = self._initial_active_state(self._config)
        logger.info("ECONOMY_MODE_ENABLED")
        self.reset_idle_timer(reason="enabled")
        return config

    def disable(self) -> dict[str, Any]:
        config = self.update_config({"enabled": False})
        with self._lock:
            self._state = EconomyModeState.DISABLED
        logger.info("ECONOMY_MODE_DISABLED")
        return config

    def reset_idle_timer(self, *, reason: str = "manual") -> dict[str, Any]:
        now = time.time()
        grace_seconds = 0
        grace_remaining = 0.0
        extended_grace = False
        with self._lock:
            self._last_interaction_at = now
            self._wake_session_message_count = 0
            self._logged_thresholds.clear()
            if self._state in {EconomyModeState.ACTIVE_AFTER_WAKE, EconomyModeState.ACTIVE_LOCAL_AFTER_WAKE}:
                grace_seconds = int(self._config.get("active_after_wake_grace_seconds", 0) or 0)
                if grace_seconds > 0:
                    self._active_after_wake_grace_until = max(
                        self._active_after_wake_grace_until,
                        now + grace_seconds,
                    )
                    self._active_after_wake_grace_end_logged = False
                    grace_remaining = max(self._active_after_wake_grace_until - now, 0.0)
                    extended_grace = True
            idle_seconds = 0.0
        logger.info("ECONOMY_ACTIVITY_MARKED source=%s", reason)
        logger.info("ECONOMY_IDLE_TIMER_RESET reason=%s", reason)
        if extended_grace:
            logger.info(
                "ECONOMY_ACTIVE_AFTER_WAKE_GRACE_EXTENDED reason=%s seconds=%s remaining_seconds=%s",
                reason,
                grace_seconds,
                int(grace_remaining),
            )
        return {
            "ok": True,
            "idle_seconds": idle_seconds,
            "reason": reason,
            "active_after_wake_grace_remaining_seconds": int(grace_remaining),
        }

    def record_interaction(self, *, reason: str = "interaction") -> None:
        self.reset_idle_timer(reason=reason)

    def set_transport_switch_in_progress(self, value: bool) -> None:
        with self._lock:
            self._transport_switch_in_progress = bool(value)

    def set_current_transport(self, transport: str) -> None:
        normalized = str(transport or "unknown").strip().lower() or "unknown"
        with self._lock:
            self._current_transport = normalized
            if normalized == "local" and self._state in {EconomyModeState.ACTIVE_CLOUD_NORMAL, EconomyModeState.ACTIVE_LOCAL_NORMAL}:
                self._state = EconomyModeState.ACTIVE_LOCAL_GEMINI_ACTIVE
            elif normalized == "cloud" and self._state in {
                EconomyModeState.ACTIVE_LOCAL_NORMAL,
                EconomyModeState.ACTIVE_LOCAL_GEMINI_ACTIVE,
                EconomyModeState.ECONOMY_LOCAL_GEMINI_ACTIVE,
            }:
                self._state = EconomyModeState.ACTIVE_CLOUD_NORMAL

    def set_handoff_cooldown(self, seconds: int | float) -> dict[str, Any]:
        cooldown = max(float(seconds or 0), 0.0)
        with self._lock:
            self._next_handoff_allowed_at = time.time() + cooldown
            next_allowed = self._next_handoff_allowed_at
        logger.info("ECONOMY_TRANSPORT_SWITCH_COOLDOWN_SET seconds=%s", int(cooldown))
        return {"next_handoff_allowed_after_seconds": int(cooldown), "next_handoff_allowed_at": next_allowed}

    def handoff_cooldown_remaining(self) -> int:
        with self._lock:
            remaining = max(self._next_handoff_allowed_at - time.time(), 0.0)
        return int(remaining)

    def set_last_switch_error(self, error: str) -> None:
        with self._lock:
            self._last_switch_error = str(error or "").strip()

    def clear_last_switch_error(self) -> None:
        self.set_last_switch_error("")

    def note_user_message(self) -> int:
        with self._lock:
            if self._state not in {EconomyModeState.ACTIVE_AFTER_WAKE, EconomyModeState.ACTIVE_LOCAL_AFTER_WAKE}:
                return self._wake_session_message_count
            self._wake_session_message_count += 1
            count = self._wake_session_message_count
            limit = int(self._config.get("exit_wake_only_after_user_messages", 3))
            logger.info("WAKE_SESSION_MESSAGE_COUNT count=%s", count)
            if count >= limit:
                self._state = EconomyModeState.ACTIVE_LOCAL_GEMINI_ACTIVE
                logger.info("WAKE_ONLY_EXIT reason=message_count_reached")
        return count

    def test_wake_word(self, text: str) -> dict[str, Any]:
        with self._lock:
            word = detect_wake_word(text, self._config.get("wake_words", []))
        detected = bool(word)
        if detected:
            logger.info("WAKE_WORD_DETECTED word=%s source=test_endpoint", word)
            wake = self.request_gemini_wake(text=text, source="test_endpoint")
            if wake.get("wake_action"):
                return {"detected": True, "word": word, "text": text, **wake}
        return {"detected": detected, "word": word, "text": text}

    def request_gemini_standby(
        self,
        *,
        runtime_snapshot: dict | None = None,
        blockers: dict[str, bool] | None = None,
        executor=None,
        source: str = "api",
        force: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            config = deepcopy(self._config)
            idle_seconds = max(now - self._last_interaction_at, 0.0)
            state = self._state
            grace_remaining = self._active_after_wake_grace_remaining(now, state)
        runtime = runtime_snapshot or {}
        blocker_map = self._build_blockers(config, runtime, blockers or {})
        blocked_reasons = [name for name, active in blocker_map.items() if active]
        if grace_remaining > 0 and not force:
            blocked_reasons.append("active_after_wake_grace")
        provider = provider_kind_from_snapshot(runtime)
        if provider not in {"cloud", "local"}:
            provider = self._current_transport
        threshold_key = (
            "return_to_economy_after_minutes"
            if state in {EconomyModeState.ACTIVE_AFTER_WAKE, EconomyModeState.ACTIVE_LOCAL_AFTER_WAKE}
            else "gemini_standby_after_minutes"
        )
        threshold_seconds = int(config.get(threshold_key, config.get("gemini_standby_after_minutes", 60))) * 60
        eligible = (
            bool(config.get("enabled"))
            and bool(config.get("gemini_standby_enabled"))
            and str(config.get("mode") or "gemini_only").lower() == "gemini_only"
            and provider == "local"
            and (force or idle_seconds >= threshold_seconds)
            and not blocked_reasons
        )
        if not eligible:
            reason = "not_eligible"
            if (force or idle_seconds >= threshold_seconds) and blocked_reasons:
                reason = "blocked"
            if provider != "local":
                reason = "waiting_for_livekit_local"
            logger.info(
                "ECONOMY_TIMER_REAL_STANDBY_BLOCKED reason=%s blocked_reasons=%s source=%s",
                reason,
                ",".join(blocked_reasons) or "none",
                source,
            )
            return {
                "ok": False,
                "action": "gemini_standby",
                "reason": reason,
                "blocked_reasons": blocked_reasons,
                "idle_seconds": round(idle_seconds, 2),
                "active_after_wake_grace_remaining_seconds": int(grace_remaining),
                "state": state.value,
            }

        standby_mode = str(config.get("gemini_standby_mode") or "dry_run").lower()
        logger.info("ECONOMY_GEMINI_STANDBY_BEGIN mode=%s source=%s", standby_mode, source)
        if bool(config.get("standby_wake_ack_generate_before_standby", True)):
            try:
                get_standby_wake_audio_cache().prepare_before_standby(
                    config=config,
                    idle_seconds=idle_seconds,
                )
            except Exception as exc:
                logger.warning("STANDBY_WAKE_ACK_CACHE_FAILED reason=%s", exc)
        if not self._real_gemini_standby_allowed(config):
            reason = self._real_gemini_standby_block_reason(config)
            logger.info("ECONOMY_GEMINI_STANDBY_DRY_RUN reason=%s", reason)
            logger.info("ECONOMY_TIMER_DRY_RUN_USED reason=%s", reason)
            with self._lock:
                self._state = EconomyModeState.ECONOMY_LOCAL_GEMINI_STANDBY
                self._economy_standby = True
                self._active_after_wake_grace_until = 0.0
                self._active_after_wake_grace_end_logged = True
                self._last_standby_at = now
            return {
                "ok": True,
                "action": "gemini_standby",
                "mode": "dry_run",
                "reason": reason,
                "state": EconomyModeState.ECONOMY_LOCAL_GEMINI_STANDBY.value,
            }

        if executor is None:
            error = "real_gemini_standby_executor_unavailable"
            logger.info("ECONOMY_TIMER_REAL_STANDBY_BEGIN source=%s", source)
            logger.error("ECONOMY_GEMINI_STANDBY_AGENT_CONTROL_FAILED reason=%s", error)
            logger.error("ECONOMY_GEMINI_STANDBY_FAILED reason=%s", error)
            logger.error("ECONOMY_TIMER_REAL_STANDBY_FAILED reason=%s", error)
            with self._lock:
                self._last_standby_error = error
                if bool(config.get("gemini_standby_rollback_on_failure", True)):
                    self._state = EconomyModeState.ACTIVE_LOCAL_GEMINI_ACTIVE
                    self._gemini_active = True
                    self._gemini_ready = True
                    self._economy_standby = False
            return {"ok": False, "action": "gemini_standby", "mode": "real", "reason": error, "state": self._state.value}

        try:
            logger.info("ECONOMY_TIMER_REAL_STANDBY_BEGIN source=%s", source)
            logger.info("ECONOMY_GEMINI_STANDBY_AGENT_CONTROL_CHECK_BEGIN")
            result = executor()
            if inspect.isawaitable(result):
                raise RuntimeError("async_executor_not_supported_in_sync_manager")
            if isinstance(result, dict) and result.get("ok") is False:
                raise RuntimeError(str(result.get("reason") or result.get("error") or "executor_failed"))
            logger.info("ECONOMY_GEMINI_STANDBY_AGENT_CONTROL_OK")
        except Exception as exc:
            error = str(exc or "executor_failed")
            logger.error("ECONOMY_GEMINI_STANDBY_AGENT_CONTROL_FAILED reason=%s", error)
            logger.error("ECONOMY_GEMINI_STANDBY_FAILED reason=%s", error)
            logger.error("ECONOMY_TIMER_REAL_STANDBY_FAILED reason=%s", error)
            with self._lock:
                self._last_standby_error = error
                if bool(config.get("gemini_standby_rollback_on_failure", True)):
                    self._state = EconomyModeState.ACTIVE_LOCAL_GEMINI_ACTIVE
                    self._gemini_active = True
                    self._gemini_ready = True
                    self._economy_standby = False
            return {"ok": False, "action": "gemini_standby", "mode": "real", "reason": error, "state": self._state.value}

        with self._lock:
            self._state = EconomyModeState.ECONOMY_LOCAL_GEMINI_STANDBY
            self._gemini_active = False
            self._gemini_ready = False
            self._economy_standby = True
            self._active_after_wake_grace_until = 0.0
            self._active_after_wake_grace_end_logged = True
            self._last_standby_at = now
            self._last_standby_error = ""
        logger.info("ECONOMY_GEMINI_STANDBY_OK mode=real")
        logger.info("ECONOMY_TIMER_REAL_STANDBY_OK")
        return {"ok": True, "action": "gemini_standby", "mode": "real", "state": EconomyModeState.ECONOMY_LOCAL_GEMINI_STANDBY.value}

    def request_gemini_wake(self, *, text: str = "", executor=None, source: str = "api", force: bool = False) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            config = deepcopy(self._config)
            state = self._state
            word = detect_wake_word(text, config.get("wake_words", [])) if text else ""
        detected = bool(word)
        if text and not detected and not force:
            return {"ok": True, "wake_action": "ignored_no_wake_word", "detected": False, "state": state.value}
        if text and detected:
            logger.info("WAKE_WORD_DETECTED word=%s source=%s", word, source)
            try:
                playback = get_standby_wake_audio_cache().request_playback(
                    text=text,
                    config=config,
                    idle_seconds=max(now - self._last_interaction_at, 0.0),
                )
                if playback.ok:
                    logger.info(
                        "STANDBY_WAKE_ACK_PLAY_REQUESTED source=%s category=%s audio_file=%s",
                        playback.source,
                        playback.category,
                        playback.audio_file,
                    )
                    logger.info("STANDBY_WAKE_ACK_PLAYED source=%s", playback.source)
                else:
                    logger.info("STANDBY_WAKE_ACK_SKIPPED_INVALID reason=%s", playback.reason or "unavailable")
            except Exception as exc:
                logger.warning("STANDBY_WAKE_ACK_CACHE_FAILED reason=%s", exc)
        if state not in {EconomyModeState.ECONOMY_LOCAL_WAKE_ONLY, EconomyModeState.ECONOMY_LOCAL_GEMINI_STANDBY}:
            self.reset_idle_timer(reason=f"wake:{source}")
            return {"ok": True, "wake_action": "already_active", "detected": detected, "word": word, "state": state.value}

        mode = str(config.get("gemini_standby_mode") or "dry_run").lower()
        logger.info("ECONOMY_WAKE_UP_GEMINI_BEGIN mode=%s source=%s", mode, source)
        with self._lock:
            self._state = EconomyModeState.WAKING_UP_GEMINI
        if not self._real_gemini_standby_allowed(config):
            with self._lock:
                self._state = EconomyModeState.ACTIVE_LOCAL_AFTER_WAKE
                self._gemini_active = True
                self._gemini_ready = True
                self._economy_standby = False
                self._last_wake_at = now
                self._last_interaction_at = now
                self._wake_session_message_count = 0
                grace_remaining = self._start_active_after_wake_grace_locked(now)
            logger.info("ECONOMY_WAKE_UP_GEMINI_READY mode=dry_run")
            if grace_remaining > 0:
                logger.info("ECONOMY_ACTIVE_AFTER_WAKE_GRACE_BEGIN seconds=%s source=%s", int(grace_remaining), source)
            logger.info('ECONOMY_WAKE_UP_RESPONSE_SENT text="To aqui, chefe." mode=dry_run')
            return {
                "ok": True,
                "detected": detected,
                "word": word,
                "wake_action": "gemini_dry_run_ready",
                "response": "To aqui, chefe.",
                "standby_mode": "dry_run",
                "state": EconomyModeState.ACTIVE_LOCAL_AFTER_WAKE.value,
            }

        if executor is None:
            error = "real_gemini_wake_executor_unavailable"
            logger.error("ECONOMY_WAKE_UP_AGENT_CONTROL_FAILED reason=%s", error)
            logger.error("ECONOMY_WAKE_UP_GEMINI_FAILED reason=%s", error)
            with self._lock:
                self._last_standby_error = error
                self._state = EconomyModeState.ECONOMY_LOCAL_GEMINI_STANDBY
                self._gemini_active = False
                self._gemini_ready = False
                self._economy_standby = True
            return {"ok": False, "detected": detected, "word": word, "wake_action": "failed", "reason": error, "state": self._state.value}
        try:
            logger.info("ECONOMY_WAKE_UP_AGENT_CONTROL_CHECK_BEGIN")
            result = executor()
            if inspect.isawaitable(result):
                raise RuntimeError("async_executor_not_supported_in_sync_manager")
            if isinstance(result, dict) and result.get("ok") is False:
                raise RuntimeError(str(result.get("reason") or result.get("error") or "executor_failed"))
            logger.info("ECONOMY_WAKE_UP_AGENT_CONTROL_OK")
        except Exception as exc:
            error = str(exc or "executor_failed")
            logger.error("ECONOMY_WAKE_UP_AGENT_CONTROL_FAILED reason=%s", error)
            logger.error("ECONOMY_WAKE_UP_GEMINI_FAILED reason=%s", error)
            with self._lock:
                self._last_standby_error = error
                self._state = EconomyModeState.ECONOMY_LOCAL_GEMINI_STANDBY
                self._gemini_active = False
                self._gemini_ready = False
                self._economy_standby = True
            return {"ok": False, "detected": detected, "word": word, "wake_action": "failed", "reason": error, "state": self._state.value}

        with self._lock:
            self._state = EconomyModeState.ACTIVE_LOCAL_AFTER_WAKE
            self._gemini_active = True
            self._gemini_ready = True
            self._economy_standby = False
            self._last_wake_at = now
            self._last_interaction_at = now
            self._wake_session_message_count = 0
            grace_remaining = self._start_active_after_wake_grace_locked(now)
            self._last_standby_error = ""
        logger.info("ECONOMY_WAKE_UP_GEMINI_READY mode=real")
        if grace_remaining > 0:
            logger.info("ECONOMY_ACTIVE_AFTER_WAKE_GRACE_BEGIN seconds=%s source=%s", int(grace_remaining), source)
        return {"ok": True, "detected": detected, "word": word, "wake_action": "gemini_real_ready", "standby_mode": "real", "state": EconomyModeState.ACTIVE_LOCAL_AFTER_WAKE.value}

    def handle_standby_text(self, text: str, *, executor=None, source: str = "text") -> dict[str, Any]:
        with self._lock:
            state = self._state
            config = deepcopy(self._config)
        if state not in {EconomyModeState.ECONOMY_LOCAL_WAKE_ONLY, EconomyModeState.ECONOMY_LOCAL_GEMINI_STANDBY}:
            return {"ok": True, "route": "normal", "state": state.value}
        if not bool(config.get("wake_text_enabled", True)):
            logger.info("ECONOMY_STANDBY_TEXT_IGNORED reason=wake_text_disabled")
            return {"ok": True, "route": "ignored", "reason": "wake_text_disabled", "gemini_allowed": False, "state": state.value}
        wake_query = extract_wake_query(text, config.get("wake_words", []))
        word = str(wake_query.get("wake_word") or "")
        if not word:
            logger.info("ECONOMY_STANDBY_TEXT_IGNORED reason=no_wake_word")
            return {"ok": True, "route": "ignored", "reason": "no_wake_word", "gemini_allowed": False, "state": state.value}
        remaining_query = str(wake_query.get("remaining_user_query") or "").strip()
        if remaining_query:
            logger.info(
                "ECONOMY_WAKE_WITH_QUERY_DETECTED word=%s source=%s query_chars=%s",
                word,
                source,
                len(remaining_query),
            )
            logger.info("ECONOMY_WAKE_QUERY_EXTRACTED source=%s query=%r", source, remaining_query[:160])
        wake = self.request_gemini_wake(text=text, executor=executor, source=source)
        wake["route"] = "wake"
        wake["gemini_allowed"] = bool(wake.get("ok"))
        wake["wake_word"] = word
        wake["remaining_user_query"] = remaining_query
        wake["wake_with_query"] = bool(remaining_query)
        return wake

    def get_status(self, runtime_snapshot: dict | None = None, blockers: dict[str, bool] | None = None) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            config = deepcopy(self._config)
            state = self._state
            transport_switch_in_progress = self._transport_switch_in_progress
            current_transport = self._current_transport
            next_handoff_allowed_at = self._next_handoff_allowed_at
            last_switch_error = self._last_switch_error
            gemini_active = self._gemini_active
            gemini_ready = self._gemini_ready
            economy_standby = self._economy_standby
            last_standby_at = self._last_standby_at
            last_wake_at = self._last_wake_at
            last_standby_error = self._last_standby_error
            grace_remaining = self._active_after_wake_grace_remaining(now, state)
            cooldown_remaining = self.handoff_cooldown_remaining()
            idle_seconds = max(now - self._last_interaction_at, 0.0)
            log_interval = max(int(config.get("log_interval_seconds", 60)), 10)
            should_log = now - self._last_log_at >= log_interval
            if should_log:
                self._last_log_at = now

        runtime = runtime_snapshot or {}
        blocker_map = self._build_blockers(config, runtime, blockers or {})
        blocked_reasons = [name for name, active in blocker_map.items() if active]
        if grace_remaining > 0:
            blocked_reasons.append("active_after_wake_grace")
        elif state in {EconomyModeState.ACTIVE_AFTER_WAKE, EconomyModeState.ACTIVE_LOCAL_AFTER_WAKE}:
            with self._lock:
                if not self._active_after_wake_grace_end_logged:
                    self._active_after_wake_grace_end_logged = True
                    logger.info("ECONOMY_ACTIVE_AFTER_WAKE_GRACE_ENDED")
        if should_log:
            logger.info("ECONOMY_IDLE_UPDATED idle_seconds=%s state=%s", int(idle_seconds), state.value)
            logger.info("ECONOMY_TIMER_LIGHT_TICK idle_seconds=%s state=%s", int(idle_seconds), state.value)
            for reason in blocked_reasons:
                logger.info("ECONOMY_BLOCKED reason=%s", reason)

        cloud_threshold_seconds = int(config.get("cloud_to_local_after_minutes", 60)) * 60
        gemini_threshold_key = (
            "return_to_economy_after_minutes"
            if state in {EconomyModeState.ACTIVE_AFTER_WAKE, EconomyModeState.ACTIVE_LOCAL_AFTER_WAKE}
            else "gemini_standby_after_minutes"
        )
        gemini_threshold_seconds = int(config.get(gemini_threshold_key, config.get("gemini_standby_after_minutes", 90))) * 60
        provider = provider_kind_from_snapshot(runtime)
        if provider in {"cloud", "local"}:
            current_transport = provider
        local_default = bool(config.get("livekit_local_default", True)) or str(config.get("mode") or "").lower() == "gemini_only"
        cloud_to_local_eligible = False
        cloud_to_local_reason = "not_applicable_local_default" if local_default else "disabled"
        if provider == "local":
            cloud_to_local_reason = "already_local"
        gemini_standby_eligible = (
            bool(config.get("enabled"))
            and str(config.get("mode") or "gemini_only").lower() == "gemini_only"
            and bool(config.get("gemini_standby_enabled"))
            and current_transport == "local"
            and idle_seconds >= gemini_threshold_seconds
            and not blocked_reasons
        )
        with self._lock:
            log_cloud_threshold = cloud_to_local_eligible and "cloud_to_local" not in self._logged_thresholds
            log_gemini_threshold = gemini_standby_eligible and "gemini_standby" not in self._logged_thresholds
            if log_cloud_threshold:
                self._logged_thresholds.add("cloud_to_local")
            if log_gemini_threshold:
                self._logged_thresholds.add("gemini_standby")
        if log_cloud_threshold:
            logger.info("ECONOMY_IDLE_THRESHOLD_REACHED threshold=cloud_to_local idle_seconds=%s", int(idle_seconds))
            logger.info("ECONOMY_CLOUD_TO_LOCAL_ELIGIBLE")
        if log_gemini_threshold:
            logger.info("ECONOMY_IDLE_THRESHOLD_REACHED threshold=gemini_standby idle_seconds=%s", int(idle_seconds))
            logger.info("ECONOMY_GEMINI_STANDBY_ELIGIBLE")
            if self._real_gemini_standby_allowed(config):
                logger.info("ECONOMY_TIMER_REAL_STANDBY_ELIGIBLE")
            elif blocked_reasons:
                logger.info("ECONOMY_TIMER_REAL_STANDBY_BLOCKED reason=%s", ",".join(blocked_reasons))
            if not self._real_gemini_standby_allowed(config):
                dry_run_reason = self._real_gemini_standby_block_reason(config)
                logger.info("ECONOMY_GEMINI_STANDBY_BEGIN mode=dry_run")
                logger.info("ECONOMY_GEMINI_STANDBY_DRY_RUN reason=%s", dry_run_reason)
                logger.info("ECONOMY_TIMER_DRY_RUN_USED reason=%s", dry_run_reason)
                with self._lock:
                    if self._state in {
                        EconomyModeState.ACTIVE_LOCAL_NORMAL,
                        EconomyModeState.ACTIVE_LOCAL_GEMINI_ACTIVE,
                        EconomyModeState.ECONOMY_LOCAL_GEMINI_ACTIVE,
                    }:
                        self._state = EconomyModeState.ECONOMY_LOCAL_GEMINI_STANDBY
                        self._economy_standby = True
                        self._last_standby_at = now
                        state = self._state
        elif idle_seconds >= gemini_threshold_seconds and blocked_reasons and should_log:
            logger.info("ECONOMY_TIMER_REAL_STANDBY_BLOCKED reason=%s", ",".join(blocked_reasons))
        if should_log and local_default:
            logger.info("ECONOMY_TRANSPORT_HANDOFF_NOT_NEEDED reason=%s", cloud_to_local_reason)
            logger.info("ECONOMY_CLOUD_TO_LOCAL_SKIPPED reason=%s", cloud_to_local_reason)

        effective_state = state
        if bool(config.get("enabled")) and state == EconomyModeState.ACTIVE_CLOUD_NORMAL and provider == "local":
            effective_state = EconomyModeState.ACTIVE_LOCAL_GEMINI_ACTIVE
        standby_states = {EconomyModeState.ECONOMY_LOCAL_WAKE_ONLY, EconomyModeState.ECONOMY_LOCAL_GEMINI_STANDBY}
        economy_standby_effective = bool(economy_standby or state in standby_states)
        transport_voice_pipeline_alive = bool(runtime.get("voice_pipeline_ready", True))
        gemini_voice_available = bool(gemini_active and gemini_ready and state not in standby_states)
        voice_available = bool(transport_voice_pipeline_alive and gemini_voice_available and not economy_standby_effective)
        try:
            wake_ack_status = get_standby_wake_audio_cache().get_status()
        except Exception:
            wake_ack_status = {}
        return {
            "ok": True,
            "enabled": bool(config.get("enabled")),
            "mode": config.get("mode"),
            "strategy": "gemini_only_local_first",
            "state": effective_state.value,
            "livekit_provider": provider,
            "gemini_standby_enabled": bool(config.get("gemini_standby_enabled")),
            "gemini_standby_mode": str(config.get("gemini_standby_mode") or "dry_run"),
            "allow_real_gemini_standby": bool(config.get("allow_real_gemini_standby")),
            "real_gemini_standby_enabled": self._real_gemini_standby_allowed(config),
            "real_gemini_standby_block_reason": self._real_gemini_standby_block_reason(config),
            "gemini_active": bool(gemini_active and state not in standby_states),
            "gemini_ready": bool(gemini_ready and state not in standby_states),
            "transport_voice_pipeline_alive": transport_voice_pipeline_alive,
            "gemini_voice_available": gemini_voice_available,
            "voice_available": voice_available,
            "voice_unavailable_reason": "gemini_standby" if economy_standby_effective else ("" if voice_available else "voice_pipeline_unavailable"),
            "transport_ready": current_transport == "local",
            "economy_standby": economy_standby_effective,
            "wake_text_enabled": bool(config.get("wake_text_enabled")),
            "wake_audio_enabled": False,
            "last_standby_at": last_standby_at,
            "last_wake_at": last_wake_at,
            "last_standby_error": last_standby_error,
            "livekit_local_default": bool(config.get("livekit_local_default")),
            "livekit_cloud_disabled_by_default": bool(config.get("livekit_cloud_disabled_by_default")),
            "livekit_cloud_fallback_enabled": bool(config.get("livekit_cloud_fallback_enabled")),
            "cloud_fallback_mode": config.get("cloud_fallback_mode"),
            "gemini_standby": state in standby_states,
            "wake_only": state == EconomyModeState.ECONOMY_LOCAL_WAKE_ONLY,
            "standby_mode": str(config.get("gemini_standby_mode") or "dry_run") if state in standby_states else "active",
            "idle_seconds": round(idle_seconds, 2),
            "idle_minutes": round(idle_seconds / 60.0, 2),
            "cloud_to_local_after_minutes": config.get("cloud_to_local_after_minutes"),
            "gemini_standby_after_minutes": config.get("gemini_standby_after_minutes"),
            "return_to_economy_after_minutes": config.get("return_to_economy_after_minutes"),
            "active_after_wake_grace_seconds": config.get("active_after_wake_grace_seconds"),
            "active_after_wake_grace_remaining_seconds": int(grace_remaining),
            "cloud_to_local_eligible": cloud_to_local_eligible,
            "cloud_to_local_reason": cloud_to_local_reason,
            "gemini_standby_eligible": gemini_standby_eligible,
            "blocked": bool(blocked_reasons),
            "blocked_reasons": blocked_reasons,
            "current_transport": current_transport,
            "transport_switch_in_progress": transport_switch_in_progress,
            "manual_handoff_enabled": bool(config.get("manual_handoff_enabled")),
            "auto_handoff_enabled": bool(config.get("auto_handoff_enabled")),
            "enable_real_cloud_to_local_handoff": bool(config.get("enable_real_cloud_to_local_handoff")),
            "handoff_cooldown_remaining_seconds": cooldown_remaining,
            "next_handoff_allowed_at": next_handoff_allowed_at if cooldown_remaining > 0 else 0.0,
            "last_switch_error": last_switch_error,
            "next_action": self._next_action(config, idle_seconds, gemini_standby_eligible, current_transport),
            "wake_session_message_count": self._wake_session_message_count,
            "last_error": self._last_error,
            **wake_ack_status,
        }

    @staticmethod
    def _initial_active_state(config: dict[str, Any]) -> EconomyModeState:
        if not bool(config.get("enabled")):
            return EconomyModeState.DISABLED
        if str(config.get("mode") or "gemini_only").strip().lower() == "gemini_only" or bool(config.get("livekit_local_default", True)):
            return EconomyModeState.ACTIVE_LOCAL_GEMINI_ACTIVE
        return EconomyModeState.ACTIVE_CLOUD_NORMAL

    @staticmethod
    def _build_blockers(config: dict[str, Any], runtime: dict[str, Any], explicit: dict[str, bool]) -> dict[str, bool]:
        return {
            "speaking": bool(config.get("do_not_enter_if_speaking")) and str(runtime.get("agent_state") or runtime.get("voice_status") or "").lower() in {"speaking", "talking"},
            "conversation_active": bool(config.get("do_not_enter_if_tool_running")) and str(runtime.get("agent_state") or "").lower() in {"thinking", "responding"},
            "active_text_queue": bool(config.get("do_not_enter_if_tool_running")) and bool(
                explicit.get("active_text_queue")
                or explicit.get("text_queue_active")
                or explicit.get("user_text_queue_active")
                or runtime.get("active_text_queue")
                or runtime.get("text_queue_active")
                or runtime.get("user_text_queue_active")
            ),
            "generation_running": bool(config.get("do_not_enter_if_tool_running")) and bool(runtime.get("generation_in_progress") or runtime.get("generation_pending") or explicit.get("generation_running")),
            "tool_running": bool(config.get("do_not_enter_if_tool_running")) and bool(explicit.get("tool_running")),
            "manus_running": bool(config.get("do_not_enter_if_manus_running")) and bool(explicit.get("manus_running")),
            "pc_operator_running": bool(config.get("do_not_enter_if_pc_operator_running")) and bool(explicit.get("pc_operator_running")),
            "messages_sending": bool(config.get("do_not_enter_if_messages_sending")) and bool(explicit.get("messages_sending")),
            "remote_client_connected": bool(config.get("do_not_enter_if_remote_client_connected")) and bool(explicit.get("remote_client_connected")),
            "transitioning": bool(explicit.get("transport_transitioning")),
        }

    def _start_active_after_wake_grace_locked(self, now: float) -> int:
        grace_seconds = int(self._config.get("active_after_wake_grace_seconds", 0) or 0)
        if grace_seconds <= 0:
            self._active_after_wake_grace_until = 0.0
            self._active_after_wake_grace_end_logged = True
            return 0
        self._active_after_wake_grace_until = now + grace_seconds
        self._active_after_wake_grace_end_logged = False
        return grace_seconds

    def _active_after_wake_grace_remaining(self, now: float, state: EconomyModeState) -> float:
        if state not in {EconomyModeState.ACTIVE_AFTER_WAKE, EconomyModeState.ACTIVE_LOCAL_AFTER_WAKE}:
            return 0.0
        return max(self._active_after_wake_grace_until - now, 0.0)

    @staticmethod
    def _real_gemini_standby_allowed(config: dict[str, Any]) -> bool:
        return bool(
            config.get("gemini_standby_enabled")
            and str(config.get("gemini_standby_mode") or "").strip().lower() == "real"
            and config.get("allow_real_gemini_standby")
        )

    @staticmethod
    def _real_gemini_standby_block_reason(config: dict[str, Any]) -> str:
        if not bool(config.get("gemini_standby_enabled")):
            return "gemini_standby_disabled"
        if str(config.get("gemini_standby_mode") or "").strip().lower() != "real":
            return "standby_mode_not_real"
        if not bool(config.get("allow_real_gemini_standby")):
            return "real_standby_not_confirmed"
        return ""

    @staticmethod
    def _next_action(config: dict[str, Any], idle_seconds: float, gemini_ready: bool, current_transport: str) -> dict[str, Any]:
        if not bool(config.get("enabled")):
            return {"type": "none", "reason": "disabled"}
        if current_transport != "local":
            return {"type": "gemini_standby", "eligible": False, "reason": "waiting_for_livekit_local"}
        if gemini_ready:
            return {"type": "gemini_standby", "eligible": True}
        gemini_in = max(int(config.get("gemini_standby_after_minutes", 90)) * 60 - idle_seconds, 0.0)
        return {"type": "gemini_standby", "eligible": False, "seconds_remaining": round(gemini_in, 2)}


_manager: EconomyModeManager | None = None


def get_economy_mode_manager() -> EconomyModeManager:
    global _manager
    if _manager is None:
        _manager = EconomyModeManager()
    return _manager
