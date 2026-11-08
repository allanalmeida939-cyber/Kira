"""Quadros: painéis que a Kira cria na hora (info, lista, grafico, questoes, noticia, tabela, flashcards) — 2026-11-05.

2026-11-07 (Fase 1F): a variação visual (VARIACOES) é sorteada a cada painel, sem repetir a anterior; quando o Chefe diz
"gostei, mantém esse", a última variação daquele tipo fica salva em configs/quadro_estilos.json e passa a valer sempre
("volta a variar" desfaz). O painel de imagens (viewer) sorteia no navegador e avisa aqui qual saiu (/api/quadro/visto),
para o mesmo "gostei" valer para ele (tipo 'imagens').
"""

from __future__ import annotations

import json
import random
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request

from services.local_request import is_local_request

TIPOS = {"info", "lista", "grafico", "questoes", "noticia", "tabela", "flashcards"}
VARIACOES = ("solto", "janela", "cards", "faixa", "destaque", "editorial")
VARIACOES_IMAGENS = ("moldura", "solto", "cinema", "polaroid", "faixa")
ANTIGOS = {"a": "janela", "b": "cards", "c": "faixa"}  # estilos fixados antes de 2026-11-07
PREFS = Path(__file__).resolve().parent.parent / "configs" / "quadro_estilos.json"


def _load(path: Path) -> dict[str, str]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def create_quadro_router(*, publish: Callable[[dict], Awaitable[Any]], prefs: Path = PREFS) -> APIRouter:
    router = APIRouter()
    last: dict[str, str] = {}  # último estilo mostrado por tipo (o que o "gostei" salva)

    def allow(request: Request) -> None:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")

    @router.post("/api/quadro")
    async def quadro(request: Request, payload: dict) -> dict[str, Any]:
        allow(request)
        tipo = str(payload.get("tipo") or "info").lower()
        if tipo not in TIPOS:
            return {"ok": False, "error": f"tipo deve ser um de: {', '.join(sorted(TIPOS))}"}
        props = payload.get("conteudo") if isinstance(payload.get("conteudo"), dict) else {}
        saved = _load(prefs).get(tipo)
        fixed = ANTIGOS.get(saved, saved)
        pedida = str(payload.get("variacao") or "").lower()
        if pedida in VARIACOES:
            estilo = pedida
        elif fixed in VARIACOES:
            estilo = fixed
        else:
            estilo = random.choice([e for e in VARIACOES if e != last.get(tipo)])  # muda de uma vez para outra
        last[tipo] = estilo
        img = str(props.get("imagem") or "")
        props["imagem"] = f"/api/ui/img?u={quote(img, safe='')}" if img.startswith("https://") else ""
        titulo = str(payload.get("titulo") or tipo.title())[:80]
        questoes = props.get("questoes") if tipo == "questoes" and isinstance(props.get("questoes"), list) else None
        # até 4 questões: um painel por questão (saem do orbe uma a uma); mais que isso, um painel só
        groups = [(i, [q]) for i, q in enumerate(questoes[:4])] if questoes and len(questoes) <= 4 else [(0, questoes)] if questoes else [(0, None)]
        for start, group in groups:
            extra = {"questoes": group, "inicio": start} if group is not None else {}
            title = f"{titulo} · {start + 1}" if len(groups) > 1 else titulo
            await publish({"type": "open_panel", "kind": "quadro", "id": f"quadro-{uuid.uuid4().hex[:8]}", "title": title,
                           "props": {**props, **extra, "tipo": tipo, "estilo": estilo}, "focus": False})
        return {"ok": True, "tipo": tipo, "estilo": estilo, "fixo": estilo == fixed, "paineis": len(groups)}

    @router.get("/api/quadro/estilos")
    async def estilos() -> dict[str, str]:
        """Variações fixadas por tipo (só leitura; a TV também lê para o painel de imagens)."""
        return _load(prefs)

    @router.post("/api/quadro/visto")
    async def visto(request: Request, payload: dict) -> dict[str, Any]:
        """O navegador avisa a variação sorteada do painel de imagens (para o "gostei, mantém esse")."""
        allow(request)
        estilo = str(payload.get("estilo") or "").lower()
        if payload.get("tipo") != "imagens" or estilo not in VARIACOES_IMAGENS:
            return {"ok": False}
        last["imagens"] = estilo
        return {"ok": True}

    @router.post("/api/quadro/fixar")
    async def fixar(request: Request, payload: dict) -> dict[str, Any]:
        """'Gostei, deixa assim': salva o último estilo do tipo (ou desfaz com 'variar': true)."""
        allow(request)
        tipo = str(payload.get("tipo") or "").lower()
        tipo = "imagens" if tipo in {"imagem", "imagens", "fotos", "viewer"} else tipo
        saved = _load(prefs)
        if payload.get("variar"):
            saved.pop(tipo, None)
        elif tipo in last:
            saved[tipo] = last[tipo]
        else:
            return {"ok": False, "error": f"ainda não mostrei nenhum painel de {tipo or '?'} nesta sessão"}
        prefs.parent.mkdir(parents=True, exist_ok=True)
        prefs.write_text(json.dumps(saved, indent=1), encoding="utf-8")
        return {"ok": True, "tipo": tipo, "estilo": saved.get(tipo, "variado")}

    return router
