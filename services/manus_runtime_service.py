from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

from services.artifact_registry import save_job_task_spec, update_job_status
from services.feature_runtime_registry import (
    get_feature_state,
    mark_feature_error,
    mark_feature_running,
    mark_feature_starting,
    mark_feature_stopped,
)
from services.manus_config import get_available_key_slots, load_manus_config

try:
    from agent.integrations.manus_client import ManusClient
    from agent.integrations.manus_job_worker import ManusJobWorker
    from agent.integrations.manus_prompt_builders import build_manus_task_spec
except Exception:  # pragma: no cover
    from integrations.manus_client import ManusClient  # type: ignore
    from integrations.manus_job_worker import ManusJobWorker  # type: ignore
    from integrations.manus_prompt_builders import build_manus_task_spec  # type: ignore

logger = logging.getLogger("kira.manus_runtime")


@dataclass
class ManusRuntimeState:
    state: str
    enabled: bool
    configured: bool
    reason: str = ""


@dataclass
class ManusSubmitResult:
    accepted: bool
    job_id: str
    task_id: str = ""
    task_url: str = ""
    reason: str = ""
    client: Any | None = None


def get_manus_status() -> dict[str, Any]:
    feature = get_feature_state("manus")
    return {
        "state": feature.state,
        "enabled": bool(feature.enabled),
        "lazy": bool(feature.lazy),
        "autostart": bool(feature.autostart),
        "shutdown_after_task": bool(feature.shutdown_after_task),
    }


def _short_reason(value: object) -> str:
    text = str(value or "unknown").strip() or "unknown"
    return text[:160]


async def ensure_manus_started(reason: str = "creation_task") -> ManusRuntimeState:
    logger.info("CREATION_TASK_EXECUTOR_CHECK_BEGIN executor=manus")
    feature = get_feature_state("manus")
    if not bool(feature.enabled):
        logger.info("CREATION_TASK_EXECUTOR_UNAVAILABLE executor=manus reason=disabled")
        logger.info("CREATION_TASK_ACK_BLOCKED reason=manus_unavailable")
        logger.info("CREATION_TASK_NO_FALSE_SUCCESS")
        return ManusRuntimeState("off", False, False, "manus_disabled")

    mark_feature_starting("manus", reason=reason)
    config = load_manus_config()
    if not bool(config.enabled):
        mark_feature_error("manus", reason="env_disabled")
        logger.info("CREATION_TASK_EXECUTOR_UNAVAILABLE executor=manus reason=disabled")
        logger.info("CREATION_TASK_ACK_BLOCKED reason=manus_unavailable")
        logger.info("CREATION_TASK_NO_FALSE_SUCCESS")
        return ManusRuntimeState("error", True, False, "manus_disabled")
    if not get_available_key_slots(config):
        mark_feature_error("manus", reason="missing_api_key")
        logger.info("MANUS_WORKER_UNAVAILABLE reason=missing_api_key")
        logger.info("CREATION_TASK_EXECUTOR_UNAVAILABLE executor=manus reason=missing_api_key")
        logger.info("CREATION_TASK_ACK_BLOCKED reason=manus_unavailable")
        logger.info("CREATION_TASK_NO_FALSE_SUCCESS")
        return ManusRuntimeState("error", True, False, "missing_api_key")

    mark_feature_running("manus", reason=reason)
    logger.info("CREATION_TASK_EXECUTOR_AVAILABLE executor=manus")
    return ManusRuntimeState("running", True, True, "")


async def submit_manus_task(
    job: dict[str, Any],
    *,
    client_factory: Callable[[], Any] | None = None,
) -> ManusSubmitResult:
    job_id = str(job.get("job_id") or "")
    state = await ensure_manus_started("creation_task")
    if not state.configured:
        try:
            update_job_status(job_id, "manus_disabled" if state.reason == "manus_disabled" else "manus_failed", error=state.reason)
        except Exception:
            pass
        return ManusSubmitResult(False, job_id, reason=state.reason)

    spec = build_manus_task_spec(job)
    save_job_task_spec(job_id, {"prompt": spec.prompt, "title": spec.title, "expected_outputs": spec.expected_outputs})
    client = client_factory() if client_factory is not None else ManusClient()
    logger.info("CREATION_TASK_MANUS_LAZY_ROUTE")
    try:
        result = await asyncio.to_thread(client.create_task, spec.prompt, spec.title)
    except Exception as exc:
        reason = _short_reason(exc)
        mark_feature_error("manus", reason=reason)
        update_job_status(job_id, "manus_failed", error=reason)
        logger.info("CREATION_TASK_EXECUTOR_UNAVAILABLE executor=manus reason=%s", reason)
        logger.info("CREATION_TASK_ACK_BLOCKED reason=manus_unavailable")
        logger.info("CREATION_TASK_NO_FALSE_SUCCESS")
        await shutdown_manus("task_failed")
        return ManusSubmitResult(False, job_id, reason=reason)

    if not bool(getattr(result, "ok", False)) or not str(getattr(result, "task_id", "") or "").strip():
        reason = _short_reason(getattr(result, "error", "") or getattr(result, "status", "") or "task_not_accepted")
        update_job_status(job_id, "manus_disabled" if reason == "manus_not_configured" else "manus_failed", error=reason)
        logger.info("CREATION_TASK_EXECUTOR_UNAVAILABLE executor=manus reason=%s", reason)
        logger.info("CREATION_TASK_ACK_BLOCKED reason=manus_unavailable")
        logger.info("CREATION_TASK_NO_FALSE_SUCCESS")
        await shutdown_manus("task_failed")
        return ManusSubmitResult(False, job_id, reason=reason)

    task_id = str(getattr(result, "task_id") or "").strip()
    task_url = str(getattr(result, "task_url", "") or "").strip()
    update_job_status(job_id, "submitted", extra={"manus_task_id": task_id, "manus_task_url": task_url, "executor": "manus"})
    logger.info("CREATION_TASK_EXECUTOR_AVAILABLE executor=manus")
    logger.info("MANUS_TASK_ACCEPTED job_id=%s task_id=%s", job_id, task_id)
    return ManusSubmitResult(True, job_id, task_id=task_id, task_url=task_url, client=client)


async def finalize_manus_task(
    job: dict[str, Any],
    submit_result: ManusSubmitResult,
    *,
    completion_notifier: Callable[[dict], Any] | None = None,
) -> None:
    try:
        latest = dict(job)
        latest["manus_task_id"] = submit_result.task_id
        latest["manus_task_url"] = submit_result.task_url
        worker = ManusJobWorker(manus_client=submit_result.client, completion_notifier=completion_notifier)
        await worker._poll_and_finalize(str(submit_result.job_id), latest)  # Reuse existing save/register pipeline.
    except Exception as exc:
        reason = _short_reason(exc)
        logger.warning("MANUS_TASK_FINALIZE_FAILED_NON_FATAL reason=%s", reason)
        try:
            update_job_status(submit_result.job_id, "manus_failed", error=reason)
        except Exception:
            pass
    finally:
        await shutdown_manus("task_finished")


async def shutdown_manus(reason: str = "task_finished") -> None:
    try:
        mark_feature_stopped("manus", reason=reason)
    except Exception as exc:
        logger.warning("MANUS_LAZY_SHUTDOWN_FAILED_NON_FATAL reason=%s", _short_reason(exc))
