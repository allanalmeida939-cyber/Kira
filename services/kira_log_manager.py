from __future__ import annotations

import json
import logging
import os
import re
import shutil
import traceback
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

_CATEGORY_CONFIG = {
    "backend": ("Backend", "backend_events"),
    "kira": ("Kira", "kira_session"),
    "voice": ("Kira", "kira_session"),
    "gemini": ("Kira", "kira_session"),
    "commands": ("Kira", "kira_commands"),
    "manus": ("Manus", "manus"),
    "artifacts": ("Artifacts", "artifacts"),
    "messages": ("Messages", "messages"),
    "errors": ("Errors", "errors"),
}

_ROTATION_RULES = {
    "Backend": ["backend_*.log", "backend_output_*.log", "boot_*.log", "backend_*.jsonl"],
    "Kira": ["kira_session_*.log", "kira_*.log", "kira_voice_*.log", "kira_gemini_*.log", "kira_commands_*.jsonl", "kira_turns_*.jsonl"],
    "Manus": ["manus_*.log", "manus_jobs_*.jsonl", "manus_api_*.jsonl", "manus_errors_*.log"],
    "Artifacts": ["artifacts_*.log", "files_*.jsonl", "artifacts_events_*.jsonl"],
    "Messages": ["messages_*.log", "messages_sends_*.jsonl", "messages_errors_*.log"],
    "Errors": ["errors_*.log", "tracebacks_*.log"],
}

_ROOT_PATTERNS = ["kira_backend*.log", "kira_backend_output*.log", "kira_console*.log", "internet_guard*.log", "diagnostico*.log", "kira_test_output*.log"]
_RUN_ID: str | None = None
_RUN_ID_REUSE_LOGGED = False
_INITIALIZED_ROOTS: set[str] = set()
_PATH_CACHE: dict[tuple[str, str, bool, str], Path] = {}
_REUSE_LOGGED: set[tuple[str, str]] = set()


def _env_bool(name: str, default: bool) -> bool:
    val = str(os.getenv(name, str(default))).strip().lower()
    return val in {"1", "true", "yes", "on"}


def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def get_log_root() -> Path:
    env_root = str(os.getenv("KIRA_LOG_ROOT", "")).strip()
    if env_root:
        return Path(env_root)
    return get_project_root() / "logs"


def _keep_recent() -> int:
    try:
        value = os.getenv("KIRA_LOG_KEEP_RECENT_SESSIONS", os.getenv("KIRA_LOG_KEEP_RECENT", "1"))
        return max(1, int(str(value).strip()))
    except Exception:
        return 1


def ensure_log_directories() -> dict[str, Path]:
    root = get_log_root()
    folders = {"backend": root / "Backend", "kira": root / "Kira", "manus": root / "Manus", "artifacts": root / "Artifacts", "messages": root / "Messages", "errors": root / "Errors"}
    root.mkdir(parents=True, exist_ok=True)
    for p in folders.values():
        p.mkdir(parents=True, exist_ok=True)
    return folders


def redact_sensitive(value: Any) -> Any:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    patterns = [r"(?i)(authorization\s*[:=]\s*bearer\s+)[a-z0-9._\-]+", r"(?i)(manus_api_key\s*[:=]\s*)[a-z0-9_\-]+", r"(?i)(openai_api_key\s*[:=]\s*)[a-z0-9_\-]+", r"(?i)(gemini_api_key\s*[:=]\s*)[a-z0-9_\-]+", r"(?i)(livekit_api_secret\s*[:=]\s*)[a-z0-9_\-]+", r"(?i)(cookie\s*[:=]\s*)[^\s,;]+", r"\b[A-Za-z0-9_\-]{24,}\b"]
    for pat in patterns:
        text = re.sub(pat, r"\1***REDACTED***" if "(" in pat else "***REDACTED***", text)
    text = re.sub(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", r"\1***@\2", text)
    if isinstance(value, str):
        return text
    try:
        return json.loads(text)
    except Exception:
        return text


def _ts_slug() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")


def get_log_run_id() -> str:
    global _RUN_ID, _RUN_ID_REUSE_LOGGED
    configured = str(os.getenv("KIRA_LOG_RUN_ID", "")).strip()
    if configured:
        if _RUN_ID != configured:
            _RUN_ID = configured
        if not _RUN_ID_REUSE_LOGGED:
            logging.getLogger(__name__).info("LOG_RUN_ID_REUSED id=%s", configured)
            _RUN_ID_REUSE_LOGGED = True
        return configured
    if _RUN_ID is None:
        _RUN_ID = _ts_slug()
        os.environ["KIRA_LOG_RUN_ID"] = _RUN_ID
        logging.getLogger(__name__).info("LOG_RUN_ID_SET id=%s", _RUN_ID)
    return _RUN_ID


def _jsonl_enabled() -> bool:
    return _env_bool("KIRA_LOG_JSONL", True)


def _log_file_for(category: str, is_jsonl: bool) -> Path:
    folder_name, prefix = _CATEGORY_CONFIG.get(category, ("Kira", "kira_session"))
    folders = ensure_log_directories()
    folder = folders[folder_name.lower() if folder_name.lower() in folders else "kira"]
    ext = "jsonl" if is_jsonl else "log"
    run_id = get_log_run_id()
    cache_key = (str(get_log_root().resolve()), str(category), bool(is_jsonl), run_id)
    existing = _PATH_CACHE.get(cache_key)
    if existing is not None:
        reuse_key = (str(category), ext)
        if reuse_key not in _REUSE_LOGGED:
            logging.getLogger(__name__).info("LOG_FILE_REUSED category=%s path=%s", category, existing)
            logging.getLogger(__name__).info("LOG_HANDLER_REUSED category=%s", category)
            _REUSE_LOGGED.add(reuse_key)
        return existing
    path = folder / f"{prefix}_{run_id}.{ext}"
    _PATH_CACHE[cache_key] = path
    return path


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def log_event(category, event, data=None, level="INFO"):
    try:
        initialize_log_session()
        safe_data = redact_sensitive(data if data is not None else {}) if _env_bool("KIRA_LOG_REDACT_SECRETS", True) else (data if data is not None else {})
        payload = {"ts": datetime.now().isoformat(timespec="milliseconds"), "level": str(level).upper(), "category": str(category), "event": str(event), "session_id": None, "turn_id": None, "job_id": None, "request_id": None, "data": safe_data}
        human = f"[{payload['ts']}] [{payload['level']}] [{payload['category']}] {payload['event']} data={json.dumps(safe_data, ensure_ascii=False)}"
        _append_line(_log_file_for(str(category), False), human)
        if _jsonl_enabled():
            _append_line(_log_file_for(str(category), True), json.dumps(payload, ensure_ascii=False))
    except Exception:
        logging.getLogger(__name__).warning("KIRA_LOG_EVENT_FAILED category=%s event=%s", category, event)


def log_backend_event(event, data=None): log_event("backend", event, data)
def log_kira_event(event, data=None): log_event("kira", event, data)
def log_voice_event(event, data=None): log_event("voice", event, data)
def log_gemini_event(event, data=None): log_event("gemini", event, data)
def log_command_event(event, data=None): log_event("commands", event, data)
def log_manus_event(event, data=None): log_event("manus", event, data)
def log_artifact_event(event, data=None): log_event("artifacts", event, data)
def log_messages_event(event, data=None): log_event("messages", event, data)

def log_error_event(event, error=None, data=None):
    merged = dict(data or {})
    if error is not None:
        merged["error"] = str(error)
        merged["traceback"] = traceback.format_exc()
    log_event("errors", event, merged, level="ERROR")


def _trim_pattern(folder: Path, pattern: str, keep_recent: int) -> int:
    files = sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for p in files[keep_recent:]:
        p.unlink(missing_ok=True)
        logging.getLogger(__name__).info("LOG_ROTATION_REMOVED file=%s", p)
        removed += 1
    return removed


def _session_raw_files(root: Path, run_id: str) -> list[Path]:
    candidates: list[Path] = []
    patterns = {
        "Kira": ("kira_session_*.log", "kira_session_*.jsonl", "kira_commands_*.log", "kira_commands_*.jsonl", "kira_voice_*.log", "kira_gemini_*.log"),
        "Artifacts": ("artifacts_*.log", "artifacts_*.jsonl", "artifacts_events_*.jsonl"),
        "Manus": ("manus_*.log", "manus_*.jsonl", "manus_jobs_*.jsonl"),
    }
    for folder_name, folder_patterns in patterns.items():
        folder = root / folder_name
        for pattern in folder_patterns:
            candidates.extend(p for p in folder.glob(pattern) if f"_{run_id}." not in p.name)
    return sorted({p.resolve() for p in candidates if p.is_file()}, key=lambda p: p.stat().st_mtime)


def archive_old_sessions(*, keep_recent: int | None = None) -> dict[str, object]:
    root = get_log_root()
    archive_dir = root / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    run_id = get_log_run_id()
    candidates = _session_raw_files(root, run_id)
    if not candidates:
        return {"archive": None, "removed": 0}
    logging.getLogger(__name__).info("LOG_ARCHIVE_BEGIN raw_count=%s", len(candidates))
    archive_slug = datetime.fromtimestamp(max(p.stat().st_mtime for p in candidates)).strftime("%Y-%m-%d_%H-%M-%S")
    archive_path = archive_dir / f"kira_logs_{archive_slug}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in candidates:
            bundle.write(path, arcname=str(path.relative_to(root)))
    logging.getLogger(__name__).info("LOG_ARCHIVE_CREATED path=%s", archive_path)
    removed = 0
    for path in candidates:
        path.unlink(missing_ok=True)
        removed += 1
    logging.getLogger(__name__).info("LOG_OLD_RAW_REMOVED count=%s", removed)
    keep = int(keep_recent) if keep_recent is not None else _keep_recent()
    archives = sorted(archive_dir.glob("kira_logs_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in archives[keep:]:
        stale.unlink(missing_ok=True)
    logging.getLogger(__name__).info("LOG_ARCHIVE_ROTATION_DONE keep=%s", keep)
    return {"archive": str(archive_path), "removed": removed}


def retain_latest_backend_session(*, keep_sessions: int = 1) -> dict[str, object]:
    root = get_log_root()
    backend_dir = root / "Backend"
    archive_dir = root / "archives"
    backend_dir.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    run_id = get_log_run_id()
    logger = logging.getLogger(__name__)
    logger.info("BACKEND_LOG_RETENTION_BEGIN keep_sessions=%s", keep_sessions)
    logger.info("BACKEND_LOG_CURRENT_RUN_DETECTED run_id=%s", run_id)
    patterns = ("backend_*.log", "backend_output_*.log", "backend_*.jsonl", "backend_events_*.log", "backend_events_*.jsonl")
    latest_output = max(backend_dir.glob("backend_output_*.log"), key=lambda p: p.stat().st_mtime, default=None)
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(
            path for path in backend_dir.glob(pattern)
            if run_id not in path.name and (latest_output is None or path.resolve() != latest_output.resolve())
        )
    candidates = sorted({path.resolve() for path in candidates if path.is_file()})
    if not candidates:
        logger.info("BACKEND_LOG_RETENTION_DONE removed=0")
        return {"archive": None, "removed": 0}
    archive_path = archive_dir / f"backend_logs_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in candidates:
            bundle.write(path, arcname=str(path.relative_to(root)))
    logger.info("BACKEND_LOG_ARCHIVE_CREATED path=%s", archive_path)
    for path in candidates:
        path.unlink(missing_ok=True)
    logger.info("BACKEND_LOG_OLD_REMOVED count=%s", len(candidates))
    archives = sorted(archive_dir.glob("backend_logs_*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in archives[max(int(keep_sessions), 1):]:
        stale.unlink(missing_ok=True)
    logger.info("BACKEND_LOG_RETENTION_DONE removed=%s", len(candidates))
    return {"archive": str(archive_path), "removed": len(candidates)}


def initialize_log_session() -> str:
    root_key = str(get_log_root().resolve())
    run_id = get_log_run_id()
    if root_key in _INITIALIZED_ROOTS:
        return run_id
    ensure_log_directories()
    write_readme()
    if _env_bool("KIRA_LOG_COMPRESS_OLD", True):
        archive_old_sessions()
        retain_latest_backend_session(keep_sessions=1)
    _INITIALIZED_ROOTS.add(root_key)
    return run_id


def reset_log_session_for_tests(run_id: str | None = None) -> str:
    global _RUN_ID, _RUN_ID_REUSE_LOGGED
    _RUN_ID = str(run_id or "").strip() or None
    _RUN_ID_REUSE_LOGGED = False
    _INITIALIZED_ROOTS.clear()
    _PATH_CACHE.clear()
    _REUSE_LOGGED.clear()
    if _RUN_ID is None:
        os.environ.pop("KIRA_LOG_RUN_ID", None)
    else:
        os.environ["KIRA_LOG_RUN_ID"] = _RUN_ID
    return get_log_run_id()


def rotate_logs(category=None, keep_recent=None):
    keep = int(keep_recent) if keep_recent is not None else _keep_recent()
    logging.getLogger(__name__).info("LOG_ROTATION_BEGIN keep_recent=%s", keep)
    root = get_log_root()
    removed = 0
    target_rules = _ROTATION_RULES if category is None else {k: v for k, v in _ROTATION_RULES.items() if k.lower() == str(category).lower()}
    for folder_name, patterns in target_rules.items():
        folder = root / folder_name
        if not folder.exists():
            continue
        before_removed = removed
        for pat in patterns:
            removed += _trim_pattern(folder, pat, keep)
        logging.getLogger(__name__).info(
            "LOG_ROTATION_CATEGORY category=%s keep_recent=%s removed=%s",
            folder_name,
            keep,
            removed - before_removed,
        )
    project_root = get_project_root()
    for pat in _ROOT_PATTERNS:
        removed += _trim_pattern(project_root, pat, keep)
    legacy_wa = project_root / "logs_messages"
    if legacy_wa.exists():
        removed += _trim_pattern(legacy_wa, "messages_*.log", keep)
    logging.getLogger(__name__).info("LOG_ROTATION_DONE removed=%s", removed)
    return removed


def cleanup_legacy_logs(dry_run=True):
    root = get_project_root().resolve()
    log_root = get_log_root().resolve()
    allowed = [log_root, (root / "logs_messages").resolve()]
    allowed += [p.resolve() for p in root.glob("logs_backup_*") if p.is_dir()]
    preserve_prefix = ["restore_points", "backups", "configs", "messages", "venv", ".venv", "agent"]
    deleted = []
    preserved = []

    def _safe_under(path: Path) -> bool:
        try:
            rp = path.resolve()
            return any(str(rp).startswith(str(a)) for a in allowed) or rp.parent == root
        except Exception:
            return False

    candidates = []
    for a in allowed:
        if a.exists():
            candidates.extend([p for p in a.rglob("*") if p.is_file()])
    for pat in _ROOT_PATTERNS:
        candidates.extend(root.glob(pat))

    for p in candidates:
        rp = p.resolve()
        if not _safe_under(rp):
            preserved.append(str(rp)); continue
        parts = {x.lower() for x in rp.parts}
        if any(pref in parts for pref in preserve_prefix):
            preserved.append(str(rp)); continue
        if rp.suffix.lower() not in {".log", ".jsonl", ".txt"} and rp.name not in {"voice_runtime_pid.txt"}:
            preserved.append(str(rp)); continue
        if dry_run:
            deleted.append(str(rp))
        else:
            try:
                rp.unlink(missing_ok=True)
                deleted.append(str(rp))
            except Exception:
                preserved.append(str(rp))

    return {"deleted_count": len(deleted), "deleted": deleted, "preserved_count": len(preserved), "preserved": preserved, "allowed_roots": [str(a) for a in allowed]}


def write_readme():
    root = get_log_root()
    root.mkdir(parents=True, exist_ok=True)
    txt = "Estrutura de logs da Kira. Uma execucao usa um run_id unico; sessoes anteriores podem ser compactadas em archives/."
    (root / "README_LOGS.txt").write_text(txt, encoding="utf-8")


# 24/7 (2026-11-07): retenção de 2 dias. Log mais velho que isso vira um resumo curto do dia em
# memory/local/resumos/AAAA-MM-DD.md (contagens + eventos importantes) e o bruto é apagado. Só mexe em logs/.
_SUMMARY_EVENTS = re.compile(
    r"IDLE_CLOUD_TO_LOCAL_\w+|ECONOMY_TRANSPORT_\w+|GEMINI_\w*(RECONNECT|ROTAT|QUOTA|FAILED)\w*|WAKE_ONLY_\w+|"
    r"AUTO_START_SESSION\w*|SHUTDOWN_BEGIN|Traceback|LIVEKIT_PROVIDER_SELECTED\w*|429|RESOURCE_EXHAUSTED"
)


def _summarize_log(path: Path, counts: dict[str, int], events: list[str]) -> None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                for level in ("ERROR", "WARNING", "CRITICAL"):
                    if level in line:
                        counts[level] = counts.get(level, 0) + 1
                match = _SUMMARY_EVENTS.search(line)
                if match:
                    counts[match.group(0)] = counts.get(match.group(0), 0) + 1
                    if len(events) < 40 and match.group(0) not in {"429"}:
                        events.append(line.strip()[:220])
    except OSError:
        pass


def cleanup_old_logs(*, days: int = 2, summary_dir: Path | None = None) -> dict[str, object]:
    root = get_log_root()
    if not root.exists():
        return {"removed": 0, "summaries": []}
    out_dir = summary_dir or (get_project_root() / "memory" / "local" / "resumos")
    cutoff = datetime.now().timestamp() - max(int(days), 1) * 86400
    by_day: dict[str, list[Path]] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".log", ".jsonl", ".txt", ".err", ".zip"}:
            continue
        if path.name == "README_LOGS.txt":
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            by_day.setdefault(datetime.fromtimestamp(mtime).strftime("%Y-%m-%d"), []).append(path)
    removed, summaries = 0, []
    for day, files in sorted(by_day.items()):
        counts: dict[str, int] = {}
        events: list[str] = []
        for path in files:
            if path.suffix.lower() != ".zip":
                _summarize_log(path, counts, events)
        out_dir.mkdir(parents=True, exist_ok=True)
        summary = out_dir / f"logs_{day}.md"
        lines = [f"# Resumo dos logs de {day}", "", f"Arquivos: {len(files)}", ""]
        lines += [f"- {key}: {value}" for key, value in sorted(counts.items(), key=lambda kv: -kv[1])[:30]]
        lines += ["", "## Eventos importantes", ""] + [f"- {e}" for e in events]
        with summary.open("a", encoding="utf-8") as fh:  # "a": vários lotes do mesmo dia somam
            fh.write("\n".join(lines) + "\n\n")
        summaries.append(str(summary))
        for path in files:
            try:
                path.unlink()
                removed += 1
            except OSError:  # aberto por outro processo: fica para a próxima
                pass
    logging.getLogger(__name__).info("LOG_RETENTION_DONE days=%s removed=%s summaries=%s", days, removed, len(summaries))
    return {"removed": removed, "summaries": summaries}
