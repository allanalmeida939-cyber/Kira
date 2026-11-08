"""Resolucao de horario e busca de lembretes para as tools de voz (sem I/O)."""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta, timezone

LOCAL_TZ = timezone(timedelta(hours=-3))  # America/Fortaleza
WEEKDAYS = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")


def fold(value: object) -> str:
    return unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower().strip()


def resolve_due(due_iso: str | None, in_minutes: int | None, now: datetime) -> datetime:
    """Horario do lembrete. `in_minutes` tem prioridade; hora sem data que ja passou hoje vira amanha."""
    if in_minutes is not None:
        if isinstance(in_minutes, bool) or not isinstance(in_minutes, int) or not 1 <= in_minutes <= 60 * 24 * 365:
            raise ValueError("Minutos inválidos.")
        return now + timedelta(minutes=in_minutes)
    text = str(due_iso or "").strip()
    if not text:
        raise ValueError("Informe o horário do lembrete.")
    try:
        due = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("Horário inválido; use o formato ISO, por exemplo 2026-10-17T22:36:00.") from exc
    if due.tzinfo is None:
        due = due.replace(tzinfo=LOCAL_TZ)
    due = due.astimezone(LOCAL_TZ)
    local_now = now.astimezone(LOCAL_TZ)
    if due < local_now - timedelta(seconds=60) and due.date() == local_now.date():
        due += timedelta(days=1)
    return due


def describe(due: datetime, now: datetime) -> str:
    local, today = due.astimezone(LOCAL_TZ), now.astimezone(LOCAL_TZ).date()
    clock = local.strftime("%H:%M")
    if local.date() == today:
        return f"{clock} de hoje"
    if local.date() == today + timedelta(days=1):
        return f"{clock} de amanhã"
    return f"{clock} de {local.strftime('%d/%m')} ({WEEKDAYS[local.weekday()]})"


def find_matches(items: list[dict], query: str) -> list[dict]:
    """Itens ativos cujo id ou texto combina com a consulta."""
    wanted = fold(query)
    active = [i for i in items if i.get("status") in {"scheduled", "firing"}]
    exact = [i for i in active if i.get("id") == query.strip()]
    if exact or not wanted:
        return exact
    return [i for i in active if wanted in fold(i.get("text"))]


def time_context(now: datetime) -> str:
    local = now.astimezone(LOCAL_TZ)
    return (
        f"CONTEXTO DE TEMPO (America/Fortaleza, UTC-03:00): agora são {local:%H:%M} de {WEEKDAYS[local.weekday()]}, {local:%d/%m/%Y}. "
        "Para lembretes e alarmes use SEMPRE a tool create_reminder: 'daqui a N minutos/horas' vira in_minutes; "
        "'às HH:MM' vira due_iso com offset -03:00 (hora sem data = hoje; se já passou, amanhã). "
        "NUNCA diga 'anotado', 'agendado' ou 'combinado' antes de a tool devolver sucesso; se falhar, diga que não conseguiu e o motivo. "
        "Se precisar saber a hora exata agora, chame get_current_time."
    )
