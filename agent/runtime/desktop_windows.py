"""Janelas e aplicativos do Windows: listar, focar, minimizar, maximizar, restaurar, fechar e encerrar.

"Fechar" pede ao aplicativo para fechar a janela (WM_CLOSE, o mesmo que clicar no X); "encerrar" termina o processo.
Processos do sistema e a própria Kira nunca são encerrados por aqui.
"""

from __future__ import annotations

import ctypes
import json
import logging
logger = logging.getLogger(__name__)
import subprocess
import unicodedata
from ctypes import wintypes
from dataclasses import dataclass

import psutil

PROTECTED_PROCESSES = frozenset({
    "explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe", "services.exe", "smss.exe", "svchost.exe",
    "dwm.exe", "system", "registry", "wininit.exe", "fontdrvhost.exe", "sihost.exe", "ctfmon.exe",
    "searchhost.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe", "textinputhost.exe",
    "runtimebroker.exe", "applicationframehost.exe", "taskmgr.exe", "audiodg.exe", "conhost.exe",
})
# Janelas destes processos são do usuário (Calculadora/Configurações vivem no ApplicationFrameHost, pastas no
# explorer): podem ser listadas e minimizadas/fechadas, só o processo continua protegido.
USER_WINDOW_HOSTS = frozenset({"applicationframehost.exe", "explorer.exe", "taskmgr.exe"})
KIRA_MARKERS = ("kira v8",)
SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE = 6, 3, 9
WM_CLOSE = 0x0010
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
GW_OWNER = 4
DWMWA_CLOAKED = 14
CREATE_NO_WINDOW = 0x08000000

user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None
dwmapi = ctypes.windll.dwmapi if hasattr(ctypes, "windll") else None


def fold(text: object) -> str:
    return unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower().strip()


@dataclass
class DesktopWindow:
    hwnd: int
    title: str
    pid: int
    process: str
    minimized: bool

    @property
    def app(self) -> str:
        return self.process.removesuffix(".exe")


def _is_kira(pid: int) -> bool:
    """Processos da própria Kira (Python e QtWebEngine rodando da pasta do projeto)."""
    try:
        proc = psutil.Process(pid)
        if not proc.name().lower().startswith(("python", "qtwebengine", "pythonw")):
            return False
        command = " ".join(proc.cmdline()).lower()
    except (psutil.Error, OSError):
        return False
    return any(marker in command for marker in KIRA_MARKERS)


def list_windows() -> list[DesktopWindow]:
    """Janelas que aparecem na barra de tarefas (visíveis, com título, sem dono, não-ferramenta)."""
    if user32 is None:
        return []
    found: list[DesktopWindow] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        cloaked = wintypes.DWORD()
        if dwmapi is not None and not dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) and cloaked.value:
            return True  # app UWP suspenso: "visível" para o Windows, mas fora da tela
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            process = psutil.Process(pid.value).name()
        except (psutil.Error, OSError):
            process = "?"
        title = buffer.value.strip()
        shell = process.lower() in PROTECTED_PROCESSES and process.lower() not in USER_WINDOW_HOSTS
        if title and title not in {"Program Manager"} and not shell:
            found.append(DesktopWindow(int(hwnd), title, int(pid.value), process, bool(user32.IsIconic(hwnd))))
        return True

    user32.EnumWindows(visit, 0)
    return found


def background_apps() -> list[str]:
    """Aplicativos do usuário rodando sem janela na barra (em geral os ícones ocultos da bandeja)."""
    with_window = {w.pid for w in list_windows()}
    names: dict[str, None] = {}
    for proc in psutil.process_iter(["pid", "name", "exe", "username"]):
        info = proc.info
        name = str(info.get("name") or "")
        exe = fold(info.get("exe") or "")
        if not name or name.lower() in PROTECTED_PROCESSES or info.get("pid") in with_window:
            continue
        if not info.get("username") or "\\windows\\" in exe or not exe:
            continue
        if "program files" in exe or "\\appdata\\" in exe:
            names[name.removesuffix(".exe")] = None
    return sorted(names, key=str.lower)[:40]


def find_windows(target: str) -> list[DesktopWindow]:
    wanted = fold(target)
    if not wanted:
        return []
    windows = [w for w in list_windows() if not _is_kira(w.pid)]
    exact = [w for w in windows if fold(w.app) == wanted]
    return exact or [w for w in windows if wanted in fold(w.title) or wanted in fold(w.app)]


def window_action(action: str, target: str) -> str:
    matches = find_windows(target)
    if not matches:
        return f"Não encontrei nenhuma janela aberta de {target}."
    for window in matches:
        if action == "minimizar":
            user32.ShowWindow(window.hwnd, SW_MINIMIZE)
        elif action == "maximizar":
            user32.ShowWindow(window.hwnd, SW_MAXIMIZE)
            user32.SetForegroundWindow(window.hwnd)
        elif action in {"restaurar", "mostrar", "focar"}:
            user32.ShowWindow(window.hwnd, SW_RESTORE)
            user32.SetForegroundWindow(window.hwnd)
        elif action == "fechar":
            user32.PostMessageW(window.hwnd, WM_CLOSE, 0, 0)
        else:
            return "Ação de janela inválida."
        if action != "fechar" and action != "minimizar":
            break  # trazer para frente só a primeira janela que combinar
    verb = {"minimizar": "Minimizei", "maximizar": "Maximizei", "restaurar": "Restaurei", "mostrar": "Mostrei", "focar": "Trouxe para frente", "fechar": "Pedi para fechar"}[action]
    names = ", ".join(dict.fromkeys(w.title[:50] for w in matches[:3]))
    return f"{verb}: {names}."


def end_process(target: str) -> str:
    wanted = fold(target).removesuffix(".exe")
    if not wanted:
        return "Não sei qual programa encerrar."
    victims = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = str(proc.info.get("name") or "")
        if fold(name).removesuffix(".exe") == wanted or (len(wanted) >= 4 and wanted in fold(name)):
            if name.lower() in PROTECTED_PROCESSES or _is_kira(proc.info["pid"]):
                return f"{name} é do sistema ou da própria Kira; não vou encerrar."
            victims.append(proc)
    if not victims:
        return f"Não há processo de {target} rodando."
    for proc in victims:
        try:
            proc.kill()
        except psutil.Error:
            continue
    return f"Encerrei {len(victims)} processo(s) de {victims[0].info['name']}."


def _powershell(script: str, timeout: float = 15.0) -> str:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=timeout, creationflags=CREATE_NO_WINDOW,
    )
    return result.stdout


def installed_apps() -> list[dict[str, str]]:
    """Aplicativos do menu Iniciar (inclui os da Microsoft Store)."""
    raw = _powershell("Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress", timeout=20.0)
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    rows = data if isinstance(data, list) else [data]
    return [{"name": str(r.get("Name") or ""), "id": str(r.get("AppID") or "")} for r in rows if isinstance(r, dict) and r.get("Name")]


def open_installed_app(name: str) -> str:
    wanted = fold(name)
    if len(wanted) < 3:  # nome vazio/curto casava com qualquer app ("" está contido em tudo) e abria o primeiro
        return "Qual aplicativo? Diga o nome."
    apps = installed_apps()
    match = next((a for a in apps if fold(a["name"]) == wanted), None) or next(
        (a for a in apps if fold(a["name"]).startswith(wanted) or f" {wanted}" in f" {fold(a['name'])}"), None)
    logger.info("OPEN_INSTALLED_APP wanted=%r match=%r", wanted, match and match["name"])
    if match is None:
        return f"Não achei {name} entre os aplicativos instalados."
    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{match['id']}"])
    return f"Abrindo {match['name']}."


def set_brightness(percent: int) -> str:
    level = max(0, min(100, int(percent)))
    script = (
        "$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods -ErrorAction SilentlyContinue; "
        f"if ($m) {{ $m | Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{{Timeout=1; Brightness={level}}} | Out-Null; 'ok' }} else {{ 'sem_suporte' }}"
    )
    out = _powershell(script).strip()
    if out.endswith("ok"):
        return f"Brilho em {level}%."
    return "Este monitor não aceita ajuste de brilho pelo Windows (comum em monitor de PC de mesa); só pelos botões do monitor."

