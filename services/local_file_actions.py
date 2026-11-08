from __future__ import annotations

import os
import subprocess
from pathlib import Path
import logging
import re
import unicodedata

logger = logging.getLogger(__name__)


def _ok(path: str | Path) -> Path | None:
    p = Path(path)
    if not p.exists():
        return None
    return p


def open_file(path) -> dict:
    p = _ok(path)
    if p is None or not p.is_file():
        return {"ok": False, "error": "file_not_found", "path": str(path)}
    os.startfile(str(p))
    return {"ok": True, "path": str(p)}


def open_folder(path) -> dict:
    p = _ok(path)
    if p is None:
        return {"ok": False, "error": "folder_not_found", "path": str(path)}
    d = p if p.is_dir() else p.parent
    os.startfile(str(d))
    return {"ok": True, "path": str(d)}


def reveal_in_explorer(path) -> dict:
    p = _ok(path)
    if p is None:
        return {"ok": False, "error": "path_not_found", "path": str(path)}
    if p.is_file():
        subprocess.run(["explorer", f"/select,{str(p)}"], check=False)
        return {"ok": True, "path": str(p), "mode": "select"}
    os.startfile(str(p))
    return {"ok": True, "path": str(p), "mode": "open_dir"}


_DEFAULT_KIND_FOLDERS = {
    "pdf": "PDFs",
    "txt": "Textos",
    "text": "Textos",
    "texto": "Textos",
    "slides": "Slides",
    "planilha": "Planilhas",
    "spreadsheet": "Planilhas",
    "imagem": "Imagens",
    "image": "Imagens",
    "codigo": "Codigos",
    "code": "Codigos",
    "prompt": "Prompts",
    "outros": "Outros",
}


def _norm(text: str) -> str:
    value = unicodedata.normalize("NFD", str(text or "").lower())
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", value)).strip()


def detect_default_folder_kind(text: str) -> str | None:
    normalized = _norm(text)
    if not re.search(r"\b(abre|abrir|mostra|mostrar)\b", normalized) or "pasta" not in normalized:
        return None
    checks = (
        (r"\bpdfs?|p d f\b", "pdf"),
        (r"\btextos?|txt\b", "text"),
        (r"\bslides?|powerpoint|apresentacoes?\b", "slides"),
        (r"\bplanilhas?|excel|xlsx\b", "spreadsheet"),
        (r"\bimagens?|imagem\b", "image"),
        (r"\bcodigos?|codigo\b", "code"),
        (r"\bprompts?|prompt\b", "prompt"),
    )
    for pattern, kind in checks:
        if re.search(pattern, normalized):
            return kind
    return None


def get_default_folder(kind: str) -> Path:
    folder = _DEFAULT_KIND_FOLDERS.get(str(kind or "").lower(), "Outros")
    return Path(os.path.expanduser("~")) / "Documents" / "Kira" / folder


def open_folder_in_explorer(path: Path) -> None:
    subprocess.Popen(["explorer", str(path)])


def handle_local_file_command(text: str) -> dict:
    kind = detect_default_folder_kind(text)
    if not kind:
        return {"handled": False}
    path = get_default_folder(kind)
    path.mkdir(parents=True, exist_ok=True)
    logger.info("LOCAL_FILE_DEFAULT_FOLDER_MATCHED kind=%s path=%s", kind, path)
    open_folder_in_explorer(path)
    logger.info("LOCAL_FILE_OPEN_FOLDER_OK path=%s", path)
    logger.info("LOCAL_COMMAND_TEXT_ONLY source=local_file_command")
    logger.info("LOCAL_COMMAND_GEMINI_DISABLED reason=local_file_command")
    logger.info("LOCAL_COMMAND_VOICE_DISABLED reason=local_file_command")
    logger.info("LOCAL_COMMAND_TURN_DONE source=local_file_command")
    logger.info("UI_MESSAGE_PREFIX_REMOVED old_prefix=Interface")
    return {"handled": True, "ok": True, "path": str(path), "response": f"Abrindo pasta {path}"}
