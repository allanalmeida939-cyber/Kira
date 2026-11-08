from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("kira.app")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = PROJECT_ROOT / "configs" / "standby_wake_ack_cache.json"

WAKE_ACK_PHRASES: dict[str, list[str]] = {
    "wake_normal": [
        "Oi chefe, to aqui. Pode falar.",
        "Aqui chefe, manda.",
        "To de volta. Pode falar.",
        "Oi Chefe, to aqui.",
    ],
    "wake_command": [
        "To aqui. Ja vou fazer isso.",
        "Ok chefe, ja resolvo.",
        "Pode deixar, vou executar.",
        "To aqui. Ja cuido disso.",
    ],
    "wake_question": [
        "To aqui, deixa eu ver isso pra voce.",
        "Aqui chefe, vou checar.",
        "Pode falar, vou analisar isso.",
        "To aqui. Me diz o que quer saber.",
    ],
    "wake_system": [
        "Oi chefe, eu tava em economia, mas ja voltei.",
        "Voltei do standby, chefe. Pode falar.",
        "To aqui de novo. Pode mandar.",
        "Oi chefe, tava economizando energia aqui, mas ja voltei.",
    ],
    "wake_long": [
        "Oi chefe, to aqui. Tava em modo economia, mas ja to voltando. Pode falar.",
        "Oi Chefe, to aqui. Dei uma olhada no sistema e ta tudo certo. Pode falar.",
    ],
}

ACTION_VERBS = {
    "abrir",
    "abre",
    "fechar",
    "fecha",
    "tocar",
    "toca",
    "colocar",
    "criar",
    "mandar",
    "enviar",
    "pesquisar",
    "procurar",
    "mostrar",
    "ocultar",
    "aumentar",
    "diminuir",
    "copiar",
    "colar",
    "escrever",
    "executar",
    "iniciar",
    "parar",
}
QUESTION_WORDS = {"como", "o que", "que", "quando", "onde", "qual", "quais", "por que", "porque", "quanto", "quem"}


@dataclass(slots=True)
class WakeAckPlaybackRequest:
    ok: bool
    category: str = ""
    phrase: str = ""
    audio_file: str = ""
    source: str = "wake_ack_cache"
    reason: str = ""


class StandbyWakeAudioCache:
    def __init__(self, *, cache_path: Path | str = CACHE_PATH) -> None:
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._last_category = ""
        self._last_generated_at = ""

    def classify(self, text: str, *, idle_seconds: float = 0.0) -> str:
        normalized = " ".join(str(text or "").strip().lower().split())
        if idle_seconds >= 3600:
            category = "wake_system"
        elif "?" in normalized or any(re.search(rf"\b{re.escape(word)}\b", normalized) for word in QUESTION_WORDS):
            category = "wake_question"
        elif any(re.search(rf"\b{re.escape(verb)}\b", normalized) for verb in ACTION_VERBS):
            category = "wake_command"
        else:
            category = "wake_normal"
        logger.info("STANDBY_WAKE_ACK_CLASSIFIED category=%s", category)
        return category

    def prepare_before_standby(self, *, config: dict[str, Any], idle_seconds: float = 0.0) -> dict[str, Any]:
        if not bool(config.get("standby_wake_ack_cache_enabled", False)):
            logger.info("STANDBY_WAKE_ACK_SKIPPED_INVALID reason=disabled")
            return {"ok": False, "reason": "disabled"}
        logger.info("STANDBY_WAKE_ACK_CACHE_BEGIN")
        category = "wake_system" if idle_seconds >= 3600 else "wake_normal"
        phrase = random.choice(WAKE_ACK_PHRASES[category])
        logger.info("STANDBY_WAKE_ACK_CACHE_PHRASE_SELECTED category=%s phrase=%r", category, phrase)
        logger.info("STANDBY_WAKE_ACK_CACHE_GENERATION_BEGIN category=%s", category)
        logger.warning("STANDBY_WAKE_ACK_AUDIO_CAPTURE_UNSUPPORTED")
        payload = self._payload(
            valid=False,
            category=category,
            phrase=phrase,
            reason="audio_capture_unsupported",
            ttl_minutes=int(config.get("standby_wake_ack_cache_ttl_minutes", 120) or 120),
        )
        self._write(payload)
        logger.warning("STANDBY_WAKE_ACK_CACHE_FAILED reason=audio_capture_unsupported")
        return {"ok": False, "reason": "audio_capture_unsupported", **payload}

    def request_playback(self, *, text: str, config: dict[str, Any], idle_seconds: float = 0.0) -> WakeAckPlaybackRequest:
        category = self.classify(text, idle_seconds=idle_seconds)
        self._last_category = category
        if not bool(config.get("standby_wake_ack_play_on_wake", True)):
            logger.info("STANDBY_WAKE_ACK_SKIPPED_INVALID reason=play_disabled")
            return WakeAckPlaybackRequest(ok=False, category=category, reason="play_disabled")
        payload = self.read()
        if not payload:
            logger.info("STANDBY_WAKE_ACK_SKIPPED_NO_CACHE")
            return WakeAckPlaybackRequest(ok=False, category=category, reason="no_cache")
        if self._is_expired(payload):
            logger.info("STANDBY_WAKE_ACK_CACHE_EXPIRED")
            logger.info("STANDBY_WAKE_ACK_SKIPPED_INVALID reason=expired")
            return WakeAckPlaybackRequest(ok=False, category=category, reason="expired")
        if not bool(payload.get("valid")):
            logger.info("STANDBY_WAKE_ACK_SKIPPED_INVALID reason=%s", payload.get("reason") or "invalid")
            return WakeAckPlaybackRequest(ok=False, category=category, reason=str(payload.get("reason") or "invalid"))
        audio_file = str(payload.get("audio_file") or "").strip()
        if not audio_file:
            logger.info("STANDBY_WAKE_ACK_SKIPPED_INVALID reason=missing_audio_file")
            return WakeAckPlaybackRequest(ok=False, category=category, reason="missing_audio_file")
        logger.info("STANDBY_WAKE_ACK_PLAY_REQUESTED source=wake_ack_cache category=%s", category)
        return WakeAckPlaybackRequest(
            ok=True,
            category=category,
            phrase=str(payload.get("phrase") or ""),
            audio_file=audio_file,
        )

    def get_status(self) -> dict[str, Any]:
        payload = self.read()
        valid = bool(payload.get("valid")) and not self._is_expired(payload) if payload else False
        return {
            "standby_wake_ack_cache_valid": valid,
            "standby_wake_ack_category": str((payload or {}).get("category") or self._last_category or ""),
            "standby_wake_ack_generated_at": str((payload or {}).get("generated_at") or self._last_generated_at or ""),
        }

    def read(self) -> dict[str, Any]:
        try:
            if not self.cache_path.exists():
                return {}
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception as exc:
            logger.warning("STANDBY_WAKE_ACK_CACHE_FAILED reason=read_failed detail=%s", exc)
            return {}

    def _write(self, payload: dict[str, Any]) -> None:
        self.cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._last_generated_at = str(payload.get("generated_at") or "")

    @staticmethod
    def _payload(
        *,
        valid: bool,
        category: str,
        phrase: str,
        reason: str,
        ttl_minutes: int,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "valid": bool(valid),
            "category": category,
            "phrase": phrase,
            "audio_file": "",
            "generated_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=max(int(ttl_minutes), 1))).isoformat(),
            "generated_by": "gemini_live_kore",
            "reason": reason,
        }

    @staticmethod
    def _is_expired(payload: dict[str, Any]) -> bool:
        try:
            raw = str(payload.get("expires_at") or "").strip()
            if not raw:
                return True
            expires_at = datetime.fromisoformat(raw)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            return time.time() >= expires_at.timestamp()
        except Exception:
            return True


_CACHE: StandbyWakeAudioCache | None = None


def get_standby_wake_audio_cache() -> StandbyWakeAudioCache:
    global _CACHE
    if _CACHE is None:
        _CACHE = StandbyWakeAudioCache()
    return _CACHE
