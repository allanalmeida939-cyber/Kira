"""Segurança por voz: rótulo de quem falou, barra do cadastro de voz, webcam que grava quem insiste e bloqueio do Windows.

O agente (token interno) manda mostrar o rótulo, ligar a câmera e bloquear a tela. A interface grava pela webcam e
devolve o vídeo e uma foto, que ficam em Vídeos\\Kira Segurança para o Chefe ver quem mexeu no computador.
"""

from __future__ import annotations

import asyncio
import ctypes
import hmac
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Request

from services.local_request import is_local_request

logger = logging.getLogger("kira.app.security_watch")
RECORDINGS_DIR = Path(os.getenv("KIRA_SECURITY_DIR") or (Path.home() / "Videos" / "Kira Segurança"))
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
KINDS = {"video": ".webm", "photo": ".jpg"}
MAX_LABEL_CHARS = 30
MAX_ENROLL_MESSAGE_CHARS = 200
ENROLL_STATES = ("gravando", "salvando", "salva", "falhou", "cancelado")


def lock_workstation() -> bool:
    """Win+L: o Windows pede a senha do Chefe para voltar."""
    return bool(ctypes.windll.user32.LockWorkStation())


def create_security_router(*, token_provider: Callable[[], str], run_js: Callable[[str], Any],
                           lock: Callable[[], bool] = lock_workstation, folder: Path = RECORDINGS_DIR) -> APIRouter:
    router = APIRouter()

    def require_token(token: str | None) -> None:
        expected = str(token_provider() or "")
        if not expected or not token or not hmac.compare_digest(str(token), expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")

    async def call_ui(js: str) -> bool:
        try:
            await asyncio.to_thread(run_js, js)
            return True
        except RuntimeError as exc:  # janela ainda não pronta ou fechada
            logger.info("SECURITY_UI_UNAVAILABLE reason=%s", exc)
            return False

    @router.post("/api/ui/voice-label")
    async def voice_label(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict:
        require_token(x_kira_internal)
        label = str((payload or {}).get("label") or "")[:MAX_LABEL_CHARS]
        ok = await call_ui(f"window.KiraVoiceLabel && window.KiraVoiceLabel.set({json.dumps(label)}); true")
        return {"ok": ok}

    @router.post("/api/ui/voice-enroll")
    async def voice_enroll(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict:
        require_token(x_kira_internal)
        data = payload if isinstance(payload, dict) else {}
        state = str(data.get("state") or "")
        if state not in ENROLL_STATES:
            raise HTTPException(status_code=422, detail="estado inválido")
        try:
            progress = max(0.0, min(1.0, float(data.get("progress") or 0.0)))
        except (TypeError, ValueError):
            progress = 0.0
        update = {"state": state, "name": str(data.get("name") or "")[:MAX_LABEL_CHARS], "progress": progress,
                  "message": str(data.get("message") or "")[:MAX_ENROLL_MESSAGE_CHARS]}
        ok = await call_ui(f"window.KiraVoiceEnroll && window.KiraVoiceEnroll.update({json.dumps(update)}); true")
        return {"ok": ok}

    @router.post("/api/security/camera")
    async def camera(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict:
        require_token(x_kira_internal)
        action = "stop" if (payload or {}).get("action") == "stop" else "start"
        logger.info("SECURITY_CAMERA action=%s", action)
        ok = await call_ui(f"window.KiraSecurityCam && window.KiraSecurityCam.{action}(); true")
        return {"ok": ok}

    @router.post("/api/security/lock")
    async def lock_screen(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict:
        require_token(x_kira_internal)
        delay = max(0.0, min(30.0, float((payload or {}).get("delay") or 0)))
        logger.warning("SECURITY_LOCK_SCHEDULED delay=%.0f", delay)
        threading.Timer(delay, lock).start()
        return {"ok": True, "delay": delay}

    @router.post("/api/security/recording")
    async def recording(request: Request, kind: str = "video") -> dict:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        if kind not in KINDS:
            raise HTTPException(status_code=422, detail="tipo invalido")
        data = await request.body()
        if not data or len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="arquivo vazio ou grande demais")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (time.strftime("%Y-%m-%d_%H-%M-%S") + KINDS[kind])
        await asyncio.to_thread(target.write_bytes, data)
        logger.info("SECURITY_RECORDING_SAVED kind=%s bytes=%d", kind, len(data))
        return {"ok": True, "file": target.name}

    return router


__all__ = ["RECORDINGS_DIR", "create_security_router", "lock_workstation"]
