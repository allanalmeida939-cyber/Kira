"""Configuração do acesso pelo celular (aba Configurações > Celular). Só vale na tela do próprio PC: pedido que chega
pelo portão (marcado com X-Kira-Remote) é recusado, então quem está no celular não troca a senha nem desliga nada."""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import Response

from services.local_request import is_local_request
from services.remote_access import GUEST_FEATURES, MAX_PASSWORD, MIN_PASSWORD, RemoteGateway, RemoteSettings, hash_password, profile_features

logger = logging.getLogger("kira.app.remote")


def create_remote_access_router(settings: RemoteSettings, gateway: RemoteGateway,
                                relay: Callable[[str, str], dict[str, Any]] | None = None, guest_chat: Any = None) -> APIRouter:
    router = APIRouter()

    def require_pc(request: Request) -> None:
        if request.headers.get("x-kira-remote") or not is_local_request(request):
            raise HTTPException(status_code=403, detail="isso só pelo PC")

    @router.get("/api/remote/settings")
    async def remote_settings(request: Request) -> dict[str, Any]:
        require_pc(request)
        return {"success": True, "data": await asyncio.to_thread(gateway.status)}

    @router.post("/api/remote/settings")
    async def remote_settings_update(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        require_pc(request)
        body = payload if isinstance(payload, dict) else {}
        changes: dict[str, Any] = {}
        if "password" in body:
            password = str(body.get("password") or "")
            if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
                return {"success": False, "error": f"a senha precisa ter de {MIN_PASSWORD} a {MAX_PASSWORD} caracteres"}
            changes["password_hash"] = await asyncio.to_thread(hash_password, password)
            changes["generation"] = settings.load()["generation"] + 1  # senha nova: todo aparelho entra de novo
        for key in ("enabled", "lan", "public_link"):
            if key in body:
                changes[key] = bool(body.get(key))
        if changes.get("lan") or changes.get("public_link"):  # marcar um jeito de entrar é querer o acesso ligado
            changes["enabled"] = True
        if changes.get("enabled") and not (changes.get("password_hash") or settings.load()["password_hash"]):
            return {"success": False, "error": "crie uma senha antes de ligar"}
        settings.update(**changes)
        logger.info("REMOTE_SETTINGS_CHANGED keys=%s", ",".join(sorted(key for key in changes if key != "password_hash")) or "-")
        return {"success": True, "data": await asyncio.to_thread(gateway.apply)}

    @router.get("/api/remote/qr")
    async def remote_qr(request: Request, kind: str = "casa") -> Response:
        """QR code do link (casa ou fixo) para abrir com a câmera do celular. Só pelo PC."""
        require_pc(request)
        status = await asyncio.to_thread(gateway.status)
        link = (status.get("links") or {}).get(kind) or (status.get("lan_preview") if kind == "casa" else "")
        if not link:
            raise HTTPException(status_code=404, detail="sem link")
        try:
            import segno
        except ImportError:
            raise HTTPException(status_code=404, detail="gerador de QR ausente") from None
        buffer = io.BytesIO()
        segno.make(str(link), error="m").save(buffer, kind="svg", scale=5, border=2, dark="#120a1f", light="#ffffff")
        return Response(buffer.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})

    @router.post("/api/chat/relay")
    async def chat_relay(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        """Texto escrito no celular: a tela do PC manda para a Kira pela sala de voz (todas as ferramentas). ok=False
        quando a voz do PC não está ativa; aí o celular usa o modo texto."""
        if not is_local_request(request):  # o portão já entrega como pedido local, só depois do login
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        body = payload if isinstance(payload, dict) else {}
        text = " ".join(str(body.get("text") or "").split())[:2000]
        turn = str(body.get("client_turn_id") or "")[:80]
        if not text or relay is None:
            return {"ok": False}
        try:
            result = await asyncio.to_thread(relay, text, turn)
        except Exception as exc:
            logger.info("REMOTE_RELAY_UNAVAILABLE reason=%s", type(exc).__name__)
            from services.channel_logs import phone_log
            phone_log("CEL_RELAY_FALHOU", level=30, erro=type(exc).__name__, chars=len(text))
            return {"ok": False}
        ok = bool((result or {}).get("ok"))
        logger.info("REMOTE_RELAY_SENT ok=%s chars=%d", str(ok).lower(), len(text))
        from services.channel_logs import phone_log, preview
        phone_log("CEL_TEXTO_ENVIADO_A_KIRA", ok=ok, chars=len(text), turno=turn, rota="voz_do_pc" if ok else "modo_texto", texto=preview(text))
        return {"ok": ok}

    # ---------- perfis (até 3; o dono é o Chefe) ----------
    def profiles_view() -> list[dict[str, Any]]:
        return [{"id": item["id"], "name": item["name"], "role": item["role"], "has_password": bool(item.get("password_hash")),
                 "features": profile_features(item)} for item in settings.profiles()]

    @router.get("/api/remote/profiles")
    async def profiles(request: Request) -> dict[str, Any]:
        require_pc(request)
        return {"success": True, "profiles": profiles_view()}

    @router.post("/api/remote/profiles")
    async def profile_add(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        require_pc(request)
        body = payload if isinstance(payload, dict) else {}
        password = str(body.get("password") or "")
        if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
            return {"success": False, "error": f"a senha precisa ter de {MIN_PASSWORD} a {MAX_PASSWORD} caracteres", "profiles": profiles_view()}
        try:
            settings.add_profile(str(body.get("name") or ""), await asyncio.to_thread(hash_password, password))
        except ValueError as exc:
            return {"success": False, "error": str(exc), "profiles": profiles_view()}
        logger.info("REMOTE_PROFILE_ADDED")
        return {"success": True, "profiles": profiles_view()}

    @router.post("/api/remote/profiles/{profile_id}/password")
    async def profile_password(profile_id: str, request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        require_pc(request)
        password = str((payload or {}).get("password") or "") if isinstance(payload, dict) else ""
        if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
            return {"success": False, "error": f"a senha precisa ter de {MIN_PASSWORD} a {MAX_PASSWORD} caracteres", "profiles": profiles_view()}
        target = settings.profile(profile_id)
        if target is None:
            return {"success": False, "error": "perfil não encontrado", "profiles": profiles_view()}
        hashed = await asyncio.to_thread(hash_password, password)
        if target["role"] == "owner":
            settings.update(password_hash=hashed, generation=settings.load()["generation"] + 1)
        else:  # só esse perfil precisa entrar de novo
            settings.update_profile(profile_id, password_hash=hashed, gen=int(target.get("gen") or 1) + 1)
        return {"success": True, "profiles": profiles_view()}

    @router.post("/api/remote/profiles/{profile_id}/features")
    async def profile_set_features(profile_id: str, request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        """O dono escolhe o que cada perfil comum pode usar."""
        require_pc(request)
        target = settings.profile(profile_id)
        if target is None or target["role"] == "owner":
            return {"success": False, "error": "perfil não encontrado", "profiles": profiles_view()}
        wanted = (payload or {}).get("features") if isinstance(payload, dict) else None
        current = profile_features(target)
        if isinstance(wanted, dict):
            current.update({key: bool(value) for key, value in wanted.items() if key in GUEST_FEATURES})
        settings.update_profile(profile_id, features=current)
        logger.info("REMOTE_PROFILE_FEATURES on=%s", ",".join(key for key, on in current.items() if on))
        return {"success": True, "profiles": profiles_view()}

    @router.delete("/api/remote/profiles/{profile_id}")
    async def profile_remove(profile_id: str, request: Request) -> dict[str, Any]:
        require_pc(request)
        removed = settings.remove_profile(profile_id)
        if removed and guest_chat is not None:
            await asyncio.to_thread(guest_chat.forget_all, profile_id)
        return {"success": removed, "profiles": profiles_view()}

    @router.get("/api/remote/profiles/{profile_id}/conversa")
    async def profile_talk(profile_id: str, request: Request) -> dict[str, Any]:
        """O dono vê a conversa e o que a Kira anotou de cada perfil comum."""
        require_pc(request)
        target = settings.profile(profile_id)
        if target is None or target["role"] == "owner" or guest_chat is None:
            raise HTTPException(status_code=404, detail="perfil não encontrado")
        return {"success": True, "name": target["name"], "lines": guest_chat.history(profile_id, 200), "facts": guest_chat.facts(profile_id)}

    @router.post("/api/remote/logout-all")
    async def remote_logout_all(request: Request) -> dict[str, Any]:
        require_pc(request)
        settings.update(generation=settings.load()["generation"] + 1)
        logger.info("REMOTE_SESSIONS_REVOKED")
        return {"success": True, "data": await asyncio.to_thread(gateway.status)}

    return router


__all__ = ["create_remote_access_router"]
