from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional
import logging
import time

logger = logging.getLogger("kira.subprocess_health")


@dataclass
class ProcessHealthSnapshot:
    name: str
    pid: int | None
    status: str
    exit_code: int | None
    alive: bool
    crashed: bool
    checked_at: float
    detail: str = ""


def _coerce_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def inspect_process(name: str, process: Any) -> ProcessHealthSnapshot:
    checked_at = time.time()
    safe_name = str(name or "unknown").strip() or "unknown"

    if process is None:
        return ProcessHealthSnapshot(
            name=safe_name,
            pid=None,
            status="missing",
            exit_code=None,
            alive=False,
            crashed=False,
            checked_at=checked_at,
            detail="process_object_missing",
        )

    try:
        pid = _coerce_optional_int(getattr(process, "pid", None))
    except Exception as exc:
        return ProcessHealthSnapshot(
            name=safe_name,
            pid=None,
            status="unknown",
            exit_code=None,
            alive=False,
            crashed=False,
            checked_at=checked_at,
            detail=f"pid_error:{exc.__class__.__name__}",
        )

    try:
        poll = getattr(process, "poll", None)
        if not callable(poll):
            returncode = _coerce_optional_int(getattr(process, "returncode", None))
            return ProcessHealthSnapshot(
                name=safe_name,
                pid=pid,
                status="unknown",
                exit_code=returncode,
                alive=False,
                crashed=False,
                checked_at=checked_at,
                detail="poll_unavailable",
            )
        poll_result = poll()
    except Exception as exc:
        try:
            returncode = _coerce_optional_int(getattr(process, "returncode", None))
        except Exception:
            returncode = None
        return ProcessHealthSnapshot(
            name=safe_name,
            pid=pid,
            status="unknown",
            exit_code=returncode,
            alive=False,
            crashed=False,
            checked_at=checked_at,
            detail=f"poll_error:{exc.__class__.__name__}",
        )

    if poll_result is None:
        return ProcessHealthSnapshot(
            name=safe_name,
            pid=pid,
            status="running",
            exit_code=None,
            alive=True,
            crashed=False,
            checked_at=checked_at,
            detail="poll_running",
        )

    exit_code = _coerce_optional_int(poll_result)
    crashed = exit_code not in (None, 0)
    return ProcessHealthSnapshot(
        name=safe_name,
        pid=pid,
        status="exited",
        exit_code=exit_code,
        alive=False,
        crashed=crashed,
        checked_at=checked_at,
        detail="poll_exited",
    )


def snapshot_to_dict(snapshot: ProcessHealthSnapshot) -> dict:
    return asdict(snapshot)


def inspect_processes(process_map: dict[str, Any]) -> dict:
    result: dict[str, dict] = {}
    try:
        items = dict(process_map or {}).items()
    except Exception:
        items = []

    for name, process in items:
        snapshot = inspect_process(name, process)
        logger.info(
            "BLOCO7_PROCESS_HEALTH_CHECK name=%s status=%s pid=%s",
            snapshot.name,
            snapshot.status,
            snapshot.pid,
        )
        if snapshot.crashed:
            logger.error(
                "BLOCO7_PROCESS_CRASH_DETECTED name=%s pid=%s exit_code=%s",
                snapshot.name,
                snapshot.pid,
                snapshot.exit_code,
            )
        result[snapshot.name] = snapshot_to_dict(snapshot)

    return result
