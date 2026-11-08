from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from services.local_request import is_local_request

try:
    import psutil as _psutil
except ImportError:  # pragma: no cover - exercised by the contract test
    _psutil = None


router = APIRouter()
PROCESS_STARTED_MONOTONIC = time.monotonic()
GPU_CACHE_SECONDS = 2.0
GPU_QUERY_TIMEOUT_SECONDS = 1.5
CPU_MIN_SPAN_SECONDS = 0.4


def _query_gpu_percent() -> float | None:
    """GPU NVIDIA via nvidia-smi quando existir; qualquer falha vira 'indisponível'."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=GPU_QUERY_TIMEOUT_SECONDS, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        values = [float(line.strip()) for line in out.stdout.splitlines() if line.strip()]
        return max(values) if values else None
    except Exception:
        return None


class StatsSampler:
    """Amostrador não bloqueante: taxas de disco e rede saem da diferença entre duas leituras."""

    def __init__(self, psutil_module: Any = _psutil, *, clock=time.monotonic, gpu_probe=_query_gpu_percent) -> None:
        self._ps = psutil_module
        self._clock = clock
        self._gpu_probe = gpu_probe
        self._lock = threading.Lock()
        self._last_io: tuple[float, Any, Any] | None = None
        self._gpu: tuple[float, float | None] | None = None
        self._cpu: tuple[float, float] | None = None
        self._disk_root = os.path.abspath(os.sep)
        if self._ps is not None:
            try:
                self._ps.cpu_percent(interval=None)
            except Exception:
                pass

    def sample(self) -> dict[str, Any]:
        now = self._clock()
        payload: dict[str, Any] = {
            "cpu_percent": None, "ram_percent": None,
            "disk": {"percent": None, "read_bps": None, "write_bps": None},
            "net": {"down_bps": None, "up_bps": None},
            "gpu": {"percent": None},
            "uptime_seconds": round(max(0.0, now - PROCESS_STARTED_MONOTONIC), 1),
            "psutil_available": bool(self._ps),
            "unavailable": [],
        }
        if self._ps is None:
            payload["unavailable"] = ["cpu", "ram", "disk", "net", "gpu"]
            payload["notice"] = "CPU, RAM, disco e rede indisponíveis nesta instalação."
            return payload
        with self._lock:
            self._fill(payload, now)
        return payload

    def _fill(self, payload: dict[str, Any], now: float) -> None:
        ps, missing = self._ps, payload["unavailable"]
        try:
            if self._cpu is None or now - self._cpu[0] >= CPU_MIN_SPAN_SECONDS:
                self._cpu = (now, float(ps.cpu_percent(interval=None)))
            payload["cpu_percent"] = self._cpu[1]
        except Exception:
            missing.append("cpu")
        try:
            payload["ram_percent"] = float(ps.virtual_memory().percent)
        except Exception:
            missing.append("ram")
        try:
            payload["disk"]["percent"] = float(ps.disk_usage(self._disk_root).percent)
        except Exception:
            missing.append("disk")
        try:
            io, net = ps.disk_io_counters(), ps.net_io_counters()
        except Exception:
            io = net = None
        previous, self._last_io = self._last_io, (now, io, net)
        if previous is not None and now > previous[0]:
            span = now - previous[0]
            if io is not None and previous[1] is not None:
                payload["disk"]["read_bps"] = max(0.0, (io.read_bytes - previous[1].read_bytes) / span)
                payload["disk"]["write_bps"] = max(0.0, (io.write_bytes - previous[1].write_bytes) / span)
            if net is not None and previous[2] is not None:
                payload["net"]["down_bps"] = max(0.0, (net.bytes_recv - previous[2].bytes_recv) / span)
                payload["net"]["up_bps"] = max(0.0, (net.bytes_sent - previous[2].bytes_sent) / span)
        else:
            if io is not None:
                payload["disk"]["read_bps"] = payload["disk"]["write_bps"] = 0.0
            if net is not None:
                payload["net"]["down_bps"] = payload["net"]["up_bps"] = 0.0
        if io is None:
            missing.append("disk_io")
        if net is None:
            missing.append("net")
        if self._gpu is None or now - self._gpu[0] >= GPU_CACHE_SECONDS:
            self._gpu = (now, self._gpu_probe())
        payload["gpu"]["percent"] = self._gpu[1]
        if self._gpu[1] is None:
            missing.append("gpu")


_SAMPLER = StatsSampler()


@router.get("/api/ui/system-stats")
async def ui_system_stats() -> dict[str, Any]:
    return await asyncio.to_thread(_SAMPLER.sample)


# ---------------------------------------------------------------------------------------------
# Coletor único com assinantes (SSE): coleta a 1 s só enquanto existe alguém olhando.
# ---------------------------------------------------------------------------------------------
STREAM_INTERVAL_SECONDS = 1.0
TOP_INTERVAL_SECONDS = 2.0
TOP_LIMIT = 8
OPEN_APPS_LIMIT = 12
GPU_REFRESH_SECONDS = 3.0


def _visible_window_pids() -> set[int]:
    """PIDs de processos com janela visível e título (só o PID; o título nunca sai daqui)."""
    if os.name != "nt":
        return set()
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        pids: set[int] = set()

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def callback(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0 and not user32.GetWindow(hwnd, 4):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                pids.add(int(pid.value))
            return True

        user32.EnumWindows(callback, 0)
        return pids
    except Exception:
        return set()


def _clean_name(name: object) -> str:
    text = str(name or "").strip()
    return text[:-4] if text.lower().endswith(".exe") else text


class SystemCollector:
    """Uma única coleta compartilhada por todos os assinantes; para sozinha sem assinantes."""

    def __init__(self, psutil_module: Any = _psutil, *, interval: float = STREAM_INTERVAL_SECONDS, window_pids=_visible_window_pids) -> None:
        self._ps = psutil_module
        self._interval = interval
        self._window_pids = window_pids
        self._gpu_value: float | None = None
        self._gpu_at = 0.0
        self._sampler = StatsSampler(psutil_module, gpu_probe=lambda: self._gpu_value)
        self._subscribers: dict[asyncio.Queue, dict[str, bool]] = {}
        self._task: asyncio.Task | None = None
        self._top_cache: tuple[float, list[dict[str, Any]], list[str]] = (0.0, [], [])
        self.last: dict[str, Any] | None = None
        self.collections = 0

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self, *, top: bool = False) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        self._subscribers[queue] = {"top": top}
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.pop(queue, None)

    async def _run(self) -> None:
        if self._ps is not None:
            await asyncio.to_thread(self._prewarm)
        while self._subscribers:
            started = time.monotonic()
            wants_top = any(opts["top"] for opts in self._subscribers.values())
            try:
                payload = await asyncio.to_thread(self._collect, wants_top)
            except Exception:
                payload = None
            if payload is not None:
                self.last = payload
                self.collections += 1
                for queue in list(self._subscribers):
                    if queue.full():
                        queue.get_nowait()
                    queue.put_nowait(payload)
            await asyncio.sleep(max(0.0, self._interval - (time.monotonic() - started)))
        self.last = None

    def _prewarm(self) -> None:
        try:
            self._ps.cpu_percent(interval=None, percpu=True)
            for proc in self._ps.process_iter():
                try:
                    proc.cpu_percent(None)
                except Exception:
                    continue
        except Exception:
            return

    def _refresh_gpu(self, now: float) -> None:
        if now - self._gpu_at >= GPU_REFRESH_SECONDS:
            self._gpu_at = now
            self._gpu_value = _query_gpu_percent()

    def _collect(self, wants_top: bool) -> dict[str, Any]:
        now = time.monotonic()
        self._refresh_gpu(now)
        payload = self._sampler.sample()
        ps = self._ps
        if ps is None:
            return payload
        extras: dict[str, Any] = {"cores": None, "memory": None, "disks": None, "battery": None, "temperature": None, "boot_time": None}
        try:
            extras["cores"] = [float(v) for v in ps.cpu_percent(interval=None, percpu=True)]
        except Exception:
            pass
        try:
            mem, swap = ps.virtual_memory(), ps.swap_memory()
            extras["memory"] = {"total": mem.total, "used": mem.used, "available": mem.available, "percent": mem.percent, "swap_used": swap.used, "swap_total": swap.total}
        except Exception:
            pass
        try:
            disks = []
            for part in ps.disk_partitions(all=False)[:6]:
                if "cdrom" in str(part.opts).lower() or not part.fstype:
                    continue
                usage = ps.disk_usage(part.mountpoint)
                disks.append({"mount": part.mountpoint, "total": usage.total, "used": usage.used, "percent": usage.percent})
            extras["disks"] = disks
        except Exception:
            pass
        try:
            battery = ps.sensors_battery()
            if battery is not None:
                extras["battery"] = {"percent": battery.percent, "plugged": bool(battery.power_plugged), "secs_left": battery.secsleft if isinstance(battery.secsleft, int) and battery.secsleft >= 0 else None}
        except Exception:
            pass
        try:
            temps = ps.sensors_temperatures() if hasattr(ps, "sensors_temperatures") else {}
            readings = [t.current for group in temps.values() for t in group if t.current]
            extras["temperature"] = max(readings) if readings else None
        except Exception:
            pass
        try:
            extras["boot_time"] = float(ps.boot_time())
        except Exception:
            pass
        payload.update(extras)
        missing = payload.setdefault("unavailable", [])
        for key, label in (("battery", "battery"), ("temperature", "temperature")):
            if extras[key] is None:
                missing.append(label)
        if wants_top:
            payload["top"], payload["apps"] = self._top_and_apps(now)
        return payload

    def _top_and_apps(self, now: float) -> tuple[list[dict[str, Any]], list[str]]:
        cached_at, top, apps = self._top_cache
        if now - cached_at < TOP_INTERVAL_SECONDS and cached_at:
            return top, apps
        ps = self._ps
        usage: dict[str, dict[str, float]] = {}
        names_by_pid: dict[int, str] = {}
        cores = max(int(ps.cpu_count() or 1), 1)
        for proc in ps.process_iter(["pid", "name", "memory_info"]):
            try:
                name = _clean_name(proc.info.get("name"))
                if not name or name.lower() in {"system idle process", "idle", "system", "registry", "memory compression"}:
                    continue
                entry = usage.setdefault(name, {"cpu": 0.0, "mem": 0.0})
                entry["cpu"] += proc.cpu_percent(None) / cores
                entry["mem"] += float(getattr(proc.info.get("memory_info"), "rss", 0) or 0)
                names_by_pid[int(proc.info["pid"])] = name
            except Exception:
                continue
        ranked = sorted(usage.items(), key=lambda kv: (-kv[1]["cpu"], -kv[1]["mem"]))[:TOP_LIMIT]
        top = [{"name": name, "cpu": round(v["cpu"], 1), "mem": int(v["mem"])} for name, v in ranked]
        apps = sorted({names_by_pid[pid] for pid in self._window_pids() if pid in names_by_pid})[:OPEN_APPS_LIMIT]
        self._top_cache = (now, top, apps)
        return top, apps


_COLLECTOR = SystemCollector()


def get_collector() -> SystemCollector:
    return _COLLECTOR


@router.get("/api/ui/system-stream")
async def ui_system_stream(request: Request, top: bool = False) -> StreamingResponse:
    """Server-Sent Events: um quadro por segundo, só para a própria máquina."""
    if not is_local_request(request):
        raise HTTPException(status_code=403, detail="origem nao autorizada")
    collector = get_collector()

    async def events():
        queue = collector.subscribe(top=top)
        try:
            while not await request.is_disconnected():
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            collector.unsubscribe(queue)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
