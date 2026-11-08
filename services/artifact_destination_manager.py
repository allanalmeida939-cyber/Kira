from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import unicodedata

logger = logging.getLogger(__name__)

_DEFAULT_FOLDERS = {
    "pdf": "PDFs",
    "prompt": "Prompts",
    "text": "Textos",
    "code": "Codigos",
    "spreadsheet": "Planilhas",
    "slides": "Slides",
    "archive": "Arquivos",
    "image": "Imagens",
    "screenshot": "Screenshots",
    "job": "Jobs",
    "log": "Logs",
    "other": "Outros",
}
_KIND_ALIASES = {
    "txt": "text", "texto": "text", "document": "text", "documento": "text", "relatorio": "text",
    "codigo": "code", "python": "code", "py": "code", "html": "code", "css": "code", "js": "code",
    "javascript": "code", "json": "code", "csharp": "code", "c#": "code", "cpp": "code", "c++": "code", "java": "code",
    "planilha": "spreadsheet", "xlsx": "spreadsheet", "csv": "spreadsheet", "excel": "spreadsheet",
    "slide": "slides", "powerpoint": "slides", "ppt": "slides", "pptx": "slides", "apresentacao": "slides",
    "zip": "archive", "arquivo compactado": "archive", "generic file": "archive", "arquivo comum": "archive",
    "imagem": "image", "print": "screenshot", "printscreen": "screenshot", "job_metadata": "job",
    "unknown": "other", "outro": "other",
}
_MANUS_SUPPORTED = frozenset({"pdf", "prompt", "text", "slides", "code", "spreadsheet"})
_DEFAULT_LOCATION_TERMS = (
    "pasta padrao", "salva no padrao", "salva na pasta padrao", "salva no local padrao",
    "bota na pasta padrao", "deixa na pasta padrao", "salva onde sempre salva",
    "no local padrao", "pode ser no padrao", "nos documentos", "na pasta da kira",
    "na pasta padrao mesmo", "na pasta de pdfs", "na pasta de textos",
    "na pasta de prompts", "na pasta de slides",
)


@dataclass(frozen=True)
class ArtifactDestination:
    kind: str
    root: Path
    folder: Path
    filename: str | None = None
    path: Path | None = None
    user_requested_default_folder: bool = False
    manus_supported: bool = False
    route_to_manus: bool = False


@dataclass(frozen=True)
class ArtifactDestinationIntent:
    mode: str
    hint: str = ""
    path: Path | None = None


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFD", str(value or "").strip().lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"\s+", " ", text).strip()


def canonical_artifact_kind(kind: str | None) -> str:
    normalized = _normalize(str(kind or "unknown"))
    return _KIND_ALIASES.get(normalized, normalized if normalized in _DEFAULT_FOLDERS else "other")


def _config_path() -> Path:
    configured = str(os.getenv("KIRA_ARTIFACT_DESTINATIONS_CONFIG", "")).strip()
    return Path(configured) if configured else Path(__file__).resolve().parents[1] / "configs" / "artifact_destinations.json"


def get_default_artifact_root() -> Path:
    override = str(os.getenv("KIRA_ARTIFACT_ROOT", "")).strip()
    if override:
        return Path(override)
    return Path.home() / "Documents" / "Kira"


def _config_payload(root: Path) -> dict[str, object]:
    return {"root": str(root), "defaults": dict(_DEFAULT_FOLDERS)}


def _load_or_create_config() -> dict[str, object]:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("defaults"), dict):
                return data
        except Exception:
            logger.warning("ARTIFACT_DESTINATION_CONFIG_INVALID path=%s", path)
    data = _config_payload(get_default_artifact_root())
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data


def ensure_default_artifact_folders() -> dict[str, Path]:
    data = _load_or_create_config()
    configured_root = str(os.getenv("KIRA_ARTIFACT_ROOT", "")).strip()
    root = Path(configured_root or str(data.get("root") or get_default_artifact_root()))
    defaults = dict(_DEFAULT_FOLDERS)
    defaults.update({str(k): str(v) for k, v in dict(data.get("defaults") or {}).items()})
    defaults.update({"pdf": "PDFs", "slides": "Slides", "prompt": "Prompts", "text": "Textos"})
    root.mkdir(parents=True, exist_ok=True)
    result = {kind: root / folder for kind, folder in defaults.items()}
    for folder in result.values():
        folder.mkdir(parents=True, exist_ok=True)
    logger.info("ARTIFACT_DESTINATION_MANAGER_READY root=%s", root)
    logger.info("ARTIFACT_DEFAULT_FOLDERS_ENSURED count=%s", len(result))
    return result


def get_default_folder_for_artifact(kind: str | None) -> Path:
    canonical = canonical_artifact_kind(kind)
    folder = ensure_default_artifact_folders()[canonical]
    logger.info("ARTIFACT_DESTINATION_RESOLVED kind=%s folder=%s", canonical, folder)
    return folder


def user_requested_default_location(text: str | None) -> bool:
    normalized = _normalize(str(text or ""))
    return any(term in normalized for term in _DEFAULT_LOCATION_TERMS)


def detect_destination_intent(text: str | None, kind: str | None = None) -> ArtifactDestinationIntent:
    raw = str(text or "").strip()
    normalized = _normalize(raw)
    if user_requested_default_location(raw) or any(term in normalized for term in ("no padrao", "deixa no padrao", "pasta de pdfs", "pasta de textos", "pasta de slides", "pasta de prompts")):
        logger.info("CREATION_TASK_DESTINATION_DETECTED mode=default kind=%s", canonical_artifact_kind(kind))
        return ArtifactDestinationIntent("default")
    for term, hint, path in (
        ("area de trabalho", "desktop", Path.home() / "Desktop"),
        ("desktop", "desktop", Path.home() / "Desktop"),
        ("documentos", "documents", Path.home() / "Documents"),
        ("downloads", "downloads", Path.home() / "Downloads"),
        ("pasta kira", "kira", get_default_artifact_root()),
    ):
        if term in normalized:
            logger.info("CREATION_TASK_DESTINATION_DETECTED mode=specific path=%s", path)
            return ArtifactDestinationIntent("specific", hint, path)
    match = re.search(r"[A-Za-z]:[\\/][^\"\n]+", raw)
    if match:
        candidate = Path(match.group(0).strip(" ."))
        if candidate.exists() and candidate.is_dir():
            logger.info("CREATION_TASK_DESTINATION_DETECTED mode=specific path=%s", candidate)
            return ArtifactDestinationIntent("specific", str(candidate), candidate)
    logger.info("CREATION_TASK_DESTINATION_MISSING mode=ask_later")
    return ArtifactDestinationIntent("ask_later")


def folder_for_destination_intent(kind: str | None, intent: ArtifactDestinationIntent) -> Path | None:
    canonical = canonical_artifact_kind(kind)
    if intent.mode == "default":
        return get_default_folder_for_artifact(canonical)
    if intent.mode != "specific" or intent.path is None:
        return None
    if intent.hint in {"documents", "downloads", "desktop", "kira"}:
        return intent.path / _DEFAULT_FOLDERS[canonical]
    return intent.path


def is_manus_supported_kind(kind: str | None) -> bool:
    canonical = canonical_artifact_kind(kind)
    supported = canonical in _MANUS_SUPPORTED
    if supported:
        logger.info("MANUS_ROUTE_SELECTED kind=%s reason=creation_artifact_supported", canonical)
    else:
        reason = "internal_file" if canonical in {"log", "job"} else "unsupported_by_manus"
        logger.info("MANUS_ROUTE_SKIPPED kind=%s reason=%s", canonical, reason)
    return supported


def sanitize_artifact_filename(title: str, extension: str | None = None) -> str:
    original = str(title or "arquivo").strip()
    base = _normalize(original)
    base = re.sub(r"[^a-z0-9_-]+", "_", base).strip("_") or "arquivo"
    suffix = str(extension or "").strip().lower().lstrip(".")
    final = f"{base}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if suffix:
        final = f"{final}.{suffix}"
    logger.info("ARTIFACT_FILENAME_SANITIZED original=%r final=%r", original, final)
    return final


def unique_artifact_path(folder: Path, filename: str) -> Path:
    candidate = folder / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    index = 1
    while (folder / f"{stem}_{index}{suffix}").exists():
        index += 1
    return folder / f"{stem}_{index}{suffix}"


def resolve_artifact_destination(
    kind: str | None,
    requested_location: str | None = None,
    *,
    title: str | None = None,
    extension: str | None = None,
) -> ArtifactDestination:
    canonical = canonical_artifact_kind(kind)
    folder = get_default_folder_for_artifact(canonical)
    requested_default = user_requested_default_location(requested_location)
    if requested_default:
        logger.info("ARTIFACT_DEFAULT_LOCATION_SELECTED kind=%s reason=user_said_default", canonical)
        logger.info("DEFAULT_SAVE_LOCATION_REQUESTED")
        logger.info("ASK_SAVE_LOCATION_BLOCKED reason=default_requested")
    filename = sanitize_artifact_filename(title, extension) if title else None
    path = unique_artifact_path(folder, filename) if filename else None
    manus_supported = is_manus_supported_kind(canonical)
    return ArtifactDestination(
        kind=canonical,
        root=folder.parent,
        folder=folder,
        filename=path.name if path else None,
        path=path,
        user_requested_default_folder=requested_default,
        manus_supported=manus_supported,
        route_to_manus=manus_supported,
    )


__all__ = [
    "ArtifactDestination",
    "ArtifactDestinationIntent",
    "canonical_artifact_kind",
    "ensure_default_artifact_folders",
    "get_default_artifact_root",
    "get_default_folder_for_artifact",
    "is_manus_supported_kind",
    "resolve_artifact_destination",
    "sanitize_artifact_filename",
    "unique_artifact_path",
    "user_requested_default_location",
    "detect_destination_intent",
    "folder_for_destination_intent",
]
