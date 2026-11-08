"""Rota da tool 'programa' (services/pc_operator.py). A acessibilidade do Windows (COM) roda numa linha de trabalho só."""

from __future__ import annotations

import asyncio
import hmac
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from fastapi import APIRouter, Body, Header, HTTPException

from services.pc_operator import PcOperator

logger = logging.getLogger("kira.app.pc_operator")

ACTIONS = ("liberar", "bloquear", "status", "midia", "abrir", "janelas", "controles", "apertar", "escrever", "calcular", "teclas", "clicar")


def _com_init() -> None:
    try:
        import comtypes

        comtypes.CoInitialize()
    except Exception:  # noqa: BLE001 - sem comtypes a acessibilidade não funciona; o erro aparece na ação
        pass


def run(operator: PcOperator, body: dict[str, Any]) -> dict[str, Any]:
    action = str(body.get("action") or "").strip().lower()
    window = str(body.get("window") or "")
    if action == "liberar":
        return {"ok": True, **operator.allow(float(body.get("minutes") or 30), str(body.get("reason") or "")),
                "como_responder": "Confirme em uma frase que está liberada para usar mouse e teclado e por quanto tempo."}
    if action == "bloquear":
        return {"ok": True, **operator.revoke(), "como_responder": "Confirme que não vai mais mexer no mouse nem no teclado."}
    if action == "status":
        return {"ok": True, **operator.permission.as_dict()}
    if action == "midia":
        return operator.media(str(body.get("key") or ""))
    if action == "abrir":
        subprocess.Popen(["cmd", "/c", "start", "", str(body.get("text") or "")], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"ok": True, "abriu": body.get("text")}
    if action == "janelas":
        return operator.windows()
    if action == "controles":
        return operator.controls(window)
    if action == "apertar":
        return operator.press(window, str(body.get("control") or ""))
    if action == "escrever":
        return operator.write(window, str(body.get("text") or ""), str(body.get("control") or ""))
    if action == "calcular":
        return operator.calculate(str(body.get("text") or ""))
    if action == "teclas":
        return operator.keys(window, str(body.get("text") or ""))
    if action == "clicar":
        return operator.click(int(body.get("x") or 0), int(body.get("y") or 0), double=bool(body.get("double")))
    return {"ok": False, "message": f"Ação desconhecida: use {', '.join(ACTIONS)}."}


def create_pc_operator_router(operator: PcOperator, *, token_provider: Callable[[], str]) -> APIRouter:
    router = APIRouter()
    worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kira-pc", initializer=_com_init)

    @router.post("/api/ui/programa")
    async def tool(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        expected = str(token_provider() or "")
        if not expected or not x_kira_internal or not hmac.compare_digest(str(x_kira_internal), expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")
        body = payload if isinstance(payload, dict) else {}
        try:
            result = await asyncio.get_running_loop().run_in_executor(worker, run, operator, body)
        except LookupError as exc:
            result = {"ok": False, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 - programa fechou no meio, controle sumiu...
            logger.info("PC_OPERATOR_FAILED action=%s reason=%s detail=%s", body.get("action"), type(exc).__name__, str(exc)[:200])
            result = {"ok": False, "message": "Não consegui fazer isso nesse programa."}
        logger.info("PC_OPERATOR action=%s ok=%s mouse=%s teclado=%s", body.get("action"), result.get("ok"), bool(result.get("usou_mouse")), bool(result.get("usou_teclado")))
        return result

    return router


__all__ = ["create_pc_operator_router", "run", "ACTIONS"]
