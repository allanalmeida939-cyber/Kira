from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass


class AssistantDesktopBridgeError(Exception):
    pass


@dataclass
class _Point:
    x: int
    y: int


class _WinPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class AssistantDesktopBridge:
    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32
        self._SM_CXSCREEN = 0
        self._SM_CYSCREEN = 1
        self._MOUSEEVENTF_MOVE = 0x0001
        self._MOUSEEVENTF_LEFTDOWN = 0x0002
        self._MOUSEEVENTF_LEFTUP = 0x0004
        self._MOUSEEVENTF_WHEEL = 0x0800

    def get_screen_size(self) -> tuple[int, int]:
        width = int(self._user32.GetSystemMetrics(self._SM_CXSCREEN))
        height = int(self._user32.GetSystemMetrics(self._SM_CYSCREEN))
        return width, height

    def validate_point(self, x: float | int, y: float | int) -> tuple[int, int]:
        width, height = self.get_screen_size()
        xi = int(round(float(x)))
        yi = int(round(float(y)))
        if xi < 0 or yi < 0 or xi >= width or yi >= height:
            raise AssistantDesktopBridgeError(
                f"Coordenadas invalidas: ({xi}, {yi}) fora da tela {width}x{height}."
            )
        return xi, yi

    def move_real_cursor_to(self, x: float | int, y: float | int) -> tuple[int, int]:
        xi, yi = self.validate_point(x, y)
        ok = bool(self._user32.SetCursorPos(xi, yi))
        if not ok:
            raise AssistantDesktopBridgeError("Falha ao mover cursor real.")
        return xi, yi

    def click_at(self, x: float | int, y: float | int, *, pre_move: bool = True) -> tuple[int, int]:
        if pre_move:
            xi, yi = self.move_real_cursor_to(x, y)
        else:
            xi, yi = self.validate_point(x, y)
        self._user32.mouse_event(self._MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        self._user32.mouse_event(self._MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        return xi, yi

    def double_click_at(self, x: float | int, y: float | int) -> tuple[int, int]:
        xi, yi = self.click_at(x, y, pre_move=True)
        time.sleep(0.06)
        self._user32.mouse_event(self._MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        self._user32.mouse_event(self._MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        return xi, yi

    def scroll_at(self, delta: int, x: float | int | None = None, y: float | int | None = None) -> dict[str, int]:
        if x is not None and y is not None:
            xi, yi = self.move_real_cursor_to(x, y)
        else:
            pt = _WinPoint()
            ok = bool(self._user32.GetCursorPos(ctypes.byref(pt)))
            if not ok:
                raise AssistantDesktopBridgeError("Falha ao obter posicao do cursor.")
            xi, yi = int(pt.x), int(pt.y)
        wheel_delta = int(delta)
        if wheel_delta == 0:
            raise AssistantDesktopBridgeError("Delta de scroll nao pode ser zero.")
        self._user32.mouse_event(self._MOUSEEVENTF_WHEEL, 0, 0, wheel_delta, 0)
        return {"x": xi, "y": yi, "delta": wheel_delta}
