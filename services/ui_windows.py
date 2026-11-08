"""Janelas satélite da Kira: perfis salvos (janelas fixas), janelas temporárias e abertura real no Windows."""

from __future__ import annotations

import asyncio
import copy
import hmac
import json
import logging
import os
import re
import sys
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Body, Header, HTTPException, Request

from services.local_request import is_local_request

logger = logging.getLogger("kira.app.ui_windows")  # filho de kira.app: vai para logs/Backend

MAIN_WINDOW_ID = "w1"
MAIN_TITLE = "Kira"  # título da janela principal (kira_app.py, create_window)
MAX_EXTRA_WINDOWS = 3
MAX_PROFILES = 5
MAX_WIDGETS = 40
WINDOW_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
ITEM_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$", re.IGNORECASE)
WIDGET_KINDS = frozenset(
    {
        "clock", "worldclock", "calendar", "bigdate", "timer", "countdown", "pomodoro",
        "news", "weather", "quotes", "scores", "moon", "dayquote", "history", "games", "freegames", "trending",
        "vitals", "cpucores", "ram", "disk", "network", "gpu", "battery", "uptime", "topapps", "openapps",
        "transcript", "reminders", "music", "apis", "viewer",
        "notes", "todo", "shortcuts", "image", "text",
    }
)
DEFAULT_SIZE = (520, 420)
SCHEMA_VERSION = 2
# Nomes falados dos widgets (a Kira descreve cada janela com eles, em vez de "clock, weather").
WIDGET_NAMES = {
    "clock": "relógio", "worldclock": "relógio mundial", "calendar": "calendário", "bigdate": "data", "timer": "cronômetro",
    "countdown": "contagem regressiva", "pomodoro": "pomodoro", "news": "notícias", "weather": "clima", "quotes": "cotações",
    "scores": "placar", "moon": "lua", "dayquote": "frase do dia", "history": "hoje na história", "games": "ofertas de jogos",
    "freegames": "jogos grátis", "trending": "em alta", "vitals": "sistema vital", "cpucores": "núcleos da CPU", "ram": "memória",
    "disk": "disco", "network": "rede", "gpu": "placa de vídeo", "battery": "bateria", "uptime": "tempo ligado",
    "topapps": "aplicativos que mais usam", "openapps": "aplicativos abertos", "transcript": "transcrição", "reminders": "lembretes",
    "music": "música", "apis": "APIs ativas", "viewer": "imagens", "notes": "notas", "todo": "tarefas", "shortcuts": "atalhos",
    "image": "imagem", "text": "texto",
}
WINDOW_STATES = ("aberta", "minimizada", "maximizada")
# Como o Chefe fala das janelas: "principal", "janela 2", "a segunda", "a janela nova", "a outra", o nome da fixa.
MAIN_WORDS = frozenset({"", "1", "w1", "um", "principal", "janela principal", "janela 1", "janela um", "primeira", "primeira janela",
                        "tela principal", "kira", "janela da kira"})
OTHER_WORDS = frozenset({"outra", "outra janela"})
NEWEST_WORDS = frozenset({"nova", "janela nova", "nova janela", "extra", "janela extra", "ultima", "ultima janela"})
NUMBER_WORDS = {"dois": "2", "segunda": "2", "tres": "3", "terceira": "3", "quatro": "4", "quarta": "4"}
OTHER_WINDOW = "outra"


def window_on_screen(title: str) -> bool | None:
    """A janela aparece agora? Não minimizada e o centro dela sem outra janela por cima. None: não deu para saber."""
    if sys.platform != "win32" or not title:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wintypes.HWND
        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return None
        if user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
            return False
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        found = user32.WindowFromPoint(wintypes.POINT((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2))
        return bool(found) and user32.GetAncestor(found, 2) == hwnd  # 2 = GA_ROOT
    except Exception:
        return None

def floating_area(number: int | None = None) -> tuple[int, int, int, int]:
    """Área útil (x, y, largura, altura) do monitor pedido (1 = principal) ou, sem pedido, do que tem a área de trabalho
    à mostra (menos coberto por programas abertos). Os dois cobertos: o monitor 2 (onde o Chefe mais olha)."""
    try:
        import ctypes
        from ctypes import wintypes

        user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
        monitors: list[list] = []  # [left, top, right, bottom, principal, área coberta]
        MONITORENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        def collect(handle, _hdc, _rect, _data):
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                w = info.rcWork
                monitors.append([w.left, w.top, w.right, w.bottom, bool(info.dwFlags & 1), 0])
            return 1

        user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(collect), 0)
        shell = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

        def cover(hwnd, _):
            cloaked, rect, cls = ctypes.c_int(0), wintypes.RECT(), ctypes.create_unicode_buffer(64)
            dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)  # 14 = escondida pelo Windows (apps em segundo plano)
            user32.GetClassNameW(hwnd, cls, 64)
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd) or cloaked.value or cls.value in shell or not user32.GetWindowTextLengthW(hwnd):
                return 1
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            for m in monitors:
                m[5] += max(0, min(rect.right, m[2]) - max(rect.left, m[0])) * max(0, min(rect.bottom, m[3]) - max(rect.top, m[1]))
            return 1

        user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(cover), 0)
        if monitors:
            monitors.sort(key=lambda m: not m[4])  # principal = monitor 1
            free = [m for m in monitors if m[5] < 0.35 * (m[2] - m[0]) * (m[3] - m[1])]
            if number and 1 <= number <= len(monitors):
                chosen = monitors[number - 1]
            else:
                chosen = min(free, key=lambda m: m[5]) if free else monitors[-1]
            return chosen[0], chosen[1], chosen[2] - chosen[0], chosen[3] - chosen[1]
    except Exception:
        pass
    return 0, 0, 1366, 728


def window_title_at(x: int, y: int) -> str | None:
    """Título da janela (a de cima, raiz) no ponto da tela, em pixels físicos. None fora do Windows ou se falhar."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        found = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        root = user32.GetAncestor(found, 2) if found else None  # 2 = GA_ROOT
        if not root:
            return None
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(root, buffer, 256)
        return buffer.value
    except Exception:
        return None


PUBLISH = Callable[[dict[str, object]], Awaitable[object]]
OPENER = Callable[[str, str, str, dict[str, int]], Any]


def fold(value: object) -> str:
    return unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower().strip()


def clean_name(value: object, fallback: str) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isprintable()).strip()[:40]
    return text or fallback


def _clean_json(value: object, depth: int = 0) -> object:
    """Opções de widget: só JSON simples, limitado em profundidade e tamanho."""
    if depth > 4:
        raise ValueError("opcoes_profundas")
    if isinstance(value, str):
        return value[:2000]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        return [_clean_json(item, depth + 1) for item in value[:60]]
    if isinstance(value, dict):
        return {str(k)[:40]: _clean_json(v, depth + 1) for k, v in list(value.items())[:40]}
    raise ValueError("opcao_invalida")


def validate_widgets(raw: object, *, drop_unknown: bool = False) -> list[dict[str, object]]:
    if not isinstance(raw, list) or len(raw) > MAX_WIDGETS:
        raise ValueError("widgets_invalidos")
    if drop_unknown:
        raw = [item for item in raw if isinstance(item, dict) and item.get("kind") in WIDGET_KINDS]
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or item.get("kind") not in WIDGET_KINDS:
            raise ValueError("widget_desconhecido")
        widget_id = str(item.get("id") or f"{item['kind']}-{uuid.uuid4().hex[:6]}")
        if not ITEM_ID_RE.fullmatch(widget_id) or widget_id in seen:
            raise ValueError("id_de_widget_invalido")
        seen.add(widget_id)
        entry: dict[str, object] = {"id": widget_id, "kind": item["kind"]}
        for key, low, high in (("x", -2000, 8000), ("y", -2000, 8000), ("w", 120, 4000), ("h", 80, 3000)):
            value = item.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                entry[key] = int(min(max(value, low), high))
        entry["style"] = str(item.get("style") or "")[:32]
        if isinstance(item.get("frame"), bool):
            entry["frame"] = item["frame"]
        options = _clean_json(item.get("options") if isinstance(item.get("options"), dict) else {})
        if len(json.dumps(options)) > 8000:
            raise ValueError("opcoes_grandes")
        entry["options"] = options
        result.append(entry)
    return result


def _virtual_screen() -> tuple[int, int, int, int] | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        metrics = ctypes.windll.user32.GetSystemMetrics
        return metrics(76), metrics(77), metrics(78), metrics(79)
    except Exception:
        return None


def position_is_visible(geo: dict[str, int], screen: tuple[int, int, int, int] | None) -> bool:
    """A janela precisa ter ao menos um canto de 80 px dentro da área virtual dos monitores."""
    if screen is None or "x" not in geo or "y" not in geo:
        return True
    left, top, width, height = screen
    return geo["x"] + 80 <= left + width and geo["x"] + geo["width"] - 80 >= left and geo["y"] + 40 <= top + height and geo["y"] >= top - 40


@dataclass
class WindowRecord:
    id: str
    number: int
    name: str
    profile_id: str | None
    widgets: list[dict[str, object]] = field(default_factory=list)
    locked: bool = False
    x: int | None = None
    y: int | None = None
    width: int = DEFAULT_SIZE[0]
    height: int = DEFAULT_SIZE[1]
    state: str = "aberta"
    handle: Any = field(default=None, repr=False)

    @property
    def title(self) -> str:
        return f"Kira — {self.name}"

    def as_dict(self) -> dict[str, object]:
        return {
            "windowId": self.id, "title": self.title, "name": self.name, "profileId": self.profile_id, "temporary": self.profile_id is None,
            "widgets": copy.deepcopy(self.widgets), "locked": self.locked, "x": self.x, "y": self.y, "width": self.width, "height": self.height,
            "state": self.state,
        }


class WindowRegistry:
    def __init__(
        self,
        *,
        opener: OPENER,
        publish: PUBLISH,
        port: int,
        profiles_path: Path | None = None,
        screen_provider: Callable[[], tuple[int, int, int, int] | None] = _virtual_screen,
        on_screen: Callable[[str], bool | None] = window_on_screen,
    ) -> None:
        self._opener = opener
        self._publish = publish
        self._port = port
        self._path = profiles_path
        self._screen = screen_provider
        self._on_screen = on_screen
        self._windows: dict[str, WindowRecord] = {}
        self._profiles: dict[str, dict[str, Any]] = self._load()
        self.ready: Callable[[str], bool] | None = None  # a página da janela já conectou (kira_app liga ao hub)
        self._lock = asyncio.Lock()

    # ---------- perfis (janelas fixas) ----------
    def _load(self) -> dict[str, dict[str, Any]]:
        if self._path is None:
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
            return {}  # layout antigo (relógio/transcrição padrão) é descartado
        profiles: dict[str, dict[str, Any]] = {}
        for pid, item in (data.get("profiles") or {}).items():
            try:
                if not ITEM_ID_RE.fullmatch(pid) or not isinstance(item, dict):
                    continue
                profiles[pid] = {
                    "id": pid, "name": clean_name(item.get("name"), "Janela"), "widgets": validate_widgets(item.get("widgets", []), drop_unknown=True),
                    "locked": bool(item.get("locked")), "openOnStart": bool(item.get("openOnStart")),
                    "geometry": {k: int(v) for k, v in (item.get("geometry") or {}).items() if k in {"x", "y", "width", "height"} and isinstance(v, (int, float))},
                }
            except ValueError:
                continue
        return dict(list(profiles.items())[:MAX_PROFILES])

    def _save(self) -> None:
        if self._path is None:
            return
        temporary: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(self._path.parent), suffix=".tmp") as handle:
                temporary = handle.name
                json.dump({"version": SCHEMA_VERSION, "profiles": self._profiles}, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except Exception:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    def profiles(self) -> list[dict[str, object]]:
        open_by_profile = {r.profile_id: r.id for r in self._windows.values() if r.profile_id}
        return [{**copy.deepcopy(p), "open": p["id"] in open_by_profile, "windowId": open_by_profile.get(p["id"])} for p in self._profiles.values()]

    def create_profile(self, name: object = None) -> dict[str, Any]:
        if len(self._profiles) >= MAX_PROFILES:
            raise ValueError("limite_de_perfis")
        pid = f"p{uuid.uuid4().hex[:8]}"
        taken = {fold(p["name"]) for p in self._profiles.values()}
        number = next(n for n in range(2, 50) if fold(f"Janela {n}") not in taken)
        profile = {"id": pid, "name": clean_name(name, f"Janela {number}"), "widgets": [], "locked": False, "openOnStart": False, "geometry": {}}
        self._profiles[pid] = profile
        self._save()
        return profile

    def update_profile(self, pid: str, *, name: object = None, open_on_start: object = None) -> dict[str, Any]:
        profile = self._profiles.get(pid)
        if profile is None:
            raise KeyError(pid)
        if name is not None:
            profile["name"] = clean_name(name, profile["name"])
            for record in self._windows.values():
                if record.profile_id == pid:
                    record.name = profile["name"]
                    self._retitle(record)
        if isinstance(open_on_start, bool):
            profile["openOnStart"] = open_on_start
        self._save()
        return profile

    def duplicate_profile(self, pid: str) -> dict[str, Any]:
        source = self._profiles.get(pid)
        if source is None:
            raise KeyError(pid)
        if len(self._profiles) >= MAX_PROFILES:
            raise ValueError("limite_de_perfis")
        copy_id = f"p{uuid.uuid4().hex[:8]}"
        self._profiles[copy_id] = {**copy.deepcopy(source), "id": copy_id, "name": clean_name(f"{source['name']} (cópia)", "Cópia"), "openOnStart": False}
        self._save()
        return self._profiles[copy_id]

    async def delete_profile(self, pid: str) -> bool:
        if pid not in self._profiles:
            return False
        for record in list(self._windows.values()):
            if record.profile_id == pid:
                await self.close(record.id)
        del self._profiles[pid]
        self._save()
        return True

    # ---------- consulta ----------
    def list(self) -> list[dict[str, object]]:
        return [r.as_dict() for r in sorted(self._windows.values(), key=lambda r: r.number)]

    def get(self, window_id: str) -> WindowRecord | None:
        return self._windows.get(window_id)

    def resolve(self, ref: object) -> str | None:
        """Aceita 'w2', '2', 'janela 2' ou o nome ('Painel TV') e devolve o windowId aberto."""
        text = fold(ref)
        for record in self._windows.values():
            if text in {record.id, fold(record.name), fold(record.title)}:
                return record.id
        match = re.search(r"(\d+)$", text)
        candidate = f"w{match.group(1)}" if match else text
        return candidate if candidate in self._windows else None

    def window_ref(self, ref: object) -> str | None:
        """Janela falada -> id aberto. "principal"/"1" -> w1; "janela 2", "dois", "a segunda", o nome -> a extra aberta;
        "a janela nova"/"extra" -> a extra mais nova; "a outra" -> OTHER_WINDOW (quem chama decide pelo lado do painel).
        None: essa janela não está aberta."""
        text = " ".join(fold(ref).replace("-", " ").split())
        for prefix in ("pra ", "para ", "pro ", "na ", "no ", "a ", "o "):
            if text.startswith(prefix):
                text = text[len(prefix):]
        if text in MAIN_WORDS:
            return MAIN_WINDOW_ID
        if text in OTHER_WORDS:
            return OTHER_WINDOW
        opened = sorted(self._windows.values(), key=lambda r: r.number)
        if text in NEWEST_WORDS:
            return opened[-1].id if opened else None
        text = " ".join(NUMBER_WORDS.get(word, word) for word in text.split())
        return self.resolve(text)

    def spoken_name(self, window_id: str) -> str:
        if window_id == MAIN_WINDOW_ID:
            return "janela principal"
        record = self._windows.get(window_id)
        if record is None:
            return f"janela {window_id.lstrip('w')}"
        return f"janela {record.number}" if record.name in {"", f"Janela {record.number}"} else f"janela {record.name}"

    def resolve_profile(self, ref: object) -> str | None:
        text = fold(ref)
        for pid, profile in self._profiles.items():
            if text in {pid, fold(profile["name"])}:
                return pid
        return None

    # ---------- estado (minimizada, à mostra, coberta) ----------
    def note_state(self, window_id: str, state: str) -> None:
        """Evento da janela real: minimizada, restaurada ou maximizada."""
        record = self._windows.get(window_id)
        if record is not None and state in WINDOW_STATES:
            record.state = state

    def window_at(self, x: int, y: int, title_at: Callable[[int, int], str | None] = window_title_at) -> str | None:
        """Janela da Kira sob o ponto da tela: "w1" (a principal, título "Kira") ou o id de uma extra. None se for outra coisa."""
        title = title_at(x, y)
        if not title:
            return None
        if title == MAIN_TITLE:
            return MAIN_WINDOW_ID
        return next((record.id for record in self._windows.values() if record.title == title), None)

    def is_shown(self, window_id: str) -> bool:
        """Aberta, não minimizada e (no Windows) sem outra janela cobrindo o centro dela."""
        record = self._windows.get(window_id)
        if record is None or record.state == "minimizada":
            return False
        try:
            seen = self._on_screen(record.title)
        except Exception:
            seen = None
        return True if seen is None else bool(seen)

    def visible_ids(self) -> list[str]:
        """Janelas extras que o usuário consegue ver agora (onde a Kira também pode mostrar conteúdo)."""
        return [r.id for r in sorted(self._windows.values(), key=lambda r: r.number) if self.is_shown(r.id)]

    def describe(self) -> str:
        if not self._windows:
            return "Nenhuma janela extra aberta (só a principal)."
        parts = []
        for record in sorted(self._windows.values(), key=lambda r: r.number):
            if record.state == "minimizada":
                status = "minimizada"
            elif not self.is_shown(record.id):
                status = "aberta, mas coberta por outra janela"
            else:
                status = "maximizada e à mostra" if record.state == "maximizada" else "à mostra"
            widgets = ", ".join(WIDGET_NAMES.get(str(w["kind"]), str(w["kind"])) for w in record.widgets) or "nenhum"
            parts.append(f"{record.name} (janela {record.number}): {status}; widgets: {widgets}")
        count = len(parts)
        return f"{count} janela{'s' if count > 1 else ''} extra{'s' if count > 1 else ''}: " + " | ".join(parts)

    # ---------- abertura ----------
    def _geometry(self, saved: dict[str, int]) -> dict[str, int]:
        geo = {"width": DEFAULT_SIZE[0], "height": DEFAULT_SIZE[1], **{k: int(v) for k, v in saved.items() if k in {"x", "y", "width", "height"}}}
        geo["width"] = min(max(geo["width"], 280), 4000)
        geo["height"] = min(max(geo["height"], 200), 3000)
        if not position_is_visible(geo, self._screen()):
            geo.pop("x", None)
            geo.pop("y", None)
        return geo

    async def _open(self, profile: dict[str, Any] | None, name: str) -> WindowRecord:
        async with self._lock:
            if len(self._windows) >= MAX_EXTRA_WINDOWS:
                raise ValueError("limite_de_janelas")
            number = next(n for n in range(2, MAX_EXTRA_WINDOWS + 3) if f"w{n}" not in self._windows)
            window_id = f"w{number}"
            geo = self._geometry(profile["geometry"] if profile else {})
            record = WindowRecord(
                window_id, number, name, profile["id"] if profile else None,
                copy.deepcopy(profile["widgets"]) if profile else [], bool(profile and profile["locked"]),
                geo.get("x"), geo.get("y"), geo["width"], geo["height"],
            )
            url = f"http://127.0.0.1:{self._port}/?kiraui=1&window={window_id}&role=satellite"
            record.handle = await asyncio.to_thread(self._opener, window_id, record.title, url, geo)
            self._windows[window_id] = record
            # pywebview devolve o handle antes de a janela Qt terminar de
            # aparecer. Restaurar/focar aqui evita que a API já diga
            # "aberta" enquanto o satélite continua atrás da janela principal.
            await asyncio.to_thread(self._focus, record)
            return record

    async def create_floating(self, monitor: int | None = None) -> dict[str, object]:
        """Kira minimizada ou na bandeja: sobreposição transparente do monitor inteiro (só os painéis aparecem, arrumados
        como na tela dela). Reaproveita a que já estiver aberta; monitor pedido diferente = muda ela de monitor."""
        x, y, width, height = floating_area(monitor)
        for record in self._windows.values():  # uma sobreposição por monitor
            if record.name == "Kira flutuante" and (not monitor or (record.x, record.y) == (x, y)):
                return record.as_dict()
        async with self._lock:
            if len(self._windows) >= MAX_EXTRA_WINDOWS:
                raise ValueError("limite_de_janelas")
            number = next(n for n in range(2, MAX_EXTRA_WINDOWS + 3) if f"w{n}" not in self._windows)
            window_id = f"w{number}"
            geo = {"x": x, "y": y, "width": width, "height": height, "on_top": True, "float": True}
            record = WindowRecord(window_id, number, "Kira flutuante", None, [], False, x, y, width, height)
            url = f"http://127.0.0.1:{self._port}/?kiraui=1&window={window_id}&role=satellite&float=1"
            record.handle = await asyncio.to_thread(self._opener, window_id, record.title, url, geo)
            self._windows[window_id] = record
        # a página nova precisa conectar para receber o painel (neste PC leva 2-6 s; 1,5 s fixo perdia o painel)
        for _ in range(40):
            if self.ready is not None and self.ready(window_id):
                await asyncio.sleep(0.4)
                break
            await asyncio.sleep(0.25)
        return record.as_dict()

    async def create_temporary(self) -> dict[str, object]:
        taken = {r.name for r in self._windows.values()}
        number = next(n for n in range(2, 20) if f"Janela {n}" not in taken and fold(f"Janela {n}") not in {fold(p["name"]) for p in self._profiles.values()})
        return (await self._open(None, f"Janela {number}")).as_dict()

    async def open_profile(self, pid: str) -> dict[str, object]:
        profile = self._profiles.get(pid)
        if profile is None:
            raise KeyError(pid)
        for record in self._windows.values():
            if record.profile_id == pid:
                await asyncio.to_thread(self._focus, record)
                return record.as_dict()
        return (await self._open(profile, profile["name"])).as_dict()

    async def open_startup(self) -> int:
        count = 0
        for pid, profile in list(self._profiles.items()):
            if profile.get("openOnStart") and len(self._windows) < MAX_EXTRA_WINDOWS:
                try:
                    await self.open_profile(pid)
                    count += 1
                except Exception:
                    continue
        return count

    async def toggle_fullscreen(self, window_id: str) -> bool:
        """F11 numa janela extra: tela cheia de verdade (cobre a barra de tarefas), como um jogo; F11 de novo volta."""
        record = self._windows.get(window_id)
        if record is None or record.handle is None:
            return False
        await asyncio.to_thread(record.handle.toggle_fullscreen)
        return True

    @staticmethod
    def _focus(record: WindowRecord) -> None:
        try:
            if record.handle is not None:
                record.handle.restore()
                record.handle.show()
                if sys.platform == "win32":
                    import ctypes

                    native = getattr(record.handle, "native", None)
                    hwnd = int(native.winId()) if native is not None else 0
                    if hwnd:
                        user32 = ctypes.windll.user32
                        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                        user32.BringWindowToTop(hwnd)
                        user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    @staticmethod
    def _retitle(record: WindowRecord) -> None:
        try:
            if record.handle is not None:
                record.handle.set_title(record.title)
        except Exception:
            pass

    # ---------- layout e geometria ----------
    async def set_layout(self, window_id: str, widgets: object, locked: object = None, *, notify: bool = False) -> dict[str, object]:
        record = self._windows.get(window_id)
        if record is None:
            raise KeyError(window_id)
        record.widgets = validate_widgets(widgets)
        if isinstance(locked, bool):
            record.locked = locked
        profile = self._profiles.get(record.profile_id or "")
        if profile is not None:
            profile["widgets"], profile["locked"] = copy.deepcopy(record.widgets), record.locked
            self._save()
        if notify:
            await self._publish({"type": "window", "windowId": window_id, "reload": True})
        return record.as_dict()

    async def add_widget(self, window_id: str, kind: str, options: object = None) -> dict[str, object]:
        record = self._windows.get(window_id)
        if record is None:
            raise KeyError(window_id)
        if kind not in WIDGET_KINDS:
            raise ValueError("widget_desconhecido")
        entry = {"id": f"{kind}-{uuid.uuid4().hex[:6]}", "kind": kind, "options": options if isinstance(options, dict) else {}}
        return await self.set_layout(window_id, [*record.widgets, entry], notify=True)

    async def remove_widget(self, window_id: str, kind: str) -> dict[str, object]:
        record = self._windows.get(window_id)
        if record is None:
            raise KeyError(window_id)
        kept = [w for w in record.widgets if w["kind"] != kind]
        if len(kept) == len(record.widgets):
            raise ValueError("widget_ausente")
        return await self.set_layout(window_id, kept, notify=True)

    def note_geometry(self, window_id: str, **geo: int) -> None:
        record = self._windows.get(window_id)
        if record is None:
            return
        for key in ("x", "y", "width", "height"):
            if key in geo and isinstance(geo[key], int):
                setattr(record, key, geo[key])
        profile = self._profiles.get(record.profile_id or "")
        if profile is not None:
            profile["geometry"] = {k: getattr(record, k) for k in ("x", "y", "width", "height") if getattr(record, k) is not None}
            self._save()

    @staticmethod
    def _apply_geometry(record: WindowRecord, geo: dict[str, int]) -> None:
        try:
            if record.handle is not None and "x" in geo and "y" in geo:
                record.handle.move(geo["x"], geo["y"])
            if record.handle is not None and "width" in geo:
                record.handle.resize(geo["width"], geo["height"])
        except Exception:
            pass

    async def move(self, window_id: str, geometry: dict[str, object]) -> dict[str, object]:
        record = self._windows.get(window_id)
        if record is None:
            raise KeyError(window_id)
        geo = self._geometry({"width": record.width, "height": record.height, **{k: v for k, v in geometry.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}})
        await asyncio.to_thread(self._apply_geometry, record, geo)
        self.note_geometry(window_id, **geo)
        return record.as_dict()

    # ---------- fechamento ----------
    async def close(self, window_id: str) -> bool:
        record = self._windows.pop(window_id, None)
        if record is None:
            return False
        try:
            if record.handle is not None:
                await asyncio.to_thread(record.handle.destroy)
        except Exception:
            pass
        await self._publish({"type": "window", "windowId": window_id, "closed": True})
        return True

    def note_closed(self, window_id: str) -> None:
        """A janela foi fechada pelo usuário (X): esquece a janela aberta; o perfil permanece."""
        self._windows.pop(window_id, None)

    async def close_all(self) -> int:
        count = 0
        for window_id in list(self._windows):
            count += 1 if await self.close(window_id) else 0
        return count


def create_ui_windows_router(registry: WindowRegistry, *, token_provider: Callable[[], str], panels: Any = None,
                             main_window: Callable[[], Any] | None = None) -> APIRouter:
    """panels: o registro de painéis (services/panel_registry.py), para mover painéis entre as janelas por voz.
    main_window: a janela principal (pywebview), para o F11 nela."""
    router = APIRouter()

    @router.post("/api/ui/windows/fullscreen")
    async def fullscreen(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")
        window_id = str((payload if isinstance(payload, dict) else {}).get("windowId") or "main")
        if window_id == "main":
            window = main_window() if main_window else None
            if window is None:
                return {"ok": False}
            await asyncio.to_thread(window.toggle_fullscreen)
            return {"ok": True}
        if not WINDOW_ID_RE.fullmatch(window_id):
            raise HTTPException(status_code=422, detail="windowId invalido")
        return {"ok": await registry.toggle_fullscreen(window_id)}

    def authorize(request: Request, supplied: str | None) -> None:
        expected = str(token_provider() or "")
        if expected and supplied and hmac.compare_digest(str(supplied), expected):
            return
        if is_local_request(request):
            return
        raise HTTPException(status_code=403, detail="acao interna nao autorizada")

    def valid_window(window_id: str) -> str:
        if not WINDOW_ID_RE.fullmatch(window_id or ""):
            raise HTTPException(status_code=422, detail="windowId invalido")
        return window_id

    def valid_profile(profile_id: str) -> str:
        if not ITEM_ID_RE.fullmatch(profile_id or ""):
            raise HTTPException(status_code=422, detail="perfil invalido")
        return profile_id

    def body_of(payload: Any) -> dict[str, Any]:
        return payload if isinstance(payload, dict) else {}

    async def guarded(action: Awaitable[Any]) -> Any:
        try:
            return await action
        except KeyError:
            raise HTTPException(status_code=404, detail="nao encontrado")
        except ValueError as exc:
            code = str(exc)
            if code in {"limite_de_janelas", "limite_de_perfis"}:
                raise HTTPException(status_code=409, detail="limite de janelas ou perfis atingido")
            raise HTTPException(status_code=422, detail="dados invalidos")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"nao foi possivel concluir: {type(exc).__name__}")

    Hdr = Header(default=None, alias="X-Kira-Internal")

    @router.get("/api/ui/windows")
    async def list_windows(request: Request, x_kira_internal: str | None = Hdr) -> dict[str, object]:
        authorize(request, x_kira_internal)
        return {"ok": True, "windows": registry.list(), "profiles": registry.profiles(), "summary": registry.describe()}

    @router.post("/api/ui/windows")
    async def create_window(request: Request, payload: Any = Body(default=None), x_kira_internal: str | None = Hdr) -> dict[str, object]:
        """Sem perfil: janela temporária (não salva). Com profileId: abre ou foca a janela fixa."""
        authorize(request, x_kira_internal)
        body = body_of(payload)
        if body.get("profileId") is not None:
            return {"ok": True, "window": await guarded(registry.open_profile(valid_profile(str(body["profileId"]))))}
        return {"ok": True, "window": await guarded(registry.create_temporary())}

    @router.patch("/api/ui/windows/{window_id}")
    async def patch_window(window_id: str, request: Request, payload: Any = Body(default=None), x_kira_internal: str | None = Hdr) -> dict[str, object]:
        authorize(request, x_kira_internal)
        valid_window(window_id)
        body = body_of(payload)
        geometry = {k: body[k] for k in ("x", "y", "width", "height") if k in body}
        if registry.get(window_id) is None:
            raise HTTPException(status_code=404, detail="janela nao encontrada")
        if geometry:
            return {"ok": True, "window": await guarded(registry.move(window_id, geometry))}
        return {"ok": True, "window": registry.get(window_id).as_dict()}  # type: ignore[union-attr]

    @router.put("/api/ui/windows/{window_id}/layout")
    async def put_layout(window_id: str, request: Request, payload: Any = Body(default=None), x_kira_internal: str | None = Hdr) -> dict[str, object]:
        authorize(request, x_kira_internal)
        valid_window(window_id)
        body = body_of(payload)
        return {"ok": True, "window": await guarded(registry.set_layout(window_id, body.get("widgets"), body.get("locked")))}

    @router.post("/api/ui/windows/{window_id}/widgets")
    async def add_widget(window_id: str, request: Request, payload: Any = Body(default=None), x_kira_internal: str | None = Hdr) -> dict[str, object]:
        authorize(request, x_kira_internal)
        valid_window(window_id)
        body = body_of(payload)
        return {"ok": True, "window": await guarded(registry.add_widget(window_id, str(body.get("kind") or ""), body.get("options")))}

    @router.delete("/api/ui/windows/{window_id}/widgets/{kind}")
    async def remove_widget(window_id: str, kind: str, request: Request, x_kira_internal: str | None = Hdr) -> dict[str, object]:
        authorize(request, x_kira_internal)
        valid_window(window_id)
        return {"ok": True, "window": await guarded(registry.remove_widget(window_id, kind))}

    @router.post("/api/ui/windows/{window_id}/hit")
    async def window_hit(window_id: str, request: Request, payload: Any = Body(default=None)) -> dict[str, object]:
        """Onde estão os painéis da sobreposição flutuante: fora deles o clique atravessa para a área de trabalho."""
        authorize(request, None)
        record = registry._windows.get(valid_window(window_id))
        if record is not None and isinstance(payload, list):
            record.hit = [tuple(int(v) for v in r[:4]) for r in payload[:12] if isinstance(r, list) and len(r) >= 4]
        return {"ok": True}

    @router.delete("/api/ui/windows/{window_id}")
    async def delete_window(window_id: str, request: Request, x_kira_internal: str | None = Hdr) -> dict[str, object]:
        authorize(request, x_kira_internal)
        valid_window(window_id)
        return {"ok": True, "closed": await registry.close(window_id)}

    @router.post("/api/ui/panels/move")
    async def move_panels(request: Request, payload: Any = Body(default=None), x_kira_internal: str | None = Hdr) -> dict[str, object]:
        """Por voz (tool move_panel): "move esse painel pra janela 2", "traz da janela 2 pra principal", "joga as imagens
        na outra janela". summary: frase curta para a Kira dizer do jeito dela."""
        authorize(request, x_kira_internal)
        if panels is None:
            raise HTTPException(status_code=503, detail="paineis indisponiveis")
        body = body_of(payload)
        target = " ".join(str(body.get("panel") or "isso").split())[:80]
        to_ref = " ".join(str(body.get("window") or "").split())[:60]
        from_ref = " ".join(str(body.get("from_window") or "").split())[:60]
        source = registry.window_ref(from_ref) if from_ref else None
        if from_ref and source in {None, OTHER_WINDOW}:
            return {"ok": True, "moved": [], "summary": f"Não achei a {from_ref} aberta."}
        screen = re.search(r"(?:monitor|tela)\s*(\d)", fold(to_ref))
        dest = str((await registry.create_floating(int(screen.group(1)))).get("windowId") or "") if screen else registry.window_ref(to_ref)
        opened = [r.id for r in sorted(registry._windows.values(), key=lambda r: r.number)]
        if dest == OTHER_WINDOW:
            where = {panels.window_of(i) for i in panels.resolve(target)} if source is None else {source}
            outside = [w for w in where if w and w != MAIN_WINDOW_ID]
            visible = registry.visible_ids()
            dest = MAIN_WINDOW_ID if outside else (visible[0] if visible else (opened[0] if opened else None))
        if dest is None:
            text = f"A {to_ref or 'janela'} não está aberta." if opened else "Não há janela extra aberta: só a principal."
            return {"ok": True, "moved": [], "summary": text + " Se o Chefe quiser, abra uma com open_window e mova de novo."}
        result = await panels.move_target(target, dest, source=source)
        name = registry.spoken_name(dest)
        if result.get("moved"):
            names = ", ".join(result.get("names") or ["painel"])
            hidden = dest != MAIN_WINDOW_ID and not registry.is_shown(dest)
            text = f"Pronto: {names} na {name}." + (" Ela está minimizada ou coberta, então ainda não aparece." if hidden else "")
        elif result.get("personal"):
            text = "Esse painel tem dado pessoal (briefing, agenda, e-mail ou saúde): ele fica só na janela principal."
        elif result.get("vitals"):
            text = f"O Sistema Vital fica na principal; na {name} dá para pôr o widget dele com add_widget."
        elif result.get("next"):
            kind = str(result["next"])
            text = f"Combinado: o próximo painel{'' if kind == 'qualquer' else ' de ' + kind} abre na {name}."
        else:
            text = f"Não achei esse painel fora da {name}."
        logger.info("UI_PANEL_MOVE to=%s moved=%d next=%s", dest, len(result.get("moved") or []), result.get("next") or "-")
        return {"ok": True, "moved": result.get("moved") or [], "window": dest, "summary": text}

    @router.post("/api/ui/profiles")
    async def create_profile(request: Request, payload: Any = Body(default=None), x_kira_internal: str | None = Hdr) -> dict[str, object]:
        """Cria a janela fixa nomeada e já a abre."""
        authorize(request, x_kira_internal)

        async def make() -> dict[str, object]:
            profile = registry.create_profile(body_of(payload).get("name"))
            return {"profile": profile, "window": await registry.open_profile(profile["id"])}

        return {"ok": True, **await guarded(make())}

    @router.patch("/api/ui/profiles/{profile_id}")
    async def patch_profile(profile_id: str, request: Request, payload: Any = Body(default=None), x_kira_internal: str | None = Hdr) -> dict[str, object]:
        authorize(request, x_kira_internal)
        body = body_of(payload)

        async def run() -> dict[str, Any]:
            return registry.update_profile(valid_profile(profile_id), name=body.get("name"), open_on_start=body.get("openOnStart"))

        return {"ok": True, "profile": await guarded(run())}

    @router.post("/api/ui/profiles/{profile_id}/duplicate")
    async def duplicate_profile(profile_id: str, request: Request, x_kira_internal: str | None = Hdr) -> dict[str, object]:
        authorize(request, x_kira_internal)

        async def run() -> dict[str, Any]:
            return registry.duplicate_profile(valid_profile(profile_id))

        return {"ok": True, "profile": await guarded(run())}

    @router.delete("/api/ui/profiles/{profile_id}")
    async def delete_profile(profile_id: str, request: Request, x_kira_internal: str | None = Hdr) -> dict[str, object]:
        authorize(request, x_kira_internal)
        return {"ok": True, "deleted": await registry.delete_profile(valid_profile(profile_id))}

    return router


__all__ = [
    "MAIN_WINDOW_ID", "MAX_EXTRA_WINDOWS", "MAX_PROFILES", "WIDGET_KINDS", "WIDGET_NAMES", "WINDOW_ID_RE",
    "WindowRegistry", "create_ui_windows_router", "validate_widgets", "window_on_screen",
]
