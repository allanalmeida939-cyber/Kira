from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path

from services.artifact_registry import (
    get_artifact_by_id,
    get_job,
    get_last_artifact,
    list_recent_artifacts,
    mark_artifact_completed_reconciled,
)

logger = logging.getLogger(__name__)

_VALID_EXTENSIONS = {
    "pdf": {".pdf"},
    "slides": {".pptx", ".pdf"},
    "slide": {".pptx", ".pdf"},
    "prompt": {".txt", ".md"},
    "text": {".txt", ".md"},
}


@dataclass(frozen=True)
class ArtifactState:
    exists_on_disk: bool
    final_path: str
    kind: str
    extension: str
    size_bytes: int
    registry_status: str
    job_status: str
    saved: bool
    should_ask_save_location: bool
    user_message: str


def _select_artifact(job_id: str | None, artifact_id: str | None, kind: str | None) -> dict | None:
    if artifact_id:
        return get_artifact_by_id(artifact_id)
    if job_id:
        for item in list_recent_artifacts(limit=100, kind=kind):
            if str(item.get("job_id") or "") == str(job_id):
                return item
        job = get_job(job_id) or {}
        files = list(job.get("output_files") or [])
        if files:
            return {"job_id": job_id, "file_path": str(files[-1]), "artifact_type": kind or job.get("task_type"), "status": job.get("status")}
    return get_last_artifact(kind)


def reconcile_artifact_state(job_id: str | None = None, artifact_id: str | None = None, kind: str | None = None) -> ArtifactState:
    normalized_kind = str(kind or "").strip().lower()
    logger.info("ARTIFACT_STATE_RECONCILE_BEGIN job_id=%s kind=%s", job_id or "none", normalized_kind or "any")
    artifact = _select_artifact(job_id, artifact_id, normalized_kind or None) or {}
    job = get_job(job_id) if job_id else None
    if not normalized_kind:
        normalized_kind = str(artifact.get("artifact_type") or artifact.get("kind") or (job or {}).get("task_type") or "any").lower()
    path = Path(str(artifact.get("file_path") or artifact.get("path") or "").strip()) if artifact else Path()
    path_text = str(path) if str(path) not in {"", "."} else ""
    extension = path.suffix.lower() if path_text else ""
    exists = bool(path_text and path.exists() and path.is_file())
    size = path.stat().st_size if exists else 0
    valid_ext = extension in _VALID_EXTENSIONS.get(normalized_kind, {extension}) if extension else False
    saved = bool(exists and size > 0 and valid_ext)
    registry_status = str(artifact.get("status") or "")
    job_status = str((job or {}).get("status") or "")
    if exists:
        logger.info("ARTIFACT_STATE_FILE_EXISTS path=%s size=%s", path_text, size)
    if saved and (registry_status not in {"completed", "completed_reconciled"} or job_status not in {"", "completed"}):
        old_status = job_status or registry_status or "unknown"
        mark_artifact_completed_reconciled(job_id or artifact.get("job_id"), path_text, normalized_kind)
        logger.info("ARTIFACT_STATE_RECONCILED_FROM_DISK old_status=%s new_status=completed", old_status)
        registry_status = "completed_reconciled"
        job_status = "completed"
    if saved:
        logger.info("ARTIFACT_STATE_SAVE_CONFIRMED path=%s", path_text)
        logger.info("ARTIFACT_STATE_SAVE_LOCATION_NOT_NEEDED reason=default_folder")
        logger.info("ARTIFACT_STATE_ASK_SAVE_LOCATION_SKIPPED reason=already_saved")
        label = "PDF" if normalized_kind == "pdf" else "Arquivo"
        message = f"{label} pronto e salvo na pasta padrao de {('PDFs' if normalized_kind == 'pdf' else normalized_kind)}."
    else:
        message = "O arquivo ainda nao foi salvo com sucesso."
    return ArtifactState(
        exists_on_disk=exists,
        final_path=path_text,
        kind=normalized_kind,
        extension=extension,
        size_bytes=size,
        registry_status=registry_status,
        job_status=job_status,
        saved=saved,
        should_ask_save_location=False if saved else False,
        user_message=message,
    )
