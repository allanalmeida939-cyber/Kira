"""
BehaviorTracker — rastreia padroes de comportamento por horario e contexto.
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).parent / "data" / "behaviors.json"


class BehaviorTracker:
    def __init__(self):
        self._data = self._load()

    def _load(self) -> dict:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        if DATA_FILE.exists():
            try:
                return json.loads(DATA_FILE.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("BEHAVIOR_LOAD_FAILED error=%s", exc)
        return {
            "hourly_patterns": {},
            "frequent_requests": {},
            "preferences": {
                "response_style": "casual",
                "proactive_level": "medium",
                "music_genres": [],
                "ignored_topics": [],
            },
            "sessions": [],
            "version": 1,
        }

    def _save(self) -> None:
        try:
            DATA_FILE.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("BEHAVIOR_SAVE_FAILED error=%s", exc)

    def record_action(self, action_type: str, context: str = "", metadata: dict | None = None) -> None:
        del metadata
        action_type = str(action_type or "").strip()
        if not action_type:
            return
        hour = datetime.now().strftime("%H")
        hourly = self._data.setdefault("hourly_patterns", {})
        hourly.setdefault(hour, {})
        hourly[hour][action_type] = int(hourly[hour].get(action_type, 0)) + 1
        freq = self._data.setdefault("frequent_requests", {})
        freq.setdefault(action_type, {"count": 0, "last_seen": None, "contexts": []})
        freq[action_type]["count"] = int(freq[action_type].get("count", 0)) + 1
        freq[action_type]["last_seen"] = time.time()
        if context:
            contexts = list(freq[action_type].get("contexts") or [])
            contexts.append(str(context)[:80])
            freq[action_type]["contexts"] = contexts[-10:]
        logger.debug("BEHAVIOR_RECORDED action=%s hour=%s count=%d", action_type, hour, freq[action_type]["count"])
        self._save()

    def get_proactive_suggestion(self) -> str | None:
        hour = datetime.now().strftime("%H")
        hourly = self._data.get("hourly_patterns", {}).get(hour, {})
        if not hourly:
            return None
        action, count = max(hourly.items(), key=lambda item: item[1], default=(None, 0))
        if not action or count < 3:
            return None
        suggestions = {
            "music": "Você costuma ouvir música nesse horário. Quer que eu coloque algo?",
            "music_play": "Você costuma ouvir música nesse horário. Quer que eu coloque algo?",
            "panel_hide": "Você costuma deixar a tela mais limpa nessa hora. Oculto os painéis?",
            "question": None,
        }
        return suggestions.get(action)

    def update_preference(self, key: str, value) -> None:
        self._data.setdefault("preferences", {})[key] = value
        logger.info("BEHAVIOR_PREF_UPDATED key=%s value=%s", key, value)
        self._save()

    def get_preference(self, key: str, default=None):
        return self._data.get("preferences", {}).get(key, default)

    def infer_response_style(self) -> str:
        return self._data.get("preferences", {}).get("response_style", "casual")

    def record_session_summary(self, duration_s: float, turn_count: int, topics: list[str]) -> None:
        self._data.setdefault("sessions", []).append({"ts": time.time(), "duration": duration_s, "turns": turn_count, "topics": topics[:5]})
        self._data["sessions"] = self._data["sessions"][-30:]
        self._save()

    def get_stats(self) -> dict:
        return {
            "tracked_hours": len(self._data.get("hourly_patterns", {})),
            "tracked_actions": len(self._data.get("frequent_requests", {})),
            "total_sessions": len(self._data.get("sessions", [])),
            "preferences": self._data.get("preferences", {}),
        }
