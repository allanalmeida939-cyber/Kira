from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import ast


@dataclass
class ValidationResult:
    ok: bool
    artifact_type: str
    reason: str = ""


def _size_ok(path: Path, min_bytes: int = 1) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size >= min_bytes


def validate_pdf(path: Path) -> ValidationResult:
    if path.suffix.lower() != ".pdf":
        return ValidationResult(False, "pdf", "invalid_extension")
    if not _size_ok(path, 1024):
        return ValidationResult(False, "pdf", "file_too_small")
    head = path.read_bytes()[:4]
    if head != b"%PDF":
        return ValidationResult(False, "pdf", "missing_pdf_header")
    return ValidationResult(True, "pdf")


def validate_text(path: Path) -> ValidationResult:
    if path.suffix.lower() not in {".txt", ".md"}:
        return ValidationResult(False, "text", "invalid_extension")
    if not _size_ok(path, 1):
        return ValidationResult(False, "text", "empty")
    try:
        _ = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ValidationResult(False, "text", "unreadable")
    return ValidationResult(True, "text")


def validate_code(path: Path, language: str | None = None) -> ValidationResult:
    if not _size_ok(path, 1):
        return ValidationResult(False, "code", "empty")
    if path.suffix.lower() == ".py":
        try:
            ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return ValidationResult(False, "code", "python_syntax_error")
    return ValidationResult(True, "code")


def validate_slide(path: Path) -> ValidationResult:
    if path.suffix.lower() not in {".pptx", ".pdf"}:
        return ValidationResult(False, "slide", "invalid_extension")
    if not _size_ok(path, 1024):
        return ValidationResult(False, "slide", "file_too_small")
    return ValidationResult(True, "slide")


def validate_spreadsheet(path: Path) -> ValidationResult:
    if path.suffix.lower() not in {".xlsx", ".csv"}:
        return ValidationResult(False, "spreadsheet", "invalid_extension")
    if not _size_ok(path, 1):
        return ValidationResult(False, "spreadsheet", "empty")
    return ValidationResult(True, "spreadsheet")


def validate_image(path: Path) -> ValidationResult:
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        return ValidationResult(False, "image", "invalid_extension")
    if not _size_ok(path, 1):
        return ValidationResult(False, "image", "empty")
    return ValidationResult(True, "image")


def validate_artifact(path, expected_type: str) -> ValidationResult:
    p = Path(path)
    t = str(expected_type or "unknown").lower()
    if t == "pdf":
        return validate_pdf(p)
    if t in {"text", "prompt", "document"}:
        return validate_text(p)
    if t in {"code", "python", "html", "javascript", "csharp"}:
        return validate_code(p, language=t)
    if t in {"slides", "powerpoint", "slide"}:
        return validate_slide(p)
    if t == "spreadsheet":
        return validate_spreadsheet(p)
    if t == "image":
        return validate_image(p)
    if _size_ok(p, 1):
        return ValidationResult(True, "unknown")
    return ValidationResult(False, "unknown", "missing_or_empty")
