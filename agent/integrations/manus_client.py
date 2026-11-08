from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

try:
    from services.manus_config import ManusConfig, get_available_key_slots, load_manus_config
except Exception:  # pragma: no cover
    import importlib.util as _importlib_util
    import sys as _sys
    from pathlib import Path as _Path

    _cfg_path = _Path(__file__).resolve().parents[2] / "services" / "manus_config.py"
    _spec = _importlib_util.spec_from_file_location("services.manus_config", str(_cfg_path))
    assert _spec and _spec.loader
    _mod = _importlib_util.module_from_spec(_spec)
    _sys.modules["services.manus_config"] = _mod
    _spec.loader.exec_module(_mod)
    ManusConfig = _mod.ManusConfig
    get_available_key_slots = _mod.get_available_key_slots
    load_manus_config = _mod.load_manus_config

logger = logging.getLogger(__name__)
logger.info("MANUS_CLIENT_CANONICAL_PATH path=%s", __file__)


@dataclass
class ManusTaskCreateResult:
    ok: bool
    status: str
    task_id: str | None = None
    task_url: str | None = None
    error: str | None = None
    error_type: str | None = None
    status_code: int | None = None


@dataclass
class ManusTaskStatusResult:
    ok: bool
    status: str
    task_id: str
    payload: dict[str, Any] | None = None
    error: str | None = None
    error_type: str | None = None
    status_code: int | None = None


class ManusClientError(Exception):
    pass


class ManusAuthError(ManusClientError):
    pass


class ManusQuotaError(ManusClientError):
    pass


class ManusClient:
    def __init__(self, config: ManusConfig | None = None, api_key: str | None = None, key_slot: int | None = None, http_client: Any | None = None) -> None:
        self.config = config or load_manus_config()
        self.http_client = http_client
        self.selected_slot = key_slot
        self.api_key = api_key
        if not self.api_key:
            slots = get_available_key_slots(self.config)
            if key_slot is not None:
                for s in slots:
                    if s.slot == key_slot:
                        self.api_key = s.key_value
                        self.selected_slot = s.slot
                        break
            if not self.api_key and slots:
                self.api_key = slots[0].key_value
                self.selected_slot = slots[0].slot
        logger.info("MANUS_CLIENT_CANONICAL_PATH path=%s", __file__)

    def is_configured(self) -> bool:
        return bool(self.config.enabled and self.api_key and self.config.base_url)

    def _disabled(self) -> ManusTaskCreateResult:
        return ManusTaskCreateResult(ok=False, status="disabled", error="manus_not_configured", error_type="disabled")

    def _api_mode(self) -> str:
        mode = str(getattr(self.config, "api_mode", "") or "").strip().lower()
        if mode:
            return mode
        return "v2_task_create"

    def get_task_create_endpoint(self) -> str:
        endpoint_builder = getattr(self.config, "get_task_create_endpoint", None)
        if callable(endpoint_builder):
            return str(endpoint_builder())
        base = str(self.config.base_url or "https://api.manus.ai").strip().rstrip("/")
        base = base.removesuffix("/v2/task.create").removesuffix("/v1/tasks").rstrip("/")
        return f"{base}/v2/task.create"
    def get_task_list_messages_endpoint(self, task_id: str, order: str = "asc", limit: int = 100, cursor: str | None = None) -> str:
        base = str(self.config.base_url or "https://api.manus.ai").strip().rstrip("/")
        base = base.removesuffix("/v2/task.listMessages").rstrip("/")
        url = f"{base}/v2/task.listMessages?task_id={task_id}&order={order}&limit={int(limit)}"
        if cursor:
            url = f"{url}&cursor={cursor}"
        return url

    def _headers(self) -> dict[str, str]:
        return {
            "x-manus-api-key": str(self.api_key or ""),
            "Content-Type": "application/json; charset=utf-8",
        }

    def _key_fingerprint(self) -> str:
        if not self.api_key:
            return ""
        return hashlib.sha256(str(self.api_key).encode("utf-8")).hexdigest()[:6]

    def _build_create_task_body_bytes(self, payload: dict) -> bytes:
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def _log_safe_create_request(self, url: str, payload: dict, body_bytes: bytes) -> None:
        message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), list) else []
        first = content[0] if content and isinstance(content[0], dict) else {}
        prompt = str(first.get("text") or "")
        logger.info("MANUS_REQUEST_DEBUG_SAFE_BEGIN")
        logger.info("MANUS_REQUEST_ENDPOINT url=%s", url)
        logger.info("MANUS_REQUEST_METHOD method=POST")
        logger.info("MANUS_REQUEST_HEADER_NAME has_x_manus_api_key=%s", str(bool(self.api_key)).lower())
        logger.info('MANUS_REQUEST_CONTENT_TYPE value="application/json; charset=utf-8"')
        logger.info("MANUS_REQUEST_KEY_FINGERPRINT slot=%s fp=%s", str(self.selected_slot or "none"), self._key_fingerprint())
        logger.info("MANUS_REQUEST_AGENT_PROFILE value=%s", str(payload.get("agent_profile") or ""))
        logger.info("MANUS_REQUEST_TITLE value=%s", str(payload.get("title") or ""))
        logger.info("MANUS_REQUEST_PROMPT_CHARS count=%d", len(prompt))
        logger.info("MANUS_REQUEST_BODY_KEYS keys=%s", list(payload.keys()))
        logger.info("MANUS_REQUEST_MESSAGE_CONTENT_TYPE value=%s", str(first.get("type") or ""))
        logger.info("MANUS_REQUEST_BODY_UTF8_BYTES yes=%s", str(isinstance(body_bytes, bytes)).lower())
        logger.info("MANUS_REQUEST_DEBUG_SAFE_DONE")

    def _request_json(self, method: str, url: str, *, payload: dict | None = None, body_bytes: bytes | None = None, timeout: int = 30) -> tuple[int, dict]:
        if self.http_client is not None:
            if method == "POST":
                response = self.http_client.post(url, json=payload or {}, headers=self._headers(), timeout=timeout)
            else:
                response = self.http_client.get(url, headers=self._headers(), timeout=timeout)
            code = int(getattr(response, "status_code", 0) or 0)
            data = response.json() if callable(getattr(response, "json", None)) else {}
            return code, data if isinstance(data, dict) else {}
        try:
            import requests

            logger.info("MANUS_CLIENT_HTTP_AVAILABLE method=requests")
            if method == "POST":
                response = requests.post(url, data=body_bytes or self._build_create_task_body_bytes(payload or {}), headers=self._headers(), timeout=timeout)
            else:
                response = requests.get(url, headers=self._headers(), timeout=timeout)
            try:
                data = response.json() if response.content else {}
            except Exception:
                data = {}
            return int(response.status_code), data if isinstance(data, dict) else {}
        except ImportError:
            pass
        logger.info("MANUS_CLIENT_HTTP_AVAILABLE method=urllib")
        body = body_bytes if body_bytes is not None else (json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if method == "POST" else None)
        request = urllib.request.Request(url, data=body, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return int(getattr(response, "status", 200) or 200), json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                data = {}
            return int(exc.code), data if isinstance(data, dict) else {}

    def _request_bytes(self, url: str, *, timeout: int = 60) -> tuple[int, bytes]:
        if self.http_client is not None:
            response = self.http_client.get(url, headers=self._headers(), timeout=timeout)
            code = int(getattr(response, "status_code", 0) or 0)
            content = getattr(response, "content", None)
            if content is None and callable(getattr(response, "read", None)):
                content = response.read()
            return code, bytes(content or b"")
        try:
            import requests

            logger.info("MANUS_CLIENT_HTTP_AVAILABLE method=requests")
            response = requests.get(url, headers=self._headers(), timeout=timeout)
            return int(response.status_code), bytes(response.content or b"")
        except ImportError:
            pass
        logger.info("MANUS_CLIENT_HTTP_AVAILABLE method=urllib")
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(getattr(response, "status", 200) or 200), response.read()
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read()

    def _build_create_task_payload(self, prompt: str, title: str = "") -> dict:
        return {
            "message": {"content": [{"type": "text", "text": prompt}]},
            "agent_profile": str(getattr(self.config, "agent_profile", None) or self.config.default_agent_profile or "manus-1.6-lite"),
            "title": title or "Tarefa Kira",
        }

    def _validate_create_payload(self, payload: dict) -> tuple[bool, list[str]]:
        """Valida os campos obrigatorios do task.create v2 sem expor a chave."""
        missing: list[str] = []
        message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), list) else []
        first = content[0] if content and isinstance(content[0], dict) else {}
        prompt_text = str(first.get("text") or "").strip()
        if not message:
            missing.append("message")
        if not content:
            missing.append("message.content")
        if not prompt_text:
            missing.append("message.content[0].text")
        if not str(payload.get("agent_profile") or "").strip():
            missing.append("agent_profile")
        if not str(payload.get("title") or "").strip():
            missing.append("title")
        return (not missing), missing

    def parse_response(self, response: dict) -> dict:
        if str((response or {}).get("task_id") or "").strip() and (response.get("ok") is not False):
            return {
                "status": "submitted",
                "task_id": str(response.get("task_id")).strip(),
                "task_url": str(response.get("task_url") or "").strip() or None,
            }
        logger.warning("MANUS_CLIENT_CREATE_TASK_FAILED status=ok_false key_slot=%s", str(self.selected_slot or "none"))
        raise ManusClientError("manus_create_task_failed")

    def handle_http_error(self, status_code: int, body: dict | None = None) -> None:
        body_text = json.dumps(body or {}, ensure_ascii=False).lower()
        if status_code == 400:
            # Bad Request: schema/payload rejeitado pela API Manus. Loga as chaves
            # do corpo de resposta (sem expor a API key) para diagnostico claro.
            logger.warning(
                "MANUS_CLIENT_CREATE_TASK_FAILED status=400 reason=bad_request key_slot=%s body_keys=%s",
                str(self.selected_slot or "none"),
                list((body or {}).keys()),
            )
            raise ManusClientError("manus_bad_request_400")
        if status_code in {401, 403}:
            logger.warning("MANUS_CLIENT_CREATE_TASK_FAILED status=%s key_slot=%s", status_code, str(self.selected_slot or "none"))
            logger.warning("MANUS_AUTH_FAILED key_slot=%s status=%s", str(self.selected_slot or "none"), status_code)
            raise ManusAuthError("manus_auth_failed")
        if status_code == 429 or "quota" in body_text:
            logger.warning("MANUS_CLIENT_CREATE_TASK_FAILED status=%s key_slot=%s", status_code, str(self.selected_slot or "none"))
            logger.warning("MANUS_QUOTA_EXHAUSTED key_slot=%s", str(self.selected_slot or "none"))
            raise ManusQuotaError("manus_quota_exhausted")
        if status_code in {404, 405}:
            logger.warning("MANUS_ENDPOINT_FAILED status=%s", status_code)
        logger.warning("MANUS_CLIENT_CREATE_TASK_FAILED status=%s key_slot=%s", status_code, str(self.selected_slot or "none"))
        raise ManusClientError("manus_create_task_failed")

    def create_task(self, prompt: str, title: str = "", key_slot: int = 1, attachments: list[Path] | None = None, metadata: dict | None = None, api_key: str | None = None) -> ManusTaskCreateResult:
        if api_key:
            self.api_key = api_key
        if key_slot is not None:
            self.selected_slot = key_slot
        if not title and metadata and metadata.get("title"):
            title = str(metadata.get("title") or "")
        logger.info("MANUS_CLIENT_MODE_SELECTED mode=%s", self._api_mode())
        url = self.get_task_create_endpoint()
        logger.info("MANUS_CLIENT_ENDPOINT_BUILT url=%s", url)
        logger.info("MANUS_CLIENT_CREATE_TASK_BEGIN key_slot=%s", str(self.selected_slot or "none"))
        if not self.is_configured():
            logger.info("MANUS_CLIENT_CREATE_TASK_DISABLED reason=not_configured")
            return self._disabled()
        try:
            payload = self._build_create_task_payload(prompt, title)
            # Validacao de payload/schema antes de gastar a API (evita 400 por
            # campo obrigatorio ausente e job fantasma). Nunca loga a chave.
            ok_payload, missing = self._validate_create_payload(payload)
            if not ok_payload:
                logger.warning("MANUS_PAYLOAD_INVALID missing=%s", missing)
                return ManusTaskCreateResult(
                    ok=False,
                    status="failed",
                    error="payload_invalid",
                    error_type="payload_invalid",
                    status_code=400,
                )
            logger.info(
                "MANUS_PAYLOAD_VALIDATED title=%s prompt_chars=%d agent_profile=%s has_message_content=%s",
                str(payload.get("title") or ""),
                len(prompt or ""),
                str(payload.get("agent_profile") or ""),
                str(bool(((payload.get("message") or {}).get("content")))).lower(),
            )
            body_bytes = self._build_create_task_body_bytes(payload)
            self._log_safe_create_request(url, payload, body_bytes)
            logger.info("MANUS_CLIENT_CREATE_TASK_HTTP_REQUEST url=%s key_slot=%s", url, str(self.selected_slot or "none"))
            code, data = self._request_json("POST", url, payload=payload, body_bytes=body_bytes, timeout=30)
            if 200 <= code < 300:
                parsed = self.parse_response(data)
                logger.info("MANUS_CLIENT_CREATE_TASK_OK task_id=%s", parsed["task_id"])
                logger.info("MANUS_CLIENT_CREATE_TASK_URL task_url_present=%s", str(bool(parsed.get("task_url"))).lower())
                return ManusTaskCreateResult(ok=True, status="submitted", task_id=parsed["task_id"], task_url=parsed.get("task_url"))
            self.handle_http_error(code, data)
            return ManusTaskCreateResult(ok=False, status="failed", error="http_error", error_type="http_error", status_code=code)
        except ManusAuthError:
            return ManusTaskCreateResult(
                ok=False,
                status="auth_failed",
                error="manus_auth_failed",
                error_type="auth_error",
                status_code=401,
            )
        except ManusQuotaError:
            return ManusTaskCreateResult(
                ok=False,
                status="quota_exhausted",
                error="manus_quota_exhausted",
                error_type="quota_exhausted",
                status_code=429,
            )
        except ManusClientError:
            raise
        except TimeoutError:
            raise ManusClientError("timeout")
        except Exception as exc:
            logger.warning("MANUS_CLIENT_CREATE_TASK_FAILED status=network_error error=%s", type(exc).__name__)
            raise ManusClientError("network_error")

    def get_task(self, task_id: str) -> ManusTaskStatusResult:
        logger.info("MANUS_CLIENT_POLL_BEGIN task_id=%s", task_id)
        if not self.is_configured():
            return ManusTaskStatusResult(ok=False, status="disabled", task_id=task_id, error="manus_not_configured", error_type="disabled")
        try:
            url = f"{str(self.config.base_url or '').rstrip('/')}/v2/task.detail?task_id={task_id}"
            code, data = self._request_json("GET", url, timeout=30)
            if 200 <= code < 300:
                st = str(data.get("status") or "unknown")
                logger.info("MANUS_CLIENT_POLL_STATUS task_id=%s status=%s", task_id, st)
                if st.lower() in {"done", "completed", "success", "succeeded"}:
                    logger.info("MANUS_CLIENT_TASK_DONE task_id=%s", task_id)
                return ManusTaskStatusResult(ok=True, status=st, task_id=task_id, payload=data)
            return ManusTaskStatusResult(ok=False, status="failed", task_id=task_id, error="http_error", error_type="http_error", status_code=code)
        except Exception:
            return ManusTaskStatusResult(ok=False, status="failed", task_id=task_id, error="network_error", error_type="network_error")

    def list_task_files(self, task_id: str) -> list:
        if not self.is_configured():
            return []
        try:
            code, data = self._request_json("GET", f"{str(self.config.base_url or '').rstrip('/')}/v2/task.files?task_id={task_id}", timeout=30)
            if code // 100 != 2:
                return []
            files = data.get("files") if isinstance(data, dict) else []
            return files if isinstance(files, list) else []
        except Exception:
            return []

    def list_task_messages(self, task_id: str, cursor: str | None = None, order: str = "asc", limit: int = 100) -> dict:
        logger.info("MANUS_CLIENT_LIST_MESSAGES_BEGIN task_id=%s", task_id)
        if not self.is_configured():
            return {"ok": False, "messages": [], "error": "manus_not_configured"}
        url = self.get_task_list_messages_endpoint(task_id, order=order, limit=limit, cursor=cursor)
        logger.info("MANUS_CLIENT_LIST_MESSAGES_HTTP_REQUEST task_id=%s url_sanitized=%s", task_id, url)
        code, data = self._request_json("GET", url, timeout=30)
        if 200 <= code < 300 and isinstance(data, dict):
            messages = data.get("messages") or data.get("data") or []
            if not isinstance(messages, list):
                messages = []
            logger.info("MANUS_CLIENT_LIST_MESSAGES_OK task_id=%s events=%d", task_id, len(messages))
            return {"ok": True, "messages": messages, "raw": data}
        return {"ok": False, "messages": [], "status_code": code, "raw": data}

    def poll_task_until_done(self, task_id: str, timeout_minutes: int, poll_interval_seconds: int) -> dict:
        import time
        deadline = time.time() + max(1, int(timeout_minutes)) * 60
        last_status = "running"
        while time.time() < deadline:
            res = self.list_task_messages(task_id=task_id, order="asc", limit=100)
            if not bool(res.get("ok")):
                time.sleep(max(1, int(poll_interval_seconds)))
                continue
            messages = list(res.get("messages") or [])
            assistant_texts: list[str] = []
            files: list[dict[str, str]] = []
            error_text = ""
            for m in messages:
                if not isinstance(m, dict):
                    continue
                mtype = str(m.get("type") or m.get("event_type") or "").strip().lower()
                if mtype == "status_update" and isinstance(m.get("status_update"), dict):
                    payload = m["status_update"]
                elif mtype == "assistant_message" and isinstance(m.get("assistant_message"), dict):
                    payload = m["assistant_message"]
                elif mtype == "error_message" and isinstance(m.get("error_message"), dict):
                    payload = m["error_message"]
                else:
                    payload = m.get("payload") if isinstance(m.get("payload"), dict) else m
                if mtype == "status_update" or "agent_status" in payload:
                    agent_status = str(payload.get("agent_status") or payload.get("status") or "").strip().lower()
                    if agent_status:
                        logger.info("MANUS_CLIENT_TASK_STATUS_UPDATE task_id=%s agent_status=%s", task_id, agent_status)
                        last_status = agent_status
                if mtype == "assistant_message":
                    content = payload.get("content")
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and str(c.get("type") or "") == "text":
                                t = str(c.get("text") or "").strip()
                                if t:
                                    assistant_texts.append(t)
                    elif isinstance(content, str):
                        txt_content = content.strip()
                        if txt_content:
                            assistant_texts.append(txt_content)
                    txt = str(payload.get("text") or "").strip()
                    if txt:
                        assistant_texts.append(txt)
                    if assistant_texts:
                        logger.info("MANUS_CLIENT_ASSISTANT_MESSAGE_FOUND chars=%d", len("\n\n".join(assistant_texts)))
                if mtype == "error_message":
                    error_text = str(payload.get("text") or payload.get("message") or "task_error").strip()
                for k in ("file_url", "fileUrl", "download_url"):
                    if payload.get(k):
                        files.append({"url": str(payload.get(k)), "name": str(payload.get("filename") or payload.get("fileName") or "output.bin")})
            if last_status == "waiting":
                logger.info("MANUS_CLIENT_TASK_WAITING_INPUT task_id=%s", task_id)
                return {"ok": True, "status": "waiting_user_input", "assistant_text": "\n\n".join(assistant_texts).strip(), "files": files}
            if last_status == "error":
                logger.warning("MANUS_CLIENT_TASK_ERROR task_id=%s", task_id)
                return {"ok": False, "status": "error", "error": error_text or "task_error", "files": files}
            if last_status == "stopped":
                logger.info("MANUS_CLIENT_TASK_DONE task_id=%s", task_id)
                return {"ok": True, "status": "completed", "assistant_text": "\n\n".join(assistant_texts).strip(), "files": files}
            time.sleep(max(1, int(poll_interval_seconds)))
        logger.warning("MANUS_CLIENT_TASK_POLL_TIMEOUT task_id=%s", task_id)
        return {"ok": False, "status": "timeout", "error": "poll_timeout"}

    def download_file(self, file_id_or_url, destination) -> dict:
        if not self.is_configured():
            return {"ok": False, "error": "manus_not_configured"}
        try:
            target = str(file_id_or_url)
            if target.startswith("http://") or target.startswith("https://"):
                url = target
            else:
                url = f"{str(self.config.base_url or '').rstrip('/')}/v2/file.download?file_id={target}"
            logger.info("MANUS_CLIENT_DOWNLOAD_BEGIN")
            code, content = self._request_bytes(url, timeout=60)
            if code // 100 != 2:
                return {"ok": False, "error": "http_error", "status_code": code}
            if not content:
                return {"ok": False, "error": "empty_content"}
            with open(destination, "wb") as f:
                f.write(content)
            logger.info("MANUS_CLIENT_DOWNLOAD_DONE path=%s", destination)
            return {"ok": True, "path": str(destination)}
        except Exception:
            return {"ok": False, "error": "network_error"}

