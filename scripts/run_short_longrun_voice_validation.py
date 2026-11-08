import argparse
import asyncio
import inspect
import json
import os
import re
import shutil
import statistics
import subprocess
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

import httpx
import psutil
from livekit import rtc


BASE_URL = "http://127.0.0.1:3000"
HEALTH_8091 = "http://127.0.0.1:8091/health"
STATUS_8091 = "http://127.0.0.1:8091/status"
TARGET_SECONDS = 0
MIN_CYCLES = 7
RUNNER_SETTINGS = {
    "cycles": MIN_CYCLES,
    "standby_minutes": 1,
    "return_minutes": 1,
    "active_after_wake_grace_seconds": 180,
    "enable_preventive_refresh": True,
    "refresh_after_wake_cycles": 4,
    "started_kira": False,
    "boot_log": "",
}
LOG_MARKERS = [
    "ASSISTANT_REPLY_REQUESTED source=text",
    "VOICE_ARB_GENERATION_START",
    "VOICE_ARB_GENERATION_DONE",
    "VOICE_OUTPUT_FIRST_CHUNK",
    "VOICE_OUTPUT_FINISHED",
    "VOICE_TEXT_TURN_AUDIO_CONFIRMED",
    "ECONOMY_TIMER_REAL_STANDBY_OK",
    "ECONOMY_TIMER_REAL_STANDBY_FAILED",
    "ECONOMY_LOCAL_GEMINI_STANDBY",
    "WAKE_WORD_DETECTED",
    "ECONOMY_WAKE_WITH_QUERY_DETECTED",
    "ECONOMY_WAKE_QUERY_DISPATCH_OK",
    "ACTIVE_LOCAL_AFTER_WAKE",
    "ReadError",
    "GEMINI_LIVE_1008_DETECTED",
    "GEMINI_LIVE_RECOVERY_",
    "SESSION_RECOVERY_",
    "AGENT_PROCESS_DIED",
    "SESSION_RUNTIME_ABORTED",
    "VOICE_READY_TO_SPEAK_TIMEOUT",
    "READY_TO_SPEAK_STUCK",
    "FIRST_CHUNK_HARD_TIMEOUT",
    "RECOVERY_EXECUTING",
    "VOICE_AUDIO_STUTTER_DETECTED",
    "user_reply_generation_without_audio",
    "user_reply_generation_failed",
    "voice_degraded",
    "voice_degraded_reason",
    "VOICE_AVAILABLE_FALSE reason=gemini_audio_degraded",
    "VOICE_HEALTH_AFTER_WAKE_OK",
    "VOICE_HEALTH_AFTER_WAKE_DEGRADED",
    "VOICE_DEGRADATION_RECOVERY_BEGIN",
    "VOICE_DEGRADATION_RECOVERY_REINSTALL_AUDIO_HOOKS",
    "VOICE_DEGRADATION_RECOVERY_CLOSE_GEMINI",
    "VOICE_DEGRADATION_RECOVERY_RECREATE_GEMINI",
    "VOICE_DEGRADATION_RECOVERY_READY",
    "VOICE_DEGRADATION_RECOVERY_FAILED",
    "VOICE_DEEP_RECOVERY_REQUIRED",
    "VOICE_DEEP_RECOVERY_BEGIN",
    "VOICE_DEEP_RECOVERY_OK",
    "VOICE_DEEP_RECOVERY_FAILED",
    "VOICE_TURN_BLOCKED_DEEP_RECOVERY",
    "VOICE_ACTIVITY_REBIND_UNSUPPORTED",
    "VOICE_FINAL_DEGRADED_MODE_ENTERED",
    "VOICE_FINAL_DEGRADED_MODE_ACTIVE",
    "LONGRUN_ABORTED_VOICE_FINAL_DEGRADED",
    "LONGRUN_GEMINI_READY_BUT_NO_AUDIO",
    "LONGRUN_VOICE_DEGRADATION_DETECTED",
    "GEMINI_WAKE_CYCLE_COUNT",
    "GEMINI_PREVENTIVE_REFRESH_ELIGIBLE",
    "GEMINI_PREVENTIVE_REFRESH_BEGIN",
    "GEMINI_PREVENTIVE_REFRESH_OK",
    "GEMINI_PREVENTIVE_REFRESH_FAILED",
]
ERROR_MARKERS = [
    "ReadError",
    "GEMINI_LIVE_1008_DETECTED",
    "AGENT_PROCESS_DIED",
    "SESSION_RUNTIME_ABORTED",
    "VOICE_READY_TO_SPEAK_TIMEOUT",
    "READY_TO_SPEAK_STUCK",
    "FIRST_CHUNK_HARD_TIMEOUT",
    "VOICE_AUDIO_STUTTER_DETECTED",
    "user_reply_generation_without_audio",
    "user_reply_generation_failed",
    "VOICE_FINAL_DEGRADED_MODE_ENTERED",
]
SAFE_CONFIG = {
    "gemini_standby_enabled": False,
    "gemini_standby_mode": "dry_run",
    "allow_real_gemini_standby": False,
    "wake_audio_enabled": False,
    "livekit_local_default": True,
    "livekit_cloud_disabled_by_default": True,
    "gemini_standby_after_minutes": 60,
    "return_to_economy_after_minutes": 60,
    "active_after_wake_grace_seconds": 180,
    "gemini_preventive_refresh_enabled": False,
    "gemini_refresh_after_wake_cycles": 4,
}
VALIDATION_CONFIG = {
    "gemini_standby_enabled": True,
    "gemini_standby_mode": "real",
    "allow_real_gemini_standby": True,
    "gemini_standby_after_minutes": 1,
    "return_to_economy_after_minutes": 1,
    "active_after_wake_grace_seconds": 180,
    "gemini_preventive_refresh_enabled": True,
    "gemini_refresh_after_wake_cycles": 4,
    "wake_text_enabled": True,
    "wake_audio_enabled": False,
}
NORMAL_MESSAGES = [
    "Kira, responde uma frase curta sobre tecnologia.",
    "Me explica em uma frase o que voce esta fazendo agora.",
    "Fala uma frase natural para testar sua voz.",
]
POST_WAKE_MESSAGES = [
    "Agora me responde uma frase curta.",
    "Diga em uma frase se voce voltou do standby.",
    "Fala uma frase natural depois do wake.",
]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def maybe_await(value):
    if inspect.isawaitable(value):
        return value

    async def _done():
        return value

    return _done()


class Diagnostics:
    def __init__(self, root: Path):
        self.root = root
        self.events_path = root / "events.jsonl"
        self.progress_path = root / "progress.json"
        self.report_path = root / "REPORT.md"
        self.snapshots = root / "snapshots"
        self.snapshots.mkdir(exist_ok=True)
        self.started_mono = time.monotonic()
        self.started_wall = now_iso()
        self.phase = "boot"
        self.status = "RUNNING"
        self.abort_requested = False
        self.abort_reason = ""
        self.failure_reasons = []
        self.metrics = {
            "cycles_completed": 0,
            "standby_entries": 0,
            "standby_ok": 0,
            "wake_query": 0,
            "wake_query_voice_ok": 0,
            "no_wake_ignored": 0,
            "normal_messages": 0,
            "post_wake_messages": 0,
            "voice_messages_expected": 0,
            "assistant_requested": 0,
            "first_chunk": 0,
            "finished": 0,
            "audio_confirmed": 0,
            "message_failures": 0,
            "latencies_first_chunk_ms": [],
            "latencies_finished_ms": [],
            "latencies_audio_confirmed_ms": [],
            "health_8091_ok": 0,
            "health_8091_fail": 0,
            "page_errors": 0,
            "cloud_active_markers": 0,
        }
        self.log_counts = defaultdict(int)
        self.error_lines = deque(maxlen=200)
        self.log_offsets = {}
        self.perf_samples = []

    def event(self, name: str, **data):
        payload = {"at": now_iso(), "elapsed_s": round(time.monotonic() - self.started_mono, 3), "event": name, **data}
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.write_progress(extra={name: data})

    def write_json(self, name: str, data):
        path = self.snapshots / name
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def write_progress(self, extra=None):
        payload = {
            "status": self.status,
            "phase": self.phase,
            "started_at": self.started_wall,
            "updated_at": now_iso(),
            "elapsed_s": round(time.monotonic() - self.started_mono, 1),
            "metrics": self.metrics,
            "log_counts": dict(self.log_counts),
            "failure_reasons": self.failure_reasons,
            "extra": extra or {},
        }
        self.progress_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def fail(self, reason: str):
        if reason not in self.failure_reasons:
            self.failure_reasons.append(reason)
        self.metrics["message_failures"] += 1
        self.event("FAILURE_RECORDED", reason=reason)


class LogWatcher:
    def __init__(self, project_root: Path, diag: Diagnostics):
        self.project_root = project_root
        self.diag = diag
        self.roots = [project_root / "logs"]
        self.positions = {}

    def _files(self):
        files = []
        for root in self.roots:
            if root.exists():
                files.extend(root.rglob("*.log"))
        return files

    def baseline(self):
        for path in self._files():
            try:
                self.positions[str(path)] = path.stat().st_size
            except OSError:
                pass
        self.diag.event("LOG_BASELINE_SET", files=len(self.positions))

    def scan(self):
        chunks = []
        for path in self._files():
            key = str(path)
            try:
                size = path.stat().st_size
                start = self.positions.get(key, 0)
                if size < start:
                    start = 0
                if size == start:
                    continue
                with path.open("rb") as fh:
                    fh.seek(start)
                    raw = fh.read(size - start)
                self.positions[key] = size
                text = raw.decode("utf-8", errors="replace")
                chunks.append(text)
            except OSError:
                continue
        if not chunks:
            return ""
        text = "\n".join(chunks)
        for line in text.splitlines():
            for marker in LOG_MARKERS:
                if marker in line:
                    self.diag.log_counts[marker] += 1
            if any(marker in line for marker in ERROR_MARKERS):
                self.diag.error_lines.append(line[-500:])
            lower = line.lower()
            if ("provider=cloud" in lower or "livekitproviderkind=cloud" in lower or "cloud provider ativo" in lower):
                self.diag.metrics["cloud_active_markers"] += 1
        return text


class PerfMonitor:
    def __init__(self, diag: Diagnostics):
        self.diag = diag
        self.stop = asyncio.Event()

    @staticmethod
    def category(proc: psutil.Process):
        try:
            name = (proc.name() or "").lower()
            cmd = " ".join(proc.cmdline()).lower()
            exe = (proc.exe() or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
        if "livekit-server" in name or "livekit-server" in exe or "livekit-server" in cmd:
            return "livekit-server.exe"
        if "python" in name:
            if "kira_app.py" in cmd or "backend" in cmd:
                return "backend_python"
            if "\\agent\\" in cmd or "/agent/" in cmd or "agent" in cmd:
                return "agent_python"
            if "kira v8" in cmd:
                return "python_other_project"
        if "webview" in name or "pywebview" in cmd or "msedgewebview" in name:
            return "WebView/PyWebView"
        return None

    async def run(self):
        for proc in psutil.process_iter(["pid"]):
            try:
                proc.cpu_percent(None)
            except Exception:
                pass
        while not self.stop.is_set():
            rows = []
            for proc in psutil.process_iter(["pid"]):
                category = self.category(proc)
                if not category:
                    continue
                try:
                    rows.append({
                        "at": now_iso(),
                        "elapsed_s": round(time.monotonic() - self.diag.started_mono, 1),
                        "phase": self.diag.phase,
                        "category": category,
                        "pid": proc.pid,
                        "name": proc.name(),
                        "cpu": proc.cpu_percent(None),
                        "rss_mb": round(proc.memory_info().rss / (1024 * 1024), 1),
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            self.diag.perf_samples.extend(rows)
            self.diag.write_progress()
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    def summarize(self):
        groups = defaultdict(lambda: {"cpu": [], "rss": []})
        for row in self.diag.perf_samples:
            key = (row["phase"], row["category"])
            groups[key]["cpu"].append(row["cpu"])
            groups[key]["rss"].append(row["rss_mb"])
        summary = []
        for (phase, category), vals in sorted(groups.items()):
            summary.append({
                "phase": phase,
                "category": category,
                "samples": len(vals["cpu"]),
                "cpu_avg": round(statistics.fmean(vals["cpu"]), 2) if vals["cpu"] else 0,
                "cpu_peak": round(max(vals["cpu"]), 2) if vals["cpu"] else 0,
                "ram_avg_mb": round(statistics.fmean(vals["rss"]), 1) if vals["rss"] else 0,
                "ram_peak_mb": round(max(vals["rss"]), 1) if vals["rss"] else 0,
            })
        return summary


async def http_get(client: httpx.AsyncClient, path_or_url: str):
    url = path_or_url if path_or_url.startswith("http") else BASE_URL + path_or_url
    response = await client.get(url)
    response.raise_for_status()
    return response.json()


async def http_post(client: httpx.AsyncClient, path: str, payload=None):
    response = await client.post(BASE_URL + path, json=payload or {})
    response.raise_for_status()
    return response.json()


async def publish_silence(source: rtc.AudioSource, stop: asyncio.Event):
    frame = rtc.AudioFrame(
        data=bytes(960 * 2),
        sample_rate=48000,
        num_channels=1,
        samples_per_channel=960,
    )
    while not stop.is_set():
        await maybe_await(source.capture_frame(frame))
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.02)
        except asyncio.TimeoutError:
            pass


async def wait_until(predicate, timeout_s: float, interval_s: float = 2.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = await predicate()
        if last:
            return last
        await asyncio.sleep(interval_s)
    return last


def count_delta(counts, baseline, marker):
    return counts.get(marker, 0) - baseline.get(marker, 0)


def port_snapshot(ports=(3000, 7880, 8081, 8091)):
    rows = []
    wanted = {int(port) for port in ports}
    for conn in psutil.net_connections(kind="inet"):
        local = getattr(conn, "laddr", None)
        if not local:
            continue
        port = int(getattr(local, "port", local[1] if len(local) > 1 else 0) or 0)
        if port not in wanted:
            continue
        rows.append(
            {
                "port": port,
                "status": str(getattr(conn, "status", "") or ""),
                "pid": int(getattr(conn, "pid", 0) or 0),
            }
        )
    return rows


def ports_are_free(ports=(3000, 7880, 8081, 8091)) -> bool:
    busy = [row for row in port_snapshot(ports) if row.get("status") == "LISTEN"]
    return not busy


def run_preboot_cleanup(project_root: Path, diag: Diagnostics, *, label: str) -> dict:
    current_pid = os.getpid()
    protected = {current_pid}
    try:
        protected.update(parent.pid for parent in psutil.Process(current_pid).parents())
    except Exception:
        pass
    project_text = str(project_root).lower()
    livekit_text = str(project_root / "livekit").lower()
    candidates = []
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        pid = int(proc.info.get("pid") or 0)
        if pid in protected:
            continue
        try:
            name = str(proc.info.get("name") or "").lower()
            exe = str(proc.info.get("exe") or "").lower()
            cmd = " ".join(proc.info.get("cmdline") or []).lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        is_livekit = "livekit-server" in name and (livekit_text in exe or livekit_text in cmd)
        is_backend = "python" in name and "kira_app.py" in cmd and project_text in cmd
        is_agent = "python" in name and ("\\agent\\" in cmd or "/agent/" in cmd) and project_text in cmd
        is_launcher = "cmd" in name and "kira.bat" in cmd and project_text in cmd
        if is_livekit or is_backend or is_agent or is_launcher:
            candidates.append(proc)
    terminated = []
    killed = []
    for proc in candidates:
        try:
            proc.terminate()
            terminated.append(proc.pid)
        except Exception:
            pass
    _, alive = psutil.wait_procs(candidates, timeout=8)
    for proc in alive:
        try:
            proc.kill()
            killed.append(proc.pid)
        except Exception:
            pass
    result = {
        "ok": True,
        "terminated": terminated,
        "killed": killed,
        "protected": sorted(protected),
        "ports": port_snapshot(),
    }
    diag.write_json(f"preboot_cleanup_{label}.json", result)
    diag.event(
        "PREBOOT_CLEANUP_RESULT",
        label=label,
        ok=result["ok"],
        terminated=terminated,
        killed=killed,
        ports=result["ports"],
    )
    return result


def start_kira(project_root: Path, diag: Diagnostics) -> subprocess.Popen:
    boot_log = diag.root / "kira_bat_runner.log"
    RUNNER_SETTINGS["boot_log"] = str(boot_log)
    log_handle = boot_log.open("w", encoding="utf-8", errors="replace")
    process = subprocess.Popen(
        ["cmd.exe", "/c", "Kira.bat"],
        cwd=str(project_root),
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    process._kira_log_handle = log_handle  # type: ignore[attr-defined]
    RUNNER_SETTINGS["started_kira"] = True
    diag.event("KIRA_BAT_STARTED", pid=process.pid, log=str(boot_log))
    return process


def stop_kira_process_tree(process: subprocess.Popen | None, diag: Diagnostics, *, project_root: Path) -> None:
    if process is not None and process.poll() is None:
        try:
            parent = psutil.Process(process.pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.terminate()
                except Exception:
                    pass
            try:
                parent.terminate()
            except Exception:
                pass
            gone, alive = psutil.wait_procs([parent, *children], timeout=8)
            for proc in alive:
                try:
                    proc.kill()
                except Exception:
                    pass
            diag.event("KIRA_PROCESS_TREE_STOPPED", pid=process.pid, gone=len(gone), alive=len(alive))
        except Exception as exc:
            diag.event("KIRA_PROCESS_TREE_STOP_FAILED", pid=getattr(process, "pid", None), error=str(exc))
    if process is not None:
        handle = getattr(process, "_kira_log_handle", None)
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
    run_preboot_cleanup(project_root, diag, label="final")


async def wait_for_backend_http(diag: Diagnostics, client: httpx.AsyncClient, timeout_s: float = 180):
    async def pred():
        try:
            state = await http_get(client, "/api/boot/state")
            return state
        except Exception:
            return None
    result = await wait_until(pred, timeout_s=timeout_s, interval_s=2)
    diag.write_json("backend_http_ready.json", result or {})
    return result


async def wait_for_voice(diag: Diagnostics, logs: LogWatcher, client: httpx.AsyncClient, cid: str, timeout_s=120):
    started = time.monotonic()
    baseline = dict(diag.log_counts)
    assistant_seen = False
    first_seen_at = None
    finished_seen_at = None
    confirmed_seen_at = None
    while time.monotonic() - started < timeout_s:
        logs.scan()
        if count_delta(diag.log_counts, baseline, "ASSISTANT_REPLY_REQUESTED source=text") > 0:
            assistant_seen = True
        if count_delta(diag.log_counts, baseline, "VOICE_OUTPUT_FIRST_CHUNK") > 0 and first_seen_at is None:
            first_seen_at = time.monotonic()
        if count_delta(diag.log_counts, baseline, "VOICE_OUTPUT_FINISHED") > 0 and finished_seen_at is None:
            finished_seen_at = time.monotonic()
        if count_delta(diag.log_counts, baseline, "VOICE_TEXT_TURN_AUDIO_CONFIRMED") > 0 and confirmed_seen_at is None:
            confirmed_seen_at = time.monotonic()
        if assistant_seen and first_seen_at and finished_seen_at and confirmed_seen_at:
            diag.metrics["assistant_requested"] += 1
            diag.metrics["first_chunk"] += 1
            diag.metrics["finished"] += 1
            diag.metrics["audio_confirmed"] += 1
            diag.metrics["latencies_first_chunk_ms"].append(round((first_seen_at - started) * 1000))
            diag.metrics["latencies_finished_ms"].append(round((finished_seen_at - started) * 1000))
            diag.metrics["latencies_audio_confirmed_ms"].append(round((confirmed_seen_at - started) * 1000))
            return {"ok": True, "cid": cid, "latency_first_chunk_ms": round((first_seen_at - started) * 1000)}
        await sample_health(diag, client)
        await asyncio.sleep(1)
    return {
        "ok": False,
        "cid": cid,
        "assistant_seen": assistant_seen,
        "first_chunk": bool(first_seen_at),
        "finished": bool(finished_seen_at),
        "audio_confirmed": bool(confirmed_seen_at),
    }


async def sample_health(diag: Diagnostics, client: httpx.AsyncClient):
    try:
        await http_get(client, HEALTH_8091)
        try:
            status_8091 = await http_get(client, STATUS_8091)
            if bool(status_8091.get("voice_degraded")):
                diag.event(
                    "GEMINI_CONTROL_8091_VOICE_DEGRADED",
                    reason=status_8091.get("voice_degraded_reason") or status_8091.get("reason"),
                    status=status_8091,
                )
        except Exception:
            pass
        diag.metrics["health_8091_ok"] += 1
        return True
    except Exception:
        diag.metrics["health_8091_fail"] += 1
        return False


FINAL_DEGRADED_REASONS = {
    "post_restart_warmup_failed_max_attempts",
    "deep_recovery_failed_after_warmup_timeouts",
    "agent_session_activity_rebind_unsupported_after_warmup_failure",
}


def voice_final_degraded_reason(status: dict) -> str:
    reason = str(
        status.get("voice_final_degraded_reason")
        or status.get("voice_degraded_reason")
        or status.get("reason")
        or ""
    ).strip()
    if bool(status.get("voice_final_degraded_mode")):
        return reason or "voice_final_degraded"
    if (
        not bool(status.get("voice_available"))
        and bool(status.get("voice_degraded"))
        and reason in FINAL_DEGRADED_REASONS
    ):
        return reason
    return ""


def abort_for_voice_final_degraded(diag: Diagnostics, *, label: str, status: dict) -> bool:
    reason = voice_final_degraded_reason(status)
    if not reason:
        return False
    diag.abort_requested = True
    diag.abort_reason = reason
    diag.event("LONGRUN_ABORTED_VOICE_FINAL_DEGRADED", label=label, reason=reason, status=status)
    diag.fail(f"voice_final_degraded:{reason}:{label}")
    return True


async def publish_chat(room: rtc.Room, text: str, cid: str):
    payload = json.dumps({"type": "chat", "text": text, "clientTurnId": cid}, ensure_ascii=False).encode("utf-8")
    await maybe_await(room.local_participant.publish_data(payload, reliable=True))


async def ensure_voice_available_before_send(
    diag: Diagnostics,
    client: httpx.AsyncClient,
    *,
    label: str,
    timeout_s: float = 12.0,
) -> bool:
    started = time.monotonic()
    first_status = None
    while time.monotonic() - started < timeout_s:
        try:
            status = await http_get(client, STATUS_8091)
        except Exception as exc:
            diag.event("VOICE_PRE_SEND_STATUS_FAILED", label=label, error=str(exc))
            await asyncio.sleep(0.5)
            continue
        if first_status is None:
            first_status = status
        voice_available = bool(status.get("voice_available"))
        voice_degraded = bool(status.get("voice_degraded"))
        if voice_available and not voice_degraded:
            if first_status is not status:
                diag.event("VOICE_PRE_SEND_RECOVERED", label=label, status=status)
            return True
        if abort_for_voice_final_degraded(diag, label=label, status=status):
            return False
        diag.event(
            "VOICE_NOT_AVAILABLE_BEFORE_SEND",
            label=label,
            reason=status.get("voice_degraded_reason") or status.get("reason") or "voice_not_available",
            status=status,
        )
        await asyncio.sleep(0.5)
    diag.event("VOICE_PRE_SEND_RECOVERY_TIMEOUT", label=label, status=first_status or {})
    return False


async def send_voice_message(diag: Diagnostics, logs: LogWatcher, client: httpx.AsyncClient, room: rtc.Room, text: str, label: str):
    cid = f"validation-{int(time.time() * 1000)}-{label}"
    if not await ensure_voice_available_before_send(diag, client, label=label):
        result = {"ok": False, "cid": cid, "reason": "voice_not_available_before_send"}
        diag.event("TEXT_DATA_CHANNEL_NOT_SENT", label=label, cid=cid, result=result)
        diag.fail(f"voice_not_available_before_send:{label}:{cid}")
        return result
    diag.metrics["voice_messages_expected"] += 1
    diag.event("TEXT_DATA_CHANNEL_SEND", label=label, cid=cid, text=text)
    await publish_chat(room, text, cid)
    result = await wait_for_voice(diag, logs, client, cid)
    diag.event("TEXT_DATA_CHANNEL_RESULT", label=label, cid=cid, result=result)
    if not result.get("ok"):
        diag.fail(f"voice_timeout:{label}:{cid}")
    return result


async def send_text_as_ui(diag: Diagnostics, logs: LogWatcher, client: httpx.AsyncClient, room: rtc.Room, text: str, label: str):
    """Mimic the UI text path: in standby, wake through HTTP before data channel."""
    try:
        status = await http_get(client, "/api/economy/status")
    except Exception:
        status = {}
    if not bool(status.get("economy_standby")):
        return await send_voice_message(diag, logs, client, room, text, label)

    wake_response = await http_post(client, "/api/chat/send", {"text": text, "source": "economy_standby_probe"})
    remaining = str(wake_response.get("remaining_user_query") or "").strip()
    diag.metrics["wake_query"] += 1
    diag.event("STANDBY_TEXT_HTTP_RESULT", label=label, response=wake_response, remaining=remaining)
    if wake_response.get("status") != "wake_with_query" or not remaining:
        diag.fail(f"standby_text_not_dispatched:{label}")
        return {"ok": False, "reason": "standby_text_not_dispatched", "response": wake_response}
    awake = await wait_for_awake(diag, logs, client)
    if not awake:
        diag.fail(f"standby_text_wake_not_ready:{label}")
        return {"ok": False, "reason": "standby_text_wake_not_ready", "response": wake_response}
    result = await send_voice_message(diag, logs, client, room, remaining, label)
    if result.get("ok"):
        diag.metrics["wake_query_voice_ok"] += 1
    return result


async def wait_for_functional_ready(diag: Diagnostics, client: httpx.AsyncClient, timeout_s=180):
    async def pred():
        try:
            state = await http_get(client, "/api/boot/state")
            ready = bool(state.get("functional_ready") or state.get("state", {}).get("functional_ready"))
            voice = bool(state.get("state", {}).get("voice_pipeline_ready") or state.get("state", {}).get("voice_ready") or state.get("functional_ready"))
            if ready and voice:
                return state
            return None
        except Exception:
            return None
    return await wait_until(pred, timeout_s=timeout_s, interval_s=2)


async def wait_for_can_start_session(diag: Diagnostics, client: httpx.AsyncClient, timeout_s=180):
    async def pred():
        try:
            state = await http_get(client, "/api/boot/state")
            can_start = bool(state.get("can_start_session") or state.get("state", {}).get("can_start_session"))
            phase = str(state.get("phase") or state.get("state", {}).get("phase") or "")
            if can_start or phase == "ready":
                return state
            return None
        except Exception:
            return None
    return await wait_until(pred, timeout_s=timeout_s, interval_s=2)


async def wait_for_standby(diag: Diagnostics, logs: LogWatcher, client: httpx.AsyncClient, label: str, timeout_s=420):
    diag.phase = "standby_wait"
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        logs.scan()
        try:
            status = await http_get(client, "/api/economy/status")
            await sample_health(diag, client)
            state = str(status.get("state") or "")
            standby = bool(status.get("economy_standby")) or state == "ECONOMY_LOCAL_GEMINI_STANDBY"
            gemini_active = bool(status.get("gemini_active"))
            voice_available = bool(status.get("voice_available"))
            if standby:
                diag.metrics["standby_entries"] += 1
                if not gemini_active and not voice_available:
                    diag.metrics["standby_ok"] += 1
                diag.event(
                    "STANDBY_REACHED",
                    label=label,
                    waited_s=round(time.monotonic() - started, 1),
                    state=state,
                    gemini_active=gemini_active,
                    voice_available=voice_available,
                    status=status,
                )
                return status
        except Exception as exc:
            diag.event("STANDBY_POLL_ERROR", label=label, error=str(exc))
        await asyncio.sleep(5)
    diag.fail(f"standby_timeout:{label}")
    return None


async def wait_for_awake(diag: Diagnostics, logs: LogWatcher, client: httpx.AsyncClient, timeout_s=60):
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        logs.scan()
        try:
            status = await http_get(client, "/api/economy/status")
            ready = await http_get(client, "/api/boot/state")
            economy_awake = not bool(status.get("economy_standby")) and bool(status.get("gemini_active"))
            backend_ready = bool(ready.get("functional_ready") or ready.get("ready"))
            if economy_awake and backend_ready:
                await asyncio.sleep(0.75)
                return {"status": status, "ready": ready, "waited_s": round(time.monotonic() - started, 1)}
        except Exception as exc:
            diag.event("AWAKE_POLL_ERROR", error=str(exc))
        await asyncio.sleep(1)
    return None


async def run_cycle(diag: Diagnostics, logs: LogWatcher, client: httpx.AsyncClient, room: rtc.Room, cycle_index: int):
    diag.event("CYCLE_BEGIN", cycle=cycle_index)
    diag.phase = "active"
    for index, text in enumerate(NORMAL_MESSAGES, start=1):
        if diag.abort_requested:
            return False
        diag.metrics["normal_messages"] += 1
        await send_text_as_ui(diag, logs, client, room, text, f"cycle{cycle_index}-normal{index}")
        if diag.abort_requested:
            return False

    standby = await wait_for_standby(diag, logs, client, f"cycle{cycle_index}-prewake")
    if not standby:
        return False

    diag.phase = "standby"
    baseline = dict(diag.log_counts)
    no_wake = await http_post(client, "/api/chat/send", {"text": "teste sem chamar", "source": "economy_standby_probe"})
    await asyncio.sleep(8)
    logs.scan()
    no_voice = count_delta(diag.log_counts, baseline, "VOICE_OUTPUT_FIRST_CHUNK") == 0
    no_gemini = count_delta(diag.log_counts, baseline, "ASSISTANT_REPLY_REQUESTED source=text") == 0
    if no_wake.get("gemini") is False and no_wake.get("voice") is False and no_voice and no_gemini:
        diag.metrics["no_wake_ignored"] += 1
    else:
        diag.fail(f"no_wake_not_ignored:cycle{cycle_index}")
    diag.event("NO_WAKE_RESULT", cycle=cycle_index, response=no_wake, no_voice=no_voice, no_gemini=no_gemini)

    wake_text = "Kira, responde uma frase curta dizendo que voltou."
    wake_response = await http_post(client, "/api/chat/send", {"text": wake_text, "source": "economy_standby_probe"})
    diag.metrics["wake_query"] += 1
    remaining = str(wake_response.get("remaining_user_query") or "").strip()
    diag.event("WAKE_QUERY_HTTP_RESULT", cycle=cycle_index, response=wake_response, remaining=remaining)
    if wake_response.get("status") != "wake_with_query" or not remaining:
        diag.fail(f"wake_query_http_failed:cycle{cycle_index}")
        return False

    diag.phase = "wake"
    awake = await wait_for_awake(diag, logs, client)
    if not awake:
        diag.fail(f"wake_query_not_awake:cycle{cycle_index}")
        return False
    wake_voice = await send_voice_message(diag, logs, client, room, remaining, f"cycle{cycle_index}-wakequery")
    if diag.abort_requested:
        return False
    if wake_voice.get("ok"):
        diag.metrics["wake_query_voice_ok"] += 1

    diag.phase = "post_wake"
    for index, text in enumerate(POST_WAKE_MESSAGES, start=1):
        if diag.abort_requested:
            return False
        diag.metrics["post_wake_messages"] += 1
        status_before = await http_get(client, "/api/economy/status")
        if bool(status_before.get("economy_standby")):
            diag.fail(f"standby_mid_postwake_before_message:cycle{cycle_index}:msg{index}")
        await send_voice_message(diag, logs, client, room, text, f"cycle{cycle_index}-postwake{index}")
        if diag.abort_requested:
            return False
        status_after = await http_get(client, "/api/economy/status")
        if bool(status_after.get("economy_standby")):
            diag.fail(f"standby_mid_postwake_after_message:cycle{cycle_index}:msg{index}")

    standby_after = await wait_for_standby(diag, logs, client, f"cycle{cycle_index}-after-last-activity")
    if not standby_after:
        return False
    diag.metrics["cycles_completed"] += 1
    diag.event("CYCLE_COMPLETE", cycle=cycle_index)
    return True


def build_report(diag: Diagnostics, perf_summary, final_config, final_status, final_boot, cleanup_note=""):
    elapsed = time.monotonic() - diag.started_mono
    lat = diag.metrics["latencies_first_chunk_ms"]
    status = "APROVADO"
    reasons = list(diag.failure_reasons)
    if diag.abort_requested:
        reasons.append(f"longrun_aborted_voice_final_degraded:{diag.abort_reason or 'unknown'}")
    expected_cycles = int(RUNNER_SETTINGS["cycles"])
    expected_voice = expected_cycles * (len(NORMAL_MESSAGES) + 1 + len(POST_WAKE_MESSAGES))
    if diag.metrics["cycles_completed"] < MIN_CYCLES:
        reasons.append(f"menos_de_{MIN_CYCLES}_ciclos")
    if diag.metrics["voice_messages_expected"] < expected_voice:
        reasons.append("voz_esperada_incompleta")
    if diag.metrics["first_chunk"] < diag.metrics["voice_messages_expected"]:
        reasons.append("first_chunk_incompleto")
    if diag.metrics["finished"] < diag.metrics["voice_messages_expected"]:
        reasons.append("finished_incompleto")
    if diag.metrics["audio_confirmed"] < diag.metrics["voice_messages_expected"]:
        reasons.append("audio_confirmed_incompleto")
    if diag.metrics["standby_ok"] < expected_cycles * 2:
        reasons.append("standby_ok_incompleto")
    if diag.metrics["wake_query_voice_ok"] < expected_cycles:
        reasons.append("wake_query_voice_incompleto")
    if diag.metrics["no_wake_ignored"] < expected_cycles:
        reasons.append("no_wake_ignored_incompleto")
    if diag.metrics["health_8091_fail"] > 0:
        reasons.append("health_8091_falhou")
    if diag.metrics["cloud_active_markers"] > 0:
        reasons.append("cloud_ativo_detectado")
    if RUNNER_SETTINGS["enable_preventive_refresh"] and diag.log_counts.get("GEMINI_PREVENTIVE_REFRESH_OK", 0) < 1:
        reasons.append("refresh_preventivo_nao_confirmado")
    for marker in ERROR_MARKERS:
        if diag.log_counts.get(marker, 0):
            reasons.append(f"marker:{marker}")
    recovery_ok = diag.log_counts.get("VOICE_DEGRADATION_RECOVERY_READY", 0) > 0
    degradation_detected = (
        diag.log_counts.get("LONGRUN_VOICE_DEGRADATION_DETECTED", 0) > 0
        or diag.log_counts.get("VOICE_AVAILABLE_FALSE reason=gemini_audio_degraded", 0) > 0
    )
    if reasons:
        status = "PARCIALMENTE APROVADO" if degradation_detected and recovery_ok else "NAO APROVADO"
    diag.status = status
    unique_reasons = []
    for reason in reasons:
        if reason not in unique_reasons:
            unique_reasons.append(reason)
    avg_latency = round(statistics.fmean(lat), 1) if lat else None
    max_latency = max(lat) if lat else None
    lines = [
        f"# Validacao real curta - Hotfix Long Run Voice Degradation",
        "",
        f"Status: **{status}**",
        f"Inicio: {diag.started_wall}",
        f"Fim: {now_iso()}",
        f"Duracao: {round(elapsed / 60, 2)} min ({round(elapsed, 1)} s)",
        f"Runner: scripts/run_short_longrun_voice_validation.py",
        f"Parametros: {json.dumps(RUNNER_SETTINGS, ensure_ascii=False)}",
        f"Ciclos completos: {diag.metrics['cycles_completed']}",
        "",
        "## Resultado",
        f"- Motivos: {', '.join(unique_reasons) if unique_reasons else 'criterios atendidos'}",
        f"- Standbys detectados: {diag.metrics['standby_entries']}",
        f"- Standbys OK: {diag.metrics['standby_ok']}",
        f"- Wake+query: {diag.metrics['wake_query']} enviados, {diag.metrics['wake_query_voice_ok']} com voz",
        f"- Textos sem wake ignorados: {diag.metrics['no_wake_ignored']}",
        f"- Mensagens normais: {diag.metrics['normal_messages']}",
        f"- Mensagens pos-wake: {diag.metrics['post_wake_messages']}",
        f"- Voz esperada: {diag.metrics['voice_messages_expected']}",
        f"- ASSISTANT_REPLY_REQUESTED: {diag.metrics['assistant_requested']}",
        f"- VOICE_OUTPUT_FIRST_CHUNK: {diag.metrics['first_chunk']}",
        f"- VOICE_OUTPUT_FINISHED: {diag.metrics['finished']}",
        f"- VOICE_TEXT_TURN_AUDIO_CONFIRMED: {diag.metrics['audio_confirmed']}",
        f"- Latencia media ate first_chunk: {avg_latency if avg_latency is not None else 'n/a'} ms",
        f"- Latencia maxima ate first_chunk: {max_latency if max_latency is not None else 'n/a'} ms",
        f"- 8091 health OK/fail: {diag.metrics['health_8091_ok']}/{diag.metrics['health_8091_fail']}",
        f"- Page errors: {diag.metrics['page_errors']} (sem internet/UI Playwright nesta validacao)",
        f"- Uso ativo de Cloud: {diag.metrics['cloud_active_markers']}",
        f"- Degradacao detectada: {'sim' if degradation_detected else 'nao'}",
        f"- Recovery de degradacao OK: {'sim' if recovery_ok else 'nao'}",
        f"- Refresh preventivo OK: {diag.log_counts.get('GEMINI_PREVENTIVE_REFRESH_OK', 0)}",
        "",
        "## Marcadores de log",
    ]
    for marker in LOG_MARKERS:
        lines.append(f"- {marker}: {diag.log_counts.get(marker, 0)}")
    lines.extend([
        "",
        "## Performance",
    ])
    for row in perf_summary:
        lines.append(
            f"- {row['phase']} / {row['category']}: samples={row['samples']} "
            f"cpu_avg={row['cpu_avg']} cpu_peak={row['cpu_peak']} "
            f"ram_avg_mb={row['ram_avg_mb']} ram_peak_mb={row['ram_peak_mb']}"
        )
    lines.extend([
        "",
        "## Estado final",
        "```json",
        json.dumps(
            {
                "config": final_config,
                "economy_status": final_status,
                "boot": final_boot,
                "ports_after_cleanup": port_snapshot(),
            },
            indent=2,
            ensure_ascii=False,
        ),
        "```",
        "",
        "## Erros recentes",
    ])
    if diag.error_lines:
        lines.extend([f"- {line}" for line in diag.error_lines])
    else:
        lines.append("- nenhum marcador critico coletado")
    if cleanup_note:
        lines.extend(["", "## Cleanup", cleanup_note])
    lines.extend([
        "",
        "## Recomendacao",
        "- Repetir validacao longa de 2h apenas se esta validacao curta ficar APROVADO.",
        "- Se ficar PARCIALMENTE APROVADO, revisar ciclo/turno da degradacao antes da validacao de 2h.",
        "- Se ficar NAO APROVADO, aplicar novo hotfix focado somente no motivo coletado.",
    ])
    diag.report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    diag.write_progress()
    return status, unique_reasons


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--diag")
    parser.add_argument("--output-dir")
    parser.add_argument("--cycles", type=int, default=7)
    parser.add_argument("--standby-minutes", type=int, default=1)
    parser.add_argument("--return-minutes", type=int, default=1)
    parser.add_argument("--active-after-wake-grace-seconds", type=int, default=180)
    parser.add_argument("--enable-preventive-refresh", action="store_true")
    parser.add_argument("--refresh-after-wake-cycles", type=int, default=4)
    parser.add_argument("--skip-start-kira", action="store_true")
    parser.add_argument("--skip-initial-cleanup", action="store_true")
    parser.add_argument("--reuse-functional-room", action="store_true")
    parser.add_argument("--backend-timeout-seconds", type=float, default=240.0)
    args = parser.parse_args()
    global MIN_CYCLES, TARGET_SECONDS, VALIDATION_CONFIG
    MIN_CYCLES = max(int(args.cycles), 1)
    TARGET_SECONDS = 0
    RUNNER_SETTINGS.update(
        {
            "cycles": MIN_CYCLES,
            "standby_minutes": max(int(args.standby_minutes), 1),
            "return_minutes": max(int(args.return_minutes), 1),
            "active_after_wake_grace_seconds": max(int(args.active_after_wake_grace_seconds), 0),
            "enable_preventive_refresh": bool(args.enable_preventive_refresh),
            "refresh_after_wake_cycles": max(int(args.refresh_after_wake_cycles), 1),
            "reuse_functional_room": bool(args.reuse_functional_room),
        }
    )
    VALIDATION_CONFIG = {
        **VALIDATION_CONFIG,
        "gemini_standby_after_minutes": RUNNER_SETTINGS["standby_minutes"],
        "return_to_economy_after_minutes": RUNNER_SETTINGS["return_minutes"],
        "active_after_wake_grace_seconds": RUNNER_SETTINGS["active_after_wake_grace_seconds"],
        "gemini_preventive_refresh_enabled": RUNNER_SETTINGS["enable_preventive_refresh"],
        "gemini_refresh_after_wake_cycles": RUNNER_SETTINGS["refresh_after_wake_cycles"],
        "wake_text_enabled": True,
        "wake_audio_enabled": False,
    }
    project_root = Path(__file__).resolve().parents[1]
    if args.output_dir:
        root = Path(args.output_dir).resolve()
    elif args.diag:
        root = Path(args.diag).resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        root = project_root / "diagnostics" / f"longrun_voice_degradation_short_validation_{stamp}"
    root.mkdir(parents=True, exist_ok=True)
    diag = Diagnostics(root)
    logs = LogWatcher(project_root, diag)
    perf = PerfMonitor(diag)
    perf_task = asyncio.create_task(perf.run())
    silence_stop = asyncio.Event()
    silence_task = None
    room = rtc.Room()
    final_config = {}
    final_status = {}
    final_boot = {}
    kira_process = None
    diag.event("RUNNER_BEGIN", project_root=str(project_root), settings=RUNNER_SETTINGS)
    if args.skip_initial_cleanup:
        diag.event("PREBOOT_CLEANUP_SKIPPED", label="initial", reason="external_kira_start")
    else:
        cleanup_initial = run_preboot_cleanup(project_root, diag, label="initial")
        if not cleanup_initial.get("ok"):
            diag.fail("initial_cleanup_failed")
    before_ports = port_snapshot()
    diag.write_json("ports_before_boot.json", before_ports)
    diag.event("PORTS_BEFORE_BOOT", ports=before_ports, free=ports_are_free())
    if not ports_are_free() and not args.skip_start_kira:
        diag.fail("ports_not_free_before_boot")
    if not args.skip_start_kira:
        kira_process = start_kira(project_root, diag)
    timeout = httpx.Timeout(20.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            backend_http = await wait_for_backend_http(diag, client, timeout_s=float(args.backend_timeout_seconds))
            if not backend_http:
                raise RuntimeError("backend_http_timeout")
            boot_initial = await http_get(client, "/api/boot/state")
            config_initial = await http_get(client, "/api/economy/config")
            diag.write_json("boot_initial.json", boot_initial)
            diag.write_json("economy_config_initial.json", config_initial)
            diag.event("BOOT_BASELINE", functional_ready=boot_initial.get("functional_ready"), can_start_session=boot_initial.get("can_start_session"))

            ready_pre_session = await wait_for_can_start_session(diag, client, timeout_s=float(args.backend_timeout_seconds))
            diag.write_json("can_start_session_before_session.json", ready_pre_session or {})
            if not ready_pre_session:
                raise RuntimeError("can_start_session_timeout_before_session")
            diag.event("CAN_START_SESSION_BEFORE_SESSION_CONFIRMED")

            participant = f"validation_user_{int(time.time())}"
            if args.reuse_functional_room:
                functional_room = str(
                    (boot_initial.get("state") or {}).get("room_name")
                    or boot_initial.get("room_name")
                    or (ready_pre_session.get("state") or {}).get("room_name")
                    or ready_pre_session.get("room_name")
                    or ""
                ).strip()
                if not functional_room:
                    raise RuntimeError("reuse_functional_room_missing_room_name")
                diag.event("SESSION_REUSE_FUNCTIONAL_ROOM", room=functional_room, participant=participant)
                session = await http_post(
                    client,
                    "/api/session/connection-details",
                    {"roomName": functional_room, "participantName": participant},
                )
                room_name = functional_room
            else:
                room_name = f"kira_validation_{int(time.time())}"
                session = await http_post(client, "/api/session/start", {"roomName": room_name, "participantName": participant})
            diag.write_json("session_start.json", session)
            url = session.get("serverUrl") or session.get("url")
            token = session.get("participantToken") or session.get("token")
            if not url or not token:
                raise RuntimeError("session_start_missing_url_or_token")
            diag.event("SESSION_STARTED", room=session.get("roomName") or room_name, url=url, provider=session.get("livekitProviderKind"))

            await maybe_await(room.connect(url, token))
            source = rtc.AudioSource(48000, 1)
            track = rtc.LocalAudioTrack.create_audio_track("validation_silent_mic", source)
            options = rtc.TrackPublishOptions()
            options.source = rtc.TrackSource.SOURCE_MICROPHONE
            await maybe_await(room.local_participant.publish_track(track, options))
            silence_task = asyncio.create_task(publish_silence(source, silence_stop))
            diag.event("SILENT_MIC_PUBLISHED")

            ready = await wait_for_functional_ready(diag, client, timeout_s=180)
            diag.write_json("functional_ready_snapshot.json", ready or {})
            if not ready:
                raise RuntimeError("functional_ready_timeout_after_single_session")
            diag.event("FUNCTIONAL_READY_CONFIRMED")

            logs.baseline()
            config_applied = await http_post(client, "/api/economy/config", VALIDATION_CONFIG)
            reset = await http_post(client, "/api/economy/reset-idle-timer", {"reason": "long_validation_start"})
            health = await http_get(client, HEALTH_8091)
            status_8091 = await http_get(client, STATUS_8091)
            status = await http_get(client, "/api/economy/status")
            diag.write_json("economy_config_validation.json", config_applied)
            diag.write_json("economy_reset_start.json", reset)
            diag.write_json("health_8091_start.json", health)
            diag.write_json("status_8091_start.json", status_8091)
            diag.write_json("economy_status_validation_start.json", status)
            diag.event("VALIDATION_CONFIG_APPLIED", status=status)
            real_standby = bool(status.get("real_gemini_standby_enabled") or status.get("gemini_standby_real_enabled"))
            idle_seconds = status.get("idle_seconds")
            diag.event(
                "VALIDATION_CONFIG_ASSERTIONS",
                real_gemini_standby_enabled=real_standby,
                idle_seconds=idle_seconds,
                health_8091_ok=bool(health.get("ok")),
                status_8091=status_8091,
                voice_available=status.get("voice_available"),
                voice_degraded=status.get("voice_degraded"),
            )
            if not real_standby:
                diag.fail("real_gemini_standby_not_confirmed")
            try:
                if idle_seconds is not None and float(idle_seconds) > 10.0:
                    diag.fail("idle_timer_not_reset_near_zero")
            except Exception:
                pass
            if not bool(health.get("ok")):
                diag.fail("health_8091_not_ok_after_config")

            cycle = 1
            while diag.metrics["cycles_completed"] < MIN_CYCLES:
                ok = await run_cycle(diag, logs, client, room, cycle)
                if not ok:
                    diag.event("CYCLE_ABORTED", cycle=cycle)
                    break
                cycle += 1

        except Exception as exc:
            diag.fail(f"runner_exception:{type(exc).__name__}:{exc}")
            diag.event("RUNNER_EXCEPTION", error=repr(exc))
        finally:
            diag.phase = "restore"
            try:
                final_config = await http_post(client, "/api/economy/config", SAFE_CONFIG)
                final_status = await http_get(client, "/api/economy/status")
                final_boot = await http_get(client, "/api/boot/state")
                diag.write_json("economy_config_restored.json", final_config)
                diag.write_json("economy_status_final.json", final_status)
                diag.write_json("boot_final_before_cleanup.json", final_boot)
                diag.event("SAFE_CONFIG_RESTORED")
            except Exception as exc:
                diag.fail(f"safe_config_restore_failed:{exc}")
            silence_stop.set()
            if silence_task:
                try:
                    await asyncio.wait_for(silence_task, timeout=3)
                except Exception:
                    pass
            try:
                await maybe_await(room.disconnect())
            except Exception:
                pass
            perf.stop.set()
            try:
                await asyncio.wait_for(perf_task, timeout=5)
            except Exception:
                pass
            perf_summary = perf.summarize()
            diag.write_json("performance_samples.json", diag.perf_samples)
            diag.write_json("performance_summary.json", perf_summary)
            stop_kira_process_tree(kira_process, diag, project_root=project_root)
            ports_after = port_snapshot()
            diag.write_json("ports_after_cleanup.json", ports_after)
            cleanup_note = f"Portas finais: {json.dumps(ports_after, ensure_ascii=False)}"
            build_report(diag, perf_summary, final_config, final_status, final_boot, cleanup_note=cleanup_note)
            diag.event("RUNNER_FINISHED", status=diag.status)


if __name__ == "__main__":
    asyncio.run(main())
