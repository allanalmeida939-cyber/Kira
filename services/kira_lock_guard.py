"""Bloqueio forte da Kira (Fase 1C): teclado e mouse presos enquanto a Kira está trancada (só Windows).

Hook de teclado de baixo nível (WH_KEYBOARD_LL): só passam letras A-Z, Espaço (segurar = microfone), Enter,
Backspace, Shift e Caps Lock. Mouse preso à janela da Kira (ClipCursor). Tudo solta sozinho se:
  - a Kira foi desbloqueada (some runtime/kira_locked);
  - existe o arquivo runtime/destravar_kira (destrava a Kira também e apaga o arquivo);
  - a tela de bloqueio parou de dar sinal por 90 s (interface caiu: sem tela não dá para digitar a senha);
  - passou MAX_HOURS horas (a tela continua trancada, só teclado/mouse voltam ao normal).
Ctrl+Alt+Del é do Windows e nenhum programa consegue bloquear: abre o Gerenciador de Tarefas e encerra a Kira
(fechar o processo solta o hook e o mouse na hora).
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
ESCAPE = ROOT / "runtime" / "destravar_kira"
BEAT_TIMEOUT_S = 90.0
MAX_HOURS = 3.0

VK_BACK, VK_RETURN, VK_SHIFT, VK_CAPITAL, VK_SPACE = 0x08, 0x0D, 0x10, 0x14, 0x20
ALLOWED = {VK_BACK, VK_RETURN, VK_SHIFT, VK_CAPITAL, VK_SPACE, 0xA0, 0xA1}  # A0/A1 = Shift esquerdo/direito


def key_allowed(vk: int) -> bool:
    """Letras para a senha e as poucas teclas de apoio; o resto (Win, Alt, Ctrl, Esc, F1-F12, números, Tab) morre aqui."""
    return 0x41 <= vk <= 0x5A or vk in ALLOWED


def should_release(*, locked: bool, escape: bool, since_beat: float, since_start: float) -> str:
    """Motivo para soltar teclado/mouse ('' = continua preso)."""
    if escape:
        return "arquivo_destravar"
    if not locked:
        return "desbloqueada"
    if since_beat > BEAT_TIMEOUT_S:
        return "tela_sem_sinal"
    if since_start > MAX_HOURS * 3600:
        return "tempo_maximo"
    return ""


class LockGuard:
    def __init__(self, flag: Path, hwnd: Callable[[], int], on_release: Callable[[str], None] = lambda _r: None,
                 escape: Path = ESCAPE, clock: Callable[[], float] = time.monotonic) -> None:
        self.flag, self.hwnd, self.on_release, self.escape, self.clock = flag, hwnd, on_release, escape, clock
        self.active = False
        self._tid = 0
        self._started = self._beat = 0.0
        self._lock = threading.Lock()

    def beat(self) -> None:
        self._beat = self.clock()

    def engage(self) -> bool:
        if sys.platform != "win32":
            return False
        with self._lock:
            if self.active:
                self.beat()
                return True
            self.active = True
            self._started = self._beat = self.clock()
        threading.Thread(target=self._hook_loop, name="kira-lock-keys", daemon=True).start()
        threading.Thread(target=self._watch, name="kira-lock-watch", daemon=True).start()
        logger.info("KIRA_LOCK_GUARD_ON")
        return True

    def release(self, reason: str = "manual") -> None:
        with self._lock:
            if not self.active:
                return
            self.active = False
        if self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)  # WM_QUIT encerra o hook
        ctypes.windll.user32.ClipCursor(None)
        logger.info("KIRA_LOCK_GUARD_OFF reason=%s", reason)
        try:
            self.on_release(reason)
        except Exception:  # noqa: BLE001
            logger.warning("KIRA_LOCK_GUARD_RELEASE_CALLBACK_FAILED", exc_info=True)

    def _watch(self) -> None:
        from ctypes import wintypes

        rect = wintypes.RECT()
        while self.active:
            now = self.clock()
            escape = self.escape.exists()
            reason = should_release(locked=self.flag.exists(), escape=escape, since_beat=now - self._beat, since_start=now - self._started)
            if reason:
                if escape:
                    self.escape.unlink(missing_ok=True)
                    self.flag.unlink(missing_ok=True)
                self.release(reason)
                return
            try:  # o Windows desfaz o ClipCursor a cada troca de foco: reaplica sempre
                hwnd = int(self.hwnd() or 0)
                if hwnd and ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    ctypes.windll.user32.ClipCursor(ctypes.byref(rect))
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)

    def _hook_loop(self) -> None:
        from ctypes import wintypes

        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        lresult = ctypes.c_ssize_t
        proto = ctypes.WINFUNCTYPE(lresult, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.restype = lresult
        user32.SetWindowsHookExW.argtypes = (ctypes.c_int, proto, wintypes.HINSTANCE, wintypes.DWORD)
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        def proc(code: int, wparam: int, lparam: int) -> int:
            if code == 0 and self.active:
                vk = ctypes.cast(lparam, ctypes.POINTER(wintypes.DWORD))[0]  # KBDLLHOOKSTRUCT.vkCode
                if not key_allowed(int(vk)):
                    return 1
            return user32.CallNextHookEx(None, code, wparam, lparam)

        callback = proto(proc)  # referência viva enquanto o laço roda
        self._tid = kernel32.GetCurrentThreadId()
        hook = user32.SetWindowsHookExW(13, callback, kernel32.GetModuleHandleW(None), 0)  # 13 = WH_KEYBOARD_LL
        if not hook:
            logger.warning("KIRA_LOCK_GUARD_HOOK_FAILED")
            return
        msg = wintypes.MSG()
        while self.active and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        user32.UnhookWindowsHookEx(hook)
        self._tid = 0
