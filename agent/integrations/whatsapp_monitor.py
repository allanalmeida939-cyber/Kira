from __future__ import annotations

"""
Monitor de mensagens do WhatsApp com reconexao automatica.
Faz polling no WhatsApp Controller (HTTP :5050).
Se o controller nao estiver disponivel, tenta reconectar indefinidamente
com backoff exponencial e nunca para sozinho.
"""

import asyncio
import inspect
import logging
import os
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import httpx

from whatsapp.whatsapp_group_guard import WhatsAppGroupGuard


logger = logging.getLogger(__name__)

WHATSAPP_BRIDGE_URL = "http://127.0.0.1:5050"
POLL_INTERVAL = 3.0
RECONNECT_MIN = 3.0
RECONNECT_MAX = 30.0
STARTUP_GRACE = 90.0


@dataclass
class WhatsAppMonitorResult:
    success: bool
    message: str


def _normalize_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _fold_text(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", _normalize_text(value).lower())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _supports_metadata_kw(callable_obj) -> bool:
    try:
        signature = inspect.signature(callable_obj)
    except Exception:
        return True

    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.name == "metadata":
            return True
    return False


def _supports_metadata_positional(callable_obj) -> bool:
    try:
        signature = inspect.signature(callable_obj)
    except Exception:
        return False

    positional_count = sum(
        1
        for parameter in signature.parameters.values()
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    )
    return positional_count >= 3


class WhatsAppMonitor:
    def __init__(
        self,
        notifier_fn: Callable[..., Awaitable[None]],
        loop: asyncio.AbstractEventLoop,
    ):
        from datetime import datetime as _dt

        self._notifier = notifier_fn
        self._loop = loop
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._seen_ids: set[str] = set()
        self._startup_time = _dt.now()
        self._status_lock = threading.Lock()
        self._connected = False
        self._last_error = "aguardando bridge"
        self._notifier_accepts_metadata_kw = _supports_metadata_kw(notifier_fn)
        self._notifier_accepts_metadata_positional = _supports_metadata_positional(notifier_fn)
        self.group_guard = WhatsAppGroupGuard()

    def start(self) -> WhatsAppMonitorResult:
        """
        Inicia o monitor em thread separada.
        Nao falha se a bridge ainda nao estiver disponivel;
        fica tentando em background ate conseguir.
        """
        thread = self._thread
        if thread is not None and thread.is_alive():
            return WhatsAppMonitorResult(
                success=True,
                message="Monitor WhatsApp iniciado - aguardando bridge na porta 5050.",
            )

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._monitor_with_reconnect,
            daemon=True,
            name="whatsapp-controller-monitor",
        )
        self._thread.start()
        self._update_status(connected=False, error="aguardando bridge")
        logger.info("[WHATSAPP MONITOR] Monitor iniciado em background.")
        return WhatsAppMonitorResult(
            success=True,
            message="Monitor WhatsApp iniciado - aguardando bridge na porta 5050.",
        )

    def stop(self):
        self._stop.set()
        self._update_status(connected=False, error="monitor parado")

    def get_status(self) -> dict[str, object]:
        thread = self._thread
        running = thread is not None and thread.is_alive() and not self._stop.is_set()
        with self._status_lock:
            return {
                "running": running,
                "connected": self._connected,
                "last_error": self._last_error,
            }

    def _monitor_with_reconnect(self):
        """
        Loop externo: tenta conectar, monitora, e se perder a conexao
        reconecta com backoff exponencial. Nunca para ate stop() ser chamado.
        """
        first_attempt = True
        initial_queue_flushed = False
        backoff = RECONNECT_MIN

        while not self._stop.is_set():
            wait_timeout = STARTUP_GRACE if first_attempt else RECONNECT_MAX
            if not self._wait_for_bridge(timeout=wait_timeout):
                if self._stop.is_set():
                    return
                self._update_status(connected=False, error="bridge indisponivel")
                logger.warning(
                    "[WHATSAPP MONITOR] Bridge indisponivel apos %.0fs - tentando novamente em %.0fs.",
                    wait_timeout,
                    backoff,
                )
                self._sleep(backoff)
                backoff = min(backoff * 1.5, RECONNECT_MAX)
                continue

            first_attempt = False
            backoff = RECONNECT_MIN
            self._update_status(connected=True, error="")
            logger.info("[WHATSAPP MONITOR] Conectado a bridge. Monitorando mensagens...")
            if not initial_queue_flushed:
                initial_queue_flushed = True
                try:
                    with httpx.Client(timeout=5.0) as flush_client:
                        flush_response = flush_client.get(
                            f"{WHATSAPP_BRIDGE_URL}/messages/new/agent", headers={"X-Kira-Internal": os.getenv("KIRA_INTERNAL_TOKEN", "")}
                        )
                        flush_payload = flush_response.json()
                        flushed_messages = (
                            flush_payload.get("messages", [])
                            if isinstance(flush_payload, dict)
                            else []
                        )
                        if flushed_messages:
                            logger.info(
                                "[WHATSAPP MONITOR] %d mensagem(ns) antiga(s) descartada(s) ao iniciar.",
                                len(flushed_messages),
                            )
                except Exception:
                    pass

            disconnected = self._poll_loop()

            if disconnected and not self._stop.is_set():
                self._update_status(connected=False, error="conexao perdida")
                logger.warning(
                    "[WHATSAPP MONITOR] Conexao perdida - reconectando em %.0fs...",
                    backoff,
                )
                self._sleep(backoff)
                backoff = min(backoff * 1.5, RECONNECT_MAX)

        self._update_status(connected=False, error="monitor parado")

    def _wait_for_bridge(self, timeout: float) -> bool:
        """
        Aguarda a bridge HTTP ficar disponivel e com WhatsApp conectado.
        Retorna True se conectou, False se timeout.
        """
        deadline = time.time() + timeout
        while time.time() < deadline and not self._stop.is_set():
            try:
                with httpx.Client(timeout=3.0) as client:
                    response = client.get(f"{WHATSAPP_BRIDGE_URL}/status")
                    payload = response.json()
                    if isinstance(payload, dict) and payload.get("connected"):
                        self._update_status(connected=True, error="")
                        return True
                    self._update_status(connected=False, error="bridge online, whatsapp conectando")
                    logger.debug("[WHATSAPP MONITOR] Bridge online, WPP ainda conectando...")
            except Exception:
                self._update_status(connected=False, error="bridge indisponivel em :5050")
                logger.debug("[WHATSAPP MONITOR] Bridge indisponivel em :5050, aguardando...")
            self._sleep(2.0)
        return False

    def _poll_loop(self) -> bool:
        """
        Loop de polling de mensagens.
        Retorna True se perdeu conexao, False se foi parado via stop().
        """
        consecutive_errors = 0
        max_consecutive_errors = 5

        while not self._stop.is_set():
            try:
                with httpx.Client(timeout=5.0) as client:
                    response = client.get(f"{WHATSAPP_BRIDGE_URL}/messages/new/agent", headers={"X-Kira-Internal": os.getenv("KIRA_INTERNAL_TOKEN", "")})
                    if response.status_code == 200:
                        consecutive_errors = 0
                        self._update_status(connected=True, error="")
                        payload = response.json()
                        messages = payload.get("messages", []) if isinstance(payload, dict) else []
                        for msg in messages:
                            if isinstance(msg, dict):
                                self._dispatch(msg)
                    else:
                        consecutive_errors += 1
                        self._update_status(
                            connected=False,
                            error=f"status HTTP inesperado: {response.status_code}",
                        )
                        logger.debug(
                            "[WHATSAPP MONITOR] Poll retornou status %s (%d/%d).",
                            response.status_code,
                            consecutive_errors,
                            max_consecutive_errors,
                        )
                        if consecutive_errors >= max_consecutive_errors:
                            return True
            except Exception as exc:
                consecutive_errors += 1
                self._update_status(connected=False, error=str(exc))
                logger.debug(
                    "[WHATSAPP MONITOR] Erro no poll (%d/%d): %s",
                    consecutive_errors,
                    max_consecutive_errors,
                    exc,
                )
                if consecutive_errors >= max_consecutive_errors:
                    return True

            self._sleep(POLL_INTERVAL)

        return False

    def _is_after_startup(self, timestamp_str: str) -> bool:
        """
        Retorna True se o timestamp da mensagem (HH:MM ou HH:MM:SS) for
        >= ao horario de inicio do monitor.
        Mensagens sem timestamp ou com formato nao-reconhecido sao aceitas.
        Mensagens com palavras como 'ontem', dia da semana ou data sao rejeitadas.
        """
        import re as _re

        ts = _normalize_text(timestamp_str)
        if not ts:
            return True

        ts_folded = _fold_text(ts)
        palavras_antigas = [
            "ontem",
            "yesterday",
            "segunda",
            "terca",
            "quarta",
            "quinta",
            "sexta",
            "sabado",
            "domingo",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ]
        if any(ts_folded.startswith(palavra) for palavra in palavras_antigas):
            return False

        if _re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", ts):
            return False

        match = _re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", ts)
        if match:
            try:
                hours = int(match.group(1))
                minutes = int(match.group(2))
                seconds = int(match.group(3) or 0)
                msg_total = hours * 3600 + minutes * 60 + seconds
                start_total = (
                    self._startup_time.hour * 3600
                    + self._startup_time.minute * 60
                    + self._startup_time.second
                )
                return msg_total >= start_total
            except Exception:
                return True

        return True

    def _dispatch(self, msg: dict):
        """Despacha mensagem nova para o notifier do agente."""
        raw_payload = dict(msg)
        msg_id = _normalize_text(raw_payload.get("id"))
        if not msg_id or msg_id in self._seen_ids:
            return

        timestamp_str = _normalize_text(raw_payload.get("timestamp"))
        if not self._is_after_startup(timestamp_str):
            logger.debug(
                "[WHATSAPP MONITOR] Ignorando mensagem antiga id=%s contact=%s ts=%s",
                msg_id,
                _normalize_text(raw_payload.get("contact")) or "?",
                timestamp_str or "?",
            )
            self._seen_ids.add(msg_id)
            return

        self._seen_ids.add(msg_id)
        if len(self._seen_ids) > 1000:
            self._seen_ids = set(list(self._seen_ids)[-500:])

        contact_raw = raw_payload.get("contact", "")
        text_raw = raw_payload.get("text", "")
        contact = _normalize_text(contact_raw)
        text = _normalize_text(text_raw)
        if not contact or not text:
            logger.debug(
                "[WHATSAPP MONITOR] Payload invalido descartado id=%s contact=%r text=%r",
                msg_id,
                contact_raw,
                text_raw,
            )
            return

        metadata = {
            "message_id": msg_id,
            "timestamp": timestamp_str or None,
            "source": "whatsapp",
            "transport": "bridge_http_poll",
            "is_group": raw_payload.get("is_group"),
            "show_container": raw_payload.get("show_container"),
            "speak": raw_payload.get("speak"),
            "send_to_llm": raw_payload.get("send_to_llm"),
            "auto_reply": raw_payload.get("auto_reply"),
            "mode": raw_payload.get("mode"),
            "raw_payload": raw_payload,
            "received_at_monotonic": time.monotonic(),
            "received_at_epoch": time.time(),
            "original_text": str(text_raw or ""),
            "normalized_text": text,
            "normalized_contact": contact,
        }

        is_group, group_reason = self.group_guard.is_group(
            {
                "contact": contact,
                "text": text,
                "message": raw_payload.get("message"),
                "is_group": raw_payload.get("is_group"),
            }
        )
        if is_group:
            logger.warning(
                "[WPP_MONITOR] GRUPO bloqueado no agent - contact=%s reason=%s",
                contact,
                group_reason,
            )
            return

        should_speak = bool(raw_payload.get("speak", False))
        should_send_llm = bool(raw_payload.get("send_to_llm", False))
        if not should_speak and not should_send_llm:
            logger.info(
                "[WPP_MONITOR] Modo silencioso ou sem acao de agent - contact=%s speak=%s send_to_llm=%s",
                contact,
                should_speak,
                should_send_llm,
            )
            return

        logger.info(
            "[WHATSAPP MONITOR] Mensagem nova id=%s contact=%s ts=%s chars=%d mode=%s speak=%s send_to_llm=%s",
            msg_id,
            contact,
            timestamp_str or "?",
            len(text),
            metadata.get("mode") or "silent",
            should_speak,
            should_send_llm,
        )

        future = asyncio.run_coroutine_threadsafe(
            self._notify_async(contact, text, metadata),
            self._loop,
        )
        future.add_done_callback(
            lambda done: self._log_notifier_result(
                done,
                message_id=msg_id,
                contact=contact,
            )
        )
        logger.debug(
            "[WHATSAPP MONITOR] Payload despachado ao agente id=%s contact=%s transport=%s",
            msg_id,
            contact,
            metadata["transport"],
        )

    async def _notify_async(
        self,
        contact: str,
        text: str,
        metadata: dict[str, object],
    ) -> None:
        if self._notifier_accepts_metadata_kw:
            await self._notifier(contact, text, metadata=metadata)
            return
        if self._notifier_accepts_metadata_positional:
            await self._notifier(contact, text, metadata)
            return
        await self._notifier(contact, text)

    def _sleep(self, seconds: float):
        """Sleep interrompivel pelo evento stop."""
        self._stop.wait(timeout=seconds)

    def _update_status(self, *, connected: bool, error: str) -> None:
        with self._status_lock:
            self._connected = connected
            self._last_error = error

    @staticmethod
    def _log_notifier_result(future, *, message_id: str, contact: str) -> None:
        try:
            future.result()
            logger.debug(
                "[WHATSAPP MONITOR] Evento entregue ao runtime id=%s contact=%s",
                message_id,
                contact,
            )
        except Exception as exc:
            logger.warning(
                "[WHATSAPP MONITOR] Falha ao entregar notificacao id=%s contact=%s: %s",
                message_id,
                contact,
                exc,
            )
