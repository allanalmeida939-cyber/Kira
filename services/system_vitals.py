from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from typing import Any

try:
    import psutil
except Exception:  # pragma: no cover - optional dependency
    psutil = None

logger = logging.getLogger(__name__)

_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SECONDS = 10.0
_LAST_CACHE: dict[str, Any] | None = None
_LAST_CACHE_TS = 0.0
_CPU_PRIMED = False
_GPU_LOGGED_STATE: str | None = None
_LOG_READY_DONE = False


def _round1(value: float | int | None) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 1)
    except Exception:
        return None


def _cpu_percent() -> float:
    global _CPU_PRIMED
    if psutil is None:
        return 0.0
    if not _CPU_PRIMED:
        psutil.cpu_percent(interval=None)
        _CPU_PRIMED = True
    return _round1(psutil.cpu_percent(interval=None)) or 0.0


def _memory_info() -> dict[str, float]:
    if psutil is None:
        return {"usage_percent": 0.0, "used_gb": 0.0, "total_gb": 0.0}
    vm = psutil.virtual_memory()
    used_gb = _round1(vm.used / (1024**3)) or 0.0
    total_gb = _round1(vm.total / (1024**3)) or 0.0
    return {
        "usage_percent": _round1(vm.percent) or 0.0,
        "used_gb": used_gb,
        "total_gb": total_gb,
    }


def _extract_temp_from_psutil() -> float | None:
    if psutil is None:
        return None
    try:
        if not hasattr(psutil, "sensors_temperatures"):
            return None
        temps = psutil.sensors_temperatures() or {}
        for entries in temps.values():
            for entry in entries or []:
                current = getattr(entry, "current", None)
                if current is not None:
                    return _round1(current)
    except Exception:
        return None
    return None


def _collect_gpu_via_nvidia_smi() -> dict[str, Any] | None:
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    cmd = [
        smi,
        "--query-gpu=name,memory.used,memory.total,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True, timeout=1.8).strip()
    if not out:
        return None
    first_line = out.splitlines()[0]
    parts = [p.strip() for p in first_line.split(",")]
    if len(parts) < 4:
        return None
    name = parts[0] or None
    used_mb = float(parts[1])
    total_mb = float(parts[2]) if float(parts[2]) > 0 else 0.0
    temp_raw = parts[3].strip()
    temp_c = None if temp_raw in {"", "N/A", "n/a"} else _round1(float(temp_raw))
    usage_percent = _round1((used_mb / total_mb) * 100.0) if total_mb > 0 else 0.0
    return {
        "name": name,
        "vram_usage_percent": usage_percent or 0.0,
        "vram_used_gb": _round1(used_mb / 1024.0) or 0.0,
        "vram_total_gb": _round1(total_mb / 1024.0) or 0.0,
        "temperature_c": temp_c,
        "temperature_available": temp_c is not None,
    }


def _collect_gpu() -> dict[str, Any]:
    global _GPU_LOGGED_STATE
    try:
        gpu = _collect_gpu_via_nvidia_smi()
        if gpu:
            if _GPU_LOGGED_STATE != "detected":
                logger.info("SYSTEM_VITALS_GPU_DETECTED name=%s", str(gpu.get("name") or "unknown"))
                _GPU_LOGGED_STATE = "detected"
            return gpu
    except Exception as exc:
        logger.debug("SYSTEM_VITALS_GPU_COLLECT_FAIL detail=%s", exc)
    if _GPU_LOGGED_STATE != "unavailable":
        logger.info("SYSTEM_VITALS_GPU_UNAVAILABLE")
        _GPU_LOGGED_STATE = "unavailable"
    return {
        "name": None,
        "vram_usage_percent": 0.0,
        "vram_used_gb": 0.0,
        "vram_total_gb": 0.0,
        "temperature_c": None,
        "temperature_available": False,
    }


def _health(cpu: dict[str, Any], ram: dict[str, Any], gpu: dict[str, Any]) -> dict[str, str]:
    reasons: list[str] = []
    level = "good"
    if float(cpu.get("usage_percent") or 0.0) >= 85:
        reasons.append("CPU alta")
        level = "critical"
    elif float(cpu.get("usage_percent") or 0.0) >= 70 and level != "critical":
        reasons.append("CPU em atencao")
        level = "warning"
    if float(ram.get("usage_percent") or 0.0) >= 85:
        reasons.append("RAM alta")
        level = "critical"
    elif float(ram.get("usage_percent") or 0.0) >= 70 and level != "critical":
        reasons.append("RAM em atencao")
        level = "warning"
    if float(gpu.get("vram_usage_percent") or 0.0) >= 85:
        reasons.append("VRAM alta")
        level = "critical"
    elif float(gpu.get("vram_usage_percent") or 0.0) >= 70 and level != "critical":
        reasons.append("VRAM em atencao")
        level = "warning"
    temp_pairs = [
        ("CPU quente", cpu.get("temperature_c")),
        ("RAM quente", ram.get("temperature_c")),
        ("GPU quente", gpu.get("temperature_c")),
    ]
    for label, value in temp_pairs:
        if value is None:
            continue
        if float(value) >= 85:
            reasons.append(label)
            level = "critical"
        elif float(value) >= 75 and level != "critical":
            reasons.append(label.replace("quente", "em atencao"))
            level = "warning"
    if level == "critical":
        return {"level": "critical", "label": "Crítico", "reason": reasons[0] if reasons else "Carga crítica"}
    if level == "warning":
        return {"level": "warning", "label": "Atenção", "reason": reasons[0] if reasons else "Recursos em atenção"}
    return {"level": "good", "label": "Saúde Boa", "reason": "Sistema dentro do normal"}


def _collect_once() -> dict[str, Any]:
    if psutil is None:
        return {"ok": False, "error": "psutil_unavailable", "timestamp": time.time(), "stale": False}
    cpu_temp = _extract_temp_from_psutil()
    if cpu_temp is None:
        logger.info("SYSTEM_VITALS_TEMPERATURE_UNAVAILABLE source=cpu")
    cpu = {
        "usage_percent": _cpu_percent(),
        "temperature_c": cpu_temp,
        "temperature_available": cpu_temp is not None,
    }
    ram_base = _memory_info()
    ram_temp = _extract_temp_from_psutil()
    if ram_temp is None:
        logger.info("SYSTEM_VITALS_TEMPERATURE_UNAVAILABLE source=ram")
    ram = {
        **ram_base,
        "temperature_c": ram_temp,
        "temperature_available": ram_temp is not None,
    }
    gpu = _collect_gpu()
    health = _health(cpu, ram, gpu)
    return {
        "ok": True,
        "timestamp": time.time(),
        "stale": False,
        "cpu": cpu,
        "ram": ram,
        "gpu": gpu,
        "health": health,
    }


def get_system_vitals() -> dict[str, Any]:
    return get_system_vitals_cached(min_interval_seconds=_CACHE_TTL_SECONDS)


def get_system_vitals_cached(*, min_interval_seconds: float = 10.0, force: bool = False, scope: str = "global") -> dict[str, Any]:
    global _LAST_CACHE, _LAST_CACHE_TS, _LOG_READY_DONE
    now = time.time()
    if not _LOG_READY_DONE:
        logger.info("SYSTEM_VITALS_SERVICE_READY")
        _LOG_READY_DONE = True
    with _CACHE_LOCK:
        ttl = max(1.0, float(min_interval_seconds or _CACHE_TTL_SECONDS))
        if not force and _LAST_CACHE is not None and (now - _LAST_CACHE_TS) < ttl:
            logger.info("SYSTEM_VITALS_CACHE_HIT scope=%s age_ms=%s", scope, int((now - _LAST_CACHE_TS) * 1000))
            return dict(_LAST_CACHE)
        try:
            payload = _collect_once()
            _LAST_CACHE = payload
            _LAST_CACHE_TS = now
            logger.info("SYSTEM_VITALS_SAMPLE_TAKEN scope=%s", scope)
            return dict(payload)
        except Exception as exc:
            logger.error("SYSTEM_VITALS_ERROR error=%s", exc)
            if _LAST_CACHE is not None:
                stale = dict(_LAST_CACHE)
                stale["stale"] = True
                stale["timestamp"] = now
                return stale
            return {"ok": False, "error": "system_vitals_unavailable", "timestamp": now, "stale": True}


def get_process_vitals(name: str) -> dict[str, Any]:
    query = str(name or "").strip().lower()
    now = time.time()
    if psutil is None:
        return {"enabled": True, "matched": False, "status": "unavailable", "message": "psutil unavailable", "sampled_at": now}
    if not query:
        return {"enabled": True, "matched": False, "message": "process name required", "sampled_at": now}
    best: Any = None
    best_score = 0
    for proc in psutil.process_iter(["pid", "name", "memory_info", "memory_percent", "cpu_percent"]):
        try:
            pname = str(proc.info.get("name") or "").lower()
        except Exception:
            continue
        if not pname:
            continue
        score = 0
        if pname == query:
            score = 100
        elif query in pname:
            score = 80
        elif pname.startswith(query):
            score = 70
        if score > best_score:
            best = proc
            best_score = score
    if best is None:
        logger.info("SYSTEM_VITALS_PROCESS_NOT_FOUND query=%s", query)
        return {"enabled": True, "matched": False, "query": query, "message": "process not found", "sampled_at": now}
    try:
        info = best.as_dict(attrs=["pid", "name", "memory_info", "memory_percent", "cpu_percent"])
        cpu = _round1(best.cpu_percent(interval=0.1)) or 0.0
        mem_info = info.get("memory_info")
        rss = float(getattr(mem_info, "rss", 0) or 0)
        payload = {
            "enabled": True,
            "matched": True,
            "query": query,
            "process": info.get("name"),
            "pid": info.get("pid"),
            "cpu_percent": cpu,
            "memory_mb": _round1(rss / (1024**2)) or 0.0,
            "memory_percent": _round1(info.get("memory_percent")) or 0.0,
            "sampled_at": time.time(),
        }
        logger.info("SYSTEM_VITALS_SAMPLE_TAKEN scope=process process=%s", payload.get("process"))
        return payload
    except Exception as exc:
        logger.info("SYSTEM_VITALS_PROCESS_NOT_FOUND query=%s error=%s", query, exc)
        return {"enabled": True, "matched": False, "query": query, "message": "process not found", "sampled_at": time.time()}
