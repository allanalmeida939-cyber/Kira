"""Rota da tool 'documento' (services/documents.py). O trabalho pesado (LibreOffice, PDF) roda fora do laço do servidor."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, Header, HTTPException

from services import documents as docs

logger = logging.getLogger("kira.app.documents")

ACTIONS = ("converter", "juntar", "separar", "criar", "ler", "achar", "abrir", "trabalho")


def _files(hints: list[str] | str, *, kinds: set[str] | None = None, many: bool = False) -> list[Path]:
    """Cada pista vira um arquivo; uma pista no plural ('os PDFs do Downloads') pode virar vários."""
    hints = [hints] if isinstance(hints, str) else list(hints or [])
    found: list[Path] = []
    for hint in hints or [""]:
        plural = many and (len(hints) == 1)
        found += [p for p in docs.find_files(hint, kinds=kinds, limit=10 if plural else 1) if p not in found]
    return found


def run(body: dict[str, Any]) -> dict[str, Any]:
    action = docs.fold(body.get("action") or "")
    files = body.get("files") or []
    where = str(body.get("where") or "")
    name = str(body.get("name") or "")
    if action not in ACTIONS:
        return {"ok": False, "message": f"Ação desconhecida: use {', '.join(ACTIONS)}."}
    try:
        if action == "criar":
            result = docs.create(name or "Documento", str(body.get("content") or ""), str(body.get("format") or "word"), where=where)
            return {**result.as_dict(), "como_responder": "Diga o nome do arquivo e a pasta onde salvou (campo 'onde'), em uma frase."}
        if action == "juntar":
            found = _files(files, kinds={".pdf"}, many=True)
            if len(found) < 2:
                return {"ok": False, "message": "Achei menos de dois PDFs com esses nomes. Diga os nomes ou a pasta onde estão."}
            return docs.merge_pdfs(found, where=where, name=name).as_dict()
        found = _files(files)
        if not found:
            return {"ok": False, "message": "Não achei esse arquivo na Área de Trabalho, Downloads, Documentos ou Imagens. Diga o nome dele."}
        src = found[0]
        if action == "converter":
            return {**docs.convert(src, str(body.get("format") or "pdf"), where=where, name=name).as_dict(), "origem": src.name}
        if action == "separar":
            return {**docs.split_pdf(src, str(body.get("pages") or ""), where=where, name=name).as_dict(), "origem": src.name}
        if action == "ler":
            return {"ok": True, "arquivo": src.name, "texto": docs.read_text(src),
                    "como_responder": "Resuma ou responda o que o Chefe perguntou sobre esse texto; não leia tudo em voz alta."}
        if action == "achar":
            more = _files(files, many=True)
            return {"ok": True, "arquivos": [{"nome": p.name, "onde": docs.friendly(p.parent)} for p in more[:8]]}
        os.startfile(str(src))  # abrir: no programa padrão do Windows
        return {"ok": True, "abriu": src.name, "onde": docs.friendly(src.parent)}
    except docs.DocumentError as exc:
        return {"ok": False, "message": str(exc)}


def create_documents_router(*, token_provider: Callable[[], str], generate: Callable[[str], Awaitable[str]] | None = None,
                            notify: Callable[[str], Any] | None = None) -> APIRouter:
    """generate: Gemini de texto (trabalho escolar); notify(texto): a Kira avisa falando e no sininho quando fica pronto."""
    router = APIRouter()
    jobs: set[asyncio.Task] = set()

    async def school_work(body: dict[str, Any]) -> None:
        from services.school_work import make_work

        tema = str(body.get("content") or body.get("name") or "").strip()
        try:
            result = await make_work(generate, tema=tema, detalhes=str(body.get("details") or ""), formato=str(body.get("format") or "pdf"),
                                     onde=str(body.get("where") or ""), autor=str(body.get("author") or ""), instituicao=str(body.get("school") or ""),
                                     cidade=str(body.get("city") or ""), secoes=int(body.get("sections") or 5))
            text = (f"Pronto: o trabalho sobre {tema} ficou em {result['onde']}, arquivo {result['nome']}, com capa, "
                    f"{result['secoes']} seções e {result['imagens']} imagens.")
        except Exception as exc:  # noqa: BLE001 - o aviso diz o que houve
            logger.info("SCHOOL_WORK_FAILED reason=%s detail=%s", type(exc).__name__, str(exc)[:200])
            text = f"Não consegui terminar o trabalho sobre {tema}: {exc if isinstance(exc, docs.DocumentError) else 'deu erro no meio'}."
        if notify is not None:
            await asyncio.to_thread(notify, text)

    @router.post("/api/ui/documento")
    async def tool(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        expected = str(token_provider() or "")
        if not expected or not x_kira_internal or not hmac.compare_digest(str(x_kira_internal), expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")
        body = payload if isinstance(payload, dict) else {}
        if docs.fold(body.get("action") or "") == "trabalho":
            if generate is None or not str(body.get("content") or body.get("name") or "").strip():
                return {"ok": False, "message": "Diga o tema do trabalho."}
            job = asyncio.ensure_future(school_work(body))
            jobs.add(job)
            job.add_done_callback(jobs.discard)
            return {"ok": True, "em_andamento": True,
                    "como_responder": ("Diga em uma frase que começou o trabalho (capa, seções e imagens) e que avisa quando ficar pronto, "
                                       "em mais ou menos um minuto. Não escreva o trabalho na fala.")}
        try:
            result = await asyncio.to_thread(run, body)
        except Exception as exc:  # noqa: BLE001 - arquivo estranho/corrompido: a Kira fala que não deu
            logger.info("DOC_FAILED action=%s reason=%s detail=%s", body.get("action"), type(exc).__name__, str(exc)[:200])
            result = {"ok": False, "message": "Não consegui fazer isso com esse arquivo."}
        logger.info("DOC_DONE action=%s ok=%s", body.get("action"), result.get("ok"))
        return result

    return router


__all__ = ["create_documents_router", "run", "ACTIONS"]
