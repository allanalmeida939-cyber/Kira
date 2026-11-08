from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any


class WhatsAppLogger:
    """
    Logger dedicado para eventos do fluxo de WhatsApp.
    """

    def __init__(self) -> None:
        root_dir = Path(__file__).resolve().parents[1]
        self.logs_dir = root_dir / "logs_whatsapp"
        self.logs_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.log_file = self.logs_dir / f"whatsapp_{timestamp}.log"

        logger_name = f"whatsapp_dedicated.{timestamp}"
        self.logger = logging.getLogger(logger_name)
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

        handler = logging.FileHandler(self.log_file, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        self.logger.handlers.clear()
        self.logger.addHandler(handler)

        self.logger.info("=" * 60)
        self.logger.info("WHATSAPP LOG SESSION STARTED")
        self.logger.info("=" * 60)
        self._last_tool_ack_key: tuple[str, str, bool] | None = None

    def log_event(self, event_type: str, data: dict[str, Any]) -> None:
        self.logger.info("[%s] %s", event_type, data)

    def log_intent(self, text: str, contact: str, message: str, classification: str) -> None:
        self.logger.info(
            "[INTENT] text=%r | contact=%s | message=%r | classification=%s",
            text,
            contact,
            message,
            classification,
        )

    def log_contact_resolution(self, query: str, found: bool, name: str | None = None) -> None:
        if found:
            self.logger.info("[CONTACT_RESOLVED] query=%r -> name=%s", query, name or "")
        else:
            self.logger.warning("[CONTACT_NOT_FOUND] query=%r", query)

    def log_pending(self, action: str, contact: str, missing: str | None = None) -> None:
        if missing:
            self.logger.info("[PENDING_CREATED] action=%s contact=%s missing=%s", action, contact, missing)
        else:
            self.logger.info("[PENDING_COMPLETED] action=%s contact=%s", action, contact)

    def log_send_attempt(self, contact: str, message: str, dry_run: bool) -> None:
        mode = "DRY_RUN" if dry_run else "REAL"
        preview = (message or "")[:80]
        self.logger.info("[SEND_ATTEMPT] mode=%s contact=%s message=%r", mode, contact, preview)

    def log_send_result(self, success: bool, contact: str, error: str | None = None) -> None:
        if success:
            self.logger.info("[SEND_SUCCESS] contact=%s", contact)
        else:
            self.logger.error("[SEND_FAILED] contact=%s error=%s", contact, error or "")

    def log_bridge_status(self, connected: bool, search_ready: bool, send_ready: bool) -> None:
        self.logger.info(
            "[BRIDGE_STATUS] connected=%s search_ready=%s send_ready=%s",
            str(bool(connected)).lower(),
            str(bool(search_ready)).lower(),
            str(bool(send_ready)).lower(),
        )

    def log_error(self, stage: str, error: str) -> None:
        self.logger.error("[ERROR] stage=%s error=%s", stage, error)

    def log_tool_ack(self, action: str, speak_text: str, success: bool) -> None:
        dedupe_key = (str(action or ""), str(speak_text or ""), bool(success))
        if dedupe_key == self._last_tool_ack_key:
            return
        self._last_tool_ack_key = dedupe_key
        status = "SUCCESS" if success else "FAILED"
        preview = (speak_text or "")[:120]
        self.logger.info("[TOOL_ACK_%s] action=%s text=%r", status, action or "", preview)


_whatsapp_logger: WhatsAppLogger | None = None


def get_whatsapp_logger() -> WhatsAppLogger:
    global _whatsapp_logger
    if _whatsapp_logger is None:
        _whatsapp_logger = WhatsAppLogger()
    return _whatsapp_logger
