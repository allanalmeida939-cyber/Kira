"""Documentos e PDF por voz: converter, juntar, separar, criar e ler arquivos.

Acha o arquivo que o Chefe fala ("esse Word", "os PDFs do Downloads", "o contrato") nas pastas de sempre, faz a
operação e salva onde ele pediu ("na área de trabalho", "em Downloads", "na pasta Faculdade"). Sem lugar claro, salva
na pasta padrão da Kira: Documentos\\Kira\\<PDFs | Textos | Planilhas | Slides | Imagens>.
Word/Excel/PowerPoint -> PDF usa o LibreOffice (sem janela); PDF -> Word usa o pdf2docx; o resto é Python puro.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("kira.app.documents")

HOME = Path.home()
KIRA_ROOT = HOME / "Documents" / "Kira"
CATEGORY = {".pdf": "PDFs", ".docx": "Textos", ".doc": "Textos", ".odt": "Textos", ".txt": "Textos", ".md": "Textos", ".rtf": "Textos",
            ".xlsx": "Planilhas", ".xls": "Planilhas", ".ods": "Planilhas", ".csv": "Planilhas", ".pptx": "Slides", ".ppt": "Slides",
            ".odp": "Slides", ".png": "Imagens", ".jpg": "Imagens", ".jpeg": "Imagens", ".webp": "Imagens"}
OFFICE_IN = {".doc", ".docx", ".odt", ".rtf", ".txt", ".html", ".htm", ".xls", ".xlsx", ".ods", ".csv", ".ppt", ".pptx", ".odp"}
IMAGE_IN = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
KIND_WORDS = {"word": {".docx", ".doc", ".odt", ".rtf"}, "pdf": {".pdf"}, "planilha": {".xlsx", ".xls", ".ods", ".csv"}, "excel": {".xlsx", ".xls"},
              "powerpoint": {".pptx", ".ppt"}, "slide": {".pptx", ".ppt", ".odp"}, "apresentacao": {".pptx", ".ppt", ".odp"},
              "imagem": IMAGE_IN, "foto": IMAGE_IN, "texto": {".txt", ".md", ".docx"}, "documento": {".docx", ".doc", ".odt", ".pdf", ".txt"}}
FORMATS = {"pdf": ".pdf", "word": ".docx", "docx": ".docx", "doc": ".docx", "texto": ".txt", "txt": ".txt", "excel": ".xlsx", "planilha": ".xlsx",
           "xlsx": ".xlsx", "csv": ".csv", "png": ".png", "imagem": ".png", "jpg": ".jpg"}
SEARCH_DEPTH = 4
MAX_SCAN = 20000
SOFFICE_TIMEOUT_S = 180


class DocumentError(Exception):
    """Mensagem curta, em português, que a Kira pode falar."""


def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(text or "").lower()) if unicodedata.category(c) != "Mn").strip()


def _known_folder(guid: str, fallback: Path) -> Path:
    """Pastas do Windows (Área de Trabalho pode estar no OneDrive)."""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8)]

        raw = bytes.fromhex(guid.replace("-", ""))
        g = GUID(int.from_bytes(raw[0:4], "big"), int.from_bytes(raw[4:6], "big"), int.from_bytes(raw[6:8], "big"), (wintypes.BYTE * 8)(*raw[8:]))
        out = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out)) == 0:
            path = Path(out.value)
            ctypes.windll.ole32.CoTaskMemFree(out)
            return path
    except Exception:  # noqa: BLE001 - fora do Windows / sem permissão: usa o caminho comum
        pass
    return fallback


def places() -> dict[str, Path]:
    desktop = _known_folder("B4BFCC3A-DB2C-424C-B029-7FE99A87C641", HOME / "Desktop")
    return {
        "area de trabalho": desktop, "desktop": desktop, "tela inicial": desktop,
        "downloads": _known_folder("374DE290-123F-4565-9164-39C4925E467B", HOME / "Downloads"), "baixados": HOME / "Downloads",
        "documentos": _known_folder("FDD39AD0-238F-46AF-ADB4-6C85480369C7", HOME / "Documents"),
        "imagens": _known_folder("33E28130-4E1E-4676-835A-98395C3BC3BB", HOME / "Pictures"), "fotos": HOME / "Pictures",
        "musicas": HOME / "Music", "videos": HOME / "Videos", "kira": KIRA_ROOT, "pasta da kira": KIRA_ROOT,
    }


def search_roots() -> list[Path]:
    p = places()
    roots = [p["area de trabalho"], p["downloads"], p["documentos"], p["imagens"]]
    return [r for i, r in enumerate(roots) if r.exists() and r not in roots[:i]]


def _walk(root: Path, depth: int = SEARCH_DEPTH) -> Iterable[Path]:
    count = 0
    stack = [(root, 0)]
    while stack:
        folder, level = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            count += 1
            if count > MAX_SCAN:
                return
            name = entry.name
            if name.startswith(("~$", ".")) or name in {"node_modules", "venv", ".venv", "__pycache__", "AppData"}:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if level < depth:
                        stack.append((Path(entry.path), level + 1))
                elif entry.is_file():
                    yield Path(entry.path)
            except OSError:
                continue


def resolve_folder(where: str) -> Path | None:
    """'área de trabalho', 'downloads', 'pasta Faculdade', 'C:\\Trabalhos' -> pasta. None = não deu para entender."""
    text = str(where or "").strip().strip('"')
    if not text:
        return None
    if re.match(r"^[a-zA-Z]:[\\/]", text):
        return Path(text)
    key = fold(text)
    key = re.sub(r"^(na|no|em|para|pra|dentro da|dentro do)\s+", "", key)
    key = re.sub(r"^(minha|meu|a|o)\s+", "", key)
    known = places()
    for name, path in known.items():
        if key == name or key == f"pasta {name}" or key == f"pasta de {name}" or key == f"pasta dos {name}" or key == f"pasta das {name}":
            return path
    # "pasta Faculdade" / "Faculdade dentro de documentos": acha uma pasta com esse nome nas pastas de sempre
    match = re.match(r"^(?:pasta\s+)?(.+?)(?:\s+(?:dentro\s+)?(?:da|do|de|em|na|no)\s+(.+))?$", key)
    wanted, parent_name = (match.group(1), match.group(2)) if match else (key, None)
    parents = [known[parent_name]] if parent_name in known else search_roots()
    for parent in parents:
        direct = [p for p in parent.iterdir() if p.is_dir() and fold(p.name) == wanted] if parent.exists() else []
        if direct:
            return direct[0]
    for parent in parents:
        for folder, _dirs, _files in os.walk(parent):
            if fold(Path(folder).name) == wanted:
                return Path(folder)
            if folder.count(os.sep) - str(parent).count(os.sep) >= 3:
                _dirs[:] = []
    return None


def default_folder(suffix: str) -> Path:
    return KIRA_ROOT / CATEGORY.get(suffix.lower(), "Outros")


def output_folder(where: str, suffix: str) -> tuple[Path, bool]:
    """(pasta, foi_pedida). Lugar não claro: pasta padrão da Kira."""
    folder = resolve_folder(where) if where else None
    if folder is None:
        return default_folder(suffix), False
    return folder, True


def safe_name(name: str, fallback: str = "Documento") -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name or "")).strip().rstrip(".")
    return (clean or fallback)[:120]


def unique_path(folder: Path, stem: str, suffix: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}{suffix}"
    n = 2
    while path.exists():
        path = folder / f"{stem} ({n}){suffix}"
        n += 1
    return path


def _score(path: Path, words: list[str], kinds: set[str] | None, phrase: str = "") -> float:
    if kinds and path.suffix.lower() not in kinds:
        return -1
    name = fold(path.stem)
    hit = sum(1 for w in words if w in name)
    # todas as palavras precisam bater: "Kira teste juntos" não pode virar outro "Kira teste..." (2026-11-02)
    if words and hit < len(words):
        return -1
    if phrase and (name == phrase or phrase in name):  # o nome dito por inteiro ("kira teste pdf a") ganha de todos
        hit += 100 if name == phrase else 50
    try:
        age_days = (time.time() - path.stat().st_mtime) / 86400
    except OSError:
        return -1
    return hit * 10 + max(0.0, 5 - age_days / 7)


def find_files(hint: str, *, kinds: set[str] | None = None, limit: int = 1, roots: list[Path] | None = None) -> list[Path]:
    """Acha arquivos pelo que o Chefe disse: caminho completo, nome (ou parte) e tipo ('o Word', 'os PDFs').
    Sem nome, pega os mais recentes daquele tipo ('esse Word' = o Word mais novo da Área de Trabalho/Downloads)."""
    text = str(hint or "").strip().strip('"')
    if text and Path(text).is_file():
        return [Path(text)]
    key = fold(text)
    phrase = " ".join(re.split(r"[^a-z0-9]+", key)).strip()  # o nome inteiro, antes de tirar tipo e pasta
    kinds = set(kinds or ())
    for word, exts in KIND_WORDS.items():
        if re.search(rf"\b{word}s?\b", key):
            kinds |= exts
            key = re.sub(rf"\b(?:o|a|os|as|esse|essa|esses|essas|este|esta|meu|minha|meus|minhas|do|da|de)?\s*{word}s?\b", " ", key)
    folder_hint = None
    for name, path in places().items():
        if re.search(rf"\b(?:da|do|na|no|em)\s+{name}\b", key):
            folder_hint = path
            key = re.sub(rf"\b(?:da|do|na|no|em)\s+{name}\b", " ", key)
    words = [w for w in re.split(r"[^a-z0-9]+", key) if len(w) > 2 and w not in {"esse", "essa", "esses", "essas", "este", "esta", "arquivo",
                                                                                 "arquivos", "meu", "minha", "que", "com", "todos", "todas", "ultimo", "ultima"}]
    scored = []
    for root in ([folder_hint] if folder_hint else (roots or search_roots())):
        for path in _walk(root):
            score = _score(path, words, kinds or None, phrase)
            if score >= 0:
                scored.append((score, path.stat().st_mtime, path))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [path for _, _, path in scored[:limit]]


def soffice() -> str | None:
    for candidate in (shutil.which("soffice"), r"C:\Program Files\LibreOffice\program\soffice.exe", r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"):
        if candidate and Path(candidate).exists():
            return candidate
    return None


@dataclass
class Result:
    path: Path
    chosen_place: bool
    note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"ok": True, "arquivo": str(self.path), "nome": self.path.name, "pasta": str(self.path.parent), "onde": friendly(self.path.parent),
                "lugar_pedido": self.chosen_place, "obs": self.note}


def friendly(folder: Path) -> str:
    known = {v: k for k, v in places().items()}
    for path, name in sorted(((p, n) for p, n in known.items()), key=lambda item: -len(str(item[0]))):
        try:
            rest = folder.relative_to(path)
        except ValueError:
            continue
        label = {"area de trabalho": "Área de Trabalho", "kira": "Documentos\\Kira", "pasta da kira": "Documentos\\Kira", "documentos": "Documentos", "downloads": "Downloads",
                 "imagens": "Imagens"}.get(name, name.title())
        return label + (f"\\{rest}" if str(rest) != "." else "")
    return str(folder)


# ---------- operações ----------
def convert(src: Path, to: str, *, where: str = "", name: str = "") -> Result:
    target = FORMATS.get(fold(to), "." + fold(to).lstrip("."))
    src_ext = src.suffix.lower()
    if src_ext == target:
        raise DocumentError(f"O arquivo já está em {target[1:].upper()}.")
    folder, chosen = output_folder(where, target)
    out = unique_path(folder, safe_name(name or src.stem), target)
    if target == ".pdf" and src_ext in IMAGE_IN:
        from PIL import Image

        Image.open(src).convert("RGB").save(out, "PDF", resolution=150)
    elif target == ".pdf" and src_ext in OFFICE_IN:
        _soffice_convert(src, "pdf", out)
    elif src_ext == ".pdf" and target == ".docx":
        from pdf2docx import Converter

        conv = Converter(str(src))
        try:
            conv.convert(str(out))
        finally:
            conv.close()
    elif src_ext == ".pdf" and target == ".txt":
        out.write_text(read_text(src, limit=None), encoding="utf-8")
    elif src_ext == ".pdf" and target in {".png", ".jpg"}:
        import fitz  # PyMuPDF (vem com o pdf2docx)

        with fitz.open(src) as doc:
            doc[0].get_pixmap(dpi=150).save(str(out))
    elif src_ext in OFFICE_IN and target in {".docx", ".xlsx", ".txt", ".csv", ".odt"}:
        _soffice_convert(src, target[1:], out)
    else:
        raise DocumentError(f"Não sei converter {src_ext[1:].upper()} para {target[1:].upper()}.")
    return Result(out, chosen)


def _soffice_convert(src: Path, fmt: str, out: Path) -> None:
    exe = soffice()
    if not exe:
        raise DocumentError("Para converter Word, Excel ou PowerPoint eu preciso do LibreOffice, que ainda está sendo instalado.")
    with tempfile.TemporaryDirectory() as tmp:
        # perfil próprio: não briga com um LibreOffice aberto pelo Chefe
        profile = Path(tmp) / "perfil"
        cmd = [exe, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore", "--convert-to", fmt, "--outdir", tmp, str(src)]
        run = subprocess.run(cmd, capture_output=True, timeout=SOFFICE_TIMEOUT_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        made = [p for p in Path(tmp).iterdir() if p.is_file() and p.suffix.lower() == f".{fmt}"]
        if run.returncode != 0 or not made:
            logger.info("DOC_SOFFICE_FAILED code=%s err=%s", run.returncode, run.stderr[-300:])
            raise DocumentError("O LibreOffice não conseguiu converter esse arquivo.")
        shutil.move(str(made[0]), out)


def merge_pdfs(files: list[Path], *, where: str = "", name: str = "") -> Result:
    from pypdf import PdfWriter

    pdfs = [f for f in files if f.suffix.lower() == ".pdf"]
    if len(pdfs) < 2:
        raise DocumentError("Para juntar eu preciso de pelo menos dois PDFs.")
    folder, chosen = output_folder(where, ".pdf")
    out = unique_path(folder, safe_name(name or f"{pdfs[0].stem} + {len(pdfs) - 1}"), ".pdf")
    writer = PdfWriter()
    for f in pdfs:
        writer.append(str(f))
    with open(out, "wb") as handle:
        writer.write(handle)
    return Result(out, chosen, f"{len(pdfs)} PDFs juntados, nesta ordem: " + ", ".join(f.name for f in pdfs))


def parse_pages(spec: str, total: int) -> list[int]:
    """'1-3, 5' -> [0, 1, 2, 4] (páginas fora do arquivo são ignoradas)."""
    pages: list[int] = []
    for part in re.split(r"[,;e]+", fold(spec)):
        part = part.strip().replace("ate", "-").replace(" a ", "-")
        if not part:
            continue
        if "-" in part:
            a, b = (int(x) for x in re.findall(r"\d+", part)[:2])
            pages += list(range(min(a, b), max(a, b) + 1))
        elif part.isdigit():
            pages.append(int(part))
    return [p - 1 for p in dict.fromkeys(pages) if 1 <= p <= total]


def split_pdf(src: Path, pages: str, *, where: str = "", name: str = "") -> Result:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(src))
    chosen_pages = parse_pages(pages, len(reader.pages)) if pages else list(range(len(reader.pages)))
    if not chosen_pages:
        raise DocumentError(f"Esse PDF tem {len(reader.pages)} páginas; diga quais quer (por exemplo 1 a 3).")
    folder, chosen = output_folder(where, ".pdf")
    out = unique_path(folder, safe_name(name or f"{src.stem} (páginas {pages or 'todas'})"), ".pdf")
    writer = PdfWriter()
    for index in chosen_pages:
        writer.add_page(reader.pages[index])
    with open(out, "wb") as handle:
        writer.write(handle)
    return Result(out, chosen, f"{len(chosen_pages)} página(s)")


def _lines(content: str) -> list[str]:
    return [line.rstrip() for line in str(content or "").replace("\r", "").split("\n")]


def create(title: str, content: str, fmt: str = "word", *, where: str = "") -> Result:
    """Cria Word, PDF, texto ou planilha. content: linhas; '# ' título, '## ' seção, '- ' item, '[ ] ' caixa, 'a | b' tabela."""
    suffix = FORMATS.get(fold(fmt), ".docx")
    folder, chosen = output_folder(where, suffix)
    out = unique_path(folder, safe_name(title), suffix)
    lines = _lines(content)
    if suffix == ".txt":
        out.write_text(f"{title}\n\n" + "\n".join(lines), encoding="utf-8")
    elif suffix in {".xlsx", ".csv"}:
        rows = [[cell.strip() for cell in re.split(r"\s*\|\s*|\t|;", line.lstrip("-* ").strip())] for line in lines if line.strip()]
        if suffix == ".csv":
            import csv

            with open(out, "w", newline="", encoding="utf-8-sig") as handle:
                csv.writer(handle, delimiter=";").writerows(rows)
        else:
            from openpyxl import Workbook
            from openpyxl.styles import Font

            wb = Workbook()
            ws = wb.active
            ws.title = safe_name(title)[:30]
            for row in rows:
                ws.append(row)
            for cell in ws[1] if rows else []:
                cell.font = Font(bold=True)
            for column in ws.columns:
                ws.column_dimensions[column[0].column_letter].width = min(60, max(10, *(len(str(c.value or "")) + 2 for c in column)))
            wb.save(out)
    else:
        docx_path = out if suffix == ".docx" else Path(tempfile.mkdtemp()) / f"{safe_name(title)}.docx"
        _write_docx(docx_path, title, lines)
        if suffix == ".pdf":
            try:
                _soffice_convert(docx_path, "pdf", out)
            except DocumentError:
                _write_pdf(out, title, lines)  # sem LibreOffice: PDF simples direto
    return Result(out, chosen)


def _write_docx(path: Path, title: str, lines: list[str]) -> None:
    from docx import Document

    doc = Document()
    doc.add_heading(title, level=0)
    table_rows: list[list[str]] = []

    def flush() -> None:
        if not table_rows:
            return
        table = doc.add_table(rows=0, cols=max(len(r) for r in table_rows))
        table.style = "Light Grid Accent 1"
        for row in table_rows:
            cells = table.add_row().cells
            for i, value in enumerate(row):
                cells[i].text = value
        table_rows.clear()

    for line in lines:
        text = line.strip()
        if "|" in text and not text.startswith(("#", "-")):
            table_rows.append([c.strip() for c in text.strip("|").split("|")])
            continue
        flush()
        if not text:
            continue
        if text.startswith("## "):
            doc.add_heading(text[3:], level=2)
        elif text.startswith("# "):
            doc.add_heading(text[2:], level=1)
        elif text.startswith(("[ ] ", "[x] ", "[X] ")):
            doc.add_paragraph(("☑ " if text[1] in "xX" else "☐ ") + text[4:])
        elif text.startswith(("- ", "* ", "• ")):
            doc.add_paragraph(text[2:], style="List Bullet")
        elif re.match(r"^\d+[.)]\s", text):
            doc.add_paragraph(re.sub(r"^\d+[.)]\s", "", text), style="List Number")
        else:
            doc.add_paragraph(text)
    flush()
    doc.save(path)


def _write_pdf(path: Path, title: str, lines: list[str]) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    styles = getSampleStyleSheet()
    story = [Paragraph(title, styles["Title"]), Spacer(1, 12)]
    for line in lines:
        text = line.strip()
        if not text:
            story.append(Spacer(1, 6))
        elif text.startswith("# "):
            story.append(Paragraph(text[2:], styles["Heading1"]))
        elif text.startswith("## "):
            story.append(Paragraph(text[3:], styles["Heading2"]))
        else:
            story.append(Paragraph(("• " + text[2:]) if text.startswith(("- ", "* ")) else text, styles["BodyText"]))
    SimpleDocTemplate(str(path), pagesize=A4, title=title).build(story)


def read_text(path: Path, limit: int | None = 6000) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        text = "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    elif ext == ".docx":
        from docx import Document

        text = "\n".join(p.text for p in Document(str(path)).paragraphs)
    elif ext in {".txt", ".md", ".csv"}:
        text = path.read_text(encoding="utf-8", errors="replace")
    elif ext in {".xlsx"}:
        from openpyxl import load_workbook

        wb = load_workbook(str(path), read_only=True, data_only=True)
        text = "\n".join(" | ".join("" if v is None else str(v) for v in row) for ws in wb.worksheets for row in ws.iter_rows(values_only=True))
    else:
        raise DocumentError(f"Não sei ler {ext[1:].upper()}.")
    return text if limit is None else text[:limit]


__all__ = ["DocumentError", "Result", "convert", "create", "find_files", "fold", "friendly", "merge_pdfs", "output_folder", "parse_pages",
           "read_text", "resolve_folder", "soffice", "split_pdf"]
