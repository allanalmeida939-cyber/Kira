"""Atalhos globais da Kira (funcionam com a Kira atrás de outro app): compartilhar tela e microfone.

Microfone em dois modos, como no Discord: "toggle" (aperta liga, aperta de novo desliga) e "hold" (segurar para falar:
liga ao apertar e desliga ao soltar). RegisterHotKey reserva a combinação no Windows (o app em foco não a recebe);
se outro programa já usa a combinação, o status volta "em_uso" para a aba Kira mostrar.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from collections.abc import Callable
from ctypes import wintypes

logger = logging.getLogger("kira.app")

MODIFIERS = {"ctrl": 0x0002, "alt": 0x0001, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000
MODIFIER_NAMES = (("ctrl", "Ctrl"), ("alt", "Alt"), ("shift", "Shift"), ("win", "Win"))
NAMED_KEYS = {
    "space": (0x20, "Space"), "insert": (0x2D, "Insert"), "home": (0x24, "Home"), "end": (0x23, "End"),
    "pageup": (0x21, "PageUp"), "pagedown": (0x22, "PageDown"), "pause": (0x13, "Pause"), "scrolllock": (0x91, "ScrollLock"),
    "backquote": (0xC0, "Backquote"),
}
WM_HOTKEY, WM_APP_RELOAD, WM_QUIT = 0x0312, 0x8001, 0x0012
SCREEN_ID, MIC_ID = 1, 2
HOLD_POLL_SECONDS = 0.03


def parse_combo(text: object) -> tuple[int, int, str] | None:
    """"Ctrl+Shift+S" -> (modificadores, tecla virtual, forma canônica). Exige um modificador, exceto F1-F24."""
    parts = [part.strip().lower() for part in str(text or "").split("+") if part.strip()]
    if not parts or len(parts) > 5:
        return None
    *mods, key = parts
    if len(set(mods)) != len(mods) or any(mod not in MODIFIERS for mod in mods):
        return None
    if len(key) == 1 and key.isascii() and key.isalnum():
        vk, label = ord(key.upper()), key.upper()
    elif key[:1] == "f" and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk, label = 0x6F + int(key[1:]), key.upper()
    elif key in NAMED_KEYS:
        vk, label = NAMED_KEYS[key]
    else:
        return None
    if not mods and not label.startswith("F"):
        return None  # tecla sozinha atrapalharia a digitação em qualquer programa
    flags = sum(MODIFIERS[mod] for mod in mods)
    canonical = "+".join([name for mod, name in MODIFIER_NAMES if mod in mods] + [label])
    return flags, vk, canonical


def clean_shortcuts(patch: object, current: dict) -> dict:
    """Valida o pedaço "shortcuts" vindo da aba Kira ("" desliga o atalho)."""
    result = dict(current)
    if not isinstance(patch, dict):
        return result
    for name in ("screen_share", "mic"):
        value = patch.get(name)
        if value == "":
            result[name] = ""
        elif isinstance(value, str) and (parsed := parse_combo(value)):
            result[name] = parsed[2]
    if patch.get("mic_mode") in {"toggle", "hold"}:
        result["mic_mode"] = patch["mic_mode"]
    return result


_active: "GlobalHotkeys | None" = None


def reload_active() -> dict[str, str]:
    """Chamado ao salvar a aba Kira: registra de novo e devolve o status de cada atalho."""
    return _active.reload() if _active is not None else {}


def active_status() -> dict[str, str]:
    return _active.status() if _active is not None else {}


class GlobalHotkeys:
    def __init__(self, run_js: Callable[[str], object], load_shortcuts: Callable[[], dict]) -> None:
        self._run_js = run_js
        self._load = load_shortcuts
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._status: dict[str, str] = {}
        self._registered = threading.Event()
        self._mic_vk = 0
        self._mic_mode = "toggle"

    def status(self) -> dict[str, str]:
        return dict(self._status)

    def start(self) -> None:
        global _active
        _active = self
        if self._thread is None and hasattr(ctypes, "windll"):
            self._thread = threading.Thread(target=self._loop, name="kira-hotkeys", daemon=True)
            self._thread.start()
            self._registered.wait(2.0)

    def reload(self) -> dict[str, str]:
        """Relê a configuração e registra de novo (na thread dona dos atalhos, como o Windows exige)."""
        if not self._thread_id:
            return self.status()
        self._registered.clear()
        ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_APP_RELOAD, 0, 0)
        self._registered.wait(2.0)
        return self.status()

    def stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    # ---------- thread dos atalhos ----------
    def _register(self) -> None:
        user32 = ctypes.windll.user32
        for hotkey_id in (SCREEN_ID, MIC_ID):
            user32.UnregisterHotKey(None, hotkey_id)
        shortcuts = self._load() or {}
        self._mic_mode = "hold" if shortcuts.get("mic_mode") == "hold" else "toggle"
        status = {}
        for hotkey_id, name in ((SCREEN_ID, "screen_share"), (MIC_ID, "mic")):
            parsed = parse_combo(shortcuts.get(name))
            if not shortcuts.get(name):
                status[name] = "desligado"
                continue
            if parsed is None:
                status[name] = "invalido"
                continue
            flags, vk, canonical = parsed
            if user32.RegisterHotKey(None, hotkey_id, flags | MOD_NOREPEAT, vk):
                status[name] = "ok"
                if hotkey_id == MIC_ID:
                    self._mic_vk = vk
            else:
                status[name] = "em_uso"
            logger.info("HOTKEY_REGISTER name=%s combo=%s status=%s", name, canonical, status[name])
        self._status = status
        self._registered.set()

    def _js(self, code: str) -> None:
        try:
            self._run_js(code)
        except Exception as exc:  # a interface pode estar recarregando: o atalho não derruba nada
            logger.info("HOTKEY_JS_FAILED reason=%s", type(exc).__name__)

    def _on_hotkey(self, hotkey_id: int) -> None:
        if hotkey_id == SCREEN_ID:
            logger.info("HOTKEY_FIRED name=screen_share")
            self._js("window.KiraUiControl && window.KiraUiControl.setScreenEnabled(!window.KiraUiControl.getState().vision_mode_enabled)")
            return
        if hotkey_id != MIC_ID:
            return
        if self._mic_mode == "toggle":
            logger.info("HOTKEY_FIRED name=mic mode=toggle")
            self._js("window.KiraUiControl && window.KiraUiControl.micShortcut(!window.KiraUiControl.getState().mic_enabled)")
            return
        # Segurar para falar: liga agora e desliga quando a tecla principal for solta.
        logger.info("HOTKEY_FIRED name=mic mode=hold")
        self._js("window.KiraUiControl && window.KiraUiControl.micShortcut(true)")
        user32 = ctypes.windll.user32
        while user32.GetAsyncKeyState(self._mic_vk) & 0x8000:
            time.sleep(HOLD_POLL_SECONDS)
        self._js("window.KiraUiControl && window.KiraUiControl.micShortcut(false)")

    def _loop(self) -> None:
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        message = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)  # cria a fila de mensagens da thread
        self._register()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message == WM_HOTKEY:
                self._on_hotkey(int(message.wParam))
            elif message.message == WM_APP_RELOAD:
                self._register()
        for hotkey_id in (SCREEN_ID, MIC_ID):
            user32.UnregisterHotKey(None, hotkey_id)


__all__ = ["GlobalHotkeys", "active_status", "clean_shortcuts", "parse_combo", "reload_active"]
