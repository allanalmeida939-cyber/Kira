"""Agenda do Google (somente leitura): eventos normalizados no fuso do usuário, ordenados e com conflitos marcados."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from services.providers.google_oauth import GoogleAuth, NotConnected

EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
TIMEZONE = ZoneInfo("America/Fortaleza")
TIMEOUT = httpx.Timeout(6.0, connect=3.0)


class CalendarUnavailable(RuntimeError):
    pass


@dataclass
class CalendarEvent:
    id: str
    title: str
    startAt: str
    endAt: str
    timezone: str
    location: str
    source: str
    status: str
    allDay: bool = False
    conflictsWith: list[str] = field(default_factory=list)


def _when(block: object) -> tuple[datetime | None, bool]:
    if not isinstance(block, dict):
        return None, False
    if block.get("dateTime"):
        try:
            return datetime.fromisoformat(str(block["dateTime"]).replace("Z", "+00:00")).astimezone(TIMEZONE), False
        except ValueError:
            return None, False
    if block.get("date"):
        try:
            return datetime.fromisoformat(str(block["date"])).replace(tzinfo=TIMEZONE), True
        except ValueError:
            return None, True
    return None, False


def normalize_events(items: list[dict[str, Any]]) -> list[CalendarEvent]:
    events: list[CalendarEvent] = []
    for item in items:
        if not isinstance(item, dict) or item.get("status") == "cancelled":
            continue
        start, all_day = _when(item.get("start"))
        end, _ = _when(item.get("end"))
        if start is None:
            continue
        end = end or start + (timedelta(days=1) if all_day else timedelta(hours=1))
        events.append(CalendarEvent(
            id=str(item.get("id") or "")[:120], title=" ".join(str(item.get("summary") or "(sem título)").split())[:160],
            startAt=start.isoformat(), endAt=end.isoformat(), timezone=str(TIMEZONE), location=" ".join(str(item.get("location") or "").split())[:160],
            source="Google Agenda", status=str(item.get("status") or "confirmed"), allDay=all_day,
        ))
    events.sort(key=lambda e: e.startAt)
    timed = [e for e in events if not e.allDay]
    for index, event in enumerate(timed):
        for other in timed[index + 1:]:
            if other.startAt >= event.endAt:
                break
            event.conflictsWith.append(other.id)
            other.conflictsWith.append(event.id)
    return events


class CalendarProvider:
    def __init__(self, auth: GoogleAuth, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._auth = auth
        self._transport = transport

    async def events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        try:
            token = await self._auth.access_token()
        except NotConnected as exc:
            raise CalendarUnavailable(str(exc)) from exc
        params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true", "orderBy": "startTime", "maxResults": "50"}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as client:
                response = await client.get(EVENTS_URL, params=params, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            raise CalendarUnavailable("a agenda não respondeu") from exc
        if response.status_code != 200:
            raise CalendarUnavailable("a agenda recusou a consulta")
        payload = response.json()
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise CalendarUnavailable("resposta da agenda inválida")
        return normalize_events(items)


def as_dicts(events: list[CalendarEvent]) -> list[dict[str, Any]]:
    return [asdict(e) for e in events]


__all__ = ["CalendarEvent", "CalendarProvider", "CalendarUnavailable", "TIMEZONE", "as_dicts", "normalize_events"]
