"""Saúde do PC: memória, processador, disco e os programas que mais gastam; limpa a RAM sozinha quando passa do limite.

A limpeza usa o Reduce Memory do Chefe (C:\\Program Files\\ReduceMemory, 'ReduceMemory_x64.exe /O' limpa sem abrir
janela). Sem ele instalado, esvazia a memória de trabalho dos processos do usuário (é o que o Reduce Memory faz).
Limite e intervalo: KIRA_RAM_LIMIT (padrão 85%) e no máximo uma limpeza a cada 10 minutos.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import subprocess
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

logger = logging.getLogger("kira.app.pc_health")

REDUCE_MEMORY = (Path(r"C:\Program Files\ReduceMemory\ReduceMemory_x64.exe"), Path(r"C:\Program Files\ReduceMemory\ReduceMemory.exe"))
CHECK_EVERY_S = 600.0  # uma olhada a cada 10 minutos (pedido do Chefe: menos peso; "limpa a memória" limpa na hora)
CLEAN_COOLDOWN_S = 540.0  # um pouco menos que o intervalo: cada olhada acima do limite pode limpar
DISK_LOW_GB = 8.0
CPU_HOT_PERCENT = 90.0
CPU_HOT_CHECKS = 2  # 2 olhadas seguidas (~10 minutos) acima de 90%


def ram_limit() -> float:
    try:
        return max(50.0, min(97.0, float(os.getenv("KIRA_RAM_LIMIT") or 85)))
    except ValueError:
        return 85.0


def gb(value: float) -> float:
    return round(value / 2 ** 30, 1)


def snapshot() -> dict[str, Any]:
    mem = psutil.virtual_memory()
    procs = []
    for proc in psutil.process_iter(["name", "memory_info"]):
        try:
            procs.append((proc.info["memory_info"].rss, proc.info["name"] or "?"))
        except (psutil.Error, AttributeError):
            continue
    by_name: dict[str, int] = {}
    for rss, name in procs:
        by_name[name] = by_name.get(name, 0) + rss
    top = sorted(by_name.items(), key=lambda item: -item[1])[:6]
    disks = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" in part.opts or not part.fstype:
            continue
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        disks.append({"disco": part.mountpoint.rstrip("\\"), "livre_gb": gb(usage.free), "total_gb": gb(usage.total), "uso": usage.percent})
    return {
        "memoria": {"total_gb": gb(mem.total), "usada_gb": gb(mem.total - mem.available), "livre_gb": gb(mem.available), "uso": mem.percent},
        "processador": {"uso": psutil.cpu_percent(interval=0.5), "nucleos": psutil.cpu_count()},
        "discos": disks,
        "mais_gastam_memoria": [{"programa": name, "mb": round(rss / 2 ** 20)} for name, rss in top],
        "limite_limpeza_auto": ram_limit(),
    }


def _empty_working_sets() -> int:
    """Plano B sem o Reduce Memory: devolve ao Windows a memória parada de cada processo do usuário."""
    psapi, kernel = ctypes.windll.psapi, ctypes.windll.kernel32
    done = 0
    for proc in psutil.process_iter():
        handle = kernel.OpenProcess(0x0100 | 0x0400, False, proc.pid)  # SET_QUOTA | QUERY_INFORMATION
        if handle:
            done += bool(psapi.EmptyWorkingSet(handle))
            kernel.CloseHandle(handle)
    return done


def optimize() -> dict[str, Any]:
    before = psutil.virtual_memory().available
    exe = next((p for p in REDUCE_MEMORY if p.exists()), None)
    if exe is not None:
        subprocess.run([str(exe), "/O"], capture_output=True, timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        tool = "Reduce Memory"
    else:
        _empty_working_sets()
        tool = "limpeza da Kira"
    time.sleep(2.0)
    after = psutil.virtual_memory()
    freed = max(0, after.available - before)
    logger.info("PC_HEALTH_RAM_CLEANED freed_mb=%d use=%s tool=%s", freed // 2 ** 20, after.percent, tool)
    return {"ok": True, "liberou_mb": round(freed / 2 ** 20), "uso_agora": after.percent, "livre_gb": gb(after.available), "com": tool}


class HealthWatch:
    """Vigia em segundo plano: limpa a RAM sozinha e guarda os avisos (disco quase cheio, processador no talo)."""

    def __init__(self) -> None:
        self.events: deque = deque(maxlen=30)
        self._last_clean = 0.0
        self._hot = 0
        self._disk_warned: set[str] = set()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._loop())

    def _note(self, kind: str, text: str) -> None:
        self.events.append({"hora": datetime.now().strftime("%H:%M"), "tipo": kind, "texto": text})

    async def _loop(self) -> None:
        while True:
            try:
                await self.check()
            except Exception as exc:  # noqa: BLE001 - a vigia nunca para
                logger.info("PC_HEALTH_CHECK_FAILED reason=%s", type(exc).__name__)
            await asyncio.sleep(CHECK_EVERY_S)

    async def check(self) -> None:
        mem = psutil.virtual_memory()
        if mem.percent >= ram_limit() and time.time() - self._last_clean > CLEAN_COOLDOWN_S:
            self._last_clean = time.time()
            result = await asyncio.to_thread(optimize)
            self._note("memoria", f"Memória em {mem.percent:.0f}%: limpei sozinha e liberei {result['liberou_mb']} MB (agora {result['uso_agora']:.0f}%).")
        cpu = await asyncio.to_thread(psutil.cpu_percent, 3.0)  # média de 3 s (uma leitura instantânea a cada 10 min engana)
        self._hot = self._hot + 1 if cpu >= CPU_HOT_PERCENT else 0
        if self._hot == CPU_HOT_CHECKS:
            self._note("processador", f"Processador acima de {CPU_HOT_PERCENT:.0f}% há uns 10 minutos.")
        for part in psutil.disk_partitions(all=False):
            if "cdrom" in part.opts or not part.fstype:
                continue
            try:
                free = psutil.disk_usage(part.mountpoint).free
            except OSError:
                continue
            key = part.mountpoint
            if free < DISK_LOW_GB * 2 ** 30 and key not in self._disk_warned:
                self._disk_warned.add(key)
                self._note("disco", f"Disco {key.rstrip(chr(92))} com só {gb(free)} GB livres.")
            elif free >= DISK_LOW_GB * 2 ** 30 * 1.5:
                self._disk_warned.discard(key)


__all__ = ["HealthWatch", "optimize", "snapshot", "ram_limit"]


def create_pc_health_router(watch: HealthWatch, *, token_provider) -> "Any":
    """Rota da tool 'saude_pc': status (números e avisos) ou otimizar (limpa a RAM agora)."""
    import hmac

    from fastapi import APIRouter, Body, Header, HTTPException

    router = APIRouter()

    @router.post("/api/ui/saude-pc")
    async def tool(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        expected = str(token_provider() or "")
        if not expected or not x_kira_internal or not hmac.compare_digest(str(x_kira_internal), expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")
        action = str((payload or {}).get("action") or "status").lower() if isinstance(payload, dict) else "status"
        if action.startswith(("otim", "limp")):
            return await asyncio.to_thread(optimize)
        return {"ok": True, **(await asyncio.to_thread(snapshot)), "avisos_recentes": list(watch.events)[-8:]}

    return router
