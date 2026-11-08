"""Trabalho completo por voz ("faz um trabalho sobre a Revolução Francesa, com capa e imagens, em PDF").

Roda em segundo plano para a voz da Kira não travar (antes ela tentava escrever o trabalho inteiro na própria fala e
parava de responder): o Gemini de texto escreve as seções em JSON, cada seção ganha uma imagem livre do Wikimedia
Commons, e o Word sai com capa (padrão ABNT: instituição, autor, título, cidade e ano), sumário simples, seções com
figura e legenda, conclusão e referências; PDF pelo LibreOffice se pedido. No fim a Kira avisa (fala e sininho).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from services import documents as docs

logger = logging.getLogger("kira.app.school_work")

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
# o Wikimedia recusa (403) identificação fora do padrão "Nome/versão (URL ou contato)"
WIKIMEDIA_UA = "KiraBot/8.0 (https://commons.wikimedia.org/wiki/Commons:API; personal use)"
MAX_SECTIONS = 8

PROMPT = """Escreva um trabalho escolar em português do Brasil sobre: {tema}
{detalhes}
Responda SÓ com JSON neste formato:
{{"titulo": "...", "subtitulo": "...", "introducao": "2 a 3 parágrafos",
 "secoes": [{{"titulo": "...", "texto": "3 a 5 parágrafos separados por linha em branco", "imagem": "busca curta em inglês para uma foto ou ilustração dessa seção"}}],
 "conclusao": "1 a 2 parágrafos", "referencias": ["referências reais e conhecidas, no formato ABNT"]}}
Use {secoes} seções. Texto claro, correto e com fatos verdadeiros; não invente citações nem números."""


def parse_json(raw: str) -> dict[str, Any]:
    text = str(raw or "")
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start:end + 1]) if start >= 0 < end else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _clean_image(raw: bytes) -> bytes | None:
    """Imagem regravada como JPEG RGB comum (até 1600 px): qualquer leitor de PDF abre."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGBA")
                base = Image.new("RGB", img.size, (255, 255, 255))
                base.paste(img, mask=img.split()[-1])
                img = base
            else:
                img = img.convert("RGB")
            img.thumbnail((1600, 1600))
            if min(img.size) < 120:
                return None
            out = io.BytesIO()
            img.save(out, "JPEG", quality=86, optimize=True)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - não é imagem válida
        return None


async def commons_image(client: httpx.AsyncClient, query: str) -> tuple[bytes, str] | None:
    """Uma imagem livre do Wikimedia Commons para a busca (bytes e crédito)."""
    if not query:
        return None
    try:
        data = (await client.get(COMMONS_API, params={"action": "query", "format": "json", "generator": "search", "gsrsearch": f"{query} filetype:bitmap",
                                                       "gsrnamespace": 6, "gsrlimit": 5, "prop": "imageinfo", "iiprop": "url|extmetadata|mime",
                                                       "iiurlwidth": 900}, timeout=20)).json()
    except (httpx.HTTPError, ValueError):
        return None
    for page in sorted((data.get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 99)):
        info = (page.get("imageinfo") or [{}])[0]
        if info.get("mime") not in {"image/jpeg", "image/png"} or not info.get("thumburl"):
            continue
        try:
            response = await client.get(info["thumburl"], timeout=30)
        except httpx.HTTPError:
            continue
        image = _clean_image(response.content) if response.status_code == 200 else None
        if not image:  # página de erro, PNG 16 bits, CMYK...: o leitor de PDF mostrava a carinha triste (2026-11-05)
            continue
        artist = re.sub(r"<[^>]+>", "", ((info.get("extmetadata") or {}).get("Artist") or {}).get("value", "")).strip()
        license_name = ((info.get("extmetadata") or {}).get("LicenseShortName") or {}).get("value", "")
        credit = f"Fonte: Wikimedia Commons{f' ({artist[:60]})' if artist else ''}{f', {license_name}' if license_name else ''}."
        return image, credit
    return None


def build_docx(path: Path, content: dict[str, Any], cover: dict[str, str], images: list[tuple[bytes, str] | None]) -> None:
    """Trabalho com acabamento editorial: capa em faixa colorida (sem parágrafos vazios empurrando o ano para uma
    página sozinha), títulos com cor, cabeçalho discreto, rodapé com número de página real e quebras controladas."""
    from docx import Document
    from docx.enum.section import WD_SECTION
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    INK, ACCENT, SOFT, MUTED = RGBColor(0x1F, 0x23, 0x3A), RGBColor(0x2B, 0x4C, 0x9B), "EEF2FB", RGBColor(0x6B, 0x72, 0x80)
    BAND = "1F2F5C"
    title = str(content.get("titulo") or cover.get("tema", "")).strip()

    doc = Document()
    sec = doc.sections[0]
    sec.top_margin, sec.bottom_margin, sec.left_margin, sec.right_margin = Cm(3), Cm(2), Cm(3), Cm(2)
    sec.different_first_page_header_footer = True  # capa sem cabeçalho/número
    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size, normal.font.color.rgb = "Calibri", Pt(11.5), INK
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
    for level, size in ((1, 16), (2, 13)):
        h = doc.styles[f"Heading {level}"]
        h.font.name, h.font.size, h.font.bold, h.font.color.rgb = "Calibri", Pt(size), True, ACCENT
        h.paragraph_format.space_before, h.paragraph_format.space_after = Pt(18 if level == 1 else 12), Pt(6)
        h.paragraph_format.keep_with_next = True

    # Título de seção: filete colorido embaixo (borda do parágrafo no próprio estilo)
    h1 = doc.styles["Heading 1"].element.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    for key, value in (("w:val", "single"), ("w:sz", "12"), ("w:space", "4"), ("w:color", "7C3AED")):
        bottom.set(qn(key), value)
    borders.append(bottom)
    h1.append(borders)

    def shade(cell, color: str) -> None:
        props = cell._tc.get_or_add_tcPr()
        fill = OxmlElement("w:shd")
        fill.set(qn("w:val"), "clear"); fill.set(qn("w:color"), "auto"); fill.set(qn("w:fill"), color)
        props.append(fill)

    def line(container, text: str, size: float, *, bold: bool = False, color=INK, align=WD_ALIGN_PARAGRAPH.CENTER, before: int = 0, after: int = 0):
        paragraph = container.add_paragraph()
        paragraph.alignment = align
        paragraph.paragraph_format.space_before, paragraph.paragraph_format.space_after = Pt(before), Pt(after)
        run = paragraph.add_run(text)
        run.bold, run.font.size, run.font.color.rgb = bold, Pt(size), color
        return paragraph

    def page_field(paragraph) -> None:
        run = paragraph.add_run()
        for kind, text in (("begin", None), (None, "PAGE"), ("end", None)):
            if kind:
                el = OxmlElement("w:fldChar"); el.set(qn("w:fldCharType"), kind)
            else:
                el = OxmlElement("w:instrText"); el.set(qn("xml:space"), "preserve"); el.text = text
            run._r.append(el)
        run.font.size, run.font.color.rgb = Pt(9), MUTED

    # ---------- capa: instituição/autor no topo, faixa com título, cidade/ano no pé (tudo numa página) ----------
    line(doc, cover.get("instituicao", "").upper(), 11, bold=True, color=ACCENT, after=2)
    line(doc, cover.get("autor", ""), 11, color=MUTED, after=0)
    band = doc.add_table(rows=1, cols=1)
    band.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell = band.rows[0].cells[0]
    shade(cell, BAND)
    cell.paragraphs[0].paragraph_format.space_before = Pt(70)
    head = cell.paragraphs[0]; head.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = head.add_run(title.upper()); run.bold, run.font.size, run.font.color.rgb = True, Pt(24), RGBColor(0xFF, 0xFF, 0xFF)
    if content.get("subtitulo"):
        line(cell, str(content["subtitulo"]), 13, color=RGBColor(0xC7, 0xD2, 0xFE), before=8)
    line(cell, "Trabalho escolar", 10, color=RGBColor(0x93, 0xA5, 0xD8), before=40, after=60)
    line(doc, f"{cover.get('cidade', '')}".strip() + (" · " if cover.get("cidade") else "") + str(cover.get("ano") or date.today().year),
         11, color=MUTED, before=150)
    doc.add_section(WD_SECTION.NEW_PAGE)

    # cabeçalho/rodapé das páginas internas (a seção nova herda; a capa fica sem)
    body_section = doc.sections[-1]
    body_section.different_first_page_header_footer = False
    header = body_section.header; header.is_linked_to_previous = False
    hp = header.paragraphs[0]; hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    hr = hp.add_run(title[:80]); hr.font.size, hr.font.color.rgb = Pt(8.5), MUTED
    footer = body_section.footer; footer.is_linked_to_previous = False
    fp = footer.paragraphs[0]; fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    page_field(fp)

    sections = content.get("secoes") or []
    doc.add_heading("Sumário", level=1)
    for index, item in enumerate(["Introdução", *[s.get("titulo", "") for s in sections], "Conclusão", "Referências"], 1):
        p = doc.add_paragraph(f"{index}.  {item}")
        p.paragraph_format.space_after = Pt(3)
        p.paragraph_format.left_indent = Cm(0.4)

    def body(text: str) -> None:
        for block in [b.strip() for b in re.split(r"\n\s*\n", str(text or "")) if b.strip()]:
            paragraph = doc.add_paragraph(block)
            paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            paragraph.paragraph_format.first_line_indent = Cm(1.25)
            paragraph.paragraph_format.line_spacing = 1.4
            paragraph.paragraph_format.space_after = Pt(6)
            paragraph.paragraph_format.widow_control = True

    # introdução começa em página nova; seções seguem corridas (sem páginas meio vazias)
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
    doc.add_heading("1. Introdução", level=1)
    body(content.get("introducao", ""))
    figure = 0
    for index, section in enumerate(sections, 2):
        doc.add_heading(f"{index}. {section.get('titulo', '')}", level=1)
        image = images[index - 2] if index - 2 < len(images) else None
        if image:
            figure += 1
            try:
                doc.add_picture(io.BytesIO(image[0]), width=Cm(12.5))
                pic = doc.paragraphs[-1]
                pic.alignment = WD_ALIGN_PARAGRAPH.CENTER
                pic.paragraph_format.keep_with_next = True  # imagem e legenda juntas
                caption = doc.add_paragraph(f"Figura {figure} – {section.get('titulo', '')}. {image[1]}")
                caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
                caption.paragraph_format.space_after = Pt(10)
                caption.runs[0].font.size, caption.runs[0].font.italic, caption.runs[0].font.color.rgb = Pt(9), True, MUTED
            except Exception:  # noqa: BLE001 - imagem que o Word não aceita: segue sem ela
                figure -= 1
        paragraphs = [b.strip() for b in re.split(r"\n\s*\n", str(section.get("texto", "") or "")) if b.strip()]
        if len(paragraphs) >= 3:  # destaque colorido com a frase-chave da seção
            key = re.split(r"(?<=[.!?])\s", paragraphs[0])[0][:260]
            box = doc.add_table(rows=1, cols=1)
            box.alignment = WD_TABLE_ALIGNMENT.CENTER
            cell = box.rows[0].cells[0]
            shade(cell, ["EDE9FE", "E0F2FE", "DCFCE7", "FEF3C7", "FCE7F3"][(index - 2) % 5])
            tip = cell.paragraphs[0]
            tip.paragraph_format.space_before, tip.paragraph_format.space_after = Pt(4), Pt(4)
            run = tip.add_run("★  " + key)
            run.italic, run.font.size, run.font.color.rgb = True, Pt(10.5), ACCENT
            doc.add_paragraph().paragraph_format.space_after = Pt(0)
        body(section.get("texto", ""))
    doc.add_heading(f"{len(sections) + 2}. Conclusão", level=1)
    body(content.get("conclusao", ""))
    refs = [str(ref) for ref in content.get("referencias") or [] if str(ref).strip()]
    if refs:
        doc.add_heading("Referências", level=1)
        for ref in refs:
            p = doc.add_paragraph(ref)
            p.paragraph_format.left_indent, p.paragraph_format.first_line_indent = Cm(0.8), Cm(-0.8)
            p.paragraph_format.space_after = Pt(4)
            p.runs[0].font.size = Pt(10.5)
    doc.save(path)


async def make_work(generate: Callable[[str], Awaitable[str]], *, tema: str, detalhes: str = "", formato: str = "pdf", onde: str = "",
                    autor: str = "", instituicao: str = "", cidade: str = "", secoes: int = 5) -> dict[str, Any]:
    secoes = max(2, min(MAX_SECTIONS, int(secoes or 5)))
    content = parse_json(await generate(PROMPT.format(tema=tema, detalhes=f"Pedidos do Chefe: {detalhes}" if detalhes else "", secoes=secoes)))
    if not content.get("secoes"):
        raise docs.DocumentError("O Gemini não devolveu o texto do trabalho; tente de novo.")
    async with httpx.AsyncClient(headers={"User-Agent": WIKIMEDIA_UA}, follow_redirects=True) as client:
        images = await asyncio.gather(*(commons_image(client, str(s.get("imagem") or s.get("titulo") or "")) for s in content["secoes"]))
    title = docs.safe_name(content.get("titulo") or tema, "Trabalho")
    suffix = ".pdf" if docs.fold(formato) == "pdf" else ".docx"
    folder, chosen = docs.output_folder(onde, suffix)
    cover = {"tema": tema, "autor": autor or "Chefe", "instituicao": instituicao, "cidade": cidade or "Fortaleza", "ano": str(date.today().year)}
    word = docs.unique_path(folder if suffix == ".docx" else docs.default_folder(".docx"), title, ".docx")
    await asyncio.to_thread(build_docx, word, content, cover, list(images))
    final = word
    if suffix == ".pdf":
        final = docs.unique_path(folder, title, ".pdf")
        await asyncio.to_thread(docs._soffice_convert, word, "pdf", final)
    logger.info("SCHOOL_WORK_DONE sections=%d images=%d pdf=%s", len(content["secoes"]), sum(1 for i in images if i), suffix == ".pdf")
    return {"ok": True, "arquivo": str(final), "nome": final.name, "onde": docs.friendly(final.parent), "lugar_pedido": chosen,
            "secoes": len(content["secoes"]), "imagens": sum(1 for i in images if i), "word": str(word)}


__all__ = ["make_work", "build_docx", "commons_image", "parse_json"]
