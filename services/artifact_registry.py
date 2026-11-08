from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

try:
    from services.artifact_destination_manager import (
        detect_destination_intent,
        folder_for_destination_intent,
        get_default_folder_for_artifact,
        resolve_artifact_destination,
    )
except Exception:  # pragma: no cover - subprocesso agent usa alias services local
    import importlib.util as _dest_import
    import sys as _dest_sys
    _dest_path = Path(__file__).with_name("artifact_destination_manager.py")
    _dest_spec = _dest_import.spec_from_file_location("services.artifact_destination_manager", str(_dest_path))
    assert _dest_spec and _dest_spec.loader
    _dest_mod = _dest_import.module_from_spec(_dest_spec)
    _dest_sys.modules["services.artifact_destination_manager"] = _dest_mod
    _dest_spec.loader.exec_module(_dest_mod)
    detect_destination_intent = _dest_mod.detect_destination_intent
    folder_for_destination_intent = _dest_mod.folder_for_destination_intent
    get_default_folder_for_artifact = _dest_mod.get_default_folder_for_artifact
    resolve_artifact_destination = _dest_mod.resolve_artifact_destination
try:
    from services.kira_paths import ensure_artifact_directories as ensure_paths
except Exception:  # pragma: no cover
    import importlib.util
    _spec = importlib.util.spec_from_file_location("kira_paths", str(Path(__file__).with_name("kira_paths.py")))
    assert _spec and _spec.loader
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    ensure_paths = _mod.ensure_artifact_directories

logger = logging.getLogger(__name__)

_RUNTIME_DIR = Path("runtime") / "artifacts"
_JOBS_PATH = _RUNTIME_DIR / "jobs.json"
_ARTIFACTS_PATH = _RUNTIME_DIR / "artifacts_index.json"
_STATE_PATH = _RUNTIME_DIR / "state.json"

_ALLOWED_STATUS = {
    "queued",
    "worker_queued",
    "sending_to_manus",
    "sent_to_manus",
    "submitted",
    "running",
    "waiting_executor",
    "manus_disabled",
    "retrying",
    "local_placeholder",
    "done",
    "completed",
    "completed_saved",
    "completed_waiting_destination",
    "completed_reconciled",
    "completed_no_file",
    "manus_failed_no_pdf_file",
    "manus_failed",
    "failed",
    "cancelled",
    # Estados de reconciliacao (hotfix manus/jobs/registry). Nunca apagam o job,
    # apenas o tiram do estado "running" quando nao ha execucao real.
    "stale_failed",
    "orphaned",
    "missing_file",
    "expired_waiting_destination",
}

# Status que /api/artifacts/status conta como "running". Apos a reconciliacao,
# jobs antigos saem deste conjunto e param de aparecer como em execucao.
_RUNNING_LIKE_STATUS = {
    "queued",
    "worker_queued",
    "sending_to_manus",
    "sent_to_manus",
    "submitted",
    "running",
    "retrying",
    "waiting_executor",
    "downloading",
    "validating",
}
_CATEGORY_BY_TYPE = {
    "pdf": "PDFs", "document": "Textos", "text": "Textos", "prompt": "Prompts",
    "code": "Codigos", "python": "Codigos", "html": "Codigos", "javascript": "Codigos", "csharp": "Codigos",
    "slides": "Slides", "powerpoint": "Slides", "spreadsheet": "Planilhas", "image": "Imagens", "unknown": "Outros",
}
_RECOVERY_EXTENSIONS = {
    "pdf": {".pdf"},
    "text": {".txt", ".md"},
    "document": {".txt", ".md"},
    "image": {".png", ".jpg", ".jpeg", ".webp"},
    "slides": {".pptx", ".pdf"},
    "slide": {".pptx", ".pdf"},
    "spreadsheet": {".xlsx", ".csv"},
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", delete=False, encoding="utf-8", dir=str(path.parent), suffix=".tmp") as tf:
        json.dump(payload, tf, ensure_ascii=False, indent=2)
        tmp = tf.name
    Path(tmp).replace(path)


def _load_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        if not path.exists():
            return default
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = path.with_suffix(path.suffix + f".corrupt_{stamp}")
        try:
            path.replace(backup)
            logger.warning("ARTIFACT_REGISTRY_CORRUPT_BACKUP path=%s", str(backup))
        except Exception:
            return default
        return default


def _load_all() -> tuple[list[dict], list[dict], dict]:
    logger.info("ARTIFACT_REGISTRY_LOAD_BEGIN")
    _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    jobs = _load_json(_JOBS_PATH, [])
    artifacts = _load_json(_ARTIFACTS_PATH, [])
    state = _load_json(_STATE_PATH, {})
    jobs = jobs if isinstance(jobs, list) else []
    artifacts = artifacts if isinstance(artifacts, list) else []
    state = state if isinstance(state, dict) else {}
    logger.info("ARTIFACT_REGISTRY_LOAD_OK jobs=%d artifacts=%d", len(jobs), len(artifacts))
    return jobs, artifacts, state


def _save_all(jobs: list[dict], artifacts: list[dict], state: dict) -> None:
    _atomic_write_json(_JOBS_PATH, jobs)
    _atomic_write_json(_ARTIFACTS_PATH, artifacts)
    _atomic_write_json(_STATE_PATH, state)


def ensure_artifact_directories() -> dict[str, str]:
    return ensure_paths()


def create_creation_job(command, source, raw_text) -> dict:
    dirs = ensure_paths()
    jobs, artifacts, state = _load_all()
    now = _now_iso()
    task_type = str(getattr(command, "task_type", "unknown") or "unknown")
    title_hint = str(getattr(command, "title_hint", "") or "").strip()
    intent = detect_destination_intent(str(raw_text or ""), task_type)
    command_mode = str(getattr(command, "destination_mode", "") or "").strip()
    command_hint = str(getattr(command, "destination_hint", "") or "").strip()
    if command_mode and command_mode != "ask_later":
        intent = detect_destination_intent(str(raw_text or ""), task_type)
    destination = resolve_artifact_destination(task_type, requested_location=str(raw_text or ""), title=title_hint or None)
    final_folder = folder_for_destination_intent(task_type, intent)
    job_id = f"job_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
    output_category = _CATEGORY_BY_TYPE.get(task_type, "Outros")
    job = {
        "job_id": job_id, "task_type": task_type, "status": "waiting_executor", "source": str(source or "unknown"),
        "user_request": str(raw_text or ""), "normalized_request": str(getattr(command, "normalized_text", "") or ""),
        "created_at": now, "updated_at": now, "title_hint": title_hint,
        "artifact_kind": destination.kind, "artifact_title": title_hint,
        "destination_policy": intent.mode,
        "destination_mode": intent.mode,
        "destination_hint": intent.hint or command_hint,
        "destination_root": str(destination.root), "destination_folder": str(destination.folder),
        "resolved_destination_folder": str(final_folder) if final_folder else "",
        "user_requested_default_folder": intent.mode == "default",
        "requires_location_confirmation": intent.mode == "ask_later",
        "manus_supported": bool(destination.manus_supported), "route_to_manus": bool(destination.route_to_manus),
        "output_category": output_category, "output_folder": str(destination.folder),
        "output_files": [], "staged_files": [], "executor": None, "manus_task_id": None, "attempts": [], "last_error": None,
    }
    jobs.append(job)
    state["last_job_id"] = job_id
    state["last_requested_artifact_type"] = task_type
    state["last_artifact_folder"] = job["destination_folder"]
    _save_all(jobs, artifacts, state)
    logger.info("ARTIFACT_JOB_CREATED job_id=%s type=%s status=%s", job_id, task_type, job["status"])
    logger.info("ARTIFACT_LAST_UPDATED job_id=%s kind=%s", job_id, task_type)
    if bool(destination.route_to_manus):
        logger.info("CREATION_TASK_ROUTED_TO_MANUS kind=%s", destination.kind)
    if bool(destination.user_requested_default_folder):
        logger.info("DEFAULT_SAVE_LOCATION_USED kind=%s path=%s", destination.kind, destination.folder)
    if intent.mode == "default":
        logger.info("ARTIFACT_SAVE_MODE_AUTO_DEFAULT kind=%s", destination.kind)
    elif intent.mode == "ask_later":
        logger.info("ARTIFACT_SAVE_MODE_ASK_USER kind=%s", destination.kind)
    return job


def update_job_status(job_id, status, error=None, extra=None) -> dict:
    if status not in _ALLOWED_STATUS:
        raise ValueError(f"invalid status: {status}")
    jobs, artifacts, state = _load_all()
    for job in jobs:
        if job.get("job_id") == job_id:
            job["status"] = status
            job["updated_at"] = _now_iso()
            if error is not None:
                job["last_error"] = str(error)
            if isinstance(extra, dict):
                job.update(extra)
            _save_all(jobs, artifacts, state)
            logger.info("ARTIFACT_JOB_STATUS_UPDATED job_id=%s status=%s", job_id, status)
            return job
    raise KeyError(job_id)


def register_artifact(job_id, file_path, artifact_type, title=None, source="manus", status="completed", metadata=None) -> dict:
    jobs, artifacts, state = _load_all()
    path = Path(file_path)
    artifact = {
        "artifact_id": f"art_{uuid4().hex[:10]}", "job_id": job_id,
        "file_path": str(path), "path": str(path), "folder": str(path.parent), "filename": path.name,
        "artifact_type": str(artifact_type or "unknown"), "kind": str(artifact_type or "unknown"),
        "title": str(title or ""), "created_at": _now_iso(), "source": str(source or "manus"),
        "status": str(status or "completed"), "metadata": dict(metadata or {}),
    }
    artifacts.append(artifact)
    for job in jobs:
        if job.get("job_id") == job_id:
            files = job.setdefault("output_files", [])
            if isinstance(files, list):
                files.append(str(file_path))
            job["updated_at"] = _now_iso()
            break
    state["last_generated_file"] = str(file_path)
    if str(artifact_type).lower() == "pdf":
        state["last_generated_pdf"] = str(file_path)
    state["last_job_id"] = job_id
    state["last_artifact_folder"] = str(Path(file_path).parent)
    _save_all(jobs, artifacts, state)
    logger.info("ARTIFACT_REGISTRY_ARTIFACT_CREATED artifact_id=%s kind=%s path=%s", artifact["artifact_id"], str(artifact_type), str(file_path))
    logger.info("ARTIFACT_REGISTRY_UPDATED id=%s", artifact["artifact_id"])
    logger.info("ARTIFACT_LAST_UPDATED kind=%s path=%s", str(artifact_type), str(file_path))
    return artifact


def mark_artifact_completed_reconciled(job_id: str | None, file_path: str, kind: str) -> dict | None:
    jobs, artifacts, state = _load_all()
    target = str(Path(file_path))
    matched = None
    for artifact in reversed(artifacts):
        same_path = str(artifact.get("file_path") or artifact.get("path") or "") == target
        same_job = bool(job_id and artifact.get("job_id") == job_id)
        if same_path or same_job:
            artifact["status"] = "completed_reconciled"
            artifact["file_path"] = target
            artifact["path"] = target
            matched = artifact
            break
    if matched is None:
        matched = {
            "artifact_id": f"art_{uuid4().hex[:10]}",
            "job_id": job_id,
            "file_path": target,
            "path": target,
            "folder": str(Path(target).parent),
            "filename": Path(target).name,
            "artifact_type": str(kind or "unknown"),
            "kind": str(kind or "unknown"),
            "title": "",
            "created_at": _now_iso(),
            "source": "disk_reconciler",
            "status": "completed_reconciled",
            "metadata": {},
        }
        artifacts.append(matched)
    for job in jobs:
        if job_id and job.get("job_id") == job_id:
            job["status"] = "completed"
            job["output_files"] = [target]
            job["updated_at"] = _now_iso()
            break
    state["last_generated_file"] = target
    if str(kind or "").lower() == "pdf":
        state["last_generated_pdf"] = target
    if job_id:
        state["last_job_id"] = str(job_id)
    state["last_artifact_folder"] = str(Path(target).parent)
    _save_all(jobs, artifacts, state)
    return matched


def register_completed_artifact(job_id, kind, path, source="manus", metadata=None) -> dict:
    artifact = register_artifact(
        job_id,
        str(path),
        str(kind or "unknown"),
        title=str((metadata or {}).get("title") or ""),
        source=source,
        status="completed",
        metadata=metadata,
    )
    update_job_status(
        job_id,
        "completed",
        extra={"output_files": [str(path)], "completed_artifact_id": artifact["artifact_id"], "executor": source},
    )
    return artifact


def get_job(job_id) -> dict | None:
    jobs, _, _ = _load_all()
    for job in jobs:
        if job.get("job_id") == job_id:
            return job
    return None


def get_last_job() -> dict | None:
    jobs, _, state = _load_all()
    last_id = state.get("last_job_id")
    if last_id:
        for job in reversed(jobs):
            if job.get("job_id") == last_id:
                return job
    return jobs[-1] if jobs else None


def get_pending_destination_job() -> dict | None:
    jobs, _, _ = _load_all()
    for job in reversed(jobs):
        if str(job.get("status") or "") != "completed_waiting_destination":
            continue
        # So oferecer destino pendente se algum arquivo staged ainda existir no disco.
        if _existing_paths(list(job.get("staged_files") or [])):
            return job
    return None


def get_last_artifact(kind=None) -> dict | None:
    _, artifacts, state = _load_all()
    items = [a for a in artifacts if str(a.get("status") or "completed").lower() in {"completed", "completed_reconciled"}]
    if kind:
        kind_norm = str(kind).lower()
        items = [a for a in items if str(a.get("artifact_type", "")).lower() == kind_norm]
    if not items:
        if kind and str(kind).lower() == "pdf" and state.get("last_generated_pdf"):
            return {"job_id": state.get("last_job_id"), "file_path": state.get("last_generated_pdf"), "artifact_type": "pdf"}
        return None
    return items[-1]


def get_last_completed_artifact(kind=None) -> dict | None:
    return get_last_artifact(kind)


def _recovery_folders(kind: str) -> list[Path]:
    normalized = str(kind or "unknown").strip().lower()
    primary = get_default_folder_for_artifact(normalized)
    if normalized in _RECOVERY_EXTENSIONS:
        return [primary]
    root = Path.home() / "Documents" / "Kira"
    return [primary, root, Path.home() / "Downloads", Path.home() / "Desktop"]


def _normalize_stem(value: str) -> str:
    return "".join(char for char in str(value or "").lower() if char.isalnum())


def _recovery_candidate_score(missing: Path, candidate: Path) -> float:
    if candidate.name.lower() == missing.name.lower():
        return 1.0
    old_stem = _normalize_stem(missing.stem)
    new_stem = _normalize_stem(candidate.stem)
    if old_stem and old_stem == new_stem:
        return 0.98
    if len(old_stem) >= 8 and (old_stem in new_stem or new_stem in old_stem):
        return 0.91
    return 0.0


def _update_recovered_artifact_path(artifact: dict, new_path: Path) -> None:
    jobs, artifacts, state = _load_all()
    artifact_id = str(artifact.get("artifact_id") or "")
    old_path = str(artifact.get("file_path") or artifact.get("path") or "")
    job_id = str(artifact.get("job_id") or "")
    updated = False
    for item in artifacts:
        if (artifact_id and item.get("artifact_id") == artifact_id) or (
            not artifact_id and str(item.get("file_path") or item.get("path") or "") == old_path
        ):
            item["file_path"] = str(new_path)
            item["path"] = str(new_path)
            item["folder"] = str(new_path.parent)
            item["filename"] = new_path.name
            item["status"] = "completed_reconciled"
            metadata = item.setdefault("metadata", {})
            if isinstance(metadata, dict):
                metadata["recovered_from_path"] = old_path
                metadata["recovered_at"] = _now_iso()
            updated = True
            break
    for job in jobs:
        if job_id and str(job.get("job_id") or "") == job_id:
            job["output_files"] = [str(new_path)]
            job["updated_at"] = _now_iso()
            break
    state["last_generated_file"] = str(new_path)
    if str(artifact.get("artifact_type") or artifact.get("kind") or "").lower() == "pdf":
        state["last_generated_pdf"] = str(new_path)
    state["last_artifact_folder"] = str(new_path.parent)
    _save_all(jobs, artifacts, state)
    if updated or old_path:
        logger.info("ARTIFACT_REGISTRY_PATH_UPDATED old=%s new=%s", old_path, new_path)


def _mark_artifact_stale(artifact: dict) -> None:
    jobs, artifacts, state = _load_all()
    artifact_id = str(artifact.get("artifact_id") or "")
    old_path = str(artifact.get("file_path") or artifact.get("path") or "")
    for item in artifacts:
        if (artifact_id and item.get("artifact_id") == artifact_id) or (
            not artifact_id and str(item.get("file_path") or item.get("path") or "") == old_path
        ):
            item["status"] = "missing"
            metadata = item.setdefault("metadata", {})
            if isinstance(metadata, dict):
                metadata["missing_path"] = old_path
                metadata["missing_detected_at"] = _now_iso()
            break
    if str(state.get("last_generated_file") or "") == old_path:
        state.pop("last_generated_file", None)
    if str(state.get("last_generated_pdf") or "") == old_path:
        state.pop("last_generated_pdf", None)
    _save_all(jobs, artifacts, state)
    logger.info("ARTIFACT_REGISTRY_STALE_MARKED path=%s", old_path)


def resolve_artifact_path_on_demand(artifact: dict | None, kind: str | None = None) -> tuple[str | None, bool]:
    if not isinstance(artifact, dict):
        return None, False
    path_text = str(artifact.get("file_path") or artifact.get("path") or "").strip()
    if not path_text:
        return None, False
    path = Path(path_text)
    exists = path.exists() and path.is_file()
    logger.info("ARTIFACT_REGISTRY_RECONCILE_BEGIN")
    logger.info("ARTIFACT_PATH_CHECK path=%s exists=%s", path, str(exists).lower())
    if exists:
        logger.info("ARTIFACT_REGISTRY_RECONCILE_DONE stale=0 recovered=0")
        return str(path), False
    logger.info("ARTIFACT_REGISTRY_STALE_FOUND path=%s", path)
    normalized_kind = str(kind or artifact.get("artifact_type") or artifact.get("kind") or "unknown").lower()
    expected_extensions = _RECOVERY_EXTENSIONS.get(normalized_kind, {path.suffix.lower()} if path.suffix else set())
    logger.info("ARTIFACT_RECOVERY_SEARCH_BEGIN type=%s name=%s", normalized_kind, path.name)
    candidates: list[tuple[float, float, Path]] = []
    for folder in _recovery_folders(normalized_kind):
        if not folder.exists() or not folder.is_dir():
            continue
        for candidate in folder.iterdir():
            if not candidate.is_file() or (expected_extensions and candidate.suffix.lower() not in expected_extensions):
                continue
            score = _recovery_candidate_score(path, candidate)
            if score >= 0.9:
                logger.info("ARTIFACT_RECOVERY_CANDIDATE path=%s score=%.2f", candidate, score)
                candidates.append((score, candidate.stat().st_mtime, candidate))
    if candidates:
        _, _, recovered = max(candidates, key=lambda item: (item[0], item[1]))
        logger.info("ARTIFACT_RECOVERY_FOUND path=%s", recovered)
        _update_recovered_artifact_path(artifact, recovered)
        logger.info("ARTIFACT_REGISTRY_RECONCILE_DONE stale=1 recovered=1")
        return str(recovered), True
    logger.info("ARTIFACT_RECOVERY_NOT_FOUND")
    _mark_artifact_stale(artifact)
    logger.info("ARTIFACT_REGISTRY_RECONCILE_DONE stale=1 recovered=0")
    return None, False


def reconcile_artifact_registry(kind: str | None = None) -> dict[str, int]:
    logger.info("ARTIFACT_REGISTRY_RECONCILE_BEGIN")
    _, artifacts, _ = _load_all()
    normalized_kind = str(kind or "").strip().lower()
    items = [
        item for item in artifacts
        if str(item.get("status") or "completed").lower() in {"completed", "completed_reconciled"}
        and (not normalized_kind or str(item.get("artifact_type") or item.get("kind") or "").lower() == normalized_kind)
    ]
    stale = recovered = 0
    for item in items:
        path = Path(str(item.get("file_path") or item.get("path") or ""))
        if path.exists() and path.is_file():
            continue
        stale += 1
        if resolve_artifact_path_on_demand(item, normalized_kind or None)[0]:
            recovered += 1
    logger.info("ARTIFACT_REGISTRY_RECONCILE_DONE stale=%s recovered=%s", stale, recovered)
    return {"checked": len(items), "stale": stale, "recovered": recovered}


_RECONCILE_BOOT_DONE = False


def _parse_job_dt(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _job_age_seconds(job: dict, now: datetime) -> float:
    dt = _parse_job_dt(str(job.get("updated_at") or "")) or _parse_job_dt(str(job.get("created_at") or ""))
    if dt is None:
        return float("inf")
    return max((now - dt).total_seconds(), 0.0)


def _existing_paths(paths) -> list[str]:
    result: list[str] = []
    for item in paths or []:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            if Path(text).exists() and Path(text).is_file():
                result.append(text)
        except Exception:
            continue
    return result


def _apply_reconcile_status(job: dict, new_status: str, reason: str, now_iso: str) -> None:
    job["previous_status"] = str(job.get("status") or "")
    job["status"] = new_status
    job["reconcile_reason"] = reason
    job["reconciled_at"] = now_iso
    job["updated_at"] = now_iso


def reconcile_jobs(
    stale_after_seconds: int = 3600,
    pending_expiry_seconds: int = 86400,
    treat_running_as_orphaned: bool | None = None,
) -> dict[str, int]:
    """Reconcilia jobs presos sem apagar historico.

    - running-like antigo  -> stale_failed (rodou tempo demais / processo morto)
    - running-like recente em boot e sem worker possivel -> orphaned
    - completed_waiting_destination sem arquivo  -> missing_file
    - completed_waiting_destination antigo demais -> expired_waiting_destination
    - completed/saved com output inexistente -> missing_file
    """
    global _RECONCILE_BOOT_DONE
    if treat_running_as_orphaned is None:
        treat_running_as_orphaned = not _RECONCILE_BOOT_DONE
    _RECONCILE_BOOT_DONE = True

    logger.info(
        "MANUS_JOB_RECONCILE_BEGIN stale_after_s=%s pending_expiry_s=%s boot=%s",
        stale_after_seconds,
        pending_expiry_seconds,
        str(bool(treat_running_as_orphaned)).lower(),
    )
    jobs, artifacts, state = _load_all()
    now = datetime.now(timezone.utc)
    now_iso = _now_iso()
    counts = {
        "checked": len(jobs),
        "stale_failed": 0,
        "orphaned": 0,
        "missing_file": 0,
        "expired_waiting_destination": 0,
        "kept_running": 0,
        "kept_pending": 0,
    }
    changed = False
    for job in jobs:
        status = str(job.get("status") or "").lower()
        job_id = str(job.get("job_id") or "")
        age = _job_age_seconds(job, now)
        if status in _RUNNING_LIKE_STATUS:
            if age >= stale_after_seconds:
                _apply_reconcile_status(job, "stale_failed", f"running_for_{int(age)}s", now_iso)
                counts["stale_failed"] += 1
                changed = True
                logger.info("MANUS_JOB_RECONCILE_STALE_RUNNING job_id=%s prev=%s age_s=%s", job_id, status, int(age))
            elif treat_running_as_orphaned:
                _apply_reconcile_status(job, "orphaned", "no_active_worker_after_restart", now_iso)
                counts["orphaned"] += 1
                changed = True
                logger.info("MANUS_JOB_RECONCILE_ORPHANED job_id=%s prev=%s age_s=%s", job_id, status, int(age))
            else:
                counts["kept_running"] += 1
        elif status == "completed_waiting_destination":
            staged = list(job.get("staged_files") or [])
            existing = _existing_paths(staged)
            logger.info(
                "ARTIFACT_PENDING_DESTINATION_CHECK job_id=%s staged=%d existing=%d age_s=%s",
                job_id,
                len(staged),
                len(existing),
                int(age),
            )
            if staged and not existing:
                _apply_reconcile_status(job, "missing_file", "staged_files_missing", now_iso)
                counts["missing_file"] += 1
                changed = True
                logger.info("ARTIFACT_PENDING_DESTINATION_MISSING_FILE job_id=%s", job_id)
                logger.info("MANUS_JOB_RECONCILE_MISSING_FILE job_id=%s prev=%s", job_id, status)
            elif age >= pending_expiry_seconds:
                _apply_reconcile_status(job, "expired_waiting_destination", f"pending_for_{int(age)}s", now_iso)
                counts["expired_waiting_destination"] += 1
                changed = True
                logger.info("ARTIFACT_PENDING_DESTINATION_EXPIRED job_id=%s age_s=%s", job_id, int(age))
            else:
                counts["kept_pending"] += 1
                logger.info("ARTIFACT_PENDING_DESTINATION_OK job_id=%s existing=%d", job_id, len(existing))
        elif status in {"completed", "completed_saved", "completed_reconciled"}:
            files = list(job.get("output_files") or [])
            if files and not _existing_paths(files):
                _apply_reconcile_status(job, "missing_file", "output_files_missing", now_iso)
                counts["missing_file"] += 1
                changed = True
                logger.info("MANUS_JOB_RECONCILE_MISSING_FILE job_id=%s prev=%s", job_id, status)
    if changed:
        _save_all(jobs, artifacts, state)
    logger.info(
        "MANUS_JOB_RECONCILE_DONE checked=%s stale_failed=%s orphaned=%s missing_file=%s expired=%s kept_running=%s kept_pending=%s",
        counts["checked"],
        counts["stale_failed"],
        counts["orphaned"],
        counts["missing_file"],
        counts["expired_waiting_destination"],
        counts["kept_running"],
        counts["kept_pending"],
    )
    return counts


def list_recent_jobs(limit=10) -> list[dict]:
    jobs, _, _ = _load_all()
    return list(reversed(jobs))[: int(limit)]


def list_recent_artifacts(limit=10, kind=None) -> list[dict]:
    _, artifacts, _ = _load_all()
    items = artifacts
    if kind:
        kind_norm = str(kind).lower()
        items = [a for a in artifacts if str(a.get("artifact_type", "")).lower() == kind_norm]
    return list(reversed(items))[: int(limit)]


def get_artifact_by_id(artifact_id: str) -> dict | None:
    _, artifacts, _ = _load_all()
    for a in artifacts:
        if str(a.get("artifact_id") or "") == str(artifact_id or ""):
            return a
    return None


def is_registered_artifact_path(path: str) -> bool:
    _, artifacts, _ = _load_all()
    target = str(path or "").strip().lower()
    return any(str(a.get("file_path") or "").strip().lower() == target for a in artifacts)


def get_jobs_summary(limit: int = 10) -> dict:
    jobs = list_recent_jobs(limit=limit)
    running = [j for j in jobs if str(j.get("status") or "").lower() in _RUNNING_LIKE_STATUS]
    return {
        "running_jobs": len(running),
        "recent_jobs": jobs,
        "last_artifact": get_last_artifact(),
    }

def resolve_artifact_path_from_kind(kind: str | None) -> str | None:
    target = str(kind or "").strip().lower()
    if target in {"", "any"}:
        last = get_last_artifact()
        return str((last or {}).get("file_path") or "") or None
    last = get_last_artifact(kind=target)
    if last and last.get("file_path"):
        return str(last.get("file_path"))
    return None


def resolve_default_artifact_folder(kind: str | None) -> str:
    return str(get_default_folder_for_artifact(kind))


def save_job_task_spec(job_id: str, task_spec: dict) -> dict:
    jobs, artifacts, state = _load_all()
    for job in jobs:
        if job.get("job_id") == job_id:
            job["task_spec"] = dict(task_spec or {})
            job["prompt_sent"] = str((task_spec or {}).get("prompt") or "")
            job["updated_at"] = _now_iso()
            _save_all(jobs, artifacts, state)
            logger.info("MANUS_TASK_SPEC_SAVED job_id=%s", job_id)
            return job
    raise KeyError(job_id)


def update_last_generated(kind: str, file_path: str, job_id: str | None = None) -> None:
    jobs, artifacts, state = _load_all()
    state["last_generated_file"] = str(file_path)
    if str(kind).lower() == "pdf":
        state["last_generated_pdf"] = str(file_path)
    if job_id:
        state["last_job_id"] = str(job_id)
    state["last_artifact_folder"] = str(Path(file_path).parent)
    _save_all(jobs, artifacts, state)
