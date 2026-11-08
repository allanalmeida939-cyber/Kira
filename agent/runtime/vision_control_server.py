from __future__ import annotations

import json
import logging
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


logger = logging.getLogger(__name__)


class _VisionControlHttpServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, request_handler_class, *, controller) -> None:
        self.controller = controller
        super().__init__(server_address, request_handler_class)


class _VisionControlRequestHandler(BaseHTTPRequestHandler):
    server_version = "KiraVisionControl/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            self._send_json(HTTPStatus.OK, {"success": True, "status": "ok"})
            return
        if path == "/vision/full-status":
            payload, error = self.server.controller.get_full_status_payload()
            if error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"success": False, "error": error},
                )
                return
            self._send_json(HTTPStatus.OK, {"success": True, **payload})
            return
        if path == "/vision/state":
            payload, error = self.server.controller.get_state_payload()
            if error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"success": False, "error": error},
                )
                return
            self._send_json(HTTPStatus.OK, {"success": True, **payload})
            return
        if path == "/vision/monitors":
            payload, error = self.server.controller.get_monitors_payload()
            if error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"success": False, "error": error},
                )
                return
            self._send_json(HTTPStatus.OK, {"success": True, **payload})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"success": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/activity/ping":
            payload = self._read_json_payload()
            source = str(payload.get("source") or "unknown").strip() or "unknown"
            result, error = self.server.controller.register_activity(source)
            if error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"success": False, "error": error},
                )
                return
            self._send_json(HTTPStatus.OK, {"success": True, **result})
            return
        if path == "/livekit-screen-share/started":
            payload = self._read_json_payload()
            result, error = self.server.controller.livekit_screen_share_started(
                track_id=str(payload.get("track_id") or "").strip(),
                settings=dict(payload.get("settings") or {}),
            )
            if error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"success": False, "error": error},
                )
                return
            self._send_json(HTTPStatus.OK, {"success": True, **result})
            return
        if path == "/livekit-screen-share/stopped":
            result, error = self.server.controller.livekit_screen_share_stopped()
            if error:
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"success": False, "error": error},
                )
                return
            self._send_json(HTTPStatus.OK, {"success": True, **result})
            return
        if path != "/vision/toggle":
            self._send_json(HTTPStatus.NOT_FOUND, {"success": False, "error": "not_found"})
            return

        payload = self._read_json_payload()
        enabled = bool(payload.get("enabled", False))
        source = str(payload.get("source") or "manual").strip() or "manual"
        monitor_id = payload.get("monitor_id", None)
        result, error = self.server.controller.toggle(enabled, source=source, monitor_id=monitor_id)
        if error:
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"success": False, "error": error},
            )
            return
        self._send_json(HTTPStatus.OK, {"success": True, **result})

    def _read_json_payload(self) -> dict[str, Any]:
        try:
            content_length = int(self.headers.get("Content-Length") or "0")
        except Exception:
            content_length = 0
        if content_length <= 0:
            return {}

        raw_body = self.rfile.read(content_length)
        if not raw_body:
            return {}
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class VisionControlServer:
    def __init__(self, *, host: str = "127.0.0.1", port: int = 8092) -> None:
        self.host = str(host or "127.0.0.1").strip() or "127.0.0.1"
        self.requested_port = max(int(port), 0)
        self.port = self.requested_port
        self._lock = threading.RLock()
        self._server: _VisionControlHttpServer | None = None
        self._thread: threading.Thread | None = None
        self._speech_coordinator: Any | None = None

    def start(self, speech_coordinator: Any) -> int:
        with self._lock:
            self._speech_coordinator = speech_coordinator
            if self._server is not None:
                return self.port

            self._server = _VisionControlHttpServer(
                (self.host, self.requested_port),
                _VisionControlRequestHandler,
                controller=self,
            )
            self.port = int(self._server.server_address[1])
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                name="KiraVisionControlServer",
                daemon=True,
            )
            self._thread.start()
            logger.info(
                "[VISION_API] Servidor local iniciado - host=%s port=%d",
                self.host,
                self.port,
            )
            return self.port

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
            self._speech_coordinator = None

        if server is None:
            return

        try:
            server.shutdown()
        except Exception:
            logger.debug("[VISION_API] Ignorando erro ao encerrar servidor", exc_info=True)
        try:
            server.server_close()
        except Exception:
            logger.debug("[VISION_API] Ignorando erro ao fechar servidor", exc_info=True)
        if thread is not None:
            thread.join(timeout=2.0)
        logger.info("[VISION_API] Servidor local encerrado")

    def get_state_payload(self) -> tuple[dict[str, Any] | None, str | None]:
        coordinator = self._get_speech_coordinator()
        if coordinator is None:
            return None, "speech_coordinator_unavailable"
        try:
            getter = getattr(coordinator, "get_vision_mode_status", None)
            if not callable(getter):
                return None, "vision_status_unavailable"
            payload = dict(getter() or {})
        except Exception as exc:
            logger.warning("[VISION_API] Falha ao consultar estado da visao: %s", exc)
            return None, str(exc)
        return payload, None

    def get_full_status_payload(self) -> tuple[dict[str, Any] | None, str | None]:
        return self.get_state_payload()

    def get_monitors_payload(self) -> tuple[dict[str, Any] | None, str | None]:
        coordinator = self._get_speech_coordinator()
        if coordinator is None:
            return None, "speech_coordinator_unavailable"
        try:
            list_method = getattr(coordinator, "list_vision_monitors", None)
            if not callable(list_method):
                return None, "vision_monitors_unavailable"
            monitors = list(list_method() or [])
        except Exception as exc:
            logger.warning("[VISION_API] Falha ao listar monitores: %s", exc)
            return None, str(exc)
        return {"monitors": monitors, "count": len(monitors)}, None

    def toggle(
        self,
        enabled: bool,
        *,
        source: str = "manual",
        monitor_id: int | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        coordinator = self._get_speech_coordinator()
        if coordinator is None:
            return None, "speech_coordinator_unavailable"
        try:
            toggle_method = getattr(coordinator, "toggle_vision_mode", None)
            if not callable(toggle_method):
                return None, "vision_toggle_unavailable"
            try:
                toggle_method(bool(enabled), source=source, monitor_id=monitor_id)
            except TypeError:
                toggle_method(bool(enabled))
        except Exception as exc:
            logger.warning("[VISION_API] Falha ao alternar modo visao: %s", exc)
            return None, str(exc)
        return self.get_state_payload()

    def register_activity(self, source: str) -> tuple[dict[str, Any] | None, str | None]:
        coordinator = self._get_speech_coordinator()
        if coordinator is None:
            return None, "speech_coordinator_unavailable"
        try:
            register_method = getattr(coordinator, "register_user_activity", None)
            if not callable(register_method):
                return None, "activity_registration_unavailable"
            register_method(source=source)
        except Exception as exc:
            logger.warning("[VISION_API] Falha ao registrar atividade: %s", exc)
            return None, str(exc)
        return self.get_state_payload()

    def livekit_screen_share_started(
        self,
        *,
        track_id: str,
        settings: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        coordinator = self._get_speech_coordinator()
        if coordinator is None:
            return None, "speech_coordinator_unavailable"
        try:
            notify = getattr(coordinator, "notify_livekit_screen_share_started", None)
            if callable(notify):
                notify(track_id=track_id, settings=settings or {})
        except Exception as exc:
            logger.warning("[VISION_API] Falha ao notificar start do screen share: %s", exc)
            return None, str(exc)
        return self.get_state_payload()

    def livekit_screen_share_stopped(self) -> tuple[dict[str, Any] | None, str | None]:
        coordinator = self._get_speech_coordinator()
        if coordinator is None:
            return None, "speech_coordinator_unavailable"
        try:
            notify = getattr(coordinator, "notify_livekit_screen_share_stopped", None)
            if callable(notify):
                notify()
        except Exception as exc:
            logger.warning("[VISION_API] Falha ao notificar stop do screen share: %s", exc)
            return None, str(exc)
        return self.get_state_payload()

    def _get_speech_coordinator(self) -> Any | None:
        with self._lock:
            return self._speech_coordinator


_VISION_CONTROL_SERVER: VisionControlServer | None = None


def get_vision_control_server() -> VisionControlServer:
    global _VISION_CONTROL_SERVER
    if _VISION_CONTROL_SERVER is None:
        port = int(os.getenv("KIRA_VISION_CONTROL_PORT", "8092") or "8092")
        _VISION_CONTROL_SERVER = VisionControlServer(port=port)
    return _VISION_CONTROL_SERVER


__all__ = ["VisionControlServer", "get_vision_control_server"]
