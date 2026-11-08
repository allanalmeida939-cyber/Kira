"""Tools de lembretes por voz: criam, listam e cancelam no backend e devolvem o resultado real."""

from __future__ import annotations

from datetime import datetime

from livekit.agents import function_tool

try:
    from agent.runtime.media_tools import _post_local
    from agent.runtime.web_tools import _UNAVAILABLE
except ImportError:  # pragma: no cover - execucao com agent/ no sys.path
    from runtime.media_tools import _post_local
    from runtime.web_tools import _UNAVAILABLE

from services.reminder_intent import LOCAL_TZ, WEEKDAYS

_TIMEOUT_SECONDS = 4.0
_FAILED = "Não consegui falar com o agendador de lembretes agora."


async def _call(path: str, payload: dict[str, object]) -> str:
    try:
        result = await _post_local(path, payload, read_timeout=_TIMEOUT_SECONDS)
    except Exception:
        return _FAILED
    if result.get("success"):
        return " ".join(str(result.get("summary") or "ok").split())[:400]
    reason = " ".join(str(result.get("error") or "").split())[:200]
    return f"Falhou: {reason}" if reason else _FAILED


class ReminderToolsMixin:
    @function_tool(
        name="create_reminder",
        description=(
            "Cria um lembrete ou alarme de verdade. Para 'daqui a N minutos/horas' passe in_minutes; para um horário "
            "passe due_iso (ISO com offset -03:00, ex.: 2026-10-17T22:36:00-03:00). kind: 'reminder' ou 'alarm'; "
            "repeat: 'none', 'daily', 'weekly' ou 'monthly' (todo mês no mesmo dia). Remédio ou tarefa em intervalo ('de 8 em 8 horas por 10 dias'): "
            "due_iso = primeiro horário, every_hours=8, for_days=10. no_celular=true quando o Chefe pedir o aviso no "
            "celular ('me manda notificação no celular às 20h', 'me avisa no celular daqui a 10 min'): na hora certa a "
            "notificação chega no celular sozinha, sem você precisar mandar depois (pedido feito pelo celular já vai "
            "para lá). O retorno diz o horário gravado ou o erro real. Só confirme ao usuário DEPOIS de receber sucesso."
        ),
    )
    async def create_reminder(
        self,
        text: str,
        in_minutes: int | None = None,
        due_iso: str | None = None,
        kind: str = "reminder",
        repeat: str = "none",
        every_hours: float | None = None,
        for_days: float | None = None,
        no_celular: bool = False,
    ) -> str:
        return await _call(
            "/api/reminders/internal/create",
            {
                "text": text, "in_minutes": in_minutes, "due_iso": due_iso, "kind": kind, "repeat": repeat,
                "every_hours": every_hours, "for_days": for_days, "notify_phone": bool(no_celular),
            },
        )

    @function_tool(name="list_reminders", description="Lista os lembretes e alarmes agendados (texto e horário).")
    async def list_reminders(self) -> str:
        return await _call("/api/reminders/internal/list", {})

    @function_tool(
        name="cancel_reminder",
        description="Cancela um lembrete ativo por trecho do texto ou id. Se mais de um combinar, o retorno lista as opções.",
    )
    async def cancel_reminder(self, text_or_id: str) -> str:
        return await _call("/api/reminders/internal/cancel", {"query": text_or_id})

    @function_tool(name="get_current_time", description="Devolve a data e a hora atuais (America/Fortaleza).")
    async def get_current_time(self) -> str:
        now = datetime.now(LOCAL_TZ)
        return f"Agora são {now:%H:%M} de {WEEKDAYS[now.weekday()]}, {now:%d/%m/%Y}."


__all__ = ["ReminderToolsMixin"]
