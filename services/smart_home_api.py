"""Rotas da casa inteligente (services/smart_home.py): a tool da Kira e a lista para a tela."""

from __future__ import annotations

import hmac
import ipaddress
import logging
import time
from dataclasses import asdict
from typing import Any, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Request

from services.local_request import is_local_request
from services.smart_home import SmartHome

logger = logging.getLogger("kira.app.smart_home")

RESCAN_AFTER_S = 10 * 60


def create_smart_home_router(home: SmartHome, *, token_provider: Callable[[], str]) -> APIRouter:
    router = APIRouter()

    async def devices(fresh: bool = False) -> list[dict[str, Any]]:
        if fresh or not home.devices or time.time() - home.scanned_at > RESCAN_AFTER_S:
            await home.scan()
        return [{**asdict(d), "label": d.label} for d in home.devices.values()]

    @router.get("/api/casa/dispositivos")
    async def listing(request: Request, fresh: bool = False) -> dict[str, Any]:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        found = await devices(fresh) if (fresh or not home.mine()) else [{**asdict(d), "label": d.label} for d in home.devices.values()]
        mine = {d.ip for d in home.mine()}
        return {"ok": True, "dispositivos": found, "meus": [d for d in found if d["ip"] in mine], "atualizado": home.scanned_at}

    def local(request: Request) -> None:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")

    @router.post("/api/casa/renomear")
    async def rename(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        local(request)
        body = payload if isinstance(payload, dict) else {}
        return {"ok": home.rename(str(body.get("ip") or ""), str(body.get("nome") or ""))}

    @router.post("/api/casa/adicionar")
    async def add(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        """Aparelho que não apareceu na busca (desligado, rede diferente): entra pelo IP, com um nome."""
        local(request)
        body = payload if isinstance(payload, dict) else {}
        ip = str(body.get("ip") or "").strip()
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            return {"ok": False, "message": "IP inválido."}
        return {"ok": True, **asdict(home.add(ip, str(body.get("nome") or "")))}

    @router.post("/api/casa/comando")
    async def command(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        local(request)
        body = payload if isinstance(payload, dict) else {}
        device = home.devices.get(str(body.get("ip") or ""))
        if device is None:
            return {"ok": False, "message": "Aparelho não encontrado."}
        try:
            return await home.command(device, str(body.get("acao") or ""), body.get("valor"))
        except Exception as exc:  # noqa: BLE001 - aparelho desligado
            logger.info("SMART_HOME_COMMAND_FAILED reason=%s", type(exc).__name__)
            return {"ok": False, "message": f"{device.label} não respondeu (está ligado e no Wi-Fi?)."}

    @router.post("/api/ui/casa")
    async def tool(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        expected = str(token_provider() or "")
        if not expected or not x_kira_internal or not hmac.compare_digest(str(x_kira_internal), expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")
        body = payload if isinstance(payload, dict) else {}
        action = str(body.get("action") or "listar").strip().lower()
        if action == "listar" and home.mine():
            return {"ok": True, "total": len(home.mine()),
                    "dispositivos": [{"nome": d.label, "tipo": d.kind, "ip": d.ip, "controla": bool(d.control)} for d in home.mine()]}
        if action in {"listar", "procurar"}:
            found = await devices(fresh=action == "procurar")
            return {"ok": True, "total": len(found),
                    "dispositivos": [{"nome": d["label"], "tipo": d["kind"], "ip": d["ip"], "controla": bool(d["control"])} for d in found]}
        if not home.devices:
            await home.scan()
        device = home.find(str(body.get("device") or ""))
        if device is None:
            return {"ok": False, "message": "Não achei esse aparelho na rede. Diga 'procurar aparelhos' para eu olhar de novo."}
        if action == "renomear":
            return {"ok": home.rename(device.ip, str(body.get("value") or "")), "aparelho": device.ip}
        try:
            return await home.command(device, action, body.get("value"))
        except Exception as exc:  # noqa: BLE001 - aparelho desligado / fora da rede
            logger.info("SMART_HOME_COMMAND_FAILED action=%s reason=%s", action, type(exc).__name__)
            return {"ok": False, "message": f"{device.label} não respondeu (está ligado e no Wi-Fi?)."}

    return router


__all__ = ["create_smart_home_router"]
