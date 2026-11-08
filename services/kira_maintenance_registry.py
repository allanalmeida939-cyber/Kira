from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger("kira.maintenance")

OFFICIAL_ENTRYPOINTS = [
    "Kira.bat",
    "kira_app.py",
    "agent/agent.py",
    "interface/index.html",
]

OPTIONAL_SERVICES = [
    "messages/main.py",
    "services/youtube_music_service.py",
    "services/youtube_audio.py",
    "services/system_vitals.py",
]

LEGACY_PATHS = [
    "_legacy_disabled/",
    "restore_points/",
    "agent/local_memory/",
]

PROTECTED_PATHS = [
    "Kira.bat",
    "boot_manager.py",
    "interface/index.html",
    "agent/agent.py",
    "agent/runtime/speech_pipeline.py",
    "agent/runtime/speech_queue.py",
    "agent/runtime/turn_coordinator.py",
    "agent/runtime/voice_state_machine.py",
    "agent/runtime/voice_watchdog.py",
    "agent/runtime/audio_confirmation.py",
    "messages/main.py",
    "messages/messages_controller.py",
    "messages/messages_bridge_server.py",
    "_legacy_disabled/",
    "restore_points/",
]

RUNTIME_PORTS = {
    3000: "FastAPI/UI",
    7880: "LiveKit",
    8081: "Agent probe",
    8092: "Vision control",
    5050: "Messages bridge",
    5100: "Messages admin",
    8765: "legado AvatarBridge/token antigo",
}


def collect_maintenance_snapshot() -> dict:
    snapshot = {
        "official_entrypoints": list(OFFICIAL_ENTRYPOINTS),
        "optional_services": list(OPTIONAL_SERVICES),
        "legacy_paths": list(LEGACY_PATHS),
        "protected_paths": list(PROTECTED_PATHS),
        "runtime_ports": dict(RUNTIME_PORTS),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info("BLOCO7_MAINTENANCE_SNAPSHOT_BUILT")
    return snapshot
