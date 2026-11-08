"""Rotas do celular (passam pelo portão de services/remote_access.py, que marca o pedido com X-Kira-Remote; as rotas
/api/remote ele bloqueia, por isso o prefixo /api/phone).

- GET  /api/phone/state         qual aparelho está ativo (o celular e o PC perguntam para saber se tocam a voz)
- POST /api/phone/active        o PC avisa que voltou a ser usado (só do próprio PC)
- POST /api/phone/voice         o celular abriu/fechou o microfone (só do celular)
- POST /api/phone/voice/join    acesso do celular à sala de voz da Kira (só do celular): entra como participante
                                "Celular" na mesma sala do PC; se a voz do PC não estiver iniciada, o PC inicia
- GET  /api/phone/push/key      chave pública para o app do celular se inscrever nas notificações
- POST /api/phone/push/subscribe   inscreve este celular (só do celular)
- POST /api/phone/push/test     manda uma notificação de teste para os celulares inscritos
"""

from __future__ import annotations

import logging
import asyncio
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, HTTPException, Request

from services.local_request import is_local_request
from services.remote_devices import DeviceTracker
from services.web_push import WebPush

logger = logging.getLogger("kira.app.remote")


def _from_phone(request: Request, *, owner: bool = True) -> bool:
    """Pedido do celular pelo portão; owner=True: só o perfil do dono (os perfis comuns não mexem na voz do Chefe)."""
    role = str(request.headers.get("x-kira-role") or "owner")
    return is_local_request(request) and bool(request.headers.get("x-kira-remote")) and (role == "owner" or not owner)


def _from_pc(request: Request) -> bool:
    return is_local_request(request) and not request.headers.get("x-kira-remote")


def create_phone_router(devices: DeviceTracker, *, voice_join: Callable[[], Awaitable[dict[str, Any]]],
                        push: WebPush | None = None, rpc: Any = None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/phone/state")
    async def state(request: Request) -> dict[str, Any]:
        if not is_local_request(request) or request.headers.get("x-kira-role", "owner") != "owner":
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        return {"ok": True, **devices.snapshot(), "me": "celular" if _from_phone(request) else "pc"}

    @router.post("/api/phone/active")
    async def active(request: Request) -> dict[str, Any]:
        if not _from_pc(request):
            raise HTTPException(status_code=403, detail="so na tela do PC")
        devices.set_active("pc", reason="pc_used")
        return {"ok": True, **devices.snapshot()}

    @router.post("/api/phone/voice")
    async def voice(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        if not _from_phone(request):
            raise HTTPException(status_code=403, detail="so pelo celular")
        devices.note_phone_voice(bool((payload or {}).get("on")) if isinstance(payload, dict) else False)
        return {"ok": True, **devices.snapshot()}

    @router.post("/api/phone/voice/join")
    async def join(request: Request) -> dict[str, Any]:
        if not _from_phone(request):
            raise HTTPException(status_code=403, detail="so pelo celular")
        devices.set_active("celular", reason="phone_join")  # entrou pelo celular: a resposta vai para lá
        try:
            result = await voice_join()
        except Exception as exc:  # a conversa por escrito continua funcionando
            logger.info("PHONE_VOICE_JOIN_FAILED reason=%s", type(exc).__name__)
            return {"ok": False, "error": "A voz pelo celular não abriu agora."}
        logger.info("PHONE_VOICE_JOIN ok=%s", str(bool(result.get("ok"))).lower())
        return result

    @router.post("/api/phone/vision")
    async def vision(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        """Câmera ou tela do celular na sala de voz: a Kira passa a ver (o agente pega o vídeo de quem publicar)."""
        if not _from_phone(request):
            raise HTTPException(status_code=403, detail="so pelo celular")
        from services.vision_livekit_state import start_vision_livekit, stop_vision_livekit

        body = payload if isinstance(payload, dict) else {}
        source = "screen" if body.get("source") == "screen" else "camera"
        on = bool(body.get("on"))
        if on:
            devices.set_active("celular", reason=f"phone_{source}")
            start_vision_livekit(reason=f"phone_{source}")
        else:
            stop_vision_livekit(reason="user_disabled")
        logger.info("PHONE_VISION source=%s on=%s", source, str(on).lower())
        return {"ok": True, "on": on, "source": source}

    @router.post("/api/phone/ui-result")
    async def ui_result(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        """Resposta da tela do celular a uma chamada de ida e volta (services/phone_rpc.py)."""
        if not _from_phone(request) or rpc is None:
            raise HTTPException(status_code=403, detail="so pelo celular")
        body = payload if isinstance(payload, dict) else {}
        return {"ok": rpc.resolve(str(body.get("id") or ""), body.get("result"))}

    @router.get("/api/phone/push/key")
    async def push_key(request: Request) -> dict[str, Any]:
        if not _from_phone(request, owner=False) or push is None:
            raise HTTPException(status_code=403, detail="so pelo celular")
        return {"ok": True, "key": await asyncio.to_thread(push.public_key)}

    @router.post("/api/phone/push/subscribe")
    async def push_subscribe(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        if not _from_phone(request, owner=False) or push is None:
            raise HTTPException(status_code=403, detail="so pelo celular")
        body = payload if isinstance(payload, dict) else {}
        agent = str(request.headers.get("user-agent") or "")
        label = "Android" if "Android" in agent else "iPhone" if "iPhone" in agent else "Celular"
        profile = str(request.headers.get("x-kira-profile") or "dono")  # avisos do Chefe só nos aparelhos do perfil dele
        ok = await asyncio.to_thread(push.subscribe, body.get("subscription"), profile=profile, label=label)
        return {"ok": ok, "devices": push.count()}

    @router.post("/api/phone/push/test")
    async def push_test(request: Request) -> dict[str, Any]:
        if not is_local_request(request) or push is None:
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        result = await push.send("Kira", "Notificações ligadas: é assim que eu te aviso no celular.", tag="teste")
        return {"ok": result["sent"] > 0, **result}

    return router


__all__ = ["create_phone_router"]
