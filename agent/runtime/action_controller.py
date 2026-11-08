from __future__ import annotations

import importlib
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _load_pyautogui_module() -> Any:
    return importlib.import_module("pyautogui")


class ActionController:
    """Executa acoes basicas de mouse e teclado com fallbacks seguros."""

    def __init__(
        self,
        enabled: bool = True,
        *,
        safety_pause: float | None = None,
        pyautogui_module: Any | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.safety_pause = (
            max(float(safety_pause), 0.0)
            if safety_pause is not None
            else max(float(os.getenv("ACTION_CONTROLLER_SAFETY_PAUSE", "0.1")), 0.0)
        )
        self._pyautogui = pyautogui_module
        self._last_error: str | None = None

        if self._pyautogui is None:
            try:
                self._pyautogui = _load_pyautogui_module()
            except ModuleNotFoundError as exc:
                self._last_error = "PyAutoGUI nao instalado. Execute: pip install pyautogui"
                self._pyautogui = None
                logger.error("[ACTION_CONTROLLER] %s", self._last_error)
                logger.debug("[ACTION_CONTROLLER] Import error", exc_info=exc)

        if self._pyautogui is not None:
            self._pyautogui.PAUSE = self.safety_pause
            self._pyautogui.FAILSAFE = True

        logger.info("[ACTION_CONTROLLER] Inicializado - enabled=%s", self.enabled)

    def click(
        self,
        x: int,
        y: int,
        button: str = "left",
        clicks: int = 1,
    ) -> bool:
        if not self._ensure_ready():
            return False

        normalized = self._normalize_coordinates(x, y)
        if normalized is None:
            return False
        norm_x, norm_y = normalized

        try:
            logger.info(
                "[ACTION_CONTROLLER] Click - x=%d y=%d button=%s clicks=%d",
                norm_x,
                norm_y,
                button,
                int(clicks),
            )
            self._pyautogui.click(norm_x, norm_y, button=button, clicks=int(clicks))
            return True
        except Exception as exc:
            self._last_error = str(exc)
            logger.error("[ACTION_CONTROLLER] Erro ao clicar: %s", exc)
            return False

    def type_text(self, text: str, interval: float = 0.05) -> bool:
        if not self._ensure_ready():
            return False

        try:
            payload = str(text or "")
            delay = max(float(interval), 0.0)
            logger.info(
                "[ACTION_CONTROLLER] Typing - len=%d texto=%r",
                len(payload),
                payload[:30],
            )
            self._pyautogui.write(payload, interval=delay)
            return True
        except Exception as exc:
            self._last_error = str(exc)
            logger.error("[ACTION_CONTROLLER] Erro ao digitar: %s", exc)
            return False

    def press_key(self, key: str) -> bool:
        if not self._ensure_ready():
            return False

        normalized_key = str(key or "").strip().lower()
        if not normalized_key:
            logger.error("[ACTION_CONTROLLER] Tecla invalida - key=%r", key)
            return False

        try:
            logger.info("[ACTION_CONTROLLER] Press key - key=%r", normalized_key)
            if "+" in normalized_key:
                keys = [part.strip() for part in normalized_key.split("+") if part.strip()]
                if len(keys) < 2:
                    logger.error("[ACTION_CONTROLLER] Hotkey invalida - key=%r", key)
                    return False
                self._pyautogui.hotkey(*keys)
            else:
                self._pyautogui.press(normalized_key)
            return True
        except Exception as exc:
            self._last_error = str(exc)
            logger.error("[ACTION_CONTROLLER] Erro ao apertar tecla: %s", exc)
            return False

    def scroll(self, clicks: int) -> bool:
        if not self._ensure_ready():
            return False

        try:
            amount = int(clicks)
            logger.info("[ACTION_CONTROLLER] Scroll - clicks=%d", amount)
            self._pyautogui.scroll(amount)
            return True
        except Exception as exc:
            self._last_error = str(exc)
            logger.error("[ACTION_CONTROLLER] Erro ao rolar: %s", exc)
            return False

    def move_mouse(self, x: int, y: int, duration: float = 0.2) -> bool:
        if not self._ensure_ready():
            return False

        normalized = self._normalize_coordinates(x, y)
        if normalized is None:
            return False
        norm_x, norm_y = normalized

        try:
            logger.info(
                "[ACTION_CONTROLLER] Move mouse - x=%d y=%d duration=%.2f",
                norm_x,
                norm_y,
                float(duration),
            )
            self._pyautogui.moveTo(norm_x, norm_y, duration=max(float(duration), 0.0))
            return True
        except Exception as exc:
            self._last_error = str(exc)
            logger.error("[ACTION_CONTROLLER] Erro ao mover mouse: %s", exc)
            return False

    def get_last_error(self) -> str | None:
        return self._last_error

    def _ensure_ready(self) -> bool:
        if not self.enabled:
            logger.warning("[ACTION_CONTROLLER] Disabled - acao ignorada")
            return False
        if self._pyautogui is None:
            logger.error("[ACTION_CONTROLLER] PyAutoGUI indisponivel")
            return False
        return True

    def _normalize_coordinates(self, x: int, y: int) -> tuple[int, int] | None:
        try:
            norm_x = int(x)
            norm_y = int(y)
        except Exception:
            logger.error("[ACTION_CONTROLLER] Coordenada invalida - x=%r y=%r", x, y)
            return None

        screen_width, screen_height = self._screen_size()
        if not (0 <= norm_x < screen_width and 0 <= norm_y < screen_height):
            logger.error(
                "[ACTION_CONTROLLER] Coordenada inválida - x=%d y=%d screen=%dx%d",
                norm_x,
                norm_y,
                screen_width,
                screen_height,
            )
            return None
        return norm_x, norm_y

    def _screen_size(self) -> tuple[int, int]:
        width, height = self._pyautogui.size()
        return int(width), int(height)


__all__ = ["ActionController"]
