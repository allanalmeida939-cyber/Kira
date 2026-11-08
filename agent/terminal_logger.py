from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from datetime import timedelta
from pathlib import Path

from resilience import is_debug_enabled
from time_utils import now_brasilia

if os.name == "nt":
    os.system("")


class ColorFormatter(logging.Formatter):
    RESET = "\033[0m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    MAGENTA = "\033[95m"
    WHITE = "\033[97m"

    def format(self, record):
        now = now_brasilia().strftime("%H:%M:%S")

        level = record.levelname
        msg = record.getMessage()

        if level == "ERROR":
            color = self.RED
        elif level == "WARNING":
            color = self.YELLOW
        elif level == "INFO":
            color = self.GREEN
        elif level == "DEBUG":
            color = self.CYAN
        else:
            color = self.WHITE

        if hasattr(record, "tag"):
            prefix = f"[{now}] [{record.tag}]"
        else:
            prefix = f"[{now}] [{level}]"

        base = f"{color}{prefix} {msg}{self.RESET}"

        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)

        return base


_BASE_LIBRARY_LOGGER_LEVELS = {
    "asyncio": logging.WARNING,
    "livekit": logging.WARNING,
    "livekit.agents": logging.WARNING,
    "livekit.plugins": logging.WARNING,
}

_CLEAN_LIBRARY_LOGGER_LEVELS = {
    "absl": logging.ERROR,
    "core.realtime": logging.WARNING,
    "google": logging.WARNING,
    "google.api_core": logging.WARNING,
    "google.auth": logging.WARNING,
    "google.genai": logging.WARNING,
    "grpc": logging.ERROR,
    "grpc._cython": logging.ERROR,
    "httpcore": logging.WARNING,
    "httpx": logging.WARNING,
    "runtime.event_handlers": logging.WARNING,
    "runtime.heartbeat": logging.WARNING,
    "runtime.livekit_debug": logging.INFO,
    "runtime.session_start": logging.WARNING,
    "runtime.speech_pipeline": logging.INFO,
    "urllib3": logging.WARNING,
    "watchfiles": logging.WARNING,
    "websockets": logging.WARNING,
}


def _configure_named_logger_levels(*, clean_logs: bool) -> None:
    configured_levels = dict(_BASE_LIBRARY_LOGGER_LEVELS)
    if clean_logs:
        configured_levels.update(_CLEAN_LIBRARY_LOGGER_LEVELS)

    for logger_name, level in configured_levels.items():
        logging.getLogger(logger_name).setLevel(level)


def setup_terminal_logger(*, clean_logs: bool = True):
    logger = logging.getLogger()
    debug_enabled = is_debug_enabled()
    logger.setLevel(logging.DEBUG if debug_enabled else logging.INFO)
    logger.handlers.clear()
    _configure_named_logger_levels(clean_logs=clean_logs and not debug_enabled)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG if debug_enabled else logging.INFO)
    handler.setFormatter(ColorFormatter())

    logger.addHandler(handler)
    _add_runtime_file_handler(logger, clean_old_days=5)
    return handler


def ensure_log_directory() -> Path:
    project_root = Path(__file__).resolve().parents[1]
    configured_root = str(os.getenv("KIRA_LOG_ROOT", "")).strip()
    logs_root = Path(configured_root) if configured_root else project_root / "logs"
    logs_dir = logs_root / "Kira"
    logs_dir.mkdir(parents=True, exist_ok=True)
    print(f"KIRA_SESSION_LOG_DIR_READY path={logs_dir}", flush=True)
    return logs_dir


def cleanup_old_logs(*, max_days: int = 5, max_keep: int = 3) -> None:
    """Remove logs antigos por idade e limita quantidade máxima retida.

    Política: apaga logs mais velhos que max_days E mantém no máximo max_keep
    arquivos (os mais recentes), descartando o restante.
    """
    logs_dir = ensure_log_directory()
    now = now_brasilia()
    cutoff = now - timedelta(days=max(max_days, 1))

    # Coleta todos os candidatos com timestamp de modificação
    candidates: list[tuple[datetime, Path]] = []
    for candidate in sorted(logs_dir.glob("*.log")):
        try:
            modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=now.tzinfo)
        except Exception as exc:
            print(f"[KIRA LOG] log_cleanup_skipped arquivo={candidate.name} motivo={exc}", flush=True)
            continue
        candidates.append((modified, candidate))

    # Ordena do mais recente ao mais antigo
    candidates.sort(key=lambda x: x[0], reverse=True)

    print(f"[KIRA LOG] log_cleanup_started total={len(candidates)} max_keep={max_keep} max_days={max_days}", flush=True)

    for idx, (modified, candidate) in enumerate(candidates):
        too_old = modified <= cutoff
        over_limit = idx >= max_keep  # mantém os max_keep mais recentes
        if too_old or over_limit:
            reason = "too_old" if too_old else "over_limit"
            try:
                candidate.unlink(missing_ok=True)
                print(f"[KIRA LOG] log_cleanup_deleted arquivo={candidate.name} motivo={reason}", flush=True)
            except Exception as exc:
                print(f"[KIRA LOG] log_cleanup_skipped arquivo={candidate.name} motivo={exc}", flush=True)


def create_session_log_file() -> Path:
    logs_dir = ensure_log_directory()
    run_id = str(os.getenv("KIRA_LOG_RUN_ID", "")).strip()
    if not run_id:
        run_id = now_brasilia().strftime("%Y-%m-%d_%H-%M-%S")
        os.environ["KIRA_LOG_RUN_ID"] = run_id
    candidate = logs_dir / f"kira_session_{run_id}.log"
    candidate.touch(exist_ok=True)
    print(f"LOG_RUN_ID_REUSED id={run_id}", flush=True)
    print("LOG_WRITER_UNIFIED component=terminal_logger", flush=True)
    print(f"LOG_FILE_REUSED path={candidate}", flush=True)
    return candidate


def configure_logging(logger: logging.Logger, session_log_path: Path) -> None:
    # Remove file handlers previos da Kira para evitar duplicidade em reinits parciais.
    for existing in list(logger.handlers):
        if isinstance(existing, logging.FileHandler) and getattr(existing, "_kira_session_file_handler", False):
            logger.removeHandler(existing)
            try:
                existing.close()
            except Exception:
                pass

    class _FlushFileHandler(logging.FileHandler):
        def emit(self, record):
            super().emit(record)
            self.flush()

    file_handler = _FlushFileHandler(
        filename=session_log_path,
        mode="a",
        encoding="utf-8",
        delay=False,
    )
    file_handler._kira_session_file_handler = True  # type: ignore[attr-defined]
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(file_handler)


def _add_runtime_file_handler(logger: logging.Logger, *, clean_old_days: int = 5) -> None:
    try:
        ensure_log_directory()
        _cleanup_legacy_root_session_logs()
        session_log_path = create_session_log_file()
        configure_logging(logger, session_log_path)
        print(f"KIRA_SESSION_LOG_PATH_SET path={session_log_path}", flush=True)
    except Exception as exc:
        # Logging em arquivo nunca deve impedir bootstrap do agente.
        print(f"[KIRA LOG] aviso: fallback sem arquivo de sessao ({exc})", flush=True)
        return


def _cleanup_legacy_root_session_logs() -> None:
    project_root = Path(__file__).resolve().parents[1]
    root_logs = project_root / "logs"
    for path in sorted(root_logs.glob("kira_*.log")):
        try:
            path.unlink(missing_ok=True)
            print(f"KIRA_ROOT_SESSION_LOG_LEGACY_REMOVED file={path}", flush=True)
        except Exception as exc:
            print(f"KIRA_ROOT_SESSION_LOG_LEGACY_REMOVE_FAILED file={path} error={exc}", flush=True)


def detach_terminal_logger(handler: logging.Handler | None) -> None:
    if handler is None:
        return

    logger = logging.getLogger()
    if handler in logger.handlers:
        logger.removeHandler(handler)


def log_info(message: str, tag: str = "INFO"):
    logging.getLogger().info(message, extra={"tag": tag})


def log_warning(message: str, tag: str = "WARN"):
    logging.getLogger().warning(message, extra={"tag": tag})


def log_error(message: str, exc_info=False, tag: str = "ERROR"):
    logging.getLogger().error(message, exc_info=exc_info, extra={"tag": tag})
