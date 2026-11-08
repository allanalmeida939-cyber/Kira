"""Rotas do navegador da Kira (aba Internet): estado, abrir, navegar, controlar, imagem ao vivo, cliques e teclas da
aba e tarefas de várias etapas. A imagem ao vivo e os cliques/teclas da aba só valem para a própria máquina; o resto
aceita a máquina ou a chave interna (a voz da Kira chama pelo agente). Erro do navegador vira resposta, nunca 500."""

from __future__ import annotations

import hmac
import logging
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from services.local_request import is_local_request
from services.live_browser import normalize_target

logger = logging.getLogger("kira.app.live_browser")

BOUNDARY = "kiraframe"
YOUTUBE_SEARCH = "https://www.youtube.com/results?search_query="
Hdr = Header(default=None, alias="X-Kira-Internal")


async def control(browser: Any, tasks: Any, payload: dict) -> dict[str, Any]:
    """Ações com a aba aberta, as mesmas que a voz e o texto digitado mandam ("volta", "desce", "clica no segundo")."""
    action = str((payload or {}).get("action") or "").strip().lower()
    if action in {"back", "forward", "reload"}:
        state = await browser.history(action)
        return {"ok": True, "description": state.get("title", "")}
    if action == "close":
        await tasks.stop(quiet=True)
        await browser.close()
        return {"ok": True, "description": "fechado"}
    if action == "key":
        await browser.keyboard(key=str(payload.get("key") or ""))
        return {"ok": True}
    if action == "type":
        await browser.keyboard(text=str(payload.get("text") or ""))
        return {"ok": True}
    if action == "scroll":
        await browser.scroll("up" if payload.get("direction") == "up" else "down", float(payload.get("amount") or 650))
        return {"ok": True}
    if action == "resize":
        return {"ok": True, "size": await browser.resize(float(payload.get("width") or 1280), float(payload.get("height") or 800))}
    if action == "click_index":
        found = await browser.click_result(max(1, min(50, int(payload.get("n") or 1))))
        return {"ok": found, "description": "" if found else "não achei esse item na página"}
    if action == "vision_click":
        return {"ok": await tasks.pick_and_click(str(payload.get("instruction") or "")[:400])}
    if action == "youtube_open":
        query = " ".join(str(payload.get("q") or "").split())[:200]
        state = await browser.navigate(YOUTUBE_SEARCH + urllib.parse.quote_plus(query))
        return {"ok": True, "via": "search", "description": state.get("title", "")}
    raise ValueError("ação desconhecida")


def create_live_browser_router(browser: Any, tasks: Any, *, token_provider: Callable[[], str]) -> APIRouter:
    router = APIRouter()

    def authorize(request: Request, supplied: str | None, *, local_only: bool = False) -> None:
        expected = str(token_provider() or "")
        if not local_only and expected and supplied and hmac.compare_digest(str(supplied), expected):
            return
        if is_local_request(request):
            return
        raise HTTPException(status_code=403, detail="origem nao autorizada")

    async def guarded(work: Awaitable[Any]) -> dict[str, Any]:
        try:
            return {"success": True, "data": await work}
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:
            logger.warning("LIVE_BROWSER_ACTION_FAILED reason=%s", type(exc).__name__)
            return {"success": False, "error": "o navegador não respondeu agora"}

    @router.get("/api/browser/state")
    async def browser_state(request: Request, x_kira_internal: str | None = Hdr) -> dict[str, Any]:
        authorize(request, x_kira_internal)
        return {"success": True, "data": {**(await browser.state()), "task": tasks.status()}}

    @router.post("/api/browser/start")
    async def browser_start(request: Request, x_kira_internal: str | None = Hdr) -> dict[str, Any]:
        authorize(request, x_kira_internal)
        return await guarded(browser.open())

    @router.post("/api/browser/navigate")
    async def browser_navigate(request: Request, payload: Any = Body(default=None), x_kira_internal: str | None = Hdr) -> dict[str, Any]:
        authorize(request, x_kira_internal)
        body = payload if isinstance(payload, dict) else {}
        return await guarded(browser.navigate(body.get("url") or body.get("q") or ""))

    @router.post("/api/browser/control")
    async def browser_control(request: Request, payload: Any = Body(default=None), x_kira_internal: str | None = Hdr) -> dict[str, Any]:
        authorize(request, x_kira_internal)
        result = await guarded(control(browser, tasks, payload if isinstance(payload, dict) else {}))
        if result["success"]:
            result["success"] = bool(result["data"].get("ok"))  # o agente lê success para dizer "cliquei"/"não achei"
        return result

    @router.post("/api/browser/input")
    async def browser_input(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        """Clique, rolagem e teclas que o Chefe faz na imagem ao vivo (x e y de 0 a 1)."""
        authorize(request, None, local_only=True)
        body = payload if isinstance(payload, dict) else {}
        kind = str(body.get("type") or "")
        if kind in {"click", "wheel"}:
            try:
                x, y, dy = float(body.get("x") or 0), float(body.get("y") or 0), float(body.get("dy") or 0)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail="posição inválida") from None
            return await guarded(browser.pointer(kind, x, y, dy))
        if kind == "key":
            return await guarded(browser.keyboard(key=str(body.get("key") or "")))
        if kind == "type":
            return await guarded(browser.keyboard(text=str(body.get("text") or "")))
        raise HTTPException(status_code=422, detail="tipo de entrada inválido")

    @router.get("/api/browser/live")
    async def browser_live(request: Request) -> StreamingResponse:
        """Imagem ao vivo em MJPEG (a aba usa num <img>); fechar a aba encerra a conexão e a captura."""
        authorize(request, None, local_only=True)

        # O Chromium só desenha um quadro do MJPEG quando já chegou o cabeçalho do próximo; numa página parada o próximo
        # não vem e a aba ficava na página anterior (até 5 s). Por isso cada quadro já sai seguido do cabeçalho seguinte.
        next_part = b"--" + BOUNDARY.encode() + b"\r\nContent-Type: image/jpeg\r\n\r\n"

        async def stream():
            yield next_part
            async for frame in browser.frames():
                yield frame + b"\r\n" + next_part

        return StreamingResponse(stream(), media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
                                 headers={"Cache-Control": "no-store"})

    @router.post("/api/browser/task")
    async def browser_task_start(request: Request, payload: Any = Body(default=None), x_kira_internal: str | None = Hdr) -> dict[str, Any]:
        authorize(request, x_kira_internal)
        body = payload if isinstance(payload, dict) else {}
        url = normalize_target(body.get("url")) if body.get("url") else ""
        return await guarded(tasks.start(str(body.get("goal") or ""), url or ""))

    @router.post("/api/browser/task/answer")
    async def browser_task_answer(request: Request, payload: Any = Body(default=None), x_kira_internal: str | None = Hdr) -> dict[str, Any]:
        authorize(request, x_kira_internal)
        body = payload if isinstance(payload, dict) else {}
        return await guarded(tasks.answer(str(body.get("answer") or ""), spoken=bool(body.get("spoken"))))

    @router.post("/api/browser/task/stop")
    async def browser_task_stop(request: Request, x_kira_internal: str | None = Hdr) -> dict[str, Any]:
        authorize(request, x_kira_internal)
        return await guarded(tasks.stop())

    @router.get("/api/browser/task/events")
    async def browser_task_events(request: Request, after: int = 0, wait: float = 25.0, x_kira_internal: str | None = Hdr) -> dict[str, Any]:
        """A voz da Kira fica aqui em laço: pergunta, pronto ou falhou viram fala."""
        authorize(request, x_kira_internal)
        events = await tasks.wait_events(int(after), max(0.0, min(30.0, float(wait))))
        return {"success": True, "data": {"events": events, "status": tasks.status()}}

    return router


__all__ = ["control", "create_live_browser_router"]
