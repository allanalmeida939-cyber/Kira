"""Seletor do compartilhamento de tela da Kira (QtWebEngine).

Sem tratar `desktopMediaRequested`, o QtWebEngine compartilhava a própria página e a Kira só via a interface dela.
Aqui o usuário escolhe (com miniaturas, como no Chrome): todos os monitores, um monitor ou uma janela. Em "todos", a
página pede uma captura por monitor (as extras são respondidas sozinhas) e junta tudo num vídeo só para o LiveKit.
Enquanto compartilha, uma barra embaixo da tela avisa e tem o botão "Parar compartilhamento", como a do Chrome.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger("kira.app")

_lock = threading.Lock()
_choice: dict[str, object] = {"kind": "none"}
_auto_screens: list[int] = []

STYLE = """
QDialog { background: #120a26; color: #f3e8ff; }
QLabel#title { font-size: 16px; font-weight: 700; color: #f0abfc; }
QLabel { color: #d8c7f5; }
QListWidget { background: #1c1238; border: 1px solid #6d28d9; border-radius: 10px; padding: 6px; font-size: 13px; color: #f3e8ff; }
QListWidget::item { padding: 8px; border-radius: 6px; }
QListWidget::item:selected { background: #7c3aed; color: white; }
QPushButton { background: #2a1a4f; color: #f3e8ff; border: 1px solid #7c3aed; border-radius: 8px; padding: 7px 16px; }
QPushButton#share { background: #7c3aed; font-weight: 700; }
"""

BAR_STYLE = """
QWidget#bar { background: rgba(18, 10, 38, 235); border: 1px solid #7c3aed; border-radius: 14px; }
QLabel { color: #f3e8ff; font-size: 13px; }
QLabel#dot { color: #f43f5e; font-size: 16px; }
QPushButton { background: #2a1a4f; color: #f3e8ff; border: 1px solid #7c3aed; border-radius: 9px; padding: 6px 14px; }
QPushButton#stop { background: #7c3aed; font-weight: 700; }
"""
WDA_EXCLUDEFROMCAPTURE = 0x00000011
_bar = None
_view = None


def current_choice() -> dict[str, object]:
    with _lock:
        return dict(_choice)


def reset_choice() -> None:
    with _lock:
        _choice.clear()
        _choice["kind"] = "none"
        _auto_screens.clear()


def _set_choice(kind: str, **fields: object) -> None:
    with _lock:
        _choice.clear()
        _choice.update({"kind": kind, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **fields})
    logger.info("SCREEN_SHARE_CHOICE kind=%s %s", kind, " ".join(f"{k}={v}" for k, v in fields.items() if k != "window"))


def _model_names(model) -> list[str]:
    from PySide6.QtCore import Qt

    return [str(model.data(model.index(row, 0), Qt.ItemDataRole.DisplayRole) or "") for row in range(model.rowCount())]


def _ordered_screens() -> list:
    """Telas do Qt na ordem do Chromium: a principal primeiro."""
    from PySide6.QtGui import QGuiApplication

    return sorted(QGuiApplication.screens(), key=lambda screen: screen is not QGuiApplication.primaryScreen())


def _screen_labels(count: int) -> list[str]:
    """'Monitor 1 (principal) · 1366×768'."""
    screens = _ordered_screens()
    labels = []
    for index in range(count):
        label = f"Monitor {index + 1}"
        if index < len(screens) and len(screens) == count:
            size = screens[index].size() * screens[index].devicePixelRatio()
            label += f"{' (principal)' if index == 0 else ''} · {round(size.width())}×{round(size.height())}"
        labels.append(label)
    return labels


def _thumbnail(screen):
    """Miniatura do monitor para o seletor (a mesma ideia das prévias do Chrome)."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon

    try:
        return QIcon(screen.grabWindow(0).scaled(176, 99, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
    except Exception:
        return QIcon()


def _default_row(screen_count: int, parent) -> int:
    """Com dois ou mais monitores, já marca um onde a Kira não está (é o que o usuário costuma querer mostrar)."""
    screens = _ordered_screens()
    handle = parent.windowHandle() if parent is not None else None
    kira_screen = handle.screen() if handle is not None else None
    if screen_count > 1 and len(screens) == screen_count and kira_screen in screens:
        other = next(index for index, screen in enumerate(screens) if screen is not kira_screen)
        return 1 + other  # a linha 0 é "Todos os monitores"
    return 0


def ask_choice(screen_count: int, windows: list[str], parent=None) -> tuple[str, int] | None:
    """Janela de escolha, sempre por cima (o atalho pode ser usado com a Kira atrás de outro app)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout

    dialog = QDialog(parent)
    dialog.setWindowTitle("O que a Kira pode ver?")
    dialog.setWindowFlags(dialog.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
    dialog.setStyleSheet(STYLE)
    dialog.resize(520, 520)
    layout = QVBoxLayout(dialog)
    title = QLabel("O que a Kira pode ver?")
    title.setObjectName("title")
    layout.addWidget(title)
    layout.addWidget(QLabel("A Kira enxerga só o que você escolher, até desligar a visão."))
    listing = QListWidget()
    from PySide6.QtCore import QSize

    listing.setIconSize(QSize(176, 99))
    layout.addWidget(listing)

    def add(text: str, data: tuple[str, int] | None, icon=None) -> None:
        item = QListWidgetItem(text)
        if icon is not None:
            item.setIcon(icon)
        if data is None:
            item.setFlags(Qt.ItemFlag.NoItemFlags)
        else:
            item.setData(Qt.ItemDataRole.UserRole, data)
        listing.addItem(item)

    if screen_count > 1:
        add(f"🖥  Todos os monitores ({screen_count})", ("all", 0))
    screens = _ordered_screens()
    for index, label in enumerate(_screen_labels(screen_count)):
        add(label, ("screen", index), _thumbnail(screens[index]) if index < len(screens) else None)
    shown = [(index, name) for index, name in enumerate(windows) if name.strip()]
    if shown:
        add("Janelas", None)
        for index, name in shown:
            add(f"🗔  {name[:70]}", ("window", index))
    listing.setCurrentRow(_default_row(screen_count, parent) if screen_count > 1 else 0)
    buttons = QHBoxLayout()
    cancel = QPushButton("Cancelar")
    share = QPushButton("Compartilhar")
    share.setObjectName("share")
    share.setDefault(True)  # Enter compartilha (sem isso o Enter caía no "Cancelar", o primeiro botão)
    cancel.setAutoDefault(False)
    buttons.addStretch(1)
    buttons.addWidget(cancel)
    buttons.addWidget(share)
    layout.addLayout(buttons)
    cancel.clicked.connect(dialog.reject)
    share.clicked.connect(dialog.accept)
    listing.itemDoubleClicked.connect(lambda _item: dialog.accept())
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    if dialog.exec() != QDialog.DialogCode.Accepted or listing.currentItem() is None:
        return None
    return listing.currentItem().data(Qt.ItemDataRole.UserRole)


def handle_request(request, *, ask=ask_choice, parent=None) -> None:
    """Responde um getDisplayMedia: capturas extras do "todos" sem perguntar; senão pergunta ao usuário."""
    screens, windows = request.screensModel(), request.windowsModel()
    with _lock:
        queued = _auto_screens.pop(0) if _auto_screens else None
    if queued is not None and queued < screens.rowCount():
        request.selectScreen(screens.index(queued, 0))
        return
    window_names = _model_names(windows)
    choice = ask(screens.rowCount(), window_names, parent)
    if choice is None:
        logger.info("SCREEN_SHARE_CHOICE kind=cancelled")
        request.cancel()
        return
    kind, index = choice
    if kind == "window":
        _set_choice("window", window=window_names[index])
        request.selectWindow(windows.index(index, 0))
        return
    if kind == "all":
        count = screens.rowCount()
        _set_choice("all", screens=count)
        with _lock:
            _auto_screens[:] = list(range(1, count))
        request.selectScreen(screens.index(0, 0))
        return
    _set_choice("screen", screen=index)
    request.selectScreen(screens.index(index, 0))


def _choice_label(choice: dict) -> str:
    kind = choice.get("kind")
    if kind == "all":
        return "todos os monitores"
    if kind == "screen":
        return f"monitor {int(choice.get('screen') or 0) + 1}"
    if kind == "window":
        return f"janela \u201c{str(choice.get('window') or '')[:40]}\u201d"
    return "sua tela"


def _on_qt_thread(fn) -> None:
    from PySide6.QtCore import QCoreApplication, QTimer

    app = QCoreApplication.instance()
    if app is not None:
        QTimer.singleShot(0, app, fn)


def _build_bar():
    """Barra "A Kira está vendo…" embaixo da tela: sempre por cima, sem roubar o foco e fora da própria captura."""
    import ctypes

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

    bar = QWidget(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
    bar.setObjectName("bar")
    bar.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
    bar.setStyleSheet(BAR_STYLE)
    row = QHBoxLayout(bar)
    row.setContentsMargins(14, 8, 10, 8)
    dot = QLabel("\u25cf")
    dot.setObjectName("dot")
    text = QLabel()
    stop = QPushButton("Parar compartilhamento")
    stop.setObjectName("stop")
    hide = QPushButton("Ocultar")
    for widget in (dot, text):
        row.addWidget(widget)
    row.addStretch(1)
    row.addWidget(stop)
    row.addWidget(hide)
    stop.clicked.connect(lambda: _view.page().runJavaScript("stopLiveKitScreenShareVision()") if _view is not None else None)
    hide.clicked.connect(bar.hide)
    bar.text_label = text

    def place() -> None:
        area = QGuiApplication.primaryScreen().availableGeometry()
        bar.adjustSize()
        width = max(bar.width(), 460)
        bar.setGeometry(area.center().x() - width // 2, area.bottom() - bar.height() - 14, width, bar.height())
        try:
            ctypes.windll.user32.SetWindowDisplayAffinity(int(bar.winId()), WDA_EXCLUDEFROMCAPTURE)
        except (AttributeError, OSError):
            pass

    bar.place = place
    return bar


def show_share_bar() -> None:
    """Chamar quando o compartilhamento começa de fato (qualquer thread)."""
    def show() -> None:
        global _bar
        if _bar is None:
            _bar = _build_bar()
        _bar.text_label.setText(f"A Kira está vendo {_choice_label(current_choice())}")
        _bar.place()
        _bar.show()

    _on_qt_thread(show)


def hide_share_bar() -> None:
    _on_qt_thread(lambda: _bar.hide() if _bar is not None else None)


def _on_permission(permission) -> None:
    """Microfone, câmera e tela liberados (a tela ainda passa pelo seletor); o resto fica com o pywebview, que nega."""
    from PySide6.QtWebEngineCore import QWebEnginePermission

    kinds = QWebEnginePermission.PermissionType
    if permission.permissionType() in {
        kinds.MediaAudioCapture, kinds.MediaVideoCapture, kinds.MediaAudioVideoCapture,
        kinds.DesktopVideoCapture, kinds.DesktopAudioVideoCapture,
    }:
        permission.grant()


def install(view) -> None:
    """Liga o seletor na página da janela principal (chamar na thread do Qt)."""
    from PySide6.QtWebEngineCore import QWebEngineSettings

    global _view
    _view = view
    page = view.page()
    # Sem isso o getDisplayMedia falha com "AbortError: Invalid state" (antes a flag do pywebview mascarava).
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.ScreenCaptureEnabled, True)
    def on_request(request) -> None:
        logger.info("SCREEN_SHARE_PICKER_REQUEST screens=%d windows=%d", request.screensModel().rowCount(), request.windowsModel().rowCount())
        try:
            handle_request(request, parent=view.window())
        except Exception:
            # Sem resposta o QtWebEngine compartilharia a tela padrão sem o usuário escolher: cancela.
            logger.exception("SCREEN_SHARE_PICKER_FAILED")
            request.cancel()

    page.desktopMediaRequested.connect(on_request)
    # O tratador antigo do pywebview (featurePermissionRequested) quebra nesta versão do PySide6 (TypeError a cada
    # pedido); microfone, câmera e tela passam pelo permissionRequested abaixo.
    try:
        page.featurePermissionRequested.disconnect()
    except (RuntimeError, TypeError):
        pass
    page.permissionRequested.connect(_on_permission)
    logger.info("SCREEN_SHARE_PICKER_INSTALLED")


__all__ = ["ask_choice", "current_choice", "handle_request", "hide_share_bar", "install", "reset_choice", "show_share_bar"]
