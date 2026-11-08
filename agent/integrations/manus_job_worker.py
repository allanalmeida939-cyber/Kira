from __future__ import annotations

import asyncio
import hashlib
import importlib
import logging
import threading
import time
from typing import Optional
from pathlib import Path
from urllib.parse import urlsplit

try:
    from services.artifact_registry import (
        get_job,
        register_completed_artifact,
        save_job_task_spec,
        update_job_status,
        update_last_generated,
    )
except Exception:  # pragma: no cover
    import importlib.util as _importlib_util
    import sys as _sys
    from pathlib import Path as _Path

    _ar_path = _Path(__file__).resolve().parents[2] / "services" / "artifact_registry.py"
    _spec = _importlib_util.spec_from_file_location("services.artifact_registry", str(_ar_path))
    assert _spec and _spec.loader
    _mod = _importlib_util.module_from_spec(_spec)
    _sys.modules["services.artifact_registry"] = _mod
    _spec.loader.exec_module(_mod)
    get_job = _mod.get_job
    register_completed_artifact = _mod.register_completed_artifact
    update_job_status = _mod.update_job_status
    save_job_task_spec = _mod.save_job_task_spec
    update_last_generated = _mod.update_last_generated

try:
    from agent.integrations.manus_client import ManusAuthError, ManusClient, ManusClientError, ManusQuotaError
except Exception:  # pragma: no cover
    try:
        from integrations.manus_client import ManusAuthError, ManusClient, ManusClientError, ManusQuotaError  # type: ignore
    except Exception:  # pragma: no cover
        ManusClient = None  # type: ignore
        ManusAuthError = ManusClientError = ManusQuotaError = Exception  # type: ignore

try:
    from agent.integrations.manus_prompt_builders import build_manus_task_spec
except Exception:  # pragma: no cover
    try:
        from integrations.manus_prompt_builders import build_manus_task_spec  # type: ignore
    except Exception:  # pragma: no cover
        build_manus_task_spec = None  # type: ignore
try:
    from agent.core.personality_response import build_personality_response
except Exception:  # pragma: no cover
    try:
        from core.personality_response import build_personality_response  # type: ignore
    except Exception:  # pragma: no cover
        build_personality_response = None  # type: ignore
try:
    from services.manus_config import get_available_key_slots, load_manus_config
    from services.manus_key_pool import ManusKeyPool
except Exception:  # pragma: no cover
    import importlib.util as _kp_util
    import sys as _kp_sys
    from pathlib import Path as _kp_path
    _kpp = _kp_path(__file__).resolve().parents[2] / "services" / "manus_key_pool.py"
    _kps = _kp_util.spec_from_file_location("services.manus_key_pool", str(_kpp))
    assert _kps and _kps.loader
    _kpm = _kp_util.module_from_spec(_kps)
    _kp_sys.modules["services.manus_key_pool"] = _kpm
    _kps.loader.exec_module(_kpm)
    ManusKeyPool = _kpm.ManusKeyPool
    from services.manus_config import get_available_key_slots, load_manus_config
try:
    from services.artifact_event_bus import artifact_event_bus
except Exception:  # pragma: no cover
    artifact_event_bus = None
try:
    from services.artifact_state_reconciler import reconcile_artifact_state
except Exception:  # pragma: no cover
    reconcile_artifact_state = None

logger = logging.getLogger(__name__)

try:
    from agent.runtime.voice_output_manager import get_voice_output_manager
except Exception:  # pragma: no cover
    try:
        from runtime.voice_output_manager import get_voice_output_manager
    except Exception:  # pragma: no cover
        get_voice_output_manager = None  # type: ignore
_QUEUED_JOB_IDS: set[str] = set()
_ACTIVE_JOB_IDS: set[str] = set()
_JOB_GUARD_LOCK = threading.Lock()


def verify_artifact_file(path: str | Path, *, expected_kind: str = "") -> tuple[bool, int]:
    candidate = Path(path)
    logger.info("ARTIFACT_FILE_VERIFY_BEGIN path=%s", candidate)
    try:
        if not candidate.exists() or not candidate.is_file():
            logger.warning("ARTIFACT_FILE_VERIFY_FAILED path=%s reason=missing", candidate)
            return False, 0
        size = int(candidate.stat().st_size)
        if size <= 0:
            logger.warning("ARTIFACT_FILE_VERIFY_FAILED path=%s reason=empty", candidate)
            return False, size
        if str(expected_kind or "").lower() == "pdf" and candidate.suffix.lower() != ".pdf":
            logger.warning("ARTIFACT_FILE_VERIFY_FAILED path=%s reason=not_pdf", candidate)
            return False, size
        logger.info("ARTIFACT_FILE_VERIFY_OK path=%s size=%s", candidate, size)
        return True, size
    except Exception as exc:
        logger.warning("ARTIFACT_FILE_VERIFY_FAILED path=%s reason=%s", candidate, exc)
        return False, 0


def extract_list_messages_file_candidates(messages: list[object], required_kind: str | None = None) -> list[dict[str, str]]:
    """Extract downloadable file references from Manus messages without logging signed URLs."""
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    require_pdf = str(required_kind or "").lower() == "pdf"

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        name = str(value.get("filename") or value.get("fileName") or value.get("name") or "output.bin")
        mime = str(value.get("mime_type") or value.get("mimeType") or value.get("content_type") or "").lower()
        ref = str(
            value.get("download_url") or value.get("downloadUrl") or value.get("file_url")
            or value.get("fileUrl") or value.get("url") or value.get("file_id") or value.get("id") or ""
        ).strip()
        looks_pdf = name.lower().endswith(".pdf") or "application/pdf" in mime or ".pdf" in ref.lower().split("?", 1)[0]
        if ref and (not require_pdf or looks_pdf):
            key = f"{ref}|{name}"
            if key not in seen:
                seen.add(key)
                if looks_pdf and not name.lower().endswith(".pdf"):
                    name = "output.pdf"
                candidates.append({"url": ref, "name": name, "mime_type": mime})
                logger.info("MANUS_LIST_MESSAGES_FILE_CANDIDATE_FOUND type=%s name=%s", mime or "unknown", Path(name).name)
                if looks_pdf:
                    logger.info("MANUS_PDF_ATTACHMENT_FOUND url_present=true filename=%s", Path(name).name)
        for key, child in value.items():
            if key.lower() in {"attachments", "files", "file", "resource", "artifact", "content", "payload", "assistant_message", "data"}:
                visit(child)

    visit(messages)
    return candidates


def safe_download_reference_label(reference: str) -> str:
    parsed = urlsplit(str(reference or ""))
    return parsed.netloc or "file_id"


def extract_requested_text_content(user_prompt: str, job_kind: str = "text") -> str | None:
    import re

    if str(job_kind or "").strip().lower() not in {"text", "txt"}:
        logger.info("MANUS_TEXT_REQUESTED_CONTENT_EXTRACTED no chars=0")
        return None
    text = str(user_prompt or "").strip()
    patterns = (
        r"\bcom\s+a\s+frase\s*[:\-]\s*(.+)$",
        r"\bcom\s+o\s+texto\s*[:\-]\s*(.+)$",
        r"\bescreva\s*[:\-]\s*(.+)$",
        r"\bconte[uú]do\s*[:\-]\s*(.+)$",
        r"\bcontendo\s*[:\-]\s*(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = match.group(1).strip().strip('"\' ')
            value = re.sub(
                r"\s*(?:[.!?]\s*)?(?:salv[ae]|grave|coloque)\s+(?:na|no|em)\s+(?:a\s+)?pasta\s+padr[aã]o[.!?]?$",
                "",
                value,
                flags=re.IGNORECASE,
            ).strip()
            value = value.rstrip(".").strip().strip('"\' ')
            logger.info("MANUS_TEXT_REQUESTED_CONTENT_EXTRACTED yes chars=%d", len(value))
            return value or None
    logger.info("MANUS_TEXT_REQUESTED_CONTENT_EXTRACTED no chars=0")
    return None


def _extract_requested_text(raw_request: str) -> str:
    return extract_requested_text_content(raw_request) or ""


def clean_manus_text_result(raw_text: str, requested_content: str | None = None, job_kind: str = "text") -> str:
    kind = str(job_kind or "").strip().lower()
    raw = str(raw_text or "").strip()
    logger.info("MANUS_TEXT_RESULT_RAW_CHARS chars=%d", len(raw))
    if kind not in {"text", "txt"}:
        logger.info("MANUS_TEXT_RESULT_CLEANED original_chars=%d cleaned_chars=%d", len(raw), len(raw))
        logger.info("MANUS_TEXT_RESULT_EXACT_REQUESTED_CONTENT_USED no")
        return raw
    requested = str(requested_content or "").strip()
    if requested:
        logger.info("MANUS_TEXT_RESULT_CLEANED original_chars=%d cleaned_chars=%d", len(raw), len(requested))
        logger.info("MANUS_TEXT_RESULT_EXACT_REQUESTED_CONTENT_USED yes")
        return requested
    report_markers = (
        "tarefa concluida",
        "tarefa concluída",
        "entendido.",
        "vou criar",
        "conforme solicitado",
        "resumo da execucao",
        "resumo da execução",
        "arquivo criado",
        "conteudo:",
        "conteúdo:",
        "localizacao:",
        "localização:",
        "pasta padrao",
        "pasta padrão",
        "/home/ubuntu",
        "o arquivo foi gerado",
        "o arquivo esta anexado",
        "o arquivo está anexado",
        "esta anexado a esta mensagem",
        "está anexado a esta mensagem",
        "resumo da acao",
        "resumo da ação",
    )
    kept: list[str] = []
    for line in raw.splitlines():
        normalized = line.strip().lower()
        if not normalized:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        if any(marker in normalized for marker in report_markers):
            continue
        kept.append(line.rstrip())
    cleaned = "\n".join(kept).strip() or raw
    logger.info("MANUS_TEXT_RESULT_CLEANED original_chars=%d cleaned_chars=%d", len(raw), len(cleaned))
    logger.info("MANUS_TEXT_RESULT_EXACT_REQUESTED_CONTENT_USED no")
    return cleaned

try:
    from services.artifact_saver import save_downloaded_file, stage_downloaded_file
    from services.artifact_destination_manager import detect_destination_intent, folder_for_destination_intent
    from services.artifact_validator import validate_artifact
except Exception:  # pragma: no cover
    import importlib.util as _importlib_util2
    import sys as _sys2
    from pathlib import Path as _Path2

    _kp2_path = _Path2(__file__).resolve().parents[2] / "services" / "kira_paths.py"
    _kp2_spec = _importlib_util2.spec_from_file_location("services.kira_paths", str(_kp2_path))
    assert _kp2_spec and _kp2_spec.loader
    _kp2_mod = _importlib_util2.module_from_spec(_kp2_spec)
    _sys2.modules["services.kira_paths"] = _kp2_mod
    _kp2_spec.loader.exec_module(_kp2_mod)

    _sv_path = _Path2(__file__).resolve().parents[2] / "services" / "artifact_saver.py"
    _sv_spec = _importlib_util2.spec_from_file_location("services.artifact_saver", str(_sv_path))
    assert _sv_spec and _sv_spec.loader
    _sv_mod = _importlib_util2.module_from_spec(_sv_spec)
    _sys2.modules["services.artifact_saver"] = _sv_mod
    _sv_spec.loader.exec_module(_sv_mod)
    save_downloaded_file = _sv_mod.save_downloaded_file

    _vl_path = _Path2(__file__).resolve().parents[2] / "services" / "artifact_validator.py"
    _vl_spec = _importlib_util2.spec_from_file_location("services.artifact_validator", str(_vl_path))
    assert _vl_spec and _vl_spec.loader
    _vl_mod = _importlib_util2.module_from_spec(_vl_spec)
    _sys2.modules["services.artifact_validator"] = _vl_mod
    _vl_spec.loader.exec_module(_vl_mod)
    validate_artifact = _vl_mod.validate_artifact


class ManusJobWorker:
    def __init__(self, manus_client: Optional[object] = None, completion_notifier: Optional[object] = None) -> None:
        self._client = manus_client
        self._completion_notifier = completion_notifier
        self._completion_notified_jobs: set[str] = set()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._running = False
        self._task: asyncio.Task | None = None
        self._lock = threading.Lock()
        self._config = load_manus_config()
        self._log_runtime_import_debug()
        env_path = Path(__file__).resolve().parents[2] / ".env"
        logger.info("MANUS_ENV_PATH path=%s", str(env_path))
        self._key_pool = ManusKeyPool(self._config)
        for s in get_available_key_slots(self._config):
            fp = hashlib.sha256(str(s.key_value or "").encode("utf-8")).hexdigest()[:6]
            logger.info("MANUS_ACTIVE_KEY_FINGERPRINT slot=%s fp=%s", s.slot, fp)
        logger.info(
            "MANUS_WORKER_CONFIG_READY enabled=%s api_mode=%s key_slots_present=%s",
            str(bool(self._config.enabled)).lower(),
            str(self._config.api_mode),
            [s.slot for s in get_available_key_slots(self._config)],
        )
        logger.info("MANUS_WORKER_KEY_POOL_READY usable_slots=%s", self._key_pool.usable_slots())
        logger.info("MANUS_WORKER_INIT")

    def set_completion_notifier(self, notifier: object) -> None:
        self._completion_notifier = notifier

    async def _notify_completion_once(self, job: dict, status: str, message: str, path: str = "") -> None:
        job_id = str(job.get("job_id") or "")
        notified_jobs = getattr(self, "_completion_notified_jobs", None)
        if notified_jobs is None:
            notified_jobs = set()
            self._completion_notified_jobs = notified_jobs
        if job_id in notified_jobs:
            logger.info("MANUS_JOB_COMPLETION_NOTIFY_DEDUPED job_id=%s", job_id)
            logger.info("CREATION_TASK_NOTICE_DEDUPED key=creation_task_completed_notify:%s", job_id)
            return
        notified_jobs.add(job_id)
        logger.info("CREATION_TASK_COMPLETED_NOTIFY key=creation_task_completed_notify:%s", job_id)
        logger.info("MANUS_JOB_COMPLETED_NOTIFY_BEGIN job_id=%s", job_id)
        task_type = str(job.get("task_type") or "unknown")
        visible_message = self._style_status_message(status=status, task_type=task_type, message=message)
        payload = {
            "job_id": job_id,
            "status": status,
            "kind": task_type,
            "source": str(job.get("source") or "unknown"),
            "message": visible_message,
            "path": path,
            "speak": str(job.get("creation_request_source") or job.get("source") or "").lower() in {"voice", "speech"},
        }
        if payload["speak"]:
            logger.info("MANUS_JOB_COMPLETION_VOICE_NOTIFY_ALLOWED")
        else:
            logger.info("MANUS_JOB_COMPLETION_TEXT_NOTIFY_ONLY")
        notifier = getattr(self, "_completion_notifier", None)
        if callable(notifier):
            result = notifier(payload)
            if asyncio.iscoroutine(result):
                await result

    def _log_runtime_import_debug(self) -> None:
        logger.info("MANUS_RUNTIME_IMPORT_DEBUG_BEGIN")
        module_names = [
            ("MANUS_CLIENT_MODULE_FILE", "agent.integrations.manus_client"),
            ("MANUS_WORKER_MODULE_FILE", __name__),
            ("MANUS_CONFIG_MODULE_FILE", "services.manus_config"),
            ("MANUS_KEY_POOL_MODULE_FILE", "services.manus_key_pool"),
        ]
        for label, module_name in module_names:
            try:
                module = importlib.import_module(module_name)
                logger.info("%s path=%s", label, str(getattr(module, "__file__", "")))
            except Exception:
                logger.info("%s path=unavailable", label)
        root = Path(__file__).resolve().parents[2]
        duplicates = [str(root / "agent" / "integrations" / "manus_client.py"), str(root / "integrations" / "manus_client.py")]
        logger.info("MANUS_CLIENT_DUPLICATE_FOUND files=%s", duplicates)
        logger.info("MANUS_RUNTIME_IMPORT_DEBUG_DONE")

    def _slot1_fingerprint(self, config) -> str:
        for slot in get_available_key_slots(config):
            if int(slot.slot) == 1:
                return hashlib.sha256(str(slot.key_value or "").encode("utf-8")).hexdigest()[:6]
        return ""

    def _reload_config_if_env_changed(self) -> bool:
        fresh = load_manus_config()
        current_fp = self._slot1_fingerprint(fresh)
        cached_fp = self._slot1_fingerprint(self._config)
        logger.info("MANUS_ENV_RELOAD_CHECK current_fp=%s cached_fp=%s", current_fp or "none", cached_fp or "none")
        if current_fp and current_fp != cached_fp:
            self._config = fresh
            self._key_pool = ManusKeyPool(self._config)
            logger.info("MANUS_ENV_RELOAD_DONE fp=%s", current_fp)
        active_fp = self._slot1_fingerprint(self._config)
        if active_fp:
            logger.info("MANUS_ACTIVE_KEY_FINGERPRINT slot=1 fp=%s", active_fp)
        return bool(current_fp and active_fp == current_fp)

    def _style_status_message(self, *, status: str, task_type: str, message: str) -> str:
        if build_personality_response is None:
            return str(message or "").removeprefix("Interface:").lstrip()
        normalized_status = str(status or "").strip().lower()
        if (
            normalized_status in {"completed", "completed_saved", "completed_waiting_destination"}
            or normalized_status.startswith("completed_") and "no_file" not in normalized_status
        ):
            response_type = "manus_task_done"
        elif any(marker in normalized_status for marker in ("failed", "disabled", "error", "no_file")):
            response_type = "generic_error"
        else:
            response_type = "manus_task_ack"
        return build_personality_response(
            response_type=response_type,
            raw_text=message,
            source="manus",
            kind=task_type,
        ).text

    async def _broadcast_status(self, job_id: str, status: str, task_type: str, message: str) -> None:
        if artifact_event_bus is None:
            return
        visible_message = str(message or "")
        if visible_message.startswith("Interface:"):
            visible_message = visible_message[len("Interface:"):].lstrip()
            logger.info("UI_MESSAGE_PREFIX_REMOVED old_prefix=Interface")
        visible_message = self._style_status_message(status=status, task_type=task_type, message=visible_message)
        payload = {
            "type": "artifact_job_update",
            "job_id": job_id,
            "status": status,
            "task_type": task_type,
            "message": visible_message,
        }
        try:
            await artifact_event_bus.publish(payload)
            logger.info("ARTIFACT_EVENT_BROADCAST job_id=%s status=%s", job_id, status)
        except Exception as exc:
            logger.warning("UI_NOTIFY_FAILED_IGNORED reason=%s", exc)
            logger.warning("TRANSCRIPT_PUBLISH_FAILED_IGNORED reason=%s", exc)
            logger.warning("UI_NOTIFY_FAILURE_NON_BLOCKING")
            logger.warning("TRANSCRIPT_PUBLISH_FAILURE_NON_BLOCKING")
            logger.info("ARTIFACT_STATE_PERSISTED_DESPITE_UI_NOTIFY_FAILURE job_id=%s", job_id)
            logger.warning("TRANSCRIPT_PUBLISH_ASYNC_FAILED ignored_for_speech=true error=%s", exc)
            logger.info("TEXT_ONLY_NOTICE_PERSISTED_TO_REGISTRY job_id=%s", job_id)
            logger.info("UI_NOTIFY_FAILURE_DID_NOT_BLOCK_JOB job_id=%s", job_id)
            logger.info("SPEECH_NOT_BLOCKED_BY_UI_NOTIFY_FAILURE")

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        loop = asyncio.get_event_loop()
        self._task = loop.create_task(self._run_loop())
        logger.info("MANUS_WORKER_START")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except BaseException:
                pass
        logger.info("MANUS_WORKER_STOP")

    def enqueue_job(self, job_id: str) -> None:
        normalized_job_id = str(job_id or "").strip()
        if not normalized_job_id:
            return
        with _JOB_GUARD_LOCK:
            if normalized_job_id in _QUEUED_JOB_IDS or normalized_job_id in _ACTIVE_JOB_IDS:
                logger.warning("MANUS_WORKER_DUPLICATE_JOB_BLOCKED job_id=%s", normalized_job_id)
                return
            _QUEUED_JOB_IDS.add(normalized_job_id)
        try:
            self._queue.put_nowait(normalized_job_id)
            try:
                update_job_status(normalized_job_id, "worker_queued")
            except Exception:
                pass
            logger.info("MANUS_WORKER_ENQUEUE job_id=%s", normalized_job_id)
        except Exception:
            with _JOB_GUARD_LOCK:
                _QUEUED_JOB_IDS.discard(normalized_job_id)
            logger.warning("MANUS_WORKER_UNAVAILABLE")

    async def run_once(self) -> None:
        job_id = await self._queue.get()
        with _JOB_GUARD_LOCK:
            _QUEUED_JOB_IDS.discard(job_id)
        try:
            await self._process_job(job_id)
        finally:
            self._queue.task_done()

    async def _poll_later(self, job_id: str) -> None:
        delay = max(1, int(getattr(getattr(self._client, "config", None), "poll_interval_seconds", 0) or self._key_pool.config.poll_interval_seconds))
        await asyncio.sleep(delay)
        if self._running:
            self._queue.put_nowait(job_id)

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("MANUS_WORKER_EXCEPTION job_id=unknown error_type=%s", type(exc).__name__)

    async def _process_job(self, job_id: str) -> None:
        with _JOB_GUARD_LOCK:
            if job_id in _ACTIVE_JOB_IDS:
                logger.warning("MANUS_WORKER_DUPLICATE_JOB_BLOCKED job_id=%s", job_id)
                return
            _ACTIVE_JOB_IDS.add(job_id)
        job = get_job(job_id)
        try:
            if not job:
                logger.info("MANUS_WORKER_PROCESS_DONE job_id=%s status=missing", job_id)
                return
            if not self._reload_config_if_env_changed():
                update_job_status(job_id, "manus_failed", error="env_key_changed")
                await self._broadcast_status(
                    job_id,
                    "manus_failed",
                    str(job.get("task_type") or "unknown"),
                    "Interface: a chave Manus mudou. Reinicie a Kira ou recarregue a configuracao.",
                )
                return
            current_status = str(job.get("status") or "").lower()
            if current_status in {"sent_to_manus", "running", "submitted", "sending_to_manus"}:
                if current_status in {"sent_to_manus", "running", "submitted"}:
                    await self._poll_and_finalize(job_id, job)
                else:
                    logger.warning("MANUS_WORKER_DUPLICATE_JOB_BLOCKED job_id=%s", job_id)
                return
            logger.info("MANUS_JOB_WORKER_STARTED job_id=%s kind=%s", job_id, str(job.get("task_type") or "unknown"))
            update_job_status(job_id, "sending_to_manus")
            await self._broadcast_status(job_id, "sending_to_manus", str(job.get("task_type") or "unknown"), "Interface: tarefa enviada ao executor.")
            logger.info("MANUS_WORKER_CREATE_TASK_BEGIN job_id=%s", job_id)
            if build_manus_task_spec is None:
                raise RuntimeError("prompt_builder_unavailable")
            spec = build_manus_task_spec(job)
            logger.info(
                "MANUS_PROMPT_BUILDER_SELECTED type=%s builder=%s",
                str(job.get("task_type") or "unknown"),
                str(type(spec).__name__),
            )
            logger.info(
                "MANUS_TASK_SPEC_BUILT job_id=%s type=%s prompt_chars=%d",
                job_id,
                str(job.get("task_type") or "unknown"),
                len(str(spec.prompt or "")),
            )
            try:
                save_job_task_spec(
                    job_id,
                    {
                        "job_id": spec.job_id,
                        "task_type": spec.task_type,
                        "title": spec.title,
                        "user_request": spec.user_request,
                        "prompt": spec.prompt,
                        "expected_outputs": spec.expected_outputs,
                        "output_category": spec.output_category,
                        "language": spec.language,
                        "quality_level": spec.quality_level,
                        "attachments": spec.attachments,
                        "constraints": spec.constraints,
                        "safety_rules": spec.safety_rules,
                        "metadata": spec.metadata,
                    },
                )
            except Exception:
                logger.exception("MANUS_PROMPT_BUILDER_FAILED job_id=%s error=save_task_spec_failed", job_id)
            client = self._client or (ManusClient(config=self._config) if ManusClient is not None else None)
            if client is None or not bool(getattr(client, "is_configured", lambda: False)()):
                update_job_status(job_id, "manus_disabled", error="manus_not_configured")
                await self._broadcast_status(job_id, "manus_disabled", str(job.get("task_type") or "unknown"), "Interface: executor nao configurado.")
                logger.info("MANUS_WORKER_MANUS_DISABLED job_id=%s", job_id)
                logger.info("MANUS_WORKER_PROCESS_DONE job_id=%s status=manus_disabled", job_id)
                return
            attempts = list(job.get("attempts") or [])
            max_attempts = max(1, min(int(getattr(getattr(client, "config", None), "max_retries", 5) or 5), 5))
            tried: set[int] = set()
            result = None
            for attempt_idx in range(1, max_attempts + 1):
                lease = self._key_pool.choose_key(job_id=job_id, exclude_slots=tried)
                if lease is None:
                    break
                tried.add(int(lease.slot))
                try:
                    try:
                        result = client.create_task(prompt=str(spec.prompt or ""), title=str(spec.title or "Tarefa Kira"), api_key=lease.key_value, key_slot=lease.slot)
                    except TypeError:
                        result = client.create_task(str(spec.prompt or ""), metadata={"title": str(spec.title or "Tarefa Kira")})
                except ManusAuthError:
                    self._key_pool.mark_auth_error(lease.slot)
                    self._key_pool.log_rotate(lease.slot, None)
                    logger.warning("MANUS_AUTH_FAILED key_slot=%s status=403", lease.slot)
                    update_job_status(job_id, "manus_failed", error="auth_error")
                    await self._broadcast_status(
                        job_id,
                        "manus_failed",
                        str(job.get("task_type") or "unknown"),
                        "Interface: o Manus recusou a autenticacao da API. Verifique a chave MANUS_API_KEY_1 no .env.",
                    )
                    logger.warning("LOCAL_ARTIFACT_FALLBACK_BLOCKED reason=manus_required")
                    logger.warning("MANUS_TASK_FAILED_NO_LOCAL_FALLBACK job_id=%s", job_id)
                    logger.info("MANUS_WORKER_PROCESS_DONE job_id=%s status=manus_failed reason=auth_error", job_id)
                    return
                except ManusQuotaError:
                    self._key_pool.mark_quota_exhausted(lease.slot)
                    self._key_pool.log_rotate(lease.slot, None)
                    err_type = "quota_exhausted"
                    result = None
                except ManusClientError as exc:
                    self._key_pool.mark_failure(lease.slot, str(exc))
                    self._key_pool.log_rotate(lease.slot, None)
                    err_type = "client_error"
                    result = None
                err_type = str(getattr(result, "error_type", "") or "")
                if result is None:
                    continue
                attempt_rec = {
                    "attempt": attempt_idx,
                    "key_slot": lease.slot,
                    "status": "success" if bool(getattr(result, "ok", False)) else "failed",
                    "error_type": err_type or None,
                    "manus_task_id": getattr(result, "task_id", None),
                    "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                }
                attempts.append(attempt_rec)
                update_job_status(job_id, "sending_to_manus", extra={"attempts": attempts})
                if bool(getattr(result, "ok", False)) and getattr(result, "task_id", None):
                    self._key_pool.mark_success(lease.slot)
                    break
                if err_type in {"quota_exhausted", "rate_limited", "429"}:
                    self._key_pool.mark_quota_exhausted(lease.slot)
                elif err_type == "auth_error":
                    self._key_pool.mark_auth_error(lease.slot)
                else:
                    self._key_pool.mark_failure(lease.slot, str(getattr(result, "error", "unknown_error") or "unknown_error"))
            if result is None:
                update_job_status(job_id, "manus_failed", error="all_keys_failed")
                logger.warning("MANUS_KEY_POOL_ALL_FAILED slots_tried=%s", sorted(tried))
                logger.warning("MANUS_JOB_WORKER_FAILED job_id=%s reason=all_keys_failed", job_id)
                logger.warning("LOCAL_ARTIFACT_FALLBACK_BLOCKED reason=manus_required")
                logger.warning("LEGACY_LOCAL_FILE_CREATOR_BLOCKED reason=manus_creation_task")
                logger.warning("MANUS_TASK_FAILED_NO_LOCAL_FALLBACK job_id=%s", job_id)
                return
            if bool(getattr(result, "ok", False)) and getattr(result, "task_id", None):
                job = update_job_status(job_id, "submitted", extra={"manus_task_id": result.task_id, "manus_task_url": getattr(result, "task_url", None), "executor": "manus"})
                kind_label = {"pdf": "PDF", "text": "texto", "slides": "apresentacao", "prompt": "prompt", "code": "codigo"}.get(str(job.get("task_type") or "").lower(), "arquivo")
                submitted_notice = (
                    f"{kind_label} enviado para o Manus. Vou salvar na pasta padrao quando terminar."
                    if str(job.get("destination_mode") or job.get("destination_policy") or "") == "default"
                    else f"{kind_label} enviado para o Manus. Vou avisar quando estiver pronto."
                )
                await self._broadcast_status(job_id, "submitted", str(job.get("task_type") or "unknown"), submitted_notice)
                logger.info("MANUS_JOB_WORKER_SUBMITTED job_id=%s task_id=%s", job_id, result.task_id)
                if not callable(getattr(client, "poll_task_until_done", None)):
                    update_job_status(job_id, "sent_to_manus", extra={"manus_task_id": result.task_id, "manus_task_url": getattr(result, "task_url", None), "executor": "manus"})
                    logger.info("MANUS_TASK_CREATED task_id=%s", result.task_id)
                    logger.info("MANUS_WORKER_PROCESS_DONE job_id=%s status=sent_to_manus", job_id)
                    return
                logger.info("MANUS_TASK_CREATED task_id=%s", result.task_id)
                await self._poll_and_finalize(job_id, job)
                logger.info("MANUS_WORKER_PROCESS_DONE job_id=%s status=submitted", job_id)
                return
            err = str(getattr(result, "error", "unknown_error") or "unknown_error")
            if getattr(result, "status", "") == "disabled":
                update_job_status(job_id, "manus_disabled", error=err)
                await self._broadcast_status(job_id, "manus_disabled", str(job.get("task_type") or "unknown"), "Interface: executor nao configurado.")
                logger.info("MANUS_WORKER_MANUS_DISABLED job_id=%s", job_id)
                logger.info("MANUS_WORKER_PROCESS_DONE job_id=%s status=manus_disabled", job_id)
                return
            update_job_status(job_id, "manus_failed", error=err)
            await self._broadcast_status(job_id, "manus_failed", str(job.get("task_type") or "unknown"), "Interface: o Manus nao conseguiu concluir a tarefa. Verifique a API ou tente novamente.")
            logger.warning("LOCAL_ARTIFACT_FALLBACK_BLOCKED reason=manus_required")
            logger.warning("LEGACY_LOCAL_FILE_CREATOR_BLOCKED reason=manus_creation_task")
            logger.warning("MANUS_TASK_FAILED_NO_LOCAL_FALLBACK job_id=%s", job_id)
            logger.warning("MANUS_WORKER_CREATE_TASK_FAILED job_id=%s error=%s", job_id, err)
            logger.info("MANUS_WORKER_PROCESS_DONE job_id=%s status=failed", job_id)
        except Exception as exc:
            update_job_status(job_id, "failed", error=str(exc))
            await self._broadcast_status(job_id, "failed", str(job.get("task_type") or "unknown"), "Interface: tarefa falhou.")
            logger.exception("MANUS_WORKER_EXCEPTION job_id=%s error_type=%s", job_id, type(exc).__name__)
            logger.info("MANUS_WORKER_PROCESS_DONE job_id=%s status=failed", job_id)
        finally:
            with _JOB_GUARD_LOCK:
                _ACTIVE_JOB_IDS.discard(job_id)
    async def _poll_and_finalize(self, job_id: str, job: dict, allow_pdf_retry: bool = True) -> None:
        try:
            task_id = str(job.get("manus_task_id") or "").strip()
            if not task_id:
                return
            client = self._client or (ManusClient() if ManusClient is not None else None)
            if client is None:
                return
            if callable(get_voice_output_manager):
                try:
                    manager = get_voice_output_manager()
                    if manager.is_speaking():
                        logger.info("MANUS_POLL_DEFERRED_DURING_SPEECH job_id=%s", job_id)
                        deadline = time.monotonic() + 10.0
                        while manager.is_speaking() and time.monotonic() < deadline:
                            await asyncio.sleep(0.5)
                    logger.info("MANUS_POLL_RUNNING_BACKGROUND_NON_BLOCKING job_id=%s", job_id)
                except Exception:
                    pass
            logger.info("MANUS_TASK_POLL_BEGIN job_id=%s task_id=%s", job_id, task_id)
            poll = await asyncio.to_thread(
                client.poll_task_until_done,
                task_id,
                int(getattr(getattr(client, "config", None), "task_timeout_minutes", 45) or 45),
                int(getattr(getattr(client, "config", None), "poll_interval_seconds", 8) or 8),
            )
            status = str((poll or {}).get("status") or "").lower()
            logger.info("MANUS_TASK_STATUS job_id=%s status=%s", job_id, status)
            if status == "waiting_user_input":
                update_job_status(job_id, "waiting_executor", extra={"executor": "manus"})
                await self._broadcast_status(job_id, "waiting_executor", str(job.get("task_type") or "unknown"), "Interface: o Manus precisa de confirmacao ou informacao adicional.")
                return
            if status in {"failed", "error", "cancelled", "timeout"}:
                state = reconcile_artifact_state(job_id=job_id, kind=str(job.get("task_type") or "unknown")) if callable(reconcile_artifact_state) else None
                if state is not None and bool(getattr(state, "saved", False)):
                    path = str(getattr(state, "final_path", ""))
                    logger.info("MANUS_JOB_COMPLETED_FILE_CONFIRMED path=%s", path)
                    logger.info("MANUS_JOB_COMPLETED_DO_NOT_ASK_SAVE_LOCATION reason=file_exists")
                    notice = f"{str(job.get('task_type') or 'arquivo').upper()} pronto e salvo em {path}"
                    await self._broadcast_status(job_id, "completed", str(job.get("task_type") or "unknown"), notice)
                    await self._notify_completion_once(job, "completed", notice, path)
                    return
                update_job_status(job_id, "manus_failed", error=str((poll or {}).get("error") or "task_failed"))
                await self._broadcast_status(job_id, "manus_failed", str(job.get("task_type") or "unknown"), "Interface: o Manus nao conseguiu concluir a tarefa. Verifique a API ou tente novamente.")
                logger.warning("LOCAL_ARTIFACT_FALLBACK_BLOCKED reason=manus_required")
                logger.warning("MANUS_TASK_FAILED_NO_LOCAL_FALLBACK job_id=%s", job_id)
                return
            files = list((poll or {}).get("files") or [])
            if not files:
                files = client.list_task_files(task_id)
            try:
                message_result = client.list_task_messages(task_id=task_id, order="asc", limit=100)
                messages = list((message_result or {}).get("messages") or [])
            except Exception:
                messages = []
            message_files = extract_list_messages_file_candidates(messages, str(job.get("task_type") or ""))
            existing_refs = {str((f or {}).get("url") or (f or {}).get("id") or (f or {}).get("file_id") or "") for f in files}
            files.extend([item for item in message_files if str(item.get("url") or "") not in existing_refs])
            logger.info("MANUS_TASK_FILES_FOUND job_id=%s count=%d", job_id, len(files))
            saved = []
            staged = []
            destination_mode = str(job.get("destination_mode") or job.get("destination_policy") or "default").lower()
            resolved_folder = str(job.get("resolved_destination_folder") or "").strip()

            def _store_ready_file(temp_path: str, kind: str, title: str) -> str:
                if destination_mode == "ask_later":
                    staged_path = stage_downloaded_file(temp_path, kind, title, job_id)
                    staged.append(str(staged_path))
                    return str(staged_path)
                final_path = (
                    save_downloaded_file(temp_path, kind, title, destination_folder=resolved_folder)
                    if resolved_folder
                    else save_downloaded_file(temp_path, kind, title)
                )
                saved.append(str(final_path))
                return str(final_path)
            assistant_text = str((poll or {}).get("assistant_text") or "").strip()
            requested_content = extract_requested_text_content(
                str(job.get("user_request") or ""),
                str(job.get("task_type") or ""),
            )
            import tempfile
            from pathlib import Path
            is_simple_text = str(job.get("task_type") or "").lower() in {"text", "txt"}
            if is_simple_text and (assistant_text or requested_content):
                final_text = clean_manus_text_result(
                    assistant_text,
                    requested_content=requested_content,
                    job_kind=str(job.get("task_type") or "text"),
                )
                with tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8") as tf_txt:
                    tf_txt.write(final_text)
                    tmp_txt = tf_txt.name
                final_path = _store_ready_file(tmp_txt, "text", str(job.get("title_hint") or "texto"))
                if destination_mode != "ask_later":
                    # Problema 4: confirmar TXT somente apos verificar o arquivo no disco.
                    logger.info("ARTIFACT_DISK_VERIFY_BEGIN path=%s type=text", str(final_path))
                    txt_ok, txt_size = verify_artifact_file(final_path, expected_kind="text")
                    if not txt_ok:
                        logger.warning("ARTIFACT_DISK_VERIFY_FAILED path=%s type=text", str(final_path))
                        update_job_status(job_id, "missing_file", error="text_file_missing_on_disk")
                        logger.info("ARTIFACT_REGISTRY_MARK_MISSING_FILE job_id=%s kind=text", job_id)
                        notice = "A tarefa foi criada, mas eu nao encontrei o arquivo final no disco."
                        await self._broadcast_status(job_id, "missing_file", "text", notice)
                        await self._notify_completion_once(job, "missing_file", notice)
                        return
                    logger.info("ARTIFACT_DISK_VERIFY_OK path=%s size=%s type=text", str(final_path), txt_size)
                    logger.info("ARTIFACT_SAVED_TO_DEFAULT_FOLDER kind=text path=%s", str(final_path))
                    register_completed_artifact(
                        job_id, "text", str(final_path), source="manus",
                        metadata={"title": str(job.get("title_hint") or ""), "manus_task_id": task_id, "manus_task_url": str(job.get("manus_task_url") or "")},
                    )
                    update_last_generated("text", str(final_path), job_id=job_id)
            for f in files:
                if is_simple_text and requested_content and saved:
                    continue
                fid = str((f or {}).get("id") or (f or {}).get("file_id") or (f or {}).get("url") or "").strip()
                fname = str((f or {}).get("name") or "output.bin")
                if not fid:
                    continue
                if str(job.get("task_type") or "").lower() == "pdf" and ".pdf" in fid.lower().split("?", 1)[0] and not fname.lower().endswith(".pdf"):
                    fname = "output.pdf"
                if str(job.get("task_type") or "").lower() == "pdf" and not (
                    fname.lower().endswith(".pdf")
                    or "application/pdf" in str((f or {}).get("mime_type") or "").lower()
                    or ".pdf" in fid.lower().split("?", 1)[0]
                ):
                    continue
                with tempfile.NamedTemporaryFile(delete=False, suffix=Path(fname).suffix or ".bin") as tf:
                    tmp = tf.name
                if str(job.get("task_type") or "").lower() == "pdf":
                    logger.info("MANUS_PDF_DOWNLOAD_BEGIN source=%s file=%s", safe_download_reference_label(fid), Path(fname).name)
                logger.info("MANUS_FILE_DOWNLOAD_BEGIN job_id=%s file=%s", job_id, Path(fname).name)
                dl = client.download_file(fid, tmp)
                if not bool((dl or {}).get("ok")):
                    if str(job.get("task_type") or "").lower() == "pdf":
                        logger.warning("MANUS_PDF_DOWNLOAD_FAILED reason=%s", str((dl or {}).get("error") or "download_error"))
                    logger.warning("ARTIFACT_DOWNLOAD_FAILED job_id=%s error=%s", job_id, str((dl or {}).get("error") or "download_error"))
                    continue
                logger.info("MANUS_FILE_DOWNLOAD_OK temp_path=%s", tmp)
                logger.info("ARTIFACT_VALIDATE_BEGIN path=%s type=%s", tmp, str(job.get("task_type") or "unknown"))
                vr = validate_artifact(tmp, str(job.get("task_type") or "unknown"))
                if not bool(getattr(vr, "ok", False)):
                    logger.warning("ARTIFACT_DOWNLOAD_FAILED job_id=%s error=%s", job_id, str(getattr(vr, "reason", "validation_failed")))
                    continue
                logger.info("ARTIFACT_VALIDATE_OK path=%s", tmp)
                final_path = _store_ready_file(tmp, str(job.get("task_type") or "unknown"), str(job.get("title_hint") or job.get("task_type") or "arquivo"))
                if destination_mode != "ask_later":
                    if str(job.get("task_type") or "").lower() == "pdf":
                        verified, _size = verify_artifact_file(final_path, expected_kind="pdf")
                        if not verified:
                            logger.warning("CREATION_TASK_CONFIRMATION_BLOCKED reason=file_missing path=%s", str(final_path))
                            continue
                        logger.info("CREATION_TASK_CONFIRMATION_ALLOWED path=%s", str(final_path))
                    logger.info("ARTIFACT_SAVE_OK final_path=%s", str(final_path))
                    logger.info("ARTIFACT_SAVED_TO_DEFAULT_FOLDER kind=%s path=%s", str(job.get("task_type") or "unknown"), str(final_path))
                    register_completed_artifact(
                        job_id, str(job.get("task_type") or "unknown"), str(final_path), source="manus",
                        metadata={"title": str(job.get("title_hint") or ""), "manus_task_id": task_id},
                    )
                    update_last_generated(str(job.get("task_type") or "unknown"), str(final_path), job_id=job_id)
                if str(job.get("task_type") or "").lower() == "pdf" and destination_mode != "ask_later":
                    logger.info("MANUS_PDF_DOWNLOAD_OK path=%s", str(final_path))
            if staged:
                update_job_status(job_id, "completed_waiting_destination", extra={"staged_files": staged, "output_files": []})
                logger.info("MANUS_JOB_COMPLETED_RESULT_READY job_id=%s", job_id)
                logger.info("MANUS_JOB_COMPLETED_WAITING_DESTINATION job_id=%s", job_id)
                kind_label = {"pdf": "PDF", "text": "texto", "slides": "apresentacao", "prompt": "prompt", "code": "codigo"}.get(str(job.get("task_type") or "").lower(), "arquivo")
                notice = f"Chefe, o {kind_label} ficou pronto. Onde voce quer que eu salve?"
                await self._broadcast_status(job_id, "completed_waiting_destination", str(job.get("task_type") or "unknown"), notice)
                await self._notify_completion_once(job, "completed_waiting_destination", notice)
                logger.info("MANUS_JOB_COMPLETED_NOTIFY_SENT status=completed_waiting_destination")
            elif saved:
                verified_saved = []
                if str(job.get("task_type") or "").lower() == "pdf":
                    for saved_path in saved:
                        verified, _size = verify_artifact_file(saved_path, expected_kind="pdf")
                        if verified:
                            verified_saved.append(saved_path)
                else:
                    verified_saved = list(saved)
                if not verified_saved:
                    update_job_status(job_id, "manus_failed", error="verified_file_missing")
                    logger.warning("CREATION_TASK_CONFIRMATION_BLOCKED reason=file_missing")
                    notice = "Chefe, o Manus terminou a tarefa, mas eu não encontrei o arquivo salvo no disco. Não vou confirmar como pronto."
                    await self._broadcast_status(job_id, "manus_failed", str(job.get("task_type") or "unknown"), notice)
                    await self._notify_completion_once(job, "manus_failed", notice)
                    return
                saved = verified_saved
                update_job_status(job_id, "completed_saved", extra={"output_files": saved})
                confirmed_path = saved[0]
                logger.info("MANUS_JOB_COMPLETED_FILE_CONFIRMED path=%s", confirmed_path)
                logger.info("MANUS_JOB_COMPLETED_DO_NOT_ASK_SAVE_LOCATION reason=file_exists")
                kind_label = {"pdf": "PDF", "text": "texto", "slides": "apresentacao", "prompt": "prompt", "code": "codigo"}.get(str(job.get("task_type") or "").lower(), "arquivo")
                notice = (
                    f"Chefe, o {kind_label} ficou pronto e ja salvei na pasta padrao."
                    if destination_mode == "default"
                    else f"Chefe, o {kind_label} ficou pronto e ja salvei em {confirmed_path}."
                )
                logger.info("MANUS_JOB_COMPLETED_SAVED path=%s", confirmed_path)
                await self._broadcast_status(job_id, "completed_saved", str(job.get("task_type") or "unknown"), notice)
                await self._notify_completion_once(job, "completed_saved", notice, confirmed_path)
                logger.info("MANUS_JOB_COMPLETED_NOTIFY_SENT status=completed_saved")
                logger.info("MANUS_JOB_COMPLETED_TEXT_ONLY_NOTIFY job_id=%s", job_id)
                logger.info("MANUS_JOB_VOICE_DISABLED job_id=%s", job_id)
                logger.info("MANUS_JOB_GEMINI_DISABLED job_id=%s", job_id)
                logger.info("CREATION_TASK_COMPLETED_TEXT_ONLY_NOTICE job_id=%s path=%s", job_id, saved[0])
                logger.info("ARTIFACT_JOB_DONE job_id=%s files=%d", job_id, len(saved))
            else:
                is_pdf = str(job.get("task_type") or "").lower() == "pdf"
                if is_pdf and allow_pdf_retry:
                    logger.warning("MANUS_PDF_NO_FILE_DETECTED job_id=%s", job_id)
                    updated = update_job_status(job_id, "completed_no_file", error="no_pdf_file")
                    logger.info("MANUS_PDF_CORRECTIVE_RETRY_BEGIN attempt=1")
                    original_prompt = str((job.get("task_spec") or {}).get("prompt") or job.get("user_request") or "")
                    corrective_prompt = (
                        f"{original_prompt}\n\nCORRECAO OBRIGATORIA: A tarefa anterior terminou sem anexar PDF. "
                        "Gere agora o arquivo PDF real e anexe o arquivo .pdf baixavel. "
                        "Nao responda apenas texto. Se nao conseguir, responda NAO_CONSEGUI_GERAR_ARQUIVO_PDF."
                    )
                    retry_result = client.create_task(prompt=corrective_prompt, title="Correcao PDF Kira")
                    if bool(getattr(retry_result, "ok", False)) and getattr(retry_result, "task_id", None):
                        logger.info("MANUS_PDF_CORRECTIVE_RETRY_SENT")
                        retry_job = update_job_status(
                            job_id,
                            "submitted",
                            extra={"manus_task_id": retry_result.task_id, "pdf_corrective_retry_attempt": 1},
                        )
                        await self._poll_and_finalize(job_id, retry_job, allow_pdf_retry=False)
                        final_job = get_job(job_id) or {}
                        if str(final_job.get("status") or "") == "completed":
                            logger.info("MANUS_PDF_CORRECTIVE_RETRY_OK path=%s", str((final_job.get("output_files") or [""])[0]))
                        return
                    logger.warning("MANUS_PDF_CORRECTIVE_RETRY_FAILED reason=create_task_failed")
                if is_pdf:
                    logger.warning("MANUS_PDF_CORRECTIVE_RETRY_FAILED reason=no_pdf_file")
                    no_file_status = "completed_no_file" if allow_pdf_retry else "manus_failed_no_pdf_file"
                    update_job_status(job_id, no_file_status, error="no_pdf_file")
                    logger.info("MANUS_JOB_COMPLETED_NO_FILE job_id=%s status=%s", job_id, no_file_status)
                    notice = "O Manus terminou, mas nao devolveu um arquivo PDF baixavel. Posso tentar de novo com um pedido mais direto."
                    await self._broadcast_status(job_id, no_file_status, "pdf", notice)
                    await self._notify_completion_once(job, no_file_status, notice)
                else:
                    update_job_status(job_id, "manus_failed", error="no_valid_files")
                    notice = "O Manus nao conseguiu concluir a tarefa. Verifique a API ou tente novamente."
                    await self._broadcast_status(job_id, "manus_failed", str(job.get("task_type") or "unknown"), notice)
                    await self._notify_completion_once(job, "manus_failed", notice)
                logger.warning("LOCAL_ARTIFACT_FALLBACK_BLOCKED reason=manus_required")
                logger.warning("MANUS_TASK_FAILED_NO_LOCAL_FALLBACK job_id=%s", job_id)
        except Exception as exc2:
            logger.warning("ARTIFACT_DOWNLOAD_FAILED job_id=%s error=%s", job_id, str(exc2))
