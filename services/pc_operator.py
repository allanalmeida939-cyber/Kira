"""Controle de programas do Windows sem atrapalhar o Chefe.

Ordem de preferência, sempre:
  1. por dentro, sem mouse e sem teclado: botões e campos pela acessibilidade do Windows (UI Automation: o botão é
     "apertado" sem o ponteiro andar), teclas de mídia (não roubam o foco), arquivos escritos direto;
  2. mouse e teclado de verdade só quando não há outro jeito E o Chefe liberou ("tô longe do PC, pode mexer",
     "pode usar mouse e teclado"). A liberação vale por um tempo e acaba com "voltei" / "não mexe no mouse".
Sem liberação, a ação que precisa de mouse/teclado volta como 'precisa_liberar' e a Kira pergunta antes.
"""

from __future__ import annotations

import ctypes
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("kira.app.pc_operator")

DEFAULT_FREE_MINUTES = 30
MAX_FREE_MINUTES = 240
MEDIA_KEYS = {"play": 0xB3, "pausa": 0xB3, "tocar": 0xB3, "proxima": 0xB0, "pular": 0xB0, "anterior": 0xB1, "voltar": 0xB1, "parar": 0xB2,
              "volume_mais": 0xAF, "volume_menos": 0xAE, "mudo": 0xAD}
# Calculadora do Windows: ids de automação (iguais em qualquer idioma)
CALC_IDS = {"0": "num0Button", "1": "num1Button", "2": "num2Button", "3": "num3Button", "4": "num4Button", "5": "num5Button", "6": "num6Button",
            "7": "num7Button", "8": "num8Button", "9": "num9Button", ",": "decimalSeparatorButton", ".": "decimalSeparatorButton",
            "+": "plusButton", "-": "minusButton", "*": "multiplyButton", "/": "divideButton", "=": "equalButton"}


@dataclass
class Permission:
    until: float = 0.0
    reason: str = ""

    @property
    def active(self) -> bool:
        return time.time() < self.until

    def as_dict(self) -> dict[str, Any]:
        left = max(0, int((self.until - time.time()) / 60)) if self.active else 0
        return {"mouse_teclado_liberado": self.active, "minutos_restantes": left, "motivo": self.reason}


class PcOperator:
    def __init__(self) -> None:
        self.permission = Permission()
        self._lock = threading.Lock()

    # ---------- liberação ----------
    def allow(self, minutes: float = DEFAULT_FREE_MINUTES, reason: str = "") -> dict[str, Any]:
        minutes = max(1.0, min(MAX_FREE_MINUTES, float(minutes or DEFAULT_FREE_MINUTES)))
        self.permission = Permission(time.time() + minutes * 60, reason[:120])
        logger.info("PC_INPUT_ALLOWED minutes=%s reason=%s", minutes, reason[:80])
        return self.permission.as_dict()

    def revoke(self) -> dict[str, Any]:
        self.permission = Permission()
        logger.info("PC_INPUT_REVOKED")
        return self.permission.as_dict()

    def _need_input(self, what: str) -> dict[str, Any] | None:
        if self.permission.active:
            return None
        return {"ok": False, "precisa_liberar": True,
                "message": (f"Para {what} eu teria que mexer no mouse/teclado de verdade. Pergunte ao Chefe se pode (ou se ele "
                            "está longe do PC); se ele liberar, chame acao 'liberar' e tente de novo.")}

    # ---------- sem mouse/teclado ----------
    @staticmethod
    def media(key: str) -> dict[str, Any]:
        code = MEDIA_KEYS.get(key)
        if code is None:
            return {"ok": False, "message": f"Tecla de mídia desconhecida: use {', '.join(MEDIA_KEYS)}."}
        user32 = ctypes.windll.user32
        user32.keybd_event(code, 0, 0, 0)
        user32.keybd_event(code, 0, 2, 0)
        return {"ok": True, "feito": key, "sem_mouse_teclado": True}

    @staticmethod
    def _desktop():
        from pywinauto import Desktop

        return Desktop(backend="uia")

    def _window(self, title: str):
        wanted = str(title or "").strip().lower()
        candidates = [w for w in self._desktop().windows() if w.is_visible() and w.window_text().strip()]
        exact = [w for w in candidates if w.window_text().lower() == wanted]
        part = [w for w in candidates if wanted and wanted in w.window_text().lower()]
        found = (exact or part or [None])[0]
        if found is None:
            raise LookupError(f"Não achei uma janela com '{title}'. Abertas: " + ", ".join(sorted({w.window_text()[:40] for w in candidates})[:12]))
        return found

    def windows(self) -> dict[str, Any]:
        names = [w.window_text()[:60] for w in self._desktop().windows() if w.is_visible() and w.window_text().strip()]
        return {"ok": True, "janelas": names[:25]}

    def controls(self, title: str) -> dict[str, Any]:
        """Botões, campos e menus da janela (para escolher o que apertar)."""
        window = self._window(title)
        items = []
        for ctrl in window.descendants()[:600]:
            try:
                kind = ctrl.element_info.control_type
                name = ctrl.window_text().strip()
            except Exception:  # noqa: BLE001 - controle sumiu no meio da leitura
                continue
            if kind in {"Button", "Edit", "MenuItem", "ListItem", "TabItem", "CheckBox", "Hyperlink", "ComboBox", "Document"} and (name or kind in {"Edit", "Document"}):
                items.append(f"{kind}: {name[:50]}")
        return {"ok": True, "janela": window.window_text()[:60], "controles": list(dict.fromkeys(items))[:120]}

    def press(self, title: str, control: str) -> dict[str, Any]:
        """Aperta um botão/item pelo nome, por dentro (o ponteiro não se mexe). Sem jeito por dentro: precisa liberar."""
        window = self._window(title)
        wanted = str(control or "").strip().lower()
        matches = []
        for ctrl in window.descendants():
            try:
                name = ctrl.window_text().strip().lower()
                auto_id = (ctrl.element_info.automation_id or "").lower()
            except Exception:  # noqa: BLE001
                continue
            if wanted and (name == wanted or auto_id == wanted):
                matches.insert(0, ctrl)
            elif wanted and wanted in name:
                matches.append(ctrl)
        if not matches:
            return {"ok": False, "message": f"Não achei '{control}' nessa janela. Use acao 'controles' para ver os nomes."}
        target = matches[0]
        for how in ("invoke", "select", "toggle", "expand"):
            try:
                getattr(target, how)()
                return {"ok": True, "apertou": target.window_text()[:50], "sem_mouse_teclado": True}
            except Exception:  # noqa: BLE001 - esse controle não aceita esse jeito: tenta o próximo
                continue
        blocked = self._need_input(f"apertar '{control}'")
        if blocked:
            return blocked
        target.click_input()
        return {"ok": True, "apertou": target.window_text()[:50], "usou_mouse": True}

    def write(self, title: str, text: str, control: str = "") -> dict[str, Any]:
        """Escreve num campo por dentro (sem digitar). Sem campo que aceite: precisa liberar o teclado."""
        window = self._window(title)
        fields = []
        for ctrl in window.descendants():
            try:
                kind = ctrl.element_info.control_type
                name = ctrl.window_text().strip().lower()
            except Exception:  # noqa: BLE001
                continue
            if kind in {"Edit", "Document"} and (not control or control.lower() in name):
                fields.append(ctrl)
        for field in fields:
            try:
                field.iface_value.SetValue(str(text))
                return {"ok": True, "escreveu_em": window.window_text()[:50], "sem_mouse_teclado": True}
            except Exception:  # noqa: BLE001 - campo sem 'Value' (ex.: documento rico): tenta o próximo
                continue
        blocked = self._need_input("escrever nessa janela")
        if blocked:
            return blocked
        window.set_focus()
        window.type_keys(_escape_keys(text), with_spaces=True, with_newlines=True, pause=0.01)
        return {"ok": True, "escreveu_em": window.window_text()[:50], "usou_teclado": True}

    def calculate(self, expression: str) -> dict[str, Any]:
        """Abre a Calculadora e faz a conta apertando os botões por dentro; devolve o resultado."""
        expr = re.sub(r"\s+", "", str(expression or "")).replace("x", "*").replace("×", "*").replace("÷", "/")
        if not re.fullmatch(r"[0-9.,+\-*/]+", expr):
            return {"ok": False, "message": "Diga a conta só com números e + - x /."}
        subprocess.Popen(["calc.exe"])
        window = None
        for _ in range(30):
            time.sleep(0.3)
            try:
                window = self._window("Calculadora")
                break
            except LookupError:
                try:
                    window = self._window("Calculator")
                    break
                except LookupError:
                    continue
        if window is None:
            return {"ok": False, "message": "A Calculadora não abriu."}
        by_id = {}
        for ctrl in window.descendants():
            try:
                by_id.setdefault(ctrl.element_info.automation_id, ctrl)
            except Exception:  # noqa: BLE001
                continue
        if "clearButton" in by_id:
            by_id["clearButton"].invoke()
        for char in expr + "=":
            button = by_id.get(CALC_IDS[char])
            if button is None:
                return {"ok": False, "message": "A Calculadora não está no modo Padrão."}
            button.invoke()
        result = by_id.get("CalculatorResults")
        shown = result.window_text() if result is not None else ""
        value = re.sub(r"[^0-9,.\-]", "", shown.split(" ")[-1] if shown else "")
        return {"ok": True, "conta": expression, "resultado_na_tela": value or shown, "sem_mouse_teclado": True}

    # ---------- mouse e teclado de verdade (só liberado) ----------
    def keys(self, title: str, keys: str) -> dict[str, Any]:
        blocked = self._need_input(f"apertar {keys}")
        if blocked:
            return blocked
        window = self._window(title) if title else None
        if window is not None:
            window.set_focus()
        from pywinauto.keyboard import send_keys

        send_keys(_hotkey(keys))
        return {"ok": True, "teclas": keys, "usou_teclado": True}

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> dict[str, Any]:
        blocked = self._need_input("clicar na tela")
        if blocked:
            return blocked
        from pywinauto import mouse

        (mouse.double_click if double else mouse.click)(button=button, coords=(int(x), int(y)))
        return {"ok": True, "clicou": [int(x), int(y)], "usou_mouse": True}


def _escape_keys(text: str) -> str:
    return re.sub(r"([{}()+^%~\[\]])", r"{\1}", str(text)).replace("\n", "{ENTER}")


def _hotkey(keys: str) -> str:
    """'ctrl+s' -> '^s', 'alt+f4' -> '%{F4}', 'enter' -> '{ENTER}'."""
    parts = [p.strip().lower() for p in re.split(r"\s*\+\s*", str(keys or "")) if p.strip()]
    mods = {"ctrl": "^", "control": "^", "alt": "%", "shift": "+", "win": "{VK_LWIN down}"}
    out = ""
    for part in parts[:-1]:
        out += mods.get(part, "")
    last = parts[-1] if parts else ""
    out += last if len(last) == 1 else "{" + last.upper() + "}"
    return out + ("{VK_LWIN up}" if "win" in parts[:-1] else "")


__all__ = ["PcOperator", "Permission", "MEDIA_KEYS"]
