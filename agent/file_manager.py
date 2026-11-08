from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from file_editor import remember_recent_file
from memory.local_memory_store import get_local_memory_store


class KiraFileManagerError(Exception):
    pass


FOLDER_MAP = {
    ".py": ("Codigos", "Python"),
    ".html": ("Codigos", "HTML"),
    ".js": ("Codigos", "JS"),
    ".cs": ("Codigos", "CSharp"),
    ".txt": ("Textos",),
    ".pdf": ("PDFs",),
}


def ensure_kira_folders(base_root: Path | None = None) -> Path:
    root = base_root or (Path.home() / "Documents")
    base = root / "Kira"
    paths = [
        base / "PDFs",
        base / "Codigos" / "Python",
        base / "Codigos" / "HTML",
        base / "Codigos" / "JS",
        base / "Codigos" / "CSharp",
        base / "Codigos" / "Outros",
        base / "Textos",
        base / "Outros",
    ]
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    return base


def resolve_user_path(texto_usuario: str | None) -> Path | None:
    text = str(texto_usuario or "").strip()
    if not text:
        return None

    lowered = _normalize(text)
    if "area de trabalho" in lowered or lowered == "desktop":
        return Path.home() / "Desktop"
    if "documentos" in lowered or lowered == "documents":
        return Path.home() / "Documents"
    if "downloads" in lowered:
        return Path.home() / "Downloads"

    cleaned = text.strip().strip('"').strip("'")
    maybe_path = Path(cleaned).expanduser()
    if maybe_path.is_absolute():
        return maybe_path
    return None


def generate_filename(base_name: str, ext: str, target_dir: Path) -> str:
    safe_base = _slugify(base_name or "arquivo_kira")
    extension = _normalize_extension(ext)
    candidate = target_dir / f"{safe_base}{extension}"
    if not candidate.exists():
        return candidate.name

    idx = 1
    while True:
        numbered = target_dir / f"{safe_base}_{idx}{extension}"
        if not numbered.exists():
            return numbered.name
        idx += 1


def save_file(
    content: str,
    filename: str,
    filetype: str | None = None,
    target_path: Path | None = None,
) -> dict[str, Any]:
    base = ensure_kira_folders()
    ext = _normalize_extension(filetype or Path(filename).suffix)
    final_name = filename.strip() if filename else ""
    if not final_name:
        final_name = f"arquivo_kira{ext}"
    if Path(final_name).suffix.lower() != ext:
        final_name = f"{Path(final_name).stem}{ext}"

    destination_root = target_path or _default_subfolder(base, ext)
    destination_root.mkdir(parents=True, exist_ok=True)

    unique_name = generate_filename(Path(final_name).stem, ext, destination_root)
    final_path = destination_root / unique_name

    try:
        if ext == ".pdf":
            _write_pdf(final_path, content)
        else:
            final_path.write_text(str(content or ""), encoding="utf-8")
    except PermissionError as exc:
        raise KiraFileManagerError(
            f"Sem permissao para salvar em {destination_root}."
        ) from exc
    except Exception as exc:
        raise KiraFileManagerError(f"Falha ao salvar arquivo: {exc}") from exc

    remember_recent_file(final_path, action="created")
    store = get_local_memory_store()
    store.record_file_action(final_path, action="created", origin="file_manager.save_file")
    store.record_folder_action(final_path.parent, action="touched", origin="file_manager.save_file")
    store.record_folder_snapshot(final_path.parent, origin="file_manager.save_file")
    return {
        "created": True,
        "saved": True,
        "path": str(final_path),
        "tipo": ext,
        "used_fallback": target_path is None,
    }


def _default_subfolder(base: Path, ext: str) -> Path:
    mapping = FOLDER_MAP.get(ext)
    if mapping is None:
        if ext in {".py", ".html", ".js", ".cs"}:
            return base / "Codigos" / "Outros"
        return base / "Outros"
    path = base
    for piece in mapping:
        path = path / piece
    return path


def _write_pdf(target: Path, content: str) -> None:
    pdf = canvas.Canvas(str(target), pagesize=A4)
    width, height = A4
    y = height - 48
    for line in str(content or "").splitlines() or [""]:
        pdf.drawString(48, y, line[:120])
        y -= 16
        if y < 48:
            pdf.showPage()
            y = height - 48
    pdf.save()


def _normalize_extension(ext: str | None) -> str:
    normalized = str(ext or "").strip().lower()
    if not normalized:
        return ".txt"
    if not normalized.startswith("."):
        normalized = f".{normalized}"
    return normalized


def _normalize(text: str) -> str:
    simple = text.lower().strip()
    replacements = {
        "á": "a",
        "à": "a",
        "â": "a",
        "ã": "a",
        "é": "e",
        "ê": "e",
        "í": "i",
        "ó": "o",
        "ô": "o",
        "õ": "o",
        "ú": "u",
        "ç": "c",
    }
    for src, dst in replacements.items():
        simple = simple.replace(src, dst)
    return simple


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(value or "").strip())
    cleaned = cleaned.strip("_")
    return cleaned or "arquivo_kira"
