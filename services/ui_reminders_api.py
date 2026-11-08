from __future__ import annotations

import hmac
from datetime import datetime, timedelta
from typing import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from services.reminder_intent import describe, find_matches, resolve_due
from services.reminders_service import ReminderService


router = APIRouter()


def _service(request: Request) -> ReminderService:
    return request.app.state.reminder_service


async def _json(request: Request) -> dict:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise ValueError("Content-Type deve ser application/json.")
    payload = await request.json()
    if not isinstance(payload, dict):
        raise ValueError("O corpo JSON precisa ser um objeto.")
    return payload


def _error(message: str, status: int = 400):
    return JSONResponse({"success": False, "error": str(message)}, status_code=status)


@router.get("/api/reminders")
async def list_reminders(request: Request):
    items = [item.as_dict() for item in _service(request).list()]
    return {"success": True, "items": items, "data": {"reminders": items}}


@router.post("/api/reminders")
async def create_reminder(request: Request):
    try:
        payload = await _json(request)
        item = _service(request).create(
            payload.get("text", ""),
            payload.get("due_at", ""),
            payload.get("kind", "reminder"),
            payload.get("repeat", "none"),
        )
        return {"success": True, "item": item.as_dict(), "data": {"reminder": item.as_dict()}}
    except Exception as exc:
        return _error(str(exc))


@router.delete("/api/reminders/{reminder_id}")
async def delete_reminder(reminder_id: str, request: Request):
    if not _service(request).delete(reminder_id):
        return _error("Lembrete não encontrado.", 404)
    return {"success": True}


@router.post("/api/reminders/{reminder_id}/dismiss")
async def dismiss_reminder(reminder_id: str, request: Request):
    try:
        item = _service(request).dismiss(reminder_id)
        return {"success": True, "item": item.as_dict()}
    except KeyError as exc:
        return _error(str(exc), 404)
    except Exception as exc:
        return _error(str(exc))


@router.post("/api/reminders/{reminder_id}/snooze")
async def snooze_reminder(reminder_id: str, request: Request):
    try:
        payload = await _json(request)
        item = _service(request).snooze(reminder_id, payload.get("minutes"))
        return {"success": True, "item": item.as_dict()}
    except KeyError as exc:
        return _error(str(exc), 404)
    except Exception as exc:
        return _error(str(exc))


@router.get("/api/reminders/events")
async def reminder_events(request: Request, since: int = 0):
    items, sequence = _service(request).events_since(since)
    return {"success": True, "events": items, "seq": sequence}


@router.get("/api/reminders/voice-pending")
async def reminder_voice_pending(request: Request):
    from services.kira_settings import voice_allowed

    if not voice_allowed():
        # Avisos por voz desligados ou horário de silêncio: ficam pendentes (o sininho mostra) e saem depois.
        return {"success": True, "items": [], "quiet": True}
    return {"success": True, "items": [item.as_dict() for item in _service(request).voice_pending_items()]}


@router.post("/api/reminders/{reminder_id}/voice-ack")
async def reminder_voice_ack(reminder_id: str, request: Request):
    try:
        await _json(request)
        item = _service(request).voice_ack(reminder_id)
        return {"success": True, "item": item.as_dict()}
    except KeyError as exc:
        return _error(str(exc), 404)
    except Exception as exc:
        return _error(str(exc))


def create_internal_reminders_router(token_provider: Callable[[], str], phone_active: Callable[[], bool] = lambda: False) -> APIRouter:
    """Rotas usadas pelas tools do agente; exigem o token interno."""
    internal = APIRouter()

    def authorized(request: Request) -> bool:
        expected = str(token_provider() or "")
        supplied = str(request.headers.get("X-Kira-Internal") or "")
        return bool(expected and supplied and hmac.compare_digest(supplied, expected))

    def describe_item(item: dict, now: datetime) -> str:
        return f"{item['text']} ({describe(datetime.fromisoformat(item['snooze_until'] or item['due_at']), now)})"

    @internal.post("/api/reminders/internal/create")
    async def internal_create(request: Request):
        if not authorized(request):
            return _error("acao interna nao autorizada", 403)
        try:
            payload = await _json(request)
            service = _service(request)
            now = service.clock().astimezone()
            due = resolve_due(payload.get("due_iso"), payload.get("in_minutes"), now)
            every_hours = float(payload.get("every_hours") or 0)
            for_days = float(payload.get("for_days") or 0)
            repeat = "interval" if every_hours > 0 else payload.get("repeat", "none")
            until = (due + timedelta(days=for_days)).isoformat() if for_days > 0 else None
            # pedido pelo celular (ou "me avisa no celular"): na hora, também notificação no celular
            notify_phone = bool(payload.get("notify_phone")) or phone_active()
            item = service.create(
                payload.get("text", ""), due.isoformat(), payload.get("kind", "reminder"), repeat,
                interval_minutes=int(round(every_hours * 60)), until=until, notify_phone=notify_phone,
            )
            summary = f"agendado para {describe(due, now)}" + (", com notificação no celular" if notify_phone else "")
            if repeat == "interval":
                summary += f", repetindo a cada {every_hours:g} h" + (f" por {for_days:g} dia(s)" if for_days > 0 else "")
            return {"success": True, "summary": summary, "item": item.as_dict()}
        except Exception as exc:
            return _error(str(exc))

    @internal.post("/api/reminders/internal/list")
    async def internal_list(request: Request):
        if not authorized(request):
            return _error("acao interna nao autorizada", 403)
        service = _service(request)
        now = service.clock().astimezone()
        active = [i.as_dict() for i in service.list() if i.status in {"scheduled", "firing"}]
        summary = "; ".join(describe_item(i, now) for i in active[:8]) or "nenhum lembrete agendado"
        return {"success": True, "summary": summary, "count": len(active)}

    @internal.post("/api/reminders/internal/cancel")
    async def internal_cancel(request: Request):
        if not authorized(request):
            return _error("acao interna nao autorizada", 403)
        try:
            payload = await _json(request)
            service = _service(request)
            matches = find_matches([i.as_dict() for i in service.list()], str(payload.get("query") or ""))
            if not matches:
                return _error("nao encontrei lembrete ativo com essa descricao")
            if len(matches) > 1:
                now = service.clock().astimezone()
                return _error("mais de um lembrete combina: " + "; ".join(describe_item(i, now) for i in matches[:4]))
            service.delete(matches[0]["id"])
            return {"success": True, "summary": f"cancelado: {matches[0]['text']}"}
        except Exception as exc:
            return _error(str(exc))

    return internal
