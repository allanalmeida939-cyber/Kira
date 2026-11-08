from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path

from services.manus_config import ManusConfig, get_available_key_slots, load_manus_config

logger = logging.getLogger(__name__)

_STATE_PATH = Path("runtime") / "manus" / "key_pool_state.json"


@dataclass
class ManusKeyState:
    slot: int
    env_name: str
    enabled: bool
    key_present: bool
    status: str
    fail_count: int
    success_count: int
    quota_exhausted_until: str | None
    last_error: str | None
    last_used_at: str | None
    key_fingerprint: str | None = None
    api_mode: str | None = None


@dataclass
class ManusKeyLease:
    slot: int
    env_name: str
    key_value: str


class ManusAllKeysFailedError(Exception):
    pass


class ManusKeyPool:
    def __init__(self, config: ManusConfig | None = None) -> None:
        self.config = config or load_manus_config()
        self.strategy = str(self.config.key_strategy or "fallback").lower()
        self.retry_auth_failed_keys = bool(getattr(self.config, "retry_auth_failed_keys", False)) or str(os.getenv("MANUS_RETRY_AUTH_FAILED_KEYS", "false")).strip().lower() in {"1", "true", "yes", "on"}
        self.reset_key_state_on_start = bool(getattr(self.config, "reset_key_state_on_start", True))
        self.states: dict[int, ManusKeyState] = {}
        self._values: dict[int, str] = {}
        self._last_rejections: list[dict[str, object]] = []
        self._load()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def key_fingerprint(key: str | None) -> str:
        if not key:
            return ""
        return hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:6]

    def _reset_slot_runtime_state(self, st: ManusKeyState) -> None:
        st.status = "healthy" if st.key_present and st.enabled else "disabled"
        st.fail_count = 0
        st.quota_exhausted_until = None
        st.last_error = None

    def _load(self) -> None:
        slots = get_available_key_slots(self.config)
        for s in self.config.api_keys:
            status = "healthy" if s.key_present and s.enabled else "disabled"
            fp = self.key_fingerprint(s.key_value)
            if fp:
                logger.info("MANUS_KEY_FINGERPRINT slot=%s fp=%s", s.slot, fp)
            self.states[s.slot] = ManusKeyState(
                slot=s.slot,
                env_name=s.env_name,
                enabled=bool(s.enabled),
                key_present=bool(s.key_present),
                status=status,
                fail_count=0,
                success_count=0,
                quota_exhausted_until=None,
                last_error=None,
                last_used_at=None,
                key_fingerprint=fp or None,
                api_mode=str(getattr(self.config, "api_mode", "v2_task_create") or "v2_task_create"),
            )
            if s.key_value:
                self._values[s.slot] = s.key_value
        self._hydrate_state_file()
        reset_slots = [s.slot for s in self.states.values() if s.key_present and s.enabled and s.status == "healthy"]
        if self.reset_key_state_on_start:
            logger.info("MANUS_KEY_STATE_RESET_ON_START enabled=true slots=%s", reset_slots)
        logger.info("MANUS_KEY_POOL_LOAD_OK keys_present=%s", [s.slot for s in slots])
        logger.info("MANUS_KEY_POOL_USABLE_SLOTS slots=%s", self.usable_slots())

    def _hydrate_state_file(self) -> None:
        if not _STATE_PATH.exists():
            return
        logger.info("MANUS_KEY_POOL_STATE_LOAD path=%s", str(_STATE_PATH))
        try:
            data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            by_slot = {int(x.get("slot")): x for x in (data.get("keys") or []) if isinstance(x, dict)}
            for slot, st in self.states.items():
                loaded = by_slot.get(slot)
                if not loaded:
                    continue
                loaded_status = str(loaded.get("status") or st.status)
                logger.info("MANUS_KEY_POOL_STATE_SLOT slot=%s state=%s", slot, loaded_status)
                loaded_fp = str(loaded.get("key_fingerprint") or "")
                loaded_mode = str(loaded.get("api_mode") or "")
                if st.key_fingerprint and loaded_fp != st.key_fingerprint:
                    logger.info("MANUS_CONFIG_RELOAD_REQUIRED old_fp=%s new_fp=%s", loaded_fp or "none", st.key_fingerprint)
                    logger.info("MANUS_CONFIG_RELOADED fp=%s", st.key_fingerprint)
                    logger.info("MANUS_KEY_FINGERPRINT_CHANGED slot=%s action=reset_state", slot)
                    logger.info("MANUS_KEY_STATE_RESET reason=fingerprint_changed")
                    continue
                if loaded_mode != str(st.api_mode or ""):
                    logger.info("MANUS_KEY_STATE_RESET slot=%s reason=key_changed_or_new_run", slot)
                    logger.info("MANUS_KEY_STATE_RESET reason=api_mode_changed slot=%s", slot)
                    continue
                if self.reset_key_state_on_start and loaded_status in {"auth_failed", "auth_error", "failed", "invalid", "blocked"}:
                    logger.info("MANUS_KEY_POOL_STATE_IGNORED slot=%s reason=stale_auth_error", slot)
                    logger.info("MANUS_KEY_STATE_RESET slot=%s reason=key_changed_or_new_run", slot)
                    continue
                st.status = str(loaded.get("status") or st.status)
                st.fail_count = int(loaded.get("fail_count") or st.fail_count)
                st.success_count = int(loaded.get("success_count") or st.success_count)
                st.quota_exhausted_until = loaded.get("quota_exhausted_until")
                st.last_error = loaded.get("last_error")
                st.last_used_at = loaded.get("last_used_at")
        except Exception:
            pass

    def _save(self) -> None:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"saved_at": self._now(), "keys": [asdict(v) for _, v in sorted(self.states.items())]}
        _STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("MANUS_KEY_POOL_STATE_SAVED")

    def usable_slots(self) -> list[int]:
        return [
            s.slot
            for s in sorted(self.states.values(), key=lambda x: x.slot)
            if s.key_present and s.enabled and self._blocked_reason(s) is None
        ]

    def _blocked_reason(self, st: ManusKeyState) -> str | None:
        if st.status == "disabled":
            return "disabled"
        if st.status == "auth_error" and not self.retry_auth_failed_keys:
            return "auth_failed_cached"
        if st.status in {"exhausted", "rate_limited", "failed"}:
            return f"{st.status}_cached"
        if st.quota_exhausted_until:
            try:
                until = datetime.fromisoformat(st.quota_exhausted_until)
                if datetime.now(timezone.utc) < until:
                    return "quota_retry_after_active"
            except Exception:
                pass
        return None

    def _is_blocked(self, st: ManusKeyState) -> bool:
        return self._blocked_reason(st) is not None

    def choose_key(self, job_id: str | None = None, exclude_slots: set[int] | None = None) -> ManusKeyLease | None:
        exclude_slots = exclude_slots or set()
        candidates = []
        rejected: list[dict[str, object]] = []
        for s in sorted(self.states.values(), key=lambda x: x.slot):
            logger.info(
                "MANUS_KEY_POOL_CANDIDATE slot=%s present=%s enabled=%s state=%s",
                s.slot,
                str(bool(s.key_present)).lower(),
                str(bool(s.enabled)).lower(),
                s.status,
            )
            if not s.key_present:
                rejected.append({"slot": s.slot, "reason": "missing_key"})
                logger.info("MANUS_KEY_POOL_CANDIDATE_REJECTED slot=%s reason=missing_key", s.slot)
                continue
            if not s.enabled:
                rejected.append({"slot": s.slot, "reason": "disabled"})
                logger.info("MANUS_KEY_POOL_CANDIDATE_REJECTED slot=%s reason=disabled", s.slot)
                continue
            if s.slot in exclude_slots:
                rejected.append({"slot": s.slot, "reason": "already_tried"})
                logger.info("MANUS_KEY_POOL_CANDIDATE_REJECTED slot=%s reason=already_tried", s.slot)
                continue
            reason = self._blocked_reason(s)
            if reason:
                rejected.append({"slot": s.slot, "reason": reason})
                logger.info("MANUS_KEY_POOL_CANDIDATE_REJECTED slot=%s reason=%s", s.slot, reason)
                logger.warning("MANUS_KEY_SKIPPED slot=%s reason=%s", s.slot, reason)
                continue
            logger.info("MANUS_KEY_POOL_CANDIDATE_ACCEPTED slot=%s", s.slot)
            candidates.append(s)
        self._last_rejections = rejected
        if not candidates:
            logger.warning("MANUS_KEY_POOL_ALL_FAILED slots_tried=%s rejected=%s", sorted(exclude_slots), rejected)
            logger.warning("MANUS_KEY_POOL_ALL_FAILED slots_tried=%s", sorted(exclude_slots))
            return None
        chosen = candidates[0]
        chosen.last_used_at = self._now()
        self._save()
        logger.info("MANUS_KEY_POOL_SELECTED slot=%s status=%s", chosen.slot, chosen.status)
        logger.info("MANUS_KEY_SELECTED slot=%s strategy=%s", chosen.slot, self.strategy or "fallback")
        return ManusKeyLease(slot=chosen.slot, env_name=chosen.env_name, key_value=self._values.get(chosen.slot, ""))

    def mark_success(self, slot: int) -> None:
        st = self.states.get(int(slot))
        if not st:
            return
        st.status = "healthy"
        st.success_count += 1
        st.last_error = None
        st.quota_exhausted_until = None
        self._save()

    def mark_quota_exhausted(self, slot: int, retry_after: int | None = None) -> None:
        st = self.states.get(int(slot))
        if not st:
            return
        st.status = "exhausted"
        st.fail_count += 1
        if retry_after is not None:
            st.quota_exhausted_until = (datetime.now(timezone.utc) + timedelta(seconds=max(1, int(retry_after)))).isoformat()
        logger.warning("MANUS_KEY_POOL_SLOT_FAILED slot=%s reason=exhausted", slot)
        self._save()

    def mark_rate_limited(self, slot: int, retry_after: int | None = None) -> None:
        st = self.states.get(int(slot))
        if not st:
            return
        st.status = "rate_limited"
        st.fail_count += 1
        if retry_after is not None:
            st.quota_exhausted_until = (datetime.now(timezone.utc) + timedelta(seconds=max(1, int(retry_after)))).isoformat()
        logger.warning("MANUS_KEY_POOL_SLOT_FAILED slot=%s reason=rate_limited", slot)
        self._save()

    def mark_auth_error(self, slot: int) -> None:
        st = self.states.get(int(slot))
        if not st:
            return
        st.status = "auth_error"
        st.fail_count += 1
        logger.warning("MANUS_KEY_POOL_SLOT_FAILED slot=%s reason=auth_error", slot)
        self._save()

    def mark_failure(self, slot: int, error: str) -> None:
        st = self.states.get(int(slot))
        if not st:
            return
        st.status = "failed"
        st.fail_count += 1
        st.last_error = str(error)
        logger.warning("MANUS_KEY_POOL_SLOT_FAILED slot=%s reason=failed", slot)
        self._save()

    def log_rotate(self, slot: int, next_slot: int | None) -> None:
        logger.info("MANUS_KEY_POOL_ROTATE slot=%s next_slot=%s", slot, str(next_slot or "none"))

    def get_state_snapshot(self) -> dict:
        return {"keys": [asdict(v) for _, v in sorted(self.states.items())]}
