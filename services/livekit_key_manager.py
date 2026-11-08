from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


log = logging.getLogger("livekit_key_manager")

DEFAULT_TOTAL_SLOTS = 4
DEFAULT_STATE_FILE = Path("logs/livekit_state.json")
LIVEKIT_SELFHOST_DEFAULTS = {
    "url": "ws://localhost:7880",
    "api_key": "devkey",
    "api_secret": "secret",
}

_LIVEKIT_STRONG_QUOTA_PATTERNS = (
    "participant minutes limit",
    "minutes limit reached",
    "free webrtc participant minutes",
    "upgrade required: livekit",
    "upgrade required livekit",
)
_LIVEKIT_SOFT_PATTERNS = (
    "upgrade required",
    "quota",
    "limit reached",
    "resource exhausted",
    "payment required",
)
_LIVEKIT_AUTH_PATTERNS = (
    "401",
    "invalid response status",
    "unauthorized",
    "invalid api key",
    "invalid credentials",
    "wss://",
)


def is_selfhost_mode(env: Mapping[str, str] | None = None) -> bool:
    """Detecta o modo LiveKit self-host local."""
    source_env = env if env is not None else os.environ
    url = str(source_env.get("LIVEKIT_URL", "") or "").strip().lower()
    return url.startswith("ws://localhost") or url.startswith("ws://127.0.0.1")


def is_livekit_quota_error(exc: Exception | str | object) -> bool:
    text = str(exc or "").strip().lower()
    if not text:
        return False
    if any(pattern in text for pattern in _LIVEKIT_STRONG_QUOTA_PATTERNS):
        return True
    # "livekit.plugins.google..." é só o nome do plugin no erro (ex.: 429 do TTS do Gemini), não a cota do LiveKit.
    text = re.sub(r"livekit\.plugins\.[\w.]+", "", text)
    return "livekit" in text and any(pattern in text for pattern in _LIVEKIT_SOFT_PATTERNS)


def is_livekit_auth_error(exc: Exception | str | object) -> bool:
    text = str(exc or "").strip().lower()
    if not text:
        return False
    return any(pattern in text for pattern in _LIVEKIT_AUTH_PATTERNS)


@dataclass(frozen=True, slots=True)
class LiveKitCredential:
    slot: int
    url: str
    api_key: str
    api_secret: str


class LiveKitKeyManager:
    def __init__(
        self,
        *,
        total_slots: int = DEFAULT_TOTAL_SLOTS,
        state_file: Path | str = DEFAULT_STATE_FILE,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._env = env if env is not None else os.environ
        self._selfhost_mode = is_selfhost_mode(self._env)
        self.total_slots = 1 if self._selfhost_mode else max(int(total_slots or DEFAULT_TOTAL_SLOTS), 1)
        self.state_file = Path(state_file)
        self.credentials = self._load_credentials()
        self.current_slot: int | None = None
        state = self._load_state()
        self.last_good_slot: int | None = self._coerce_slot(state.get("last_good_slot"))
        self.last_quota_slot: int | None = self._coerce_slot(state.get("last_quota_slot"))
        self.last_auth_failed_slot: int | None = self._coerce_slot(state.get("last_auth_failed_slot"))

    def _getenv(self, name: str) -> str:
        return str(self._env.get(name, "") or "").strip()

    def _coerce_slot(self, value: object) -> int | None:
        try:
            slot = int(value)
        except Exception:
            return None
        if 1 <= slot <= self.total_slots:
            return slot
        return None

    def _load_credentials(self) -> list[LiveKitCredential]:
        creds: list[LiveKitCredential] = []
        if self._selfhost_mode:
            url = self._getenv("LIVEKIT_URL") or LIVEKIT_SELFHOST_DEFAULTS["url"]
            key = self._getenv("LIVEKIT_API_KEY") or LIVEKIT_SELFHOST_DEFAULTS["api_key"]
            secret = self._getenv("LIVEKIT_API_SECRET") or LIVEKIT_SELFHOST_DEFAULTS["api_secret"]
            if url and key and secret:
                creds.append(
                    LiveKitCredential(
                        slot=1,
                        url=url,
                        api_key=key,
                        api_secret=secret,
                    )
                )
                log.info("LIVEKIT_SELFHOST_MODE url=%s", url)
            return creds
        for slot in range(1, self.total_slots + 1):
            url = self._getenv(f"LIVEKIT_{slot}_URL")
            key = self._getenv(f"LIVEKIT_{slot}_API_KEY")
            secret = self._getenv(f"LIVEKIT_{slot}_API_SECRET")
            if slot == 1:
                url = url or self._getenv("LIVEKIT_URL")
                key = key or self._getenv("LIVEKIT_API_KEY")
                secret = secret or self._getenv("LIVEKIT_API_SECRET")
            if url and key and secret:
                creds.append(
                    LiveKitCredential(
                        slot=slot,
                        url=url,
                        api_key=key,
                        api_secret=secret,
                    )
                )
        return creds

    def _load_state(self) -> dict[str, object]:
        try:
            if self.state_file.exists():
                raw = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return raw
        except Exception:
            log.exception("LIVEKIT_STATE_LOAD_FAILED")
        return {}

    def _save_state(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "last_good_slot": self.last_good_slot,
                "last_quota_slot": self.last_quota_slot,
                "last_auth_failed_slot": self.last_auth_failed_slot,
            }
            self.state_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            log.exception("LIVEKIT_STATE_SAVE_FAILED")

    def has_credentials(self) -> bool:
        return bool(self.credentials)

    def configured_slots(self) -> list[int]:
        return [cred.slot for cred in self.credentials]

    def preferred_start_slot(self, *, exclude_slots: set[int] | None = None) -> int:
        excluded = {self._coerce_slot(slot) for slot in (exclude_slots or set())}
        configured = [slot for slot in self.configured_slots() if slot not in excluded]
        if not configured:
            return 1

        preferred = self.last_good_slot or self._coerce_slot(self._getenv("LIVEKIT_ACTIVE_SLOT")) or 1
        if preferred not in configured:
            preferred = configured[0]

        avoid_slots = {
            slot
            for slot in (self.last_quota_slot, self.last_auth_failed_slot)
            if slot is not None and slot in configured
        }
        if preferred in avoid_slots and len(configured) > 1:
            start_index = configured.index(preferred)
            for offset in range(1, len(configured) + 1):
                candidate = configured[(start_index + offset) % len(configured)]
                if candidate not in avoid_slots:
                    return candidate
        return preferred

    def ordered_credentials(self, *, exclude_slots: set[int] | None = None) -> list[LiveKitCredential]:
        if not self.credentials:
            return []
        excluded = {self._coerce_slot(slot) for slot in (exclude_slots or set())}
        slots = {cred.slot: cred for cred in self.credentials if cred.slot not in excluded}
        if not slots:
            return []
        start = self.preferred_start_slot(exclude_slots=exclude_slots)
        ordered: list[LiveKitCredential] = []
        for offset in range(self.total_slots):
            slot = ((start - 1 + offset) % self.total_slots) + 1
            cred = slots.get(slot)
            if cred is not None and cred not in ordered:
                ordered.append(cred)
        return ordered

    def mark_current(self, slot: int | None) -> None:
        self.current_slot = self._coerce_slot(slot)

    def mark_quota_exceeded(self, slot: int | None = None) -> None:
        target_slot = self._coerce_slot(slot) or self.current_slot
        if target_slot is None:
            return
        self.last_quota_slot = target_slot
        self._save_state()
        log.warning("LIVEKIT_QUOTA_SLOT_MARKED slot=%s", target_slot)

    def mark_auth_failed(self, slot: int | None = None) -> None:
        target_slot = self._coerce_slot(slot) or self.current_slot
        if target_slot is None:
            return
        self.last_auth_failed_slot = target_slot
        self._save_state()
        log.warning("LIVEKIT_AUTH_SLOT_MARKED slot=%s", target_slot)

    def save_last_good_slot(self, slot: int) -> None:
        coerced = self._coerce_slot(slot)
        if coerced is None:
            return
        self.last_good_slot = coerced
        if self.last_quota_slot == coerced:
            self.last_quota_slot = None
        if self.last_auth_failed_slot == coerced:
            self.last_auth_failed_slot = None
        self._save_state()
        log.info("LIVEKIT_LAST_GOOD_SLOT_SAVED slot=%s", coerced)

    def current_credential(self) -> LiveKitCredential | None:
        if self.current_slot is None:
            return None
        for cred in self.credentials:
            if cred.slot == self.current_slot:
                return cred
        return None

    def get_status_payload(self) -> dict[str, object]:
        current_slot = self.current_slot
        return {
            "livekit_key": f"{current_slot if current_slot is not None else '-'}/{self.total_slots}",
            "livekit_current_slot": current_slot,
            "livekit_total_slots": self.total_slots,
            "livekit_mode": "selfhost" if self._selfhost_mode else "cloud",
            "livekit_last_good_slot": self.last_good_slot,
            "livekit_last_quota_slot": self.last_quota_slot,
            "livekit_last_auth_failed_slot": self.last_auth_failed_slot,
            "livekit_configured_slots": self.configured_slots(),
        }
