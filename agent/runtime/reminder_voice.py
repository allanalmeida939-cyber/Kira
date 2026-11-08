"""Entrega por voz dos lembretes persistentes sem depender da janela desktop."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("kira.reminder_voice")

JsonGetter = Callable[[str], Awaitable[dict[str, Any]]]
JsonPoster = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
IdleCheck = Callable[[], bool]


IDLE_WAIT_CAP_SECONDS = 20.0
Speaker = Callable[[str], Awaitable[bool]]


def _data_text(value: object) -> str:
    """Texto do usuario como dado: sem marcadores, controles ou quebras que virem instrucao."""
    cleaned = "".join(ch if ch.isprintable() else " " for ch in str(value or ""))
    return " ".join(cleaned.replace("<", " ").replace(">", " ").split())[:200]


def _message(item: dict[str, Any]) -> str:
    text = _data_text(item.get("text"))
    if str(item.get("status") or "") == "missed" or item.get("late_seconds"):
        # Passou da hora (Kira fechada ou ocupada): avisa e pergunta se já foi feito (ex.: remédio).
        # Só o dado: a pergunta "já fez?" está nas instruções do aviso, nunca dentro do texto do usuário.
        return f"Lembrete perdido, passou da hora: {text}" if text else "Lembrete perdido."
    prefix = "Alarme" if str(item.get("kind") or "reminder") == "alarm" else "Lembrete"
    return f"{prefix}: {text}" if text else f"{prefix}: você tem um aviso agendado."


async def _wait_interval(seconds: float, stop_event: asyncio.Event | None) -> bool:
    if stop_event is None:
        await asyncio.sleep(seconds)
        return True
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=max(seconds, 0.1))
    except asyncio.TimeoutError:
        return True
    return False


async def reminder_voice_loop(
    *,
    speech_coordinator=None,
    speak: Speaker | None = None,
    is_idle: IdleCheck,
    base_url: str,
    poll_seconds: float = 2.0,
    stop_event: asyncio.Event | None = None,
    get_json: JsonGetter | None = None,
    post_json: JsonPoster | None = None,
    logger: logging.Logger | None = None,
) -> None:
    """Fala itens pendentes quando a conversa está livre e confirma somente após sucesso."""

    output = logger or log
    client = None
    if get_json is None or post_json is None:
        try:
            import httpx

            client = httpx.AsyncClient(
                base_url=str(base_url).rstrip("/"),
                timeout=httpx.Timeout(connect=0.5, read=3.0, write=1.0, pool=0.5),
                verify=str(base_url).lower().startswith("https://"),
            )

            async def _get(path: str) -> dict[str, Any]:
                response = await client.get(path)
                response.raise_for_status()
                return response.json()

            async def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
                response = await client.post(path, json=payload)
                response.raise_for_status()
                return response.json()

            get_json, post_json = _get, _post
        except Exception as exc:
            output.warning("REMINDER_VOICE_LOOP_DISABLED reason=http_client:%s", type(exc).__name__)
            return

    try:
        output.info("REMINDER_VOICE_LOOP_STARTED poll_s=%.1f", max(poll_seconds, 0.5))
        busy_since: float | None = None
        while True:
            if not await _wait_interval(max(poll_seconds, 0.5), stop_event):
                return
            idle = is_idle()
            if idle:
                busy_since = None
            else:
                busy_since = busy_since if busy_since is not None else time.monotonic()
                if time.monotonic() - busy_since < IDLE_WAIT_CAP_SECONDS:
                    continue
            try:
                payload = await get_json("/api/reminders/voice-pending")
                items = payload.get("items") if isinstance(payload, dict) else []
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if not idle and not (busy_since is not None and time.monotonic() - busy_since >= IDLE_WAIT_CAP_SECONDS):
                        break
                    reminder_id = str(item.get("id") or "").strip()
                    if not reminder_id:
                        continue
                    if speak is not None:
                        delivered = await asyncio.wait_for(speak(_message(item)), timeout=35.0)
                    else:
                        delivered = await asyncio.wait_for(
                            speech_coordinator.speak_proactive_message(
                                _message(item),
                                label="LEMBRETE",
                                add_to_chat_ctx=True,
                                allow_interruptions=False,
                            ),
                            timeout=35.0,
                        )
                    busy_since = None
                    if not delivered:
                        output.warning("REMINDER_VOICE_NOT_DELIVERED id=%s", reminder_id)
                        continue
                    await post_json(f"/api/reminders/{reminder_id}/voice-ack", {})
                    output.info("REMINDER_VOICE_ACK id=%s", reminder_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                output.warning("REMINDER_VOICE_LOOP_ERROR reason=%s", type(exc).__name__)
    finally:
        if client is not None:
            await client.aclose()


__all__ = ["reminder_voice_loop"]
