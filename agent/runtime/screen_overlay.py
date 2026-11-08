"""Destaque sobre a tela (processo próprio): contorno pulsante e rótulo em volta do que a Kira encontrou.

Janela por monitor, transparente, sempre no topo, sem foco e atravessável pelo mouse. Some sozinha (8 s), com Esc
ou quando o agente encerra o processo ("pode tirar"). Coordenadas em pixels físicos (escala do Qt desligada).

Uso: python -m runtime.screen_overlay '{"seconds": 8, "monitors": [...], "boxes": [...]}'
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import sys

os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"  # antes do Qt: tudo em pixels físicos, como a captura

from PySide6.QtCore import QRectF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOPMOST = 0x00080000, 0x00000020, 0x00000008
WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = 0x08000000, 0x00000080
HWND_TOPMOST = -1
SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
WDA_EXCLUDEFROMCAPTURE = 0x00000011
VK_ESCAPE = 0x1B
OVERLAY_EX_STYLE = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
DEFAULT_SECONDS = 8.0
ACCENT = QColor(232, 121, 249)


class Overlay(QWidget):
    def __init__(self, monitor: dict, boxes: list[dict]) -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.monitor = monitor
        self.boxes = [{**box, "x": box["x"] - monitor["left"], "y": box["y"] - monitor["top"]} for box in boxes]
        self.phase = 0.0
        self.scale = 1.0
        self.setGeometry(monitor["left"], monitor["top"], monitor["width"], monitor["height"])

    def place(self) -> None:
        """Estilos nativos: atravessável, sem foco, no topo e fora de capturas de tela."""
        user32 = ctypes.windll.user32
        hwnd = int(self.winId())
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, user32.GetWindowLongW(hwnd, GWL_EXSTYLE) | OVERLAY_EX_STYLE)
        m = self.monitor
        user32.SetWindowPos(hwnd, HWND_TOPMOST, m["left"], m["top"], m["width"], m["height"], SWP_NOACTIVATE | SWP_SHOWWINDOW)
        user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        try:
            self.scale = max(1.0, user32.GetDpiForWindow(hwnd) / 96.0)
        except (AttributeError, OSError):
            self.scale = 1.0

    def tick(self) -> None:
        self.phase += 0.12
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - API do Qt
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pulse = 0.5 + 0.5 * math.sin(self.phase)
        pad = 6 * self.scale
        font = QFont("Segoe UI")
        font.setPixelSize(round(14 * self.scale))
        font.setBold(True)
        painter.setFont(font)
        metrics = QFontMetricsF(font)
        for box in self.boxes:
            rect = QRectF(box["x"], box["y"], box["width"], box["height"]).adjusted(-pad, -pad, pad, pad)
            glow = QColor(ACCENT)
            glow.setAlpha(round(60 + 60 * pulse))
            painter.setPen(QPen(glow, 9 * self.scale))
            painter.drawRoundedRect(rect, 10 * self.scale, 10 * self.scale)
            line = QColor(ACCENT)
            line.setAlpha(round(170 + 85 * pulse))
            painter.setPen(QPen(line, 3 * self.scale))
            painter.drawRoundedRect(rect, 10 * self.scale, 10 * self.scale)
            label = str(box.get("label") or "")[:80]
            if not label:
                continue
            width = metrics.horizontalAdvance(label) + 20 * self.scale
            height = metrics.height() + 10 * self.scale
            top = rect.top() - height - 6 * self.scale
            if top < 4:
                top = rect.bottom() + 6 * self.scale
            left = min(max(4.0, rect.left()), self.width() - width - 4)
            pill = QRectF(left, top, width, height)
            path = QPainterPath()
            path.addRoundedRect(pill, height / 2, height / 2)
            painter.fillPath(path, QColor(24, 12, 48, 230))
            painter.setPen(QPen(line, 1.5 * self.scale))
            painter.drawPath(path)
            painter.setPen(QColor(250, 232, 255))
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, label)


def boxes_by_monitor(spec: dict) -> list[tuple[dict, list[dict]]]:
    """Cada caixa vai para o monitor que contém o centro dela; monitor sem caixa não ganha janela."""
    result: list[tuple[dict, list[dict]]] = []
    for monitor in spec.get("monitors") or []:
        inside = [
            box for box in spec.get("boxes") or []
            if monitor["left"] <= box["x"] + box["width"] / 2 < monitor["left"] + monitor["width"]
            and monitor["top"] <= box["y"] + box["height"] / 2 < monitor["top"] + monitor["height"]
        ]
        if inside:
            result.append((monitor, inside))
    return result


def main(argv: list[str]) -> int:
    spec = json.loads(argv[1]) if len(argv) > 1 else {}
    app = QApplication(argv[:1])
    windows = []
    for monitor, boxes in boxes_by_monitor(spec):
        window = Overlay(monitor, boxes)
        window.show()
        window.place()
        windows.append(window)
    if not windows:
        return 0
    animation = QTimer()
    animation.timeout.connect(lambda: [window.tick() for window in windows])
    animation.start(50)
    escape = QTimer()
    escape.timeout.connect(lambda: app.quit() if ctypes.windll.user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000 else None)
    escape.start(100)
    QTimer.singleShot(round(float(spec.get("seconds") or DEFAULT_SECONDS) * 1000), app.quit)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
