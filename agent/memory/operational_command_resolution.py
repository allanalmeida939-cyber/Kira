from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .local_memory_store import LocalMemoryStore, get_local_memory_store


_EXT_RE = re.compile(r"\.(txt|bat|cmd|py|html|css|js|json|md|csv|png|jpg|jpeg|webp|exe)\b", re.IGNORECASE)
_INDIRECT_MARKERS = (
    "ultimo",
    "último",
    "last",
    "aquele",
    "aquilo",
    "isso",
    "antes",
    "hoje",
    "sessao",
    "sessão",
    "agora ha pouco",
    "agora há pouco",
    "recente",
    "recentemente",
    "você",
    "voce",
    "print",
    "screenshot",
)


@dataclass(frozen=True)
class OperationalCommandResolution:
    status: str
    path: str = ""
    reason: str = ""
    candidates: tuple[str, ...] = ()


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _is_today(ts: float) -> bool:
    if not ts:
        return False
    return time.localtime(ts).tm_yday == time.localtime().tm_yday and time.localtime(ts).tm_year == time.localtime().tm_year


def _extract_extension(text: str) -> str:
    m = _EXT_RE.search(text or "")
    if not m:
        return ""
    return f".{m.group(1).lower()}"


def _requires_indirect_resolution(text: str) -> bool:
    lowered = _normalize_text(text)
    if not lowered:
        return False
    if re.match(r"^[a-z]:\\", lowered):
        return False
    if any(marker in lowered for marker in _INDIRECT_MARKERS):
        return True
    return bool(_extract_extension(lowered) and ("arquivo" in lowered or "file" in lowered))


def _wants_folder_from_file(text: str) -> bool:
    lowered = _normalize_text(text)
    return any(
        token in lowered
        for token in (
            "pasta desse arquivo",
            "pasta deste arquivo",
            "pasta onde",
            "onde voce salvou",
            "onde você salvou",
        )
    )


def _allow_ambiguity(text: str, *, extension: str) -> bool:
    lowered = _normalize_text(text)
    if any(token in lowered for token in ("aquele", "aquilo", "desse", "desse arquivo", "daquele")):
        return True
    is_edit_like = any(token in lowered for token in ("edita", "altera", "renomeia", "apaga", "deleta", "move"))
    if is_edit_like and bool(extension):
        return True
    return False


def _infer_action_filter(text: str) -> str:
    lowered = _normalize_text(text)
    if any(token in lowered for token in ("criou", "gerou", "criado", "created")):
        return "created"
    if any(token in lowered for token in ("editou", "mexeu", "alterou", "edited")):
        return "edited"
    if any(token in lowered for token in ("abriu", "opened")):
        return "opened"
    if any(token in lowered for token in ("print", "screenshot")):
        return "screenshot"
    return ""


def _infer_time_scope(text: str) -> str:
    lowered = _normalize_text(text)
    if "hoje" in lowered:
        return "today"
    if "ultima sessao" in lowered or "última sessão" in lowered or "nessa sessao" in lowered or "nessa sessão" in lowered:
        return "session"
    if "agora ha pouco" in lowered or "agora há pouco" in lowered:
        return "short_recent"
    if "recentemente" in lowered:
        return "recent"
    return ""


def _score_file(item: dict[str, Any], *, action_filter: str, time_scope: str) -> float:
    score = float(item.get("updated_at", 0.0) or 0.0)
    action = str(item.get("last_action") or "").lower()
    if action_filter:
        if action_filter == "screenshot" and "screenshot" in action:
            score += 10_000
        elif action_filter in action:
            score += 10_000
    if time_scope == "today":
        if _is_today(float(item.get("updated_at", 0.0) or 0.0)) or _is_today(float(item.get("created_at", 0.0) or 0.0)):
            score += 5_000
        else:
            score -= 5_000
    if time_scope == "short_recent":
        age = max(0.0, time.time() - float(item.get("updated_at", 0.0) or 0.0))
        score += max(0.0, 2_000 - age)
    return score


def resolve_operational_memory_target(
    text: str,
    *,
    expected_type: str | None = None,
    store: LocalMemoryStore | None = None,
) -> OperationalCommandResolution:
    raw = str(text or "").strip()
    if not _requires_indirect_resolution(raw):
        return OperationalCommandResolution(status="skip", reason="not_indirect")

    mem = store or get_local_memory_store()
    lowered = _normalize_text(raw)
    extension = _extract_extension(lowered)
    action_filter = _infer_action_filter(lowered)
    time_scope = _infer_time_scope(lowered)
    want_folder = expected_type == "folder" or _wants_folder_from_file(lowered) or "pasta" in lowered

    if "print" in lowered or "screenshot" in lowered:
        extension = extension or ".png"
        action_filter = action_filter or "screenshot"

    if want_folder:
        if _wants_folder_from_file(lowered):
            last_file = mem.get_last_active_file(extension=extension or None)
            if last_file and last_file.get("path"):
                parent = Path(str(last_file["path"])).parent
                if parent.exists():
                    return OperationalCommandResolution(status="resolved", path=str(parent), reason="parent_from_last_file")
        folders = mem.list_active_folders(limit=20)
        if not folders:
            return OperationalCommandResolution(status="not_found", reason="no_active_folders")
        folders.sort(key=lambda item: float(item.get("updated_at", 0.0) or 0.0), reverse=True)
        return OperationalCommandResolution(status="resolved", path=str(folders[0].get("path") or ""), reason="last_folder")

    session_paths: list[str] = []
    if time_scope == "session":
        summary = mem.get_last_session_summary()
        session_paths = [str(p) for p in (summary.get("recent_files") or []) if p]

    candidates: list[dict[str, Any]] = []
    if session_paths:
        for raw_path in session_paths:
            p = Path(raw_path)
            if p.exists() and p.is_file():
                candidates.append(
                    {
                        "path": str(p),
                        "name": p.name,
                        "extension": p.suffix.lower(),
                        "updated_at": p.stat().st_mtime,
                        "created_at": p.stat().st_ctime,
                        "last_action": "session_recent",
                    }
                )
    else:
        candidates = mem.list_active_files(extension=extension or None, limit=120)

    if extension:
        candidates = [item for item in candidates if str(item.get("extension", "")).lower() == extension]

    if time_scope == "today":
        candidates = [
            item for item in candidates
            if _is_today(float(item.get("updated_at", 0.0) or 0.0)) or _is_today(float(item.get("created_at", 0.0) or 0.0))
        ]

    if action_filter:
        narrowed = [item for item in candidates if action_filter in str(item.get("last_action", "")).lower()]
        if narrowed:
            candidates = narrowed

    if not candidates:
        deleted = mem.list_deleted_items(item_type="file", extension=extension or None, limit=40)
        if deleted:
            return OperationalCommandResolution(
                status="deleted",
                reason="best_match_deleted",
                path=str(deleted[0].get("path") or ""),
            )
        return OperationalCommandResolution(status="not_found", reason="no_candidate")

    scored = sorted(
        candidates,
        key=lambda item: _score_file(item, action_filter=action_filter, time_scope=time_scope),
        reverse=True,
    )

    first = scored[0]
    first_path = Path(str(first.get("path") or ""))
    if not first_path.exists():
        return OperationalCommandResolution(status="not_found", reason="top_candidate_missing")

    if len(scored) >= 2 and _allow_ambiguity(lowered, extension=extension):
        second = scored[1]
        top_score = _score_file(first, action_filter=action_filter, time_scope=time_scope)
        second_score = _score_file(second, action_filter=action_filter, time_scope=time_scope)
        if abs(top_score - second_score) < 30.0:
            options: list[str] = []
            for item in scored[:3]:
                path = str(item.get("path") or "")
                if path:
                    options.append(path)
            return OperationalCommandResolution(
                status="ambiguous",
                reason="multiple_close_candidates",
                candidates=tuple(options),
            )

    return OperationalCommandResolution(status="resolved", path=str(first_path), reason="best_candidate")
