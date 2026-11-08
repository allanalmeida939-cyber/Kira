from __future__ import annotations

from pathlib import Path
import shutil
import tempfile

import logging

try:
    from services.artifact_destination_manager import resolve_artifact_destination
except Exception:  # pragma: no cover - subprocesso agent usa alias services local
    import importlib.util as _dest_import
    import sys as _dest_sys
    _dest_path = Path(__file__).with_name("artifact_destination_manager.py")
    _dest_spec = _dest_import.spec_from_file_location("services.artifact_destination_manager", str(_dest_path))
    assert _dest_spec and _dest_spec.loader
    _dest_mod = _dest_import.module_from_spec(_dest_spec)
    _dest_sys.modules["services.artifact_destination_manager"] = _dest_mod
    _dest_spec.loader.exec_module(_dest_mod)
    resolve_artifact_destination = _dest_mod.resolve_artifact_destination

logger = logging.getLogger(__name__)

def destination_for_artifact(task_type, filename) -> Path:
    destination = resolve_artifact_destination(task_type)
    return destination.folder / filename


def save_downloaded_file(temp_path, task_type, title_hint, destination_folder=None) -> Path:
    src = Path(temp_path)
    ext = src.suffix or ".bin"
    destination = resolve_artifact_destination(
        task_type,
        title=title_hint or src.stem,
        extension=ext,
    )
    dst = destination.path if destination_folder is None else Path(destination_folder) / str(destination.filename)
    assert dst is not None
    logger.info("ARTIFACT_SAVE_DESTINATION_RESOLVED kind=%s folder=%s", destination.kind, destination.folder)
    logger.info("ARTIFACT_DESTINATION_RESOLVED kind=%s path=%s", destination.kind, dst.parent)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(dst))
    except Exception as exc:
        logger.warning("ARTIFACT_SAVE_FAILED reason=%s", exc)
        raise
    logger.info("ARTIFACT_FILE_SAVED path=%s", dst)
    logger.info("ARTIFACT_SAVED_OK path=%s", dst)
    if destination_folder is None:
        logger.info("ARTIFACT_SAVED_TO_DEFAULT_FOLDER kind=%s path=%s", destination.kind, dst)
    return dst


def stage_downloaded_file(temp_path, task_type, title_hint, job_id) -> Path:
    src = Path(temp_path)
    ext = src.suffix or ".bin"
    destination = resolve_artifact_destination(task_type, title=title_hint or src.stem, extension=ext)
    staged_dir = Path(tempfile.gettempdir()) / "Kira" / "staged_artifacts" / str(job_id)
    staged_dir.mkdir(parents=True, exist_ok=True)
    dst = staged_dir / str(destination.filename or src.name)
    shutil.move(str(src), str(dst))
    logger.info("ARTIFACT_STAGED_PENDING_DESTINATION job_id=%s path=%s", job_id, dst)
    return dst


def copy_to_artifact_folder(source_path, task_type, title_hint) -> Path:
    src = Path(source_path)
    ext = src.suffix or ".bin"
    destination = resolve_artifact_destination(
        task_type,
        title=title_hint or src.stem,
        extension=ext,
    )
    dst = destination.path
    assert dst is not None
    logger.info("ARTIFACT_SAVE_DESTINATION_RESOLVED kind=%s folder=%s", destination.kind, destination.folder)
    logger.info("ARTIFACT_DESTINATION_RESOLVED kind=%s path=%s", destination.kind, dst.parent)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(str(src), str(dst))
    except Exception as exc:
        logger.warning("ARTIFACT_SAVE_FAILED reason=%s", exc)
        raise
    logger.info("ARTIFACT_FILE_SAVED path=%s", dst)
    logger.info("ARTIFACT_SAVED_OK path=%s", dst)
    return dst
