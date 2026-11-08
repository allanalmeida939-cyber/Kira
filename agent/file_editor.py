from __future__ import annotations

import difflib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from file_resolution import (
    TEXT_EDITABLE_EXTENSIONS,
    RecentFileRegistry,
    resolve_search_roots,
)
from memory.local_memory_store import get_local_memory_store


class KiraFileEditorError(Exception):
    pass


SUPPORTED_EXTENSIONS = set(TEXT_EDITABLE_EXTENSIONS) | {".cs"}

BLOCKED_BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".exe",
    ".dll",
    ".pdf",
    ".zip",
    ".rar",
    ".7z",
    ".mp3",
    ".mp4",
    ".avi",
    ".mov",
}

_RECENT_FILES = RecentFileRegistry()


def remember_recent_file(path: Path | str, *, action: str) -> None:
    _RECENT_FILES.remember(path, action=action)
    store = get_local_memory_store()
    try:
        store.record_file_action(path, action=action, origin="file_editor.recent")
    except Exception:
        pass


def resolve_edit_target(user_text: str, explicit_path: str | None = None) -> Path | None:
    candidates = resolve_edit_candidates(user_text, explicit_path=explicit_path)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]
    return None


def resolve_edit_candidates(user_text: str, explicit_path: str | None = None) -> list[Path]:
    if explicit_path:
        path = Path(str(explicit_path).strip().strip('"').strip("'")).expanduser()
        if path.exists() and path.is_file():
            return [path.resolve()]

    text = str(user_text or "").strip()
    if not text:
        return []

    roots = _resolve_roots_from_text(text)
    collected: list[Path] = []
    lowered_text = _fold(text)
    store = get_local_memory_store()

    # Prioriza artefatos recentes para comandos como "edita o ultimo txt".
    if any(token in lowered_text for token in ("ultimo", "último", "agora ha pouco", "acabou de", "que voce criou")):
        extension_hint = _extract_extension_hint(lowered_text)
        collected.extend(_RECENT_FILES.recent(extension=extension_hint, limit=6))
        local_last = store.get_last_active_file(extension=extension_hint)
        if local_last and local_last.get("path"):
            collected.append(Path(str(local_last["path"])))

    quoted = re.findall(r'["\']([^"\']+)["\']', text)
    for candidate in quoted:
        path = Path(candidate).expanduser()
        if path.is_absolute() and path.exists() and path.is_file():
            collected.append(path.resolve())

    for token in re.findall(r"[A-Za-z0-9_./\\:-]+\.[A-Za-z0-9]+", text):
        path = Path(token).expanduser()
        if path.is_absolute() and path.exists() and path.is_file():
            collected.append(path.resolve())

    file_candidates = re.findall(r"([A-Za-z0-9_\-]+\.[A-Za-z0-9]+)", text)
    if file_candidates:
        wanted = file_candidates[-1].lower()
        collected.extend(_search_all_by_filename(wanted, roots))

    ext_candidates = re.findall(r"\.(txt|md|py|html|css|js|json|cs|bat|cmd|csv)\b", text.lower())
    if not ext_candidates:
        ext_word_map = {
            "txt": ".txt",
            "texto": ".txt",
            "markdown": ".md",
            "md": ".md",
            "python": ".py",
            "py": ".py",
            "html": ".html",
            "css": ".css",
            "javascript": ".js",
            "js": ".js",
            "json": ".json",
            "bat": ".bat",
            "batch": ".bat",
            "csv": ".csv",
            "c#": ".cs",
            "csharp": ".cs",
            "cs": ".cs",
        }
        folded_text = _fold(text)
        for key, ext in ext_word_map.items():
            if key in folded_text:
                ext_candidates = [ext.replace(".", "")]
                break
    if ext_candidates:
        wanted_ext = f".{ext_candidates[-1]}"
        by_ext = _search_all_by_extension(wanted_ext, roots)
        collected.extend(by_ext)

    partial = _extract_partial_name(text)
    if partial:
        collected.extend(_search_all_by_partial_name(partial, roots))
        for item in store.search_active_files(partial, extension=_extract_extension_hint(lowered_text), limit=8):
            path = item.get("path")
            if path:
                collected.append(Path(str(path)))

    unique: list[Path] = []
    seen: set[str] = set()
    for item in collected:
        key = str(item).lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return sorted(unique, key=lambda p: _candidate_sort_key(p, lowered_text), reverse=True)


def detect_edit_ambiguity(
    instruction: str,
    candidates: list[Path],
    file_type: str | None = None,
    target_content: str | None = None,
) -> dict[str, Any] | None:
    lowered = _fold(instruction)

    if _is_instruction_vague(lowered):
        return {
            "ambiguity_type": "instruction",
            "message": "Nao ficou claro o que voce quer mudar. Voce quer alterar cor, texto, botao ou titulo?",
        }

    if len(candidates) > 1:
        return {
            "ambiguity_type": "file",
            "message": build_ambiguity_prompt("file", candidates=candidates),
            "candidates": [str(path) for path in candidates[:8]],
        }

    if target_content:
        if ("botao" in lowered or "botao" in lowered) and target_content.lower().count("<button") > 1:
            return {
                "ambiguity_type": "element",
                "message": "Encontrei mais de um botao nesse arquivo. Qual deles voce quer alterar?",
            }
        if "titulo" in lowered and target_content.lower().count("<title") > 1:
            return {
                "ambiguity_type": "element",
                "message": "Encontrei mais de um titulo possivel. Qual titulo voce quer alterar?",
            }

    return None


def build_ambiguity_prompt(ambiguity_type: str, *, candidates: list[Path] | None = None) -> str:
    if ambiguity_type == "file":
        options = candidates or []
        if not options:
            return "Qual arquivo voce quer que eu edite?"
        ext = options[0].suffix.lower().replace(".", "") if options[0].suffix else "arquivo"
        lines = [
            f"Chefe, encontrei {len(options)} arquivos {ext.upper()} possiveis:",
        ]
        for idx, item in enumerate(options[:8], start=1):
            lines.append(f"{idx}. {item.name}")
        lines.append("Qual deles voce quer que eu edite?")
        return "\n".join(lines)
    if ambiguity_type == "instruction":
        return "Nao ficou claro o que voce quer mudar. Voce quer alterar cor, texto, botao ou titulo?"
    if ambiguity_type == "element":
        return "Encontrei mais de um elemento possivel. Qual voce quer alterar?"
    return "Preciso de confirmacao para continuar a edicao."


def resolve_candidate_selection(user_text: str, candidates: list[Path]) -> Path | None:
    if not candidates:
        return None
    text = str(user_text or "").strip()
    if not text:
        return None

    number_match = re.search(r"\b(\d+)\b", text)
    if number_match:
        idx = int(number_match.group(1))
        if 1 <= idx <= len(candidates):
            return candidates[idx - 1]

    lowered = text.lower()
    matched = [item for item in candidates if item.name.lower() in lowered or item.stem.lower() in lowered]
    if len(matched) == 1:
        return matched[0]
    return None


def backup_file(path: Path) -> Path:
    target = Path(path)
    if not target.exists():
        raise KiraFileEditorError("Arquivo alvo nao existe para backup.")

    primary = target.with_name(f"{target.name}.bak")
    if not primary.exists():
        primary.write_bytes(target.read_bytes())
        return primary

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stamped = target.with_name(f"{target.stem}_{stamp}{target.suffix}.bak")
    stamped.write_bytes(target.read_bytes())
    return stamped


def read_text_file(path: Path) -> str:
    text, _encoding = _read_text_file_with_encoding(path)
    return text


def _read_text_file_with_encoding(path: Path) -> tuple[str, str]:
    target = Path(path)
    if not target.exists() or not target.is_file():
        raise KiraFileEditorError("Arquivo alvo nao encontrado.")
    suffix = target.suffix.lower()
    if suffix and suffix not in SUPPORTED_EXTENSIONS:
        raise KiraFileEditorError("Esse tipo de arquivo nao e editavel como texto.")
    if target.suffix.lower() in BLOCKED_BINARY_EXTENSIONS:
        raise KiraFileEditorError("Esse tipo de arquivo parece binario e nao pode ser editado.")
    raw = target.read_bytes()
    if b"\x00" in raw:
        raise KiraFileEditorError("Arquivo binario detectado. Nao vou editar.")
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise KiraFileEditorError("Nao consegui ler o arquivo em um encoding textual suportado.")


def write_text_file(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    try:
        Path(path).write_text(str(content), encoding=encoding)
    except PermissionError as exc:
        raise KiraFileEditorError("Sem permissao para salvar esse arquivo.") from exc


def detect_file_type(path: Path) -> str:
    suffix = Path(path).suffix.lower()
    return suffix if suffix else ".txt"


def build_edit_summary(
    original_content: str,
    new_content: str,
    instruction: str,
    file_type: str,
) -> str:
    if original_content == new_content:
        return "nao houve mudanca efetiva"

    lowered = _fold(instruction)
    if "troca" in lowered and " por " in lowered:
        repl_match = re.search(
            r"troca\s+['\"]?(.+?)['\"]?\s+por\s+['\"]?(.+?)['\"]?(?:\s|$)",
            instruction,
            flags=re.IGNORECASE,
        )
        if repl_match:
            old = repl_match.group(1).strip()
            new = repl_match.group(2).strip()
            return f"troquei '{old}' por '{new}'"

    if file_type == ".html":
        if "fundo escuro" in lowered:
            return "altei o tema para escuro no HTML"
        if "botao" in lowered:
            return "adicionei um botao no body"
        if "titulo" in lowered:
            return "alterei o titulo do HTML"

    if file_type == ".py" and ("funcao" in lowered or "fun\u00e7\u00e3o" in lowered):
        old_fns = set(re.findall(r"def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", original_content))
        new_fns = set(re.findall(r"def\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", new_content))
        added_fns = [name for name in new_fns if name not in old_fns]
        if added_fns:
            return f"adicionei a funcao {added_fns[0]}()"
        return "adicionei uma funcao Python"

    if file_type == ".js" and "funcao" in lowered:
        old_fns = set(re.findall(r"function\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", original_content))
        new_fns = set(re.findall(r"function\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", new_content))
        added_fns = [name for name in new_fns if name not in old_fns]
        if added_fns:
            return f"adicionei a funcao {added_fns[0]}() em JS"
        return "adicionei uma funcao JS"

    if file_type == ".cs" and "metodo" in lowered:
        return "adicionei um metodo na classe C#"

    old_lines = original_content.splitlines()
    new_lines = new_content.splitlines()
    diff = list(difflib.ndiff(old_lines, new_lines))
    added = sum(1 for line in diff if line.startswith("+ "))
    removed = sum(1 for line in diff if line.startswith("- "))

    if added and not removed:
        if new_content.startswith(original_content):
            return "adicionei conteudo no final do arquivo"
        if new_content.endswith(original_content):
            return "adicionei conteudo no comeco do arquivo"
        return f"adicionei {added} linha(s)"
    if removed and not added:
        return f"removi {removed} linha(s)"
    return f"ajustei o conteudo ({added} linha(s) adicionadas e {removed} removidas)"


def edit_file_content(path: Path, instruction: str, file_type: str | None = None) -> dict[str, Any]:
    target = Path(path)
    detected = (file_type or detect_file_type(target)).lower()
    if detected not in SUPPORTED_EXTENSIONS:
        raise KiraFileEditorError(f"Extensao {detected or '(sem extensao)'} nao suportada para edicao textual.")
    original, source_encoding = _read_text_file_with_encoding(target)
    backup = backup_file(target)

    edited = original
    try:
        edited, _ = _apply_edit(edited, instruction, detected)
        changed = edited != original
        summary = build_edit_summary(original, edited, instruction, detected)
        if changed:
            write_text_file(target, edited, encoding=source_encoding)
            remember_recent_file(target, action="edited")
            store = get_local_memory_store()
            store.record_file_action(target, action="edited", origin="file_editor.edit_file_content")
            store.record_folder_action(target.parent, action="touched", origin="file_editor.edit_file_content")
            store.record_folder_snapshot(target.parent, origin="file_editor.edit_file_content")
    except Exception:
        try:
            write_text_file(target, backup.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
        raise

    return {
        "target": str(target),
        "backup_path": str(backup),
        "backup_created": True,
        "edited": changed,
        "changed": changed,
        "file_type": detected,
        "summary": summary,
        "diff_log": {
            "target": str(target),
            "changed": changed,
            "summary": summary,
            "backup_created": True,
        },
    }


def _apply_edit(content: str, instruction: str, file_type: str) -> tuple[str, str]:
    instruction_text = str(instruction or "").strip()
    lowered = _fold(instruction_text)

    direct = _apply_direct_simple_edit(content, instruction_text, lowered)
    if direct is not None:
        return direct

    structured = _apply_structured_edit(content, instruction_text, lowered, file_type=file_type)
    if structured is not None:
        return structured

    return content, "nenhuma alteracao detectada"


def _apply_direct_simple_edit(
    content: str,
    instruction: str,
    lowered: str,
) -> tuple[str, str] | None:
    repl_match = re.search(
        r"troca\s+['\"]?(.+?)['\"]?\s+por\s+['\"]?(.+?)['\"]?(?:\s|$)",
        instruction,
        flags=re.IGNORECASE,
    )
    if repl_match:
        old = repl_match.group(1).strip()
        new = repl_match.group(2).strip()
        return content.replace(old, new), f"substitui '{old}' por '{new}'"

    add_end = re.search(r"(?:adiciona|insere).*(?:no final|fim):?\s*(.+)$", instruction, flags=re.IGNORECASE | re.DOTALL)
    if add_end:
        block = add_end.group(1).strip()
        sep = "" if content.endswith("\n") or not content else "\n"
        return f"{content}{sep}{block}\n", "conteudo adicionado no final"

    add_start = re.search(r"(?:adiciona|insere).*(?:no comeco|no inicio):?\s*(.+)$", instruction, flags=re.IGNORECASE | re.DOTALL)
    if add_start:
        block = add_start.group(1).strip()
        sep = "" if block.endswith("\n") else "\n"
        return f"{block}{sep}{content}", "conteudo adicionado no comeco"

    if "muda o texto do titulo" in lowered or "mudar o texto do titulo" in lowered:
        title_match = re.search(r"<title>.*?</title>", content, flags=re.IGNORECASE | re.DOTALL)
        if title_match:
            return re.sub(r"<title>.*?</title>", "<title>Titulo Atualizado</title>", content, flags=re.IGNORECASE | re.DOTALL), "titulo atualizado"

    return None


def _apply_structured_edit(
    content: str,
    instruction: str,
    lowered: str,
    *,
    file_type: str,
) -> tuple[str, str] | None:
    if file_type == ".html":
        if "fundo escuro" in lowered:
            if "</head>" in content.lower():
                style_block = "\n<style>body{background:#121212;color:#f2f2f2;}</style>\n"
                return _insert_before_case_insensitive(content, "</head>", style_block), "tema escuro aplicado"
            return f"<style>body{{background:#121212;color:#f2f2f2;}}</style>\n{content}", "tema escuro aplicado"
        if "adiciona um botao" in lowered or "adicionar um botao" in lowered:
            button = "\n<button id=\"kira-btn\">Novo Botao</button>\n"
            return _insert_before_case_insensitive(content, "</body>", button), "botao adicionado"

    if file_type == ".py":
        if "adiciona uma funcao" in lowered or "adicionar uma funcao" in lowered:
            fn = (
                "\n\ndef nova_funcao_kira():\n"
                "    \"\"\"Funcao adicionada automaticamente pela Kira.\"\"\"\n"
                "    return \"ok\"\n"
            )
            return f"{content.rstrip()}{fn}\n", "funcao Python adicionada"

    if file_type == ".js":
        if "adiciona funcao" in lowered or "adiciona uma funcao" in lowered:
            fn = "\n\nfunction novaFuncaoKira() {\n  return 'ok';\n}\n"
            return f"{content.rstrip()}{fn}\n", "funcao JS adicionada"

    if file_type == ".cs":
        if "adiciona um metodo" in lowered or "adicionar um metodo" in lowered:
            method = "\n    public string NovoMetodoKira()\n    {\n        return \"ok\";\n    }\n"
            class_close = content.rfind("}")
            if class_close != -1:
                return f"{content[:class_close]}{method}{content[class_close:]}", "metodo C# adicionado"
            return f"{content.rstrip()}\n{method}\n", "metodo C# adicionado"

    return None


def _resolve_roots_from_text(text: str) -> list[Path]:
    lowered = _fold(text)
    roots = resolve_search_roots(include_project=True)
    ranked: list[Path] = []
    for root in roots:
        root_text = _fold(str(root))
        score = 0
        if "desktop" in lowered or "area de trabalho" in lowered:
            if "desktop" in root_text:
                score += 8
        if "downloads" in lowered:
            if "downloads" in root_text:
                score += 8
        if "documentos" in lowered or "documents" in lowered:
            if "documents" in root_text:
                score += 8
        if "projeto" in lowered and str(Path.cwd()).lower() in str(root).lower():
            score += 12
        if Path.cwd() == root:
            score += 4
        ranked.append((score, root))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [root for _, root in ranked]


def _search_all_by_filename(name_lower: str, roots: list[Path]) -> list[Path]:
    matches: list[Path] = []
    for root in roots:
        try:
            for candidate in root.rglob("*"):
                if candidate.is_file() and candidate.name.lower() == name_lower:
                    matches.append(candidate)
        except Exception:
            continue
    return matches


def _search_all_by_extension(ext: str, roots: list[Path]) -> list[Path]:
    matches: list[Path] = []
    for root in roots:
        try:
            for candidate in root.rglob(f"*{ext}"):
                if candidate.is_file():
                    matches.append(candidate)
        except Exception:
            continue
    return matches


def _search_all_by_partial_name(partial: str, roots: list[Path]) -> list[Path]:
    matches: list[Path] = []
    for root in roots:
        try:
            for candidate in root.rglob("*"):
                if not candidate.is_file():
                    continue
                if partial in candidate.stem.lower():
                    matches.append(candidate)
        except Exception:
            continue
    return matches


def _extract_partial_name(text: str) -> str:
    lowered = _fold(text)
    match = re.search(r"(?:arquivo|file)\s+([a-z0-9_-]{3,})", lowered)
    if match:
        return match.group(1)
    return ""


def _extract_extension_hint(lowered_text: str) -> str | None:
    mapping = {
        "txt": ".txt",
        "texto": ".txt",
        "md": ".md",
        "markdown": ".md",
        "py": ".py",
        "python": ".py",
        "html": ".html",
        "css": ".css",
        "js": ".js",
        "javascript": ".js",
        "json": ".json",
        "bat": ".bat",
        "batch": ".bat",
        "csv": ".csv",
    }
    for key, ext in mapping.items():
        if key in lowered_text:
            return ext
    return None


def _candidate_sort_key(path: Path, lowered_text: str) -> tuple[float, float]:
    score = 0.0
    path_text = _fold(str(path))
    if str(Path.cwd()).lower() in str(path).lower():
        score += 15.0
    if "desktop" in lowered_text or "area de trabalho" in lowered_text:
        if "desktop" in path_text:
            score += 8.0
    if "downloads" in lowered_text and "downloads" in path_text:
        score += 8.0
    if ("documentos" in lowered_text or "documents" in lowered_text) and "documents" in path_text:
        score += 8.0
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return score, mtime


def _is_instruction_vague(lowered: str) -> bool:
    vague_patterns = [
        "muda a cor",
        "altera a cor",
        "troca o texto",
        "altera o botao",
        "altera o botao",
        "muda o botao",
        "altera isso",
        "muda isso",
    ]
    return any(pattern in lowered for pattern in vague_patterns)


def _fold(value: str) -> str:
    text = str(value or "").lower()
    table = str.maketrans(
        {
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
    )
    return " ".join(text.translate(table).split())


def _insert_before_case_insensitive(content: str, needle: str, block: str) -> str:
    lower_content = content.lower()
    lower_needle = needle.lower()
    idx = lower_content.rfind(lower_needle)
    if idx == -1:
        return f"{content.rstrip()}\n{block}\n"
    return f"{content[:idx]}{block}{content[idx:]}"
