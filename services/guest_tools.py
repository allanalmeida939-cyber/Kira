"""Ações dos perfis comuns do celular (as que o Chefe liberou para cada perfil, services/remote_access.py GUEST_FEATURES):
clima, notícias, receita, música e lembretes da própria pessoa. Cada ação vira um cartão na tela dela
(interface/guest.html) e, quando faz sentido, uma frase a mais para a Kira falar.

Lembretes do convidado ficam separados dos do Chefe (memory/local/perfis/<id>/lembretes.json) e avisam no celular dele
(notificação do app, só para os aparelhos desse perfil) e na própria tela, em voz alta, se ela estiver aberta.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from services.guest_chat import FOLDER, PROFILE_ID_RE
from services.reminders_service import ReminderService

logger = logging.getLogger("kira.app.guest")

TICK_S = 10.0
MAX_NEWS = 6


def _clean(text: object, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _degrees(value: object) -> str:
    return f"{round(value)} graus" if isinstance(value, (int, float)) else ""


class GuestReminders:
    """Lembretes de cada perfil comum; `push(título, texto, perfil)` manda a notificação para os aparelhos dele."""

    def __init__(self, folder: Path = FOLDER, *, push: Callable[[str, str, str], Awaitable[Any]] | None = None) -> None:
        self._folder = folder
        self._push = push
        self._services: dict[str, ReminderService] = {}
        self._pending: dict[str, deque[str]] = {}

    def service(self, profile_id: str) -> ReminderService:
        if not PROFILE_ID_RE.fullmatch(str(profile_id or "")):
            raise ValueError("perfil inválido")
        if profile_id not in self._services:
            self._services[profile_id] = ReminderService(self._folder / profile_id / "lembretes.json")
        return self._services[profile_id]

    def create(self, profile_id: str, text: str, when: str) -> dict[str, Any]:
        item = self.service(profile_id).create(text, when, notify_phone=True)
        return {"id": item.id, "text": item.text, "when": item.due_at}

    def upcoming(self, profile_id: str) -> list[dict[str, Any]]:
        items = [item for item in self.service(profile_id).list() if item.status == "scheduled"]
        return [{"id": item.id, "text": item.text, "when": item.due_at} for item in sorted(items, key=lambda i: i.due_at)][:20]

    def delete(self, profile_id: str, reminder_id: str) -> bool:
        return self.service(profile_id).delete(reminder_id)

    def pop_due(self, profile_id: str) -> list[str]:
        """Lembretes que tocaram e a tela ainda não falou."""
        queue = self._pending.pop(profile_id, deque())
        return list(queue)

    def _known_profiles(self) -> list[str]:
        try:
            found = [path.parent.name for path in self._folder.glob("p*/lembretes.json") if PROFILE_ID_RE.fullmatch(path.parent.name)]
        except OSError:
            found = []
        return list(dict.fromkeys(found + list(self._services)))

    async def tick(self) -> int:
        fired = 0
        for profile_id in self._known_profiles():
            for event in self.service(profile_id).tick():
                text = _clean((event.get("reminder") or {}).get("text"), 200)
                if not text:
                    continue
                fired += 1
                self._pending.setdefault(profile_id, deque(maxlen=20)).append(text)
                if self._push is not None:
                    try:
                        await self._push("Kira: lembrete", text, profile_id)
                    except Exception as exc:  # sem push: a tela aberta ainda fala
                        logger.info("GUEST_REMINDER_PUSH_FAILED reason=%s", type(exc).__name__)
        return fired

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception as exc:  # um arquivo ruim não derruba o laço
                logger.info("GUEST_REMINDERS_TICK_FAILED reason=%s", type(exc).__name__)
            try:
                await asyncio.wait_for(stop.wait(), TICK_S)
            except asyncio.TimeoutError:
                pass


class GuestTools:
    """Executa a ação pedida na conversa do convidado e devolve {"card": {...}, "say": "frase a mais"}."""

    def __init__(self, *, weather: Callable[[str], Awaitable[dict[str, Any]]], news: Callable[[str, list[str]], Awaitable[list[dict[str, Any]]]],
                 recipe: Callable[[str], Awaitable[dict[str, Any]]], music: Callable[[str], Awaitable[dict[str, Any] | None]],
                 reminders: GuestReminders, default_city: Callable[[], str] = lambda: "") -> None:
        self._weather = weather
        self._news = news
        self._recipe = recipe
        self._music = music
        self.reminders = reminders
        self._default_city = default_city

    async def run(self, profile_id: str, action: dict[str, str], settings: dict[str, Any]) -> dict[str, Any]:
        kind = action.get("tipo")
        try:
            if kind == "clima":
                return await self.weather(action.get("cidade") or settings.get("cidade") or self._default_city())
            if kind == "noticias":
                return await self.news(action.get("tema") or "", settings.get("interesses") or [])
            if kind == "receita":
                try:
                    recipe = await self._recipe(action["prato"])
                except ValueError:
                    return {"card": None, "say": "Não consegui montar essa receita agora. Pede de novo?"}
                return {"card": {"type": "receita", **recipe},
                        "say": f"São {len(recipe['steps'])} passos. Quando estiver pronta, toca em começar que eu vou te guiando."}
            if kind == "musica":
                track = await self._music(action["busca"])
                if not track:
                    return {"card": None, "say": "Não achei essa música agora."}
                return {"card": {"type": "musica", **track}, "say": ""}
            if kind == "lembrete":
                made = await asyncio.to_thread(self.reminders.create, profile_id, action["texto"], action["quando"])
                when = datetime.fromisoformat(made["when"])
                return {"card": {"type": "lembrete", **made}, "say": f"Marquei para {when.strftime('%d/%m às %H:%M')}."}
        except ValueError as exc:
            return {"card": None, "say": str(exc) if str(exc) else "Não deu certo agora."}
        except Exception as exc:  # fonte fora do ar: a conversa segue
            logger.info("GUEST_ACTION_FAILED kind=%s reason=%s", kind, type(exc).__name__)
            return {"card": None, "say": "Não consegui buscar isso agora. Tenta daqui a pouco?"}
        return {"card": None, "say": ""}

    async def weather(self, city: str) -> dict[str, Any]:
        city = _clean(city, 60)
        if not city:
            return {"card": None, "say": "Me diz a sua cidade (no menu, em Meus ajustes) que eu vejo o clima."}
        data = await self._weather(city)
        current = data.get("current") or {}
        days = [{"date": day.get("date"), "condition": day.get("condition"), "min": day.get("min"), "max": day.get("max"),
                 "rain": day.get("precipitationProbability")} for day in (data.get("forecast") or [])[:4]]
        place = _clean((data.get("location") or {}).get("name") or city, 60)
        say = ", ".join(part for part in (f"Agora em {place}: {_degrees(current.get('temperature'))}", _clean(current.get("condition"), 40).lower()) if part)
        if days and isinstance(days[0].get("max"), (int, float)):
            say += f". Hoje a máxima é de {_degrees(days[0]['max'])}"
            if isinstance(days[0].get("rain"), (int, float)) and days[0]["rain"] >= 40:
                say += f", com {round(days[0]['rain'])}% de chance de chuva"
        return {"card": {"type": "clima", "city": place, "now": {"temp": current.get("temperature"), "condition": current.get("condition")},
                         "days": days}, "say": say + "."}

    async def news(self, topic: str, interests: list[str]) -> dict[str, Any]:
        items = await self._news(_clean(topic, 80), interests)
        picked = [{"title": _clean(item.get("title"), 200), "source": _clean(item.get("source"), 60), "url": str(item.get("link") or "")}
                  for item in items if item.get("title")][:MAX_NEWS]
        if not picked:
            return {"card": None, "say": "Não achei notícia sobre isso agora."}
        say = "As principais: " + "; ".join(item["title"] for item in picked[:3]) + "."
        return {"card": {"type": "noticias", "topic": _clean(topic, 80) or ", ".join(interests[:3]), "items": picked}, "say": say}

    async def briefing(self, profile_id: str, settings: dict[str, Any], features: dict[str, bool]) -> list[dict[str, Any]]:
        """Resumo do dia da pessoa: clima da cidade dela, notícias dos interesses e os próximos lembretes."""
        parts: list[dict[str, Any]] = []
        jobs = []
        if features.get("clima") and (settings.get("cidade") or self._default_city()):
            jobs.append(self.weather(settings.get("cidade") or self._default_city()))
        if features.get("noticias"):
            jobs.append(self.news("", settings.get("interesses") or []))
        for result in await asyncio.gather(*jobs, return_exceptions=True):
            if isinstance(result, dict) and result.get("card"):
                parts.append(result)
        if features.get("lembretes"):
            upcoming = await asyncio.to_thread(self.reminders.upcoming, profile_id)
            if upcoming:
                first = datetime.fromisoformat(upcoming[0]["when"])
                parts.append({"card": {"type": "lembretes", "items": upcoming[:5]},
                              "say": f"E você tem {len(upcoming)} lembrete{'s' if len(upcoming) > 1 else ''}; o próximo, {upcoming[0]['text']}, "
                                     f"às {first.strftime('%H:%M')}."})
        return parts


__all__ = ["GuestReminders", "GuestTools"]
