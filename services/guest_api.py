"""Rotas do perfil comum do celular (services/guest_chat.py). Só chegam pelo portão, que marca o pedido com o perfil da
sessão (X-Kira-Profile, X-Kira-Role); o dono tem a Kira inteira e não usa estas rotas.

Cada função (voz, clima, notícias, cozinha, música, lembretes) só responde se o Chefe liberou para o perfil
(services/remote_access.py profile_features)."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, Response

from services.guest_chat import GuestChat
from services.local_request import is_local_request
from services.remote_access import GUEST_FEATURES, profile_features

logger = logging.getLogger("kira.app.guest")

WEEKDAYS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")
SPEAK_PER_MINUTE = 20


def create_guest_router(chat: GuestChat, *, profile_of: Callable[[str], dict[str, Any] | None], page: str,
                        tools: Any = None, voice: Any = None) -> APIRouter:
    router = APIRouter()
    spoken: dict[str, list[float]] = {}

    def guest(request: Request) -> dict[str, Any]:
        """Perfil comum vindo do portão; qualquer outra origem é recusada."""
        profile = profile_of(str(request.headers.get("x-kira-profile") or ""))
        if not (is_local_request(request) and request.headers.get("x-kira-remote") and request.headers.get("x-kira-role") == "guest"
                and profile is not None and profile.get("role") == "guest"):
            raise HTTPException(status_code=403, detail="so pelo perfil do celular")
        return profile

    def allowed(profile: dict[str, Any], feature: str) -> dict[str, bool]:
        features = profile_features(profile)
        if not features.get(feature):
            raise HTTPException(status_code=403, detail="funcao nao liberada para este perfil")
        return features

    @router.get("/guest")
    async def guest_page(request: Request) -> FileResponse:
        guest(request)
        return FileResponse(page, media_type="text/html", headers={"Cache-Control": "no-store"})

    @router.get("/api/guest/history")
    async def history(request: Request) -> dict[str, Any]:
        profile = guest(request)
        return {"ok": True, "name": profile["name"], "lines": chat.history(profile["id"])}

    @router.get("/api/guest/me")
    async def me(request: Request) -> dict[str, Any]:
        """Nome, o que o Chefe liberou e os ajustes da própria pessoa (para o menu da tela)."""
        profile = guest(request)
        features = profile_features(profile)
        return {"ok": True, "name": profile["name"], "features": features, "labels": GUEST_FEATURES,
                "settings": chat.settings(profile["id"]), "voice": bool(voice is not None and features.get("voz"))}

    @router.post("/api/guest/settings")
    async def save_settings(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        profile = guest(request)
        body = payload if isinstance(payload, dict) else {}
        return {"ok": True, "settings": chat.save_settings(profile["id"], body)}

    @router.post("/api/guest/chat")
    async def talk(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        profile = guest(request)
        features = profile_features(profile)
        text = str((payload or {}).get("text") or "") if isinstance(payload, dict) else ""
        now = datetime.now().astimezone()
        when = f"{WEEKDAYS[now.weekday()]}, {now.strftime('%d/%m/%Y %H:%M')} (fuso {now.strftime('%z')})"
        answer = await chat.reply(profile, text, features if tools is not None else {}, when)
        action = answer.pop("action", None)
        if action and tools is not None:
            result = await tools.run(profile["id"], action, chat.settings(profile["id"]))
            answer["card"] = result.get("card")
            if result.get("say"):
                answer["say"] = result["say"]
                chat.remember_card(profile["id"], result["say"])
        return answer

    @router.post("/api/guest/briefing")
    async def briefing(request: Request) -> dict[str, Any]:
        """Resumo do dia da pessoa (clima, notícias dos interesses dela, próximos lembretes)."""
        profile = guest(request)
        if tools is None:
            return {"ok": False, "parts": []}
        features = profile_features(profile)
        parts = await tools.briefing(profile["id"], chat.settings(profile["id"]), features)
        return {"ok": True, "parts": parts}

    @router.get("/api/guest/reminders")
    async def reminders(request: Request) -> dict[str, Any]:
        profile = guest(request)
        allowed(profile, "lembretes")
        if tools is None:
            return {"ok": True, "items": [], "due": []}
        return {"ok": True, "items": tools.reminders.upcoming(profile["id"]), "due": tools.reminders.pop_due(profile["id"])}

    @router.post("/api/guest/reminders")
    async def reminder_add(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        """Timer da receita: vira lembrete, que avisa no celular mesmo com a tela fechada."""
        profile = guest(request)
        allowed(profile, "lembretes")
        body = payload if isinstance(payload, dict) else {}
        try:
            minutes = int(body.get("minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        text = " ".join(str(body.get("text") or "").split())[:120]
        if tools is None or not 1 <= minutes <= 24 * 60 or not text:
            raise HTTPException(status_code=422, detail="lembrete invalido")
        when = (datetime.now().astimezone() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        return {"ok": True, **tools.reminders.create(profile["id"], text, when)}

    @router.delete("/api/guest/reminders/{reminder_id}")
    async def reminder_delete(reminder_id: str, request: Request) -> dict[str, Any]:
        profile = guest(request)
        allowed(profile, "lembretes")
        return {"ok": bool(tools is not None and tools.reminders.delete(profile["id"], reminder_id))}

    @router.post("/api/guest/speak")
    async def speak(request: Request, payload: Any = Body(default=None)) -> Response:
        """A resposta na voz da Kira (MP3); a tela usa a voz do navegador só se isto falhar."""
        profile = guest(request)
        allowed(profile, "voz")
        if voice is None:
            raise HTTPException(status_code=503, detail="voz indisponivel")
        now = time.time()
        recent = [at for at in spoken.get(profile["id"], []) if now - at < 60]
        if len(recent) >= SPEAK_PER_MINUTE:
            raise HTTPException(status_code=429, detail="devagar")
        spoken[profile["id"]] = recent + [now]
        text = str((payload or {}).get("text") or "") if isinstance(payload, dict) else ""
        try:
            audio = await voice.speak(text)
        except ValueError:
            raise HTTPException(status_code=422, detail="nada para falar")
        except Exception as exc:
            logger.info("GUEST_VOICE_FAILED reason=%s detail=%s", type(exc).__name__, str(exc)[:160])
            raise HTTPException(status_code=503, detail="voz indisponivel")
        return Response(audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})

    return router


__all__ = ["create_guest_router"]
