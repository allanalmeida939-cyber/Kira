from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import sys
import unicodedata
from typing import Callable
import importlib.util

logger = logging.getLogger(__name__)


def _destination_folder(kind: str) -> Path:
    service_path = Path(__file__).resolve().parents[2] / "services" / "artifact_destination_manager.py"
    spec = importlib.util.spec_from_file_location("_kira_artifact_destination_manager", str(service_path))
    if spec is None or spec.loader is None:
        return Path.home() / "Documents" / "Kira" / kind
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return Path(module.get_default_folder_for_artifact(kind))


@dataclass(frozen=True)
class LocalFileCommand:
    action: str
    target_kind: str
    path_hint: str
    raw_text: str
    normalized_text: str
    source: str = "local_file_command"
    handled: bool = True
    speak: bool = False
    gemini: bool = False


def normalize_local_file_text(text: str) -> str:
    value = str(text or "").strip().lower()
    value = unicodedata.normalize("NFD", value)
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"^(?:ei\s+|hey\s+|ok\s+)?(?:kira\s+ai|kira|akira|kiri)\s+", "", value).strip()


def is_local_file_phrase(text: str) -> bool:
    normalized = normalize_local_file_text(text)
    return bool(
        re.search(
            r"\b(pasta|arquivo|documentos?|downloads?|area de trabalho|desktop|caminho|local onde)\b",
            normalized,
        )
    )


def detect_local_file_command(text: str) -> LocalFileCommand | None:
    normalized = normalize_local_file_text(text)
    logger.info("LOCAL_FILE_COMMAND_GATE_CHECK text=%r", normalized[:180])
    if not normalized:
        logger.info("LOCAL_FILE_COMMAND_GATE_NOT_MATCHED reason=empty")
        return None
    if " aba " in f" {normalized} ":
        logger.info("LOCAL_FILE_COMMAND_GATE_NOT_MATCHED reason=explicit_ui_tab")
        return None
    if re.search(r"\b(status|sistema vital|transcricao)\b", normalized):
        logger.info("LOCAL_FILE_COMMAND_GATE_NOT_MATCHED reason=ui_panel_phrase")
        return None
    if not re.search(r"\b(abre|abri|abrir|abra|mostra|mostrar|mostre|cade|onde)\b", normalized):
        logger.info("LOCAL_FILE_COMMAND_GATE_NOT_MATCHED reason=no_local_action")
        return None
    # Sinal claro de web/navegador -> cede para o Browser Agent Gate.
    if re.search(r"(https?://|www\.|file://|\bsite\b|\bnavegador\b|\bpagina web\b|\bna internet\b|\bfaz login\b|\bfazer login\b|\bformulario\b)", normalized):
        logger.info("LOCAL_FILE_COMMAND_GATE_NOT_MATCHED reason=browser_command_priority")
        return None
    if re.search(
        r"\b(pdf|ultimo arquivo|ultimo texto|ultimo pdf|ultimo documento|arquivo|pasta do arquivo|"
        r"onde esta o arquivo|que voce criou|que voce gerou|que voce fez|texto que voce)\b",
        normalized,
    ):
        logger.info("LOCAL_FILE_COMMAND_GATE_NOT_MATCHED reason=artifact_command_priority")
        return None

    target_kind = ""
    path_hint = ""
    default_kinds = (
        (r"\b(prompts?|prompt)\b", "prompt"),
        (r"\b(codigos?|codigo)\b", "code"),
        (r"\b(planilhas?|planilha)\b", "spreadsheet"),
        (r"\b(slides?|apresentacoes?|powerpoint)\b", "slides"),
        (r"\b(textos?|texto)\b", "text"),
        (r"\b(arquivos?)\b", "archive"),
        (r"\b(imagens?|imagem)\b", "image"),
        (r"\b(screenshots?|prints?)\b", "screenshot"),
        (r"\b(logs?)\b", "log"),
    )
    folder_request = bool(re.search(r"\b(pasta|local|abre|abri|abrir|abra)\b", normalized))
    named_kira_folder = re.search(
        r"\bpasta\s+(?:(?:chamada|nomeada|com\s+nome)\s+)?(?P<name>[a-z0-9][\w\s]{1,80}?)\s+(?:da|do|na|no)\s+kira\b",
        normalized,
    )
    default_kind = next((kind for pattern, kind in default_kinds if folder_request and re.search(pattern, normalized)), None)
    # "pasta teste kira" é uma pasta do Chefe com "kira" no nome, não o projeto: a tool abrir_item procura ela.
    user_folder = re.search(r"\bpasta\s+(?:chamada\s+)?(\w+)\s+kira\b", normalized)
    if user_folder and user_folder.group(1) not in {"da", "do", "de", "na", "no", "projeto"}:
        logger.info("LOCAL_FILE_COMMAND_GATE_NOT_MATCHED reason=user_folder_named_kira")
        return None
    if named_kira_folder:
        folder_name = re.sub(r"\s+", " ", named_kira_folder.group("name")).strip()
        target_kind, path_hint = "named_kira_folder", f"Kira/{folder_name}"
    elif re.search(r"\b(documentos?)\b", normalized) and "kira" in normalized:
        target_kind, path_hint = "documents_kira", "Documents/Kira"
    elif re.search(r"\b(area de trabalho|desktop)\b", normalized) and "kira" in normalized:
        target_kind, path_hint = "desktop_kira", "Desktop/Kira"
    elif "projeto kira" in normalized:
        target_kind, path_hint = "desktop_kira", "Desktop/Kira"
    elif "kira" in normalized and re.search(r"\b(pasta|parte|local)\b", normalized):
        target_kind, path_hint = "project_kira", "Desktop/Kira|Documents/Kira"
    elif default_kind:
        target_kind, path_hint = f"default_{default_kind}", default_kind
    elif re.search(r"\b(documentos?|meus documentos)\b", normalized):
        target_kind, path_hint = "documents", "Documents"
    elif re.search(r"\bdownloads?\b", normalized):
        target_kind, path_hint = "downloads", "Downloads"
    elif re.search(r"\b(area de trabalho|desktop)\b", normalized):
        target_kind, path_hint = "desktop", "Desktop"
    elif re.search(r"\b(musicas?|music)\b", normalized):
        target_kind, path_hint = "music", "Music"
    elif re.search(r"\b(imagens?|pictures?)\b", normalized):
        target_kind, path_hint = "pictures", "Pictures"
    elif re.search(r"\b(videos?)\b", normalized):
        target_kind, path_hint = "videos", "Videos"
    elif re.search(r"\b(explorador|explorer|gerenciador de arquivos)\b", normalized):
        target_kind, path_hint = "home", "~"
    else:
        logger.info("LOCAL_FILE_COMMAND_GATE_NOT_MATCHED reason=unknown_folder")
        return None

    command = LocalFileCommand(
        action="open_folder",
        target_kind=target_kind,
        path_hint=path_hint,
        raw_text=str(text or ""),
        normalized_text=normalized,
    )
    logger.info(
        "LOCAL_FILE_COMMAND_GATE_MATCHED action=%s target_kind=%s",
        command.action,
        command.target_kind,
    )
    return command


def _resolve_folder(command: LocalFileCommand, *, home: Path) -> tuple[Path | None, str | None]:
    if command.target_kind.startswith("default_"):
        kind = command.target_kind.removeprefix("default_")
        path = _destination_folder(kind)
        logger.info("LOCAL_FILE_DEFAULT_FOLDER_MATCHED kind=%s path=%s", kind, path)
        return path, None
    candidates = {
        "documents": home / "Documents",
        "documents_kira": home / "Documents" / "Kira",
        "desktop": home / "Desktop",
        "desktop_kira": home / "Desktop" / "Kira",
        "downloads": home / "Downloads",
        "music": home / "Music",
        "pictures": home / "Pictures",
        "videos": home / "Videos",
        "home": home,
    }
    if command.target_kind == "named_kira_folder":
        raw_name = str(command.path_hint or "").replace("\\", "/").split("/")[-1].strip()
        safe_name = re.sub(r"[^\w\s.-]", "", raw_name).strip(" .")
        if not safe_name:
            return None, "Interface: nao entendi o nome da pasta da Kira."
        named_candidates = [
            Path.cwd() / safe_name,
            home / "Desktop" / "Kira" / safe_name,
            home / "Documents" / "Kira" / safe_name,
        ]
        for candidate in named_candidates:
            if candidate.exists() and candidate.is_dir():
                return candidate, None
        return named_candidates[0], None
    if command.target_kind != "project_kira":
        return candidates.get(command.target_kind), None
    desktop_kira = candidates["desktop_kira"]
    documents_kira = candidates["documents_kira"]
    if desktop_kira.exists() and documents_kira.exists():
        return None, "Interface: encontrei Kira em Documentos e Area de Trabalho. Qual delas voce quer abrir?"
    if desktop_kira.exists():
        return desktop_kira, None
    return documents_kira, None


def execute_local_file_command(
    command: LocalFileCommand,
    *,
    home: Path | None = None,
    opener: Callable[[str], object] | None = None,
) -> dict[str, object]:
    base_home = Path(home) if home is not None else Path.home()
    path, ambiguity_notice = _resolve_folder(command, home=base_home)
    if ambiguity_notice:
        logger.info("LOCAL_FILE_COMMAND_TEXT_ONLY_NOTICE text=%r", ambiguity_notice)
        return {"handled": True, "ok": False, "notice": ambiguity_notice, "path": None}
    if path is None or not path.exists() or not path.is_dir():
        logger.info("LOCAL_FILE_COMMAND_NOT_FOUND target_kind=%s", command.target_kind)
        notice = f"Interface: nao encontrei a pasta {command.path_hint}."
        logger.info("LOCAL_FILE_COMMAND_TEXT_ONLY_NOTICE text=%r", notice)
        return {"handled": True, "ok": False, "notice": notice, "path": str(path or "")}
    logger.info("LOCAL_FILE_COMMAND_EXECUTE_BEGIN action=%s path=%s", command.action, path)
    try:
        (opener or os.startfile)(str(path))
    except Exception as exc:
        logger.warning("LOCAL_FILE_OPEN_FAILED error=%s", exc)
        notice = "Interface: nao consegui abrir essa pasta."
        logger.info("LOCAL_FILE_COMMAND_TEXT_ONLY_NOTICE text=%r", notice)
        return {"handled": True, "ok": False, "notice": notice, "path": str(path)}
    logger.info("LOCAL_FILE_OPEN_FOLDER_OK path=%s", path)
    logger.info("LOCAL_FILE_COMMAND_EXECUTE_DONE action=%s", command.action)
    notice = f"Interface: abri a pasta {path}."
    logger.info("LOCAL_FILE_COMMAND_TEXT_ONLY_NOTICE text=%r", notice)
    return {"handled": True, "ok": True, "notice": notice, "path": str(path)}


__all__ = [
    "LocalFileCommand",
    "detect_local_file_command",
    "execute_local_file_command",
    "is_local_file_phrase",
    "normalize_local_file_text",
]
