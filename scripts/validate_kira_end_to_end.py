import json
import time
from pathlib import Path
from urllib.error import URLError, HTTPError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
BACKEND_LOG_DIR = ROOT / "logs" / "Backend"
LOG_PATH = next(iter(sorted(BACKEND_LOG_DIR.glob("backend_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)), ROOT / "kira_backend.log")
OUTPUT_LOG_PATH = next(iter(sorted(BACKEND_LOG_DIR.glob("backend_output_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)), ROOT / "kira_backend_output.log")
REPORT_DIR = ROOT / "diagnostics" / "final_validation"
REPORT_PATH = REPORT_DIR / "RELATORIO_FINAL.txt"

EVENTS = [
    "PREBOOT_CLEANUP_DONE",
    "BACKEND_READY_PUBLISHED",
    "LIVEKIT_READY_PUBLISHED",
    "AGENT_WORKER_READY",
    "START_BUTTON_UNLOCKED",
    "SESSION_START_RESPONSE_OK",
    "UI_ROOM_CONNECTED",
    "UI_MIC_PUBLISHED_CONFIRMED",
    "SESSION_ROOM_CONNECTED",
    "GEMINI_READY",
    "VOICE_PIPELINE_READY",
    "SESSION_RUNTIME_READY",
    "FUNCTIONAL_READY",
    "FRONTEND_TRANSCRIPT_FINAL_RENDERED speaker=user",
    "FRONTEND_TRANSCRIPT_FINAL_RENDERED speaker=kira",
]


def read_url_json(url: str) -> dict:
    try:
        with urlopen(url, timeout=5) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except (URLError, HTTPError, TimeoutError, json.JSONDecodeError):
        return {}


def _latest_log_window_start() -> tuple[str, str]:
    if not OUTPUT_LOG_PATH.exists():
        return "", ""
    text = OUTPUT_LOG_PATH.read_text(encoding="utf-8", errors="replace")
    marker = "PREBOOT_CLEANUP_DONE"
    idx = text.rfind(marker)
    if idx == -1:
        return "", text
    return marker, text[idx:]


def scan_events() -> tuple[dict[str, bool], str]:
    if not LOG_PATH.exists():
        return {event: False for event in EVENTS}, ""
    _, output_window = _latest_log_window_start()
    text = LOG_PATH.read_text(encoding="utf-8", errors="replace")
    combined = "\n".join(part for part in (output_window, text) if part)
    if not combined:
        combined = text
    return {event: (event in combined) for event in EVENTS}, ("PREBOOT_CLEANUP_DONE" if output_window else "")


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    time.sleep(2.0)
    boot_state = read_url_json("http://127.0.0.1:3000/api/boot/state")
    system_status = read_url_json("http://127.0.0.1:3000/api/system-status")
    events, boot_marker = scan_events()

    lines = []
    lines.append("VALIDACAO_KIRA_END_TO_END")
    lines.append(f"timestamp={time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("boot_state=" + json.dumps(boot_state, ensure_ascii=False))
    lines.append("system_status=" + json.dumps(system_status, ensure_ascii=False))
    lines.append(f"VALIDATOR_BOOT_ID={int(time.time())}")
    lines.append(f"VALIDATOR_LOG_WINDOW_START={boot_marker or 'fallback_full_log'}")
    lines.append(f"VALIDATOR_UI_STILL_ALIVE={'true' if bool(system_status) else 'false'}")
    lines.append("")
    lines.append("events:")
    for event, present in events.items():
        lines.append(f"{event}={'sim' if present else 'nao'}")

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(str(REPORT_PATH))


if __name__ == "__main__":
    main()
