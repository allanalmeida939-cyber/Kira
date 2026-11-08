"""
Static boot-contract validation for Kira.

This script is intentionally conservative: it checks that the files, endpoints,
functions and readiness logs that define the boot path still exist. It does not
start LiveKit, Gemini or the UI.
"""

from __future__ import annotations

import py_compile
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.boot_guard import BOOT_CRITICAL_MANIFEST  # noqa: E402


CRITICAL_COMPILE_FILES = [
    "kira_app.py",
    "boot_manager.py",
    "agent/agent.py",
    "agent/runtime/speech_pipeline.py",
    "agent/runtime/event_handlers.py",
    "agent/runtime/turn_coordinator.py",
]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8", errors="replace")


def _assert(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def _validate_files(errors: list[str]) -> None:
    for rel_path in BOOT_CRITICAL_MANIFEST["files"]:
        _assert((ROOT / rel_path).exists(), f"critical file missing: {rel_path}", errors)


def _validate_endpoints(errors: list[str]) -> None:
    app_text = _read("kira_app.py")
    for endpoint in BOOT_CRITICAL_MANIFEST["endpoints"]:
        _assert(endpoint in app_text, f"critical endpoint missing: {endpoint}", errors)


def _validate_logs(errors: list[str]) -> None:
    searchable_files = [
        "kira_app.py",
        "agent/agent.py",
    ]
    corpus = "\n".join(_read(path) for path in searchable_files if (ROOT / path).exists())
    for marker in BOOT_CRITICAL_MANIFEST["logs"]:
        _assert(marker in corpus, f"critical log marker missing: {marker}", errors)


def _validate_functions(errors: list[str]) -> None:
    for dotted in BOOT_CRITICAL_MANIFEST["functions"]:
        module_name, func_name = dotted.rsplit(".", 1)
        rel_path = module_name.replace(".", "/") + ".py"
        path = ROOT / rel_path
        _assert(path.exists(), f"function module missing: {dotted}", errors)
        if not path.exists():
            continue
        text = _read(rel_path)
        pattern = rf"^\s*(async\s+def|def)\s+{re.escape(func_name)}\s*\("
        _assert(re.search(pattern, text, flags=re.MULTILINE) is not None, f"function missing: {dotted}", errors)


def _validate_compilation(errors: list[str]) -> None:
    for rel_path in CRITICAL_COMPILE_FILES:
        try:
            py_compile.compile(str(ROOT / rel_path), doraise=True)
        except Exception as exc:
            errors.append(f"compile failed: {rel_path}: {exc}")


def _validate_optional_import_policy(errors: list[str]) -> None:
    app_text = _read("kira_app.py")
    for service in ("system_vitals", "youtube_music_service", "messages_data"):
        _assert(
            f'safe_import_optional("services.{service}"' in app_text,
            f"optional service not guarded in kira_app.py: services.{service}",
            errors,
        )
    agent_text = _read("agent/agent.py")
    for marker in (
        "service=vision reason=optional_import_failed",
        "service=proactive reason=optional_import_failed",
        "service=reminders reason=optional_import_failed",
    ):
        _assert(marker in agent_text, f"optional agent guard missing: {marker}", errors)


def main() -> int:
    errors: list[str] = []
    _validate_files(errors)
    _validate_endpoints(errors)
    _validate_logs(errors)
    _validate_functions(errors)
    _validate_compilation(errors)
    _validate_optional_import_policy(errors)

    if errors:
        print("BOOT_CONTRACT_FAILED")
        for error in errors:
            print(f"- {error}")
        return 1

    print("BOOT_CONTRACT_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

