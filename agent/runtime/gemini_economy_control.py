from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


logger = logging.getLogger(__name__)


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class GeminiEconomyControlServer:
    def __init__(
        self,
        *,
        session_ref: Callable[[], object | None],
        loop: asyncio.AbstractEventLoop,
        host: str = "127.0.0.1",
        port: int | None = None,
        economy_config_loader: Callable[[], dict[str, Any]] | None = None,
        voice_status_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.session_ref = session_ref
        self.loop = loop
        self.host = host
        self.port = int(os.getenv("KIRA_AGENT_GEMINI_CONTROL_PORT", "8091")) if port is None else int(port)
        self.economy_config_loader = economy_config_loader
        self.voice_status_provider = voice_status_provider
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._keepalive_thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._started_at = 0.0
        self._wake_cycle_count = 0

    def start(self) -> None:
        if self._server is not None:
            return

        logger.info("GEMINI_ECONOMY_CONTROL_STARTING")
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *args) -> None:
                logger.debug("GEMINI_ECONOMY_CONTROL_HTTP " + _format, *args)

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0].rstrip("/")
                if path in {"", "/health"}:
                    status = parent.status()
                    phase = "after_standby" if status.get("economy_standby") else "before_standby"
                    logger.info("GEMINI_ECONOMY_CONTROL_HEALTH_OK %s", phase)
                    self._send_json(parent.health())
                    return
                if path == "/status":
                    self._send_json(parent.status())
                    return
                self._send_json({"ok": False, "reason": "not_found"}, status=404)

            def do_POST(self) -> None:
                path = self.path.split("?", 1)[0].rstrip("/")
                if path == "/economy/gemini-standby":
                    logger.info("GEMINI_ECONOMY_CONTROL_STANDBY_REQUEST_RECEIVED")
                    logger.info("GEMINI_ECONOMY_CONTROL_STANDBY_HANDLER_BEGIN")
                    try:
                        result = parent._coerce_standby_result(parent._run_agent_coroutine(parent._enter_standby()))
                        logger.info("GEMINI_ECONOMY_CONTROL_STANDBY_RESPONSE_BUILD_BEGIN")
                        sent = self._send_json(result, status=200 if result.get("ok") else 503)
                        if sent:
                            logger.info("GEMINI_ECONOMY_CONTROL_STANDBY_RESPONSE_SENT")
                    except Exception as exc:
                        logger.exception("GEMINI_ECONOMY_CONTROL_STANDBY_HANDLER_FAILED reason=%s", exc)
                        failure = parent._standby_failure_payload(str(exc or "standby_handler_failed"))
                        self._send_json(failure, status=503)
                    return
                if path == "/economy/gemini-wake":
                    result = parent._run_agent_coroutine(parent._wake())
                    self._send_json(result, status=200 if result.get("ok") else 503)
                    return
                self._send_json({"ok": False, "reason": "not_found"}, status=404)

            def _send_json(self, payload: dict, *, status: int = 200) -> bool:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
                    return True
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
                    logger.warning("GEMINI_ECONOMY_CONTROL_CONNECTION_LOST_DURING_STANDBY reason=%s", exc)
                    return False

        self._stopping.clear()
        try:
            self._server = _ReusableThreadingHTTPServer((self.host, self.port), Handler)
        except Exception as exc:
            logger.error("GEMINI_ECONOMY_CONTROL_START_FAILED reason=%s", exc)
            raise
        self.port = int(self._server.server_address[1])
        self._started_at = time.time()
        self._thread = threading.Thread(
            target=self._serve_forever,
            name="kira-gemini-economy-control",
            daemon=True,
        )
        self._thread.start()
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop,
            name="kira-gemini-economy-control-keepalive",
            daemon=True,
        )
        self._keepalive_thread.start()
        logger.info("GEMINI_ECONOMY_CONTROL_STARTED url=http://%s:%s", self.host, self.port)

    def stop(self, *, reason: str = "cleanup") -> None:
        server = self._server
        if server is None:
            return
        self._stopping.set()
        server.shutdown()
        server.server_close()
        self._server = None
        logger.info("GEMINI_ECONOMY_CONTROL_STOPPED reason=%s", reason)

    def health(self) -> dict[str, object]:
        return {
            "ok": self._server is not None and not self._stopping.is_set(),
            "service": "gemini_economy_control",
            "port": self.port,
            "uptime_seconds": round(max(time.time() - self._started_at, 0.0), 2) if self._started_at else 0.0,
        }

    def status(self) -> dict[str, object]:
        health = self.health()
        realtime_session = self._realtime_session()
        voice_status: dict[str, Any] = {}
        if self.voice_status_provider is not None:
            try:
                voice_status = dict(self.voice_status_provider() or {})
            except Exception as exc:
                logger.warning("GEMINI_ECONOMY_CONTROL_VOICE_STATUS_FAILED reason=%s", exc)
                voice_status = {}
        economy_standby = bool(getattr(realtime_session, "economy_standby", False))
        live_connected = bool(getattr(realtime_session, "is_live_connected", False))
        voice_degraded_mode = bool(voice_status.get("voice_degraded_mode"))
        voice_degraded = bool(
            getattr(realtime_session, "voice_audio_degraded", False)
            or voice_status.get("voice_degraded")
            or voice_degraded_mode
        )
        voice_degraded_reason = str(
            voice_status.get("voice_degraded_reason")
            or getattr(realtime_session, "voice_audio_degraded_reason", "")
            or ""
        )
        participant_disconnect_recovery_in_progress = bool(
            voice_status.get("participant_disconnect_recovery_in_progress")
        )
        voice_deep_recovery_in_progress = bool(voice_status.get("voice_deep_recovery_in_progress"))
        voice_deep_recovery_required = bool(voice_status.get("voice_deep_recovery_required"))
        voice_final_degraded_mode = bool(voice_status.get("voice_final_degraded_mode"))
        recovery_in_progress = bool(
            getattr(realtime_session, "_voice_refresh_in_progress", False)
            or participant_disconnect_recovery_in_progress
            or voice_deep_recovery_in_progress
        )
        warmup_required = bool(voice_status.get("warmup_required"))
        post_restart_warmup_in_progress = bool(voice_status.get("post_restart_warmup_in_progress"))
        last_had_first_chunk = False
        try:
            if __package__ and __package__.startswith("agent"):
                from agent.runtime.audio_confirmation import get_audio_confirmation
            else:  # pragma: no cover
                from runtime.audio_confirmation import get_audio_confirmation

            last_had_first_chunk = bool(get_audio_confirmation().last_had_first_chunk())
        except Exception:
            last_had_first_chunk = False
        if economy_standby:
            state = "ECONOMY_LOCAL_GEMINI_STANDBY"
            gemini_active = False
            gemini_ready = False
            voice_available = False
            reason = "economy_standby"
        else:
            state = "ACTIVE_LOCAL_GEMINI_ACTIVE" if live_connected else "ACTIVE_LOCAL_GEMINI_UNKNOWN"
            gemini_active = live_connected
            gemini_ready = live_connected
            voice_available = (
                live_connected
                and not voice_degraded
                and not warmup_required
                and not post_restart_warmup_in_progress
                and not participant_disconnect_recovery_in_progress
            )
            if participant_disconnect_recovery_in_progress:
                voice_available = False
                reason = "participant_disconnect_recovery_in_progress"
            elif voice_final_degraded_mode:
                voice_available = False
                reason = str(voice_status.get("voice_final_degraded_reason") or voice_degraded_reason or "voice_final_degraded")
            elif voice_deep_recovery_in_progress:
                voice_available = False
                reason = "voice_deep_recovery_in_progress"
            elif voice_deep_recovery_required:
                voice_available = False
                reason = "voice_deep_recovery_required"
            elif voice_degraded_mode:
                voice_available = False
                reason = voice_degraded_reason or "post_restart_warmup_failed_max_attempts"
            elif post_restart_warmup_in_progress:
                reason = "post_restart_warmup_in_progress"
            elif warmup_required:
                reason = "post_restart_warmup_required"
            else:
                reason = "gemini_audio_degraded" if voice_degraded else ""
        return {
            **health,
            "state": state,
            "economy_standby": economy_standby,
            "gemini_active": gemini_active,
            "gemini_ready": gemini_ready,
            "voice_available": voice_available,
            "voice_degraded": voice_degraded,
            "voice_degraded_reason": voice_degraded_reason,
            "voice_degraded_mode": voice_degraded_mode,
            "voice_degraded_since": float(voice_status.get("voice_degraded_since", 0.0) or 0.0),
            "recovery_in_progress": recovery_in_progress,
            "reason": reason,
            "last_had_first_chunk": last_had_first_chunk,
            "warmup_required": warmup_required,
            "post_restart_warmup_in_progress": post_restart_warmup_in_progress,
            "warmup_elapsed_seconds": float(voice_status.get("warmup_elapsed_seconds", 0.0) or 0.0),
            "last_warmup_error": str(
                voice_status.get("last_warmup_error")
                or voice_status.get("post_restart_warmup_last_error")
                or ""
            ),
            "warmup_stuck_count": int(voice_status.get("warmup_stuck_count", 0) or 0),
            "post_restart_warmup_failure_count": int(voice_status.get("post_restart_warmup_failure_count", 0) or 0),
            "post_restart_warmup_max_failures": int(voice_status.get("post_restart_warmup_max_failures", 2) or 2),
            "participant_disconnect_recovery_in_progress": participant_disconnect_recovery_in_progress,
            "last_participant_disconnect_recovery_at": float(
                voice_status.get("last_participant_disconnect_recovery_at", 0.0) or 0.0
            ),
            "last_participant_disconnect_recovery_result": str(
                voice_status.get("last_participant_disconnect_recovery_result", "") or ""
            ),
            "last_participant_disconnect_recovery_error": str(
                voice_status.get("last_participant_disconnect_recovery_error", "") or ""
            ),
            "participant_disconnect_recovery_count": int(
                voice_status.get("participant_disconnect_recovery_count", 0) or 0
            ),
            "voice_deep_recovery_required": voice_deep_recovery_required,
            "voice_deep_recovery_in_progress": voice_deep_recovery_in_progress,
            "voice_deep_recovery_count": int(voice_status.get("voice_deep_recovery_count", 0) or 0),
            "last_voice_deep_recovery_at": float(voice_status.get("last_voice_deep_recovery_at", 0.0) or 0.0),
            "last_voice_deep_recovery_result": str(voice_status.get("last_voice_deep_recovery_result", "") or ""),
            "last_voice_deep_recovery_error": str(voice_status.get("last_voice_deep_recovery_error", "") or ""),
            "voice_final_degraded_mode": voice_final_degraded_mode,
            "voice_final_degraded_reason": str(voice_status.get("voice_final_degraded_reason", "") or ""),
            "agent_session_activity_rebind_supported": bool(
                voice_status.get("agent_session_activity_rebind_supported", False)
            ),
            "agent_session_activity_rebind_last_result": str(
                voice_status.get("agent_session_activity_rebind_last_result", "") or ""
            ),
            "voice_turns_blocked_by_warmup": int(voice_status.get("voice_turns_blocked_by_warmup", 0) or 0),
            "last_warmup_blocked_turn_at": float(voice_status.get("last_warmup_blocked_turn_at", 0.0) or 0.0),
            "last_warmup_blocked_source": str(voice_status.get("last_warmup_blocked_source", "") or ""),
            "preventive_rotation_enabled": bool(voice_status.get("preventive_rotation_enabled", False)),
            "preventive_rotation_active": bool(voice_status.get("preventive_rotation_active", False)),
            "preventive_rotation_epoch": int(voice_status.get("preventive_rotation_epoch", 0) or 0),
            "preventive_rotation_next_at": float(voice_status.get("preventive_rotation_next_at", 0.0) or 0.0),
            "standby_wake_ack_cache_valid": bool(voice_status.get("standby_wake_ack_cache_valid", False)),
            "standby_wake_ack_category": str(voice_status.get("standby_wake_ack_category", "") or ""),
            "standby_wake_ack_generated_at": str(voice_status.get("standby_wake_ack_generated_at", "") or ""),
            "gemini_active_epoch": int(getattr(realtime_session, "live_session_epoch", 0) or 0),
            "live_session_epoch": int(getattr(realtime_session, "live_session_epoch", 0) or 0),
        }

    def _serve_forever(self) -> None:
        try:
            server = self._server
            if server is not None:
                server.serve_forever()
        except Exception as exc:
            if not self._stopping.is_set():
                logger.exception("GEMINI_ECONOMY_CONTROL_UNEXPECTED_STOP reason=%s", exc)
        finally:
            if not self._stopping.is_set():
                logger.error("GEMINI_ECONOMY_CONTROL_UNEXPECTED_STOP reason=serve_forever_exited")

    def _keepalive_loop(self) -> None:
        interval = max(float(os.getenv("KIRA_GEMINI_ECONOMY_CONTROL_KEEPALIVE_SECONDS", "30")), 5.0)
        while not self._stopping.wait(interval):
            if self._server is None:
                if not self._stopping.is_set():
                    logger.error("GEMINI_ECONOMY_CONTROL_UNEXPECTED_STOP reason=server_missing")
                return
            logger.info("GEMINI_ECONOMY_CONTROL_KEEPALIVE")

    def _run_agent_coroutine(self, coro) -> dict:
        try:
            if self.loop.is_closed() or not self.loop.is_running():
                coro.close()
                return {"ok": False, "reason": "agent_loop_not_running"}
            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
            result = future.result(timeout=30.0)
            return result if isinstance(result, dict) else {"ok": True, "detail": result}
        except Exception as exc:
            logger.warning("GEMINI_ECONOMY_CONTROL_FAILED reason=%s", exc)
            return {"ok": False, "reason": str(exc), "type": exc.__class__.__name__}

    def _realtime_session(self):
        session = self.session_ref()
        return getattr(getattr(session, "_activity", None), "realtime_llm_session", None)

    async def _enter_standby(self) -> dict:
        realtime_session = self._realtime_session()
        standby = getattr(realtime_session, "enter_economy_standby", None)
        if not callable(standby):
            return self._standby_failure_payload("gemini_realtime_session_unavailable")
        logger.info("GEMINI_ECONOMY_CONTROL_STANDBY_CALL_REALTIME_BEGIN")
        try:
            timeout = max(float(os.getenv("KIRA_GEMINI_ECONOMY_CONTROL_STANDBY_TIMEOUT_SECONDS", "20")), 1.0)
            result = await asyncio.wait_for(standby(), timeout=timeout)
        except Exception as exc:
            return self._standby_failure_payload(str(exc or exc.__class__.__name__))
        logger.info("GEMINI_ECONOMY_CONTROL_STANDBY_CALL_REALTIME_OK")
        return self._coerce_standby_result(result if isinstance(result, dict) else {"detail": result})

    async def _wake(self) -> dict:
        realtime_session = self._realtime_session()
        wake = getattr(realtime_session, "wake_from_economy_standby", None)
        if not callable(wake):
            return {"ok": False, "reason": "gemini_realtime_session_unavailable"}
        result = await wake()
        payload = dict(result) if isinstance(result, dict) else {"ok": True, "detail": result}
        if payload.get("ok") is False:
            return payload
        self._wake_cycle_count += 1
        logger.info("GEMINI_WAKE_CYCLE_COUNT count=%s", self._wake_cycle_count)
        refresh_payload = await self._maybe_preventive_refresh_after_wake(realtime_session)
        if refresh_payload is not None:
            payload["preventive_refresh"] = refresh_payload
        return payload

    def _load_economy_config(self) -> dict[str, Any]:
        if self.economy_config_loader is not None:
            return dict(self.economy_config_loader() or {})
        try:
            from services.economy_mode_config import load_economy_mode_config

            return dict(load_economy_mode_config() or {})
        except Exception as exc:
            logger.warning("GEMINI_PREVENTIVE_REFRESH_CONFIG_LOAD_FAILED reason=%s", exc)
            return {}

    async def _maybe_preventive_refresh_after_wake(self, realtime_session: object | None) -> dict[str, object] | None:
        config = self._load_economy_config()
        if not bool(config.get("gemini_preventive_refresh_enabled", False)):
            return None
        threshold = int(config.get("gemini_refresh_after_wake_cycles", 4) or 4)
        threshold = max(threshold, 1)
        if self._wake_cycle_count < threshold:
            return None
        logger.warning(
            "GEMINI_PREVENTIVE_REFRESH_ELIGIBLE count=%s threshold=%s",
            self._wake_cycle_count,
            threshold,
        )
        logger.warning(
            "GEMINI_PREVENTIVE_REFRESH_PRE_TURN_CHECK count=%s threshold=%s",
            self._wake_cycle_count,
            threshold,
        )
        logger.warning("GEMINI_PREVENTIVE_REFRESH_BLOCKING_TURN")
        logger.warning("GEMINI_PREVENTIVE_REFRESH_BEFORE_WAKE_QUERY")
        refresh = getattr(realtime_session, "refresh_after_voice_degradation", None)
        if not callable(refresh):
            logger.warning("GEMINI_PREVENTIVE_REFRESH_FAILED reason=refresh_method_unavailable")
            return {"ok": False, "reason": "refresh_method_unavailable", "wake_cycle_count": self._wake_cycle_count}
        logger.warning("GEMINI_PREVENTIVE_REFRESH_BEGIN count=%s threshold=%s", self._wake_cycle_count, threshold)
        try:
            result = await refresh(reason="preventive_wake_cycle_count")
        except TypeError:
            result = await refresh()
        except Exception as exc:
            logger.warning("GEMINI_PREVENTIVE_REFRESH_FAILED reason=%s", exc)
            return {"ok": False, "reason": str(exc or "preventive_refresh_failed"), "wake_cycle_count": self._wake_cycle_count}
        payload = dict(result) if isinstance(result, dict) else {"ok": bool(result), "detail": result}
        if payload.get("ok"):
            logger.warning("GEMINI_PREVENTIVE_REFRESH_OK count=%s threshold=%s", self._wake_cycle_count, threshold)
            self._wake_cycle_count = 0
            logger.warning("GEMINI_PREVENTIVE_REFRESH_COUNTER_RESET")
            logger.info("VOICE_HEALTH_AFTER_WAKE_OK reason=preventive_refresh")
            logger.info("VOICE_HEALTH_AFTER_WAKE_STATUS_RECONCILED reason=preventive_refresh")
        else:
            logger.warning(
                "GEMINI_PREVENTIVE_REFRESH_FAILED reason=%s",
                payload.get("reason") or payload.get("error") or "unknown",
            )
        payload["wake_cycle_count"] = self._wake_cycle_count
        return payload

    def _coerce_standby_result(self, result: dict | object) -> dict:
        if not isinstance(result, dict):
            result = {"detail": result}
        if result.get("ok") is False:
            return self._standby_failure_payload(str(result.get("reason") or result.get("error") or "standby_failed"))
        payload = dict(result)
        payload.update(
            {
                "ok": True,
                "state": "ECONOMY_LOCAL_GEMINI_STANDBY",
                "gemini_active": False,
                "gemini_ready": False,
                "voice_available": False,
                "reason": payload.get("reason") or "economy_standby",
            }
        )
        return payload

    @staticmethod
    def _standby_failure_payload(reason: str) -> dict[str, object]:
        return {
            "ok": False,
            "error": "standby_failed",
            "reason": str(reason or "standby_failed"),
            "gemini_active": True,
            "gemini_ready": True,
            "voice_available": True,
            "state": "ACTIVE_LOCAL_GEMINI_ACTIVE",
        }
