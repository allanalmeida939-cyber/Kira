"""
CommandLearner — aprende comandos personalizados do usuario.
"""

import json
import logging
import re
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).parent / "data" / "commands.json"
SIMILARITY_THRESHOLD = 0.80
MIN_REPETITIONS = 2
REPETITION_WINDOW_S = 7 * 24 * 3600


class LearnedCommand:
    def __init__(self, pattern: str, action: str, action_data: dict):
        self.pattern = pattern
        self.action = action
        self.action_data = action_data
        self.times_used = 0
        self.created_at = time.time()
        self.last_used = None


class CommandLearner:
    def __init__(self, panel_executor=None, speech_pipeline=None):
        self._data = self._load()
        self._pending: dict[str, list] = defaultdict(list)
        self._panel_executor = panel_executor
        self._speech_pipeline = speech_pipeline

    def _load(self) -> dict:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        if DATA_FILE.exists():
            try:
                return json.loads(DATA_FILE.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("CMD_LOAD_FAILED error=%s", exc)
        return {"commands": {}, "pending": {}, "version": 1}

    def _save(self) -> None:
        try:
            DATA_FILE.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("CMD_SAVE_FAILED error=%s", exc)

    def _normalize_pattern(self, text: str) -> str:
        text = str(text or "").lower().strip()
        text = re.sub(r"[^\w\s]", "", text)
        return re.sub(r"\s+", " ", text)

    def _similarity(self, a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio()

    def observe_interaction(self, user_text: str, action_taken: str, action_data: dict | None = None) -> None:
        pattern = self._normalize_pattern(user_text)
        if not pattern or not action_taken:
            return
        now = time.time()
        for cmd_name, cmd in self._data.get("commands", {}).items():
            if self._similarity(pattern, str(cmd.get("pattern") or "")) >= SIMILARITY_THRESHOLD:
                cmd["times_used"] = int(cmd.get("times_used", 0)) + 1
                cmd["last_used"] = now
                logger.info("CMD_KNOWN_USED name=%r times=%d", cmd_name, cmd["times_used"])
                self._save()
                return

        pending = list(self._data.setdefault("pending", {}).get(pattern, []))
        pending.append({"ts": now, "action": action_taken, "action_data": action_data or {}})
        pending = [p for p in pending if now - float(p.get("ts", now)) < REPETITION_WINDOW_S]
        self._data["pending"][pattern] = pending
        same_action = [p for p in pending if p.get("action") == action_taken]
        if len(same_action) >= MIN_REPETITIONS:
            logger.info("CMD_PATTERN_CANDIDATE pattern=%r action=%r repetitions=%d", pattern, action_taken, len(same_action))
            self._propose_learning(pattern=pattern, action=action_taken, action_data=action_data or {}, repetitions=len(same_action))
            self._data["pending"].pop(pattern, None)
        self._save()

    def _propose_learning(self, pattern: str, action: str, action_data: dict, repetitions: int) -> None:
        cmd_name = pattern[:30].replace(" ", "_")
        self._data.setdefault("commands", {})[cmd_name] = {
            "pattern": pattern,
            "action": action,
            "action_data": action_data,
            "times_used": repetitions,
            "learned_at": time.time(),
            "last_used": time.time(),
        }
        logger.info("CMD_LEARNED name=%r pattern=%r action=%r", cmd_name, pattern, action)
        self._save()

    def match_command(self, text: str) -> dict | None:
        pattern = self._normalize_pattern(text)
        best_match = None
        best_score = 0.0
        for cmd_name, cmd in self._data.get("commands", {}).items():
            score = self._similarity(pattern, str(cmd.get("pattern") or ""))
            if score >= SIMILARITY_THRESHOLD and score > best_score:
                best_score = score
                best_match = dict(cmd)
                best_match["_name"] = cmd_name
                best_match["_score"] = score
        if best_match:
            logger.info("CMD_MATCHED name=%r score=%.2f pattern=%r", best_match["_name"], best_match["_score"], pattern)
        return best_match

    def register_command_manually(self, name: str, pattern: str, action: str, action_data: dict) -> None:
        self._data.setdefault("commands", {})[name] = {
            "pattern": self._normalize_pattern(pattern),
            "action": action,
            "action_data": action_data,
            "times_used": 0,
            "learned_at": time.time(),
            "last_used": None,
            "manual": True,
        }
        logger.info("CMD_REGISTERED_MANUALLY name=%r pattern=%r", name, pattern)
        self._save()

    def get_stats(self) -> dict:
        return {"learned_commands": len(self._data.get("commands", {})), "pending_patterns": len(self._data.get("pending", {}))}
