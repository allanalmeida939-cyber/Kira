"""Design da Kira (2026-11-05): PDF diagramado, gráfico, diagrama e QR Code, sem chave e sem instalar nada pesado.

- PDF: HTML+CSS no estilo da Kira -> Chrome/Edge do PC em modo headless (--print-to-pdf). Capa com foto, sumário e imagens
  embutidas (Wikipédia; sem foto, gerada por IA). Salva onde o Chefe pediu; sem lugar, Documentos\\Kira\\PDFs.
- Gráfico e QR: QuickChart.io (Chart.js, grátis). Diagrama: mermaid.ink (fluxograma, sequência, Gantt, mapa mental...).
As imagens são baixadas para runtime/design e aparecem num painel (card) com a fonte.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import re
import subprocess
import tempfile
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from urllib.parse import quote
from fastapi import APIRouter, Body, Header, HTTPException, Request
from fastapi.responses import FileResponse

from services import documents
from services.local_request import is_local_request

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "runtime" / "design"
# a Wikimedia recusa User-Agent sem contato (devolvia HTML 403 e as fotos não vinham)
UA = {"User-Agent": f"KiraAssistant/8.0 ({os.getenv('KIRA_HTTP_CONTACT') or 'https://github.com/kira-local/kira'}) python-httpx"}
BROWSERS = (r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
CSS = """
@page { size: A4; margin: 18mm 16mm; }
body { font-family: 'Segoe UI', Arial, sans-serif; color: #1e1b2e; line-height: 1.55; font-size: 11.5pt; }
h1 { font-size: 24pt; margin: 0 0 4mm; color: #6d28d9; }
h2 { font-size: 15pt; color: #6d28d9; border-bottom: 2px solid #ede9fe; padding-bottom: 2mm; margin-top: 8mm; }
h3 { font-size: 12.5pt; color: #be185d; }
.capa { height: 252mm; display: flex; flex-direction: column; justify-content: flex-end; padding: 14mm; box-sizing: border-box;
  border-radius: 5mm; color: #fff; background: linear-gradient(160deg, #2e1065, #6d28d9 55%, #db2777); background-size: cover;
  background-position: center; page-break-after: always; position: relative; overflow: hidden; }
.capa::before { content: ""; position: absolute; inset: 0; background: linear-gradient(180deg, rgba(20,8,40,.15), rgba(20,8,40,.88)); }
.capa > * { position: relative; } .capa h1 { color: #fff; font-size: 34pt; line-height: 1.1; margin: 0 0 4mm; }
.capa .sub { font-size: 14pt; opacity: .92; } .capa .data { margin-top: 8mm; font-size: 10pt; letter-spacing: .12em; opacity: .8; }
.sumario { background: #f5f3ff; border-radius: 3mm; padding: 4mm 7mm; margin-bottom: 6mm; } .sumario b { color: #6d28d9; }
figure { margin: 5mm 0; page-break-inside: avoid; text-align: center; } figure img { max-height: 105mm; }
figcaption { font-size: 9pt; color: #6b6489; margin-top: 1.5mm; } h2 { page-break-after: avoid; }
* { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
table { width: 100%; border-collapse: collapse; margin: 4mm 0; } th { background: #7c3aed; color: #fff; text-align: left; }
th, td { padding: 2mm 3mm; border-bottom: 1px solid #e9e5f5; } tr:nth-child(even) td { background: #faf8ff; }
blockquote { border-left: 4px solid #db2777; margin: 4mm 0; padding: 2mm 5mm; background: #fdf2f8; }
img { max-width: 100%; border-radius: 2mm; } .rodape { color: #8b85a5; font-size: 9pt; margin-top: 10mm; }
li.feito { list-style: '☑ '; } li.todo { list-style: '☐ '; }
"""


def markdown_html(text: str) -> str:
    """Markdown simples -> HTML (títulos, listas, checklist, tabela 'a | b', citação, negrito, imagem por URL)."""
    out, table, items = [], [], []

    def flush() -> None:
        if items:
            out.append("<ul>" + "".join(items) + "</ul>")
            items.clear()
        if table:
            head, *rows = table
            out.append("<table><tr>" + "".join(f"<th>{c}</th>" for c in head) + "</tr>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</table>")
            table.clear()

    for raw in str(text or "").splitlines():
        line = html.escape(raw.strip())
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"(?<![*\w])\*([^*]+)\*(?!\*)", r"<i>\1</i>", line)
        img = re.fullmatch(r"!\[([^\]]*)\]\(((?:https?://|data:image/)[^)\s]+)\)", line)
        if img:  # imagem sozinha na linha = figura com legenda (crédito depois de ' — ')
            flush()
            out.append(f'<figure><img src="{img[2]}" alt=""><figcaption>{img[1]}</figcaption></figure>')
            continue
        if re.match(r"^\d+[.)] ", line):  # lista numerada vira item
            line = "- " + line.split(" ", 1)[1]
        if re.match(r"^[-*] \[( |x)\] ", line, re.I):
            items.append(f'<li class="{"feito" if line[3].lower() == "x" else "todo"}">{line[6:]}</li>')
            continue
        if line.startswith(("- ", "* ")):
            items.append(f"<li>{line[2:]}</li>")
            continue
        if "|" in line and not set(line) <= {"|", "-", " ", ":"}:
            table.append([c.strip() for c in line.strip("|").split("|")])
            continue
        if set(line) <= {"|", "-", " ", ":"} and line:
            continue
        flush()
        if line.startswith("### "):
            out.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{line[2:]}</h1>")
        elif line.startswith("&gt; "):
            out.append(f"<blockquote>{line[5:]}</blockquote>")
        elif line:
            out.append(f"<p>{line}</p>")
    flush()
    return "\n".join(out)


def make_pdf(title: str, content: str, subtitle: str = "", where: str = "", cover: str = "") -> Path:
    browser = next((b for b in BROWSERS if Path(b).exists()), None)
    if not browser:
        raise RuntimeError("Chrome/Edge não encontrado")
    title = re.sub(r"_+", " ", title).strip() or "Documento"  # 'Chernobyl_Historia' virava o título
    body = content if "<h1" in content or "<p" in content else markdown_html(content)
    heads = re.findall(r"<h2>(.*?)</h2>", body)
    toc = f"<div class='sumario'><b>Sumário</b><ol>{''.join(f'<li>{h}</li>' for h in heads)}</ol></div>" if len(heads) >= 3 else ""
    bg = f" style=\"background-image: url('{cover}')\"" if cover.startswith("data:image/") else ""
    page = (f"<!doctype html><html lang='pt-BR'><meta charset='utf-8'><title>{html.escape(title)}</title><style>{CSS}</style><body>"
            f"<div class='capa'{bg}><h1>{html.escape(title)}</h1>{f'<div class=sub>{html.escape(subtitle)}</div>' if subtitle else ''}"
            f"<div class='data'>KIRA · {date.today():%d/%m/%Y}</div></div>{toc}{body}"
            f"<div class='rodape'>Feito pela Kira</div></body></html>")
    page = re.sub(r"(?is)<script.*?</script>", "", page)  # PDF não roda código
    folder, _ = documents.output_folder(where, ".pdf")  # 'na área de trabalho' vale; sem lugar, pasta da Kira
    target = documents.unique_path(folder, documents.safe_name(title), ".pdf")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "doc.html"
        src.write_text(page, encoding="utf-8")
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", f"--user-data-dir={tmp}\\p",
                        f"--print-to-pdf={target}", src.as_uri()], timeout=120, capture_output=True, check=False)
    if not target.exists():
        raise RuntimeError("o navegador não gerou o PDF")
    return target


async def _data_uri(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(url, headers=UA)
    kind = r.headers.get("content-type", "").split(";")[0]
    if r.status_code != 200 or not kind.startswith("image/") or len(r.content) > 6_000_000:
        return ""
    return f"data:{kind};base64,{base64.b64encode(r.content).decode()}"


async def find_image(client: httpx.AsyncClient, query: str, allow_ai: bool = True) -> tuple[str, str]:
    """(data URI, crédito): foto da Wikipédia (pt, depois en); sem foto, gera uma (Pollinations, grátis)."""
    for lang in ("pt", "en"):
        try:
            r = await client.get(f"https://{lang}.wikipedia.org/w/api.php", params={
                "action": "query", "format": "json", "generator": "search", "gsrsearch": query, "gsrlimit": 6,
                "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 1200}, headers=UA)
            pages = sorted((r.json().get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
            # artigo do assunto antes de "(minissérie)", "(filme)"...: senão vinha pôster de série
            pages = [p for p in pages if p.get("thumbnail")]
            page = next((p for p in pages if "(" not in p.get("title", "")), pages[0] if pages else {})
            src = (page.get("thumbnail") or {}).get("source")
            if src and (uri := await _data_uri(client, src)):
                return uri, f"Wikipédia · {page.get('title', '')}"
        except (httpx.HTTPError, ValueError):
            continue
    if allow_ai:
        try:
            from services.image_creation import provider_url
            uri = await _data_uri(client, provider_url(f"{query}, detailed editorial illustration", 1024, 640, sum(map(ord, query)) % 99999))
            if uri:
                return uri, "imagem gerada pela Kira (IA)"
        except (httpx.HTTPError, ImportError):
            pass
    return "", ""


async def embed_images(title: str, content: str) -> tuple[str, str]:
    """Troca cada ![legenda](...) por imagem EMBUTIDA (antes saía como texto cru) e acha a foto da capa."""
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        first = re.search(r"!\[([^\]]+)\]", content)  # a 1ª imagem descreve o assunto melhor que o título
        cover, _ = await find_image(client, first[1] if first else re.sub(r"_+", " ", title), allow_ai=False)
        for m in reversed(list(re.finditer(r"!\[([^\]]*)\]\(([^)]*)\)", content))[:6]):
            alt, src = m[1].strip(), m[2].strip()
            uri, credit = ((await _data_uri(client, src)), "") if src.startswith("http") else ("", "")
            if not uri:
                uri, credit = await find_image(client, alt or Path(src).stem.replace("_", " "), allow_ai=False)
            if not uri:
                uri, credit = await find_image(client, f"{title} {alt}")
            caption = f"{alt} — {credit}" if credit else alt
            content = content[:m.start()] + (f"\n![{caption}]({uri})\n" if uri else "") + content[m.end():]
    return content, cover


async def fetch_image(url: str, *, post: dict | None = None) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=40, follow_redirects=True) as client:
        r = await (client.post(url, json=post) if post else client.get(url))
    r.raise_for_status()
    if not r.headers.get("content-type", "").startswith("image/"):
        raise ValueError("a fonte não devolveu imagem")
    path = OUT / f"{uuid.uuid4().hex[:10]}.png"
    path.write_bytes(r.content)
    return path


def create_design_router(*, publish: Callable[[dict], Awaitable[Any]], token_provider: Callable[[], str]) -> APIRouter:
    router = APIRouter()

    def allow(request: Request, token: str | None) -> None:
        if not (is_local_request(request) or (token and token == token_provider())):
            raise HTTPException(status_code=403, detail="origem nao autorizada")

    async def show(title: str, path: Path, credit: str, text: str = "") -> None:
        await publish({"type": "open_panel", "kind": "tutor", "id": f"tutor-card-{uuid.uuid4().hex[:6]}", "title": title[:60],
                       "props": {"mode": "card", "titulo": title, "texto": text, "imagem": {"url": f"/api/design/file/{path.name}", "credito": credit}}, "focus": False})

    @router.get("/api/design/file/{name}")
    async def file(name: str, request: Request) -> FileResponse:
        allow(request, None)
        path = OUT / Path(name).name
        if not path.exists():
            raise HTTPException(status_code=404)
        return FileResponse(path)

    @router.post("/api/design/{action}")
    async def act(action: str, request: Request, body: dict = Body(default={}), x_kira_internal: str | None = Header(default=None)) -> dict[str, Any]:
        allow(request, x_kira_internal)
        title = str(body.get("titulo") or "Kira")[:120]
        content = str(body.get("conteudo") or "")
        try:
            if action == "pdf":
                content, cover = await embed_images(title, content)
                path = await asyncio.to_thread(make_pdf, title, content, str(body.get("subtitulo") or ""), str(body.get("destino") or ""), cover)
                return {"ok": True, "arquivo": str(path), "pasta": str(path.parent)}
            if action == "abrir":  # 'abre o PDF' / 'abre a pasta' (tipo='pasta' mostra o arquivo selecionado no Explorer)
                target = Path(content.strip().strip('"'))
                if not target.exists():
                    return {"ok": False, "error": "caminho não existe"}
                if body.get("tipo") == "pasta" and target.is_file():
                    subprocess.Popen(["explorer", "/select,", str(target)])
                else:
                    os.startfile(str(target))  # noqa: S606 - arquivo local
                return {"ok": True, "aberto": str(target)}
            if action == "grafico":  # conteudo = JSON do Chart.js, ou "rótulo: valor" por linha
                try:
                    chart = json.loads(content)
                except ValueError:
                    pairs = [l.split(":", 1) for l in content.splitlines() if ":" in l]
                    chart = {"type": str(body.get("tipo") or "bar"), "data": {"labels": [p[0].strip() for p in pairs],
                             "datasets": [{"label": title, "data": [float(re.sub(r"[^\d.,-]", "", p[1]).replace(",", ".") or 0) for p in pairs]}]}}
                path = await fetch_image("https://quickchart.io/chart", post={"chart": chart, "width": 800, "height": 450, "backgroundColor": "white", "format": "png"})
                await show(title, path, "Gráfico · QuickChart.io")
                return {"ok": True, "imagem": str(path)}
            if action == "diagrama":  # conteudo = código Mermaid
                code = base64.urlsafe_b64encode(content.encode("utf-8")).decode()
                path = await fetch_image(f"https://mermaid.ink/img/{code}?type=png&bgColor=ffffff")
                await show(title, path, "Diagrama · mermaid.ink")
                return {"ok": True, "imagem": str(path)}
            if action == "qr":
                path = await fetch_image(f"https://quickchart.io/qr?size=400&text={quote(content[:900])}")
                await show(title, path, "QR Code · QuickChart.io", content[:200])
                return {"ok": True, "imagem": str(path)}
        except (httpx.HTTPError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": str(exc)[:200]}
        raise HTTPException(status_code=404, detail="acao desconhecida")

    return router
