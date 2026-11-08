from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _documents_dir() -> Path:
    home = Path.home()
    docs = home / "Documents"
    if docs.exists() or docs.parent.exists():
        return docs
    return home / "Documents"


def ensure_artifact_directories() -> dict[str, str]:
    logger.info("ARTIFACT_PATHS_INIT_BEGIN")
    root = _documents_dir() / "Kira"
    mapping = {
        "root": root,
        "PDFs": root / "PDFs",
        "Textos": root / "Textos",
        "Codigos": root / "Codigos",
        "Slides": root / "Slides",
        "Imagens": root / "Imagens",
        "Planilhas": root / "Planilhas",
        "Prompts": root / "Prompts",
        "Outros": root / "Outros",
        "Jobs": root / "Jobs",
        "Logs": root / "Logs",
    }
    for path in mapping.values():
        path.mkdir(parents=True, exist_ok=True)
        logger.info("ARTIFACT_PATHS_DIR_READY path=%s", str(path))
    logger.info("ARTIFACT_PATHS_INIT_DONE root=%s", str(root))
    return {k: str(v) for k, v in mapping.items()}
