from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from itertools import product
from pathlib import Path
from typing import Any, Literal

if __package__ and __package__.startswith("agent"):
    from agent.settings import detect_quota_type, get_cooldown_minutes
else:  # pragma: no cover - suporte a execucao direta em agent/
    from settings import detect_quota_type, get_cooldown_minutes


logger = logging.getLogger(__name__)

KeyType = Literal["text", "tts"]

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "api_keys_config.json"
_PLACEHOLDER_FRAGMENTS = (
    "CHAVE_API_AQUI",
    "REPLACE_WITH",
    "SUA_CHAVE",
    "YOUR_KEY",
)
_KEY_TYPES: tuple[KeyType, KeyType] = ("text", "tts")
_GEMINI_QUOTA_PATTERNS = (
    "resource_exhausted",
    "quota exceeded",
    "429",
    "rate limit",
    "too many requests",
    "exceeded your current quota",
)
_GEMINI_KEY_PREFERENCE_ENV = "KIRA_GEMINI_KEY_PREFERENCE"
_GEMINI_KEY_PREFERENCE_ALIASES = {
    "gemini": "GEMINI_API_KEY",
    "gemini_api_key": "GEMINI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "google_api_key": "GOOGLE_API_KEY",
}


def _parse_iso_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def is_gemini_quota_error(exc: Exception | str | object) -> bool:
    """Detecta erros comuns de quota do Gemini."""
    text = str(exc or "").strip().lower()
    if not text:
        return False
    return any(pattern in text for pattern in _GEMINI_QUOTA_PATTERNS)


def _is_usable_key_value(value: str) -> bool:
    key = str(value or "").strip()
    if not key:
        return False
    upper_key = key.upper()
    return not any(fragment in upper_key for fragment in _PLACEHOLDER_FRAGMENTS)


def _preferred_legacy_env_name() -> str:
    raw = str(os.getenv(_GEMINI_KEY_PREFERENCE_ENV, "gemini_api_key") or "gemini_api_key").strip().lower()
    normalized = _GEMINI_KEY_PREFERENCE_ALIASES.get(raw)
    if normalized is None:
        logger.warning(
            "GEMINI_KEY_PREFERENCE_INVALID value=%s fallback=GEMINI_API_KEY",
            raw or "none",
        )
        return "GEMINI_API_KEY"
    return normalized


def _select_legacy_env_key() -> tuple[str, str | None, bool]:
    gemini_key = str(os.getenv("GEMINI_API_KEY", "") or "").strip()
    google_key = str(os.getenv("GOOGLE_API_KEY", "") or "").strip()
    gemini_present = _is_usable_key_value(gemini_key)
    google_present = _is_usable_key_value(google_key)
    preferred = _preferred_legacy_env_name()
    ambiguous = bool(gemini_present and google_present and gemini_key != google_key)
    selected_env = preferred if (preferred == "GEMINI_API_KEY" and gemini_present) or (preferred == "GOOGLE_API_KEY" and google_present) else ""
    if not selected_env:
        selected_env = "GEMINI_API_KEY" if gemini_present else ("GOOGLE_API_KEY" if google_present else "")
    if ambiguous:
        logger.warning(
            "GEMINI_KEY_ENV_AMBIGUOUS gemini_api_key_present=true google_api_key_present=true preference=%s selected=%s",
            preferred,
            selected_env or "none",
        )
    if selected_env:
        logger.info("GEMINI_KEY_ENV_SELECTED env=%s preference=%s", selected_env, preferred)
    if selected_env == "GEMINI_API_KEY":
        return gemini_key, selected_env, ambiguous
    if selected_env == "GOOGLE_API_KEY":
        return google_key, selected_env, ambiguous
    return "", None, ambiguous


class GeminiKeyManager:
    # gemini-2.0-flash e gemini-2.5-pro foram descontinuados (404 em todas as chaves em 2026-10-21): com eles na
    # rotação a pergunta visual esgotava as tentativas, esperava 60 s e a resposta estourava o tempo.
    VISION_MODELS = [
        "gemini-2.5-flash",
        "gemini-flash-latest",
    ]
    """
    Gerencia rotação de chaves Gemini com status separado para texto e TTS.
    """

    def __init__(
        self,
        config_path: str | os.PathLike[str] | None = None,
        *,
        keys: list[str] | None = None,
    ) -> None:
        self.config_path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_PATH
        self.config = self._load_config()
        env_keys = self._load_keys_from_env()
        source_keys = keys if keys is not None else (env_keys or self.config.get("gemini_api_keys", []))
        self._raw_keys: list[str] = [str(item or "").strip() for item in source_keys]
        if env_keys:
            self.config["gemini_api_keys"] = list(env_keys)
        self._usable_indexes = [
            index for index, key in enumerate(self._raw_keys) if self._is_usable_key(key)
        ]
        if not self._usable_indexes:
            raise ValueError(
                "Nenhuma chave Gemini utilizavel configurada em "
                f"{self.config_path}. Preencha gemini_api_keys com pelo menos uma chave real."
            )

        self.key_status = self._hydrate_key_status()
        self.current_text_index = self._resolve_initial_index(
            candidates=(
                self.config.get("last_working_text_key_index"),
                self.config.get("last_working_key_index"),
                self._usable_indexes[0],
            )
        )
        self.current_tts_index = self._resolve_initial_index(
            candidates=(
                self.config.get("last_working_tts_key_index"),
                self.current_text_index,
                self._usable_indexes[0],
            )
        )
        self._vision_cooldowns: dict[tuple[int, str], float] = {}
        self._vision_combination_cursor = 0
        logger.info(
            "[KEY_MANAGER] Iniciado com %s chave(s) utilizaveis de %s configuradas. "
            "text_index=%s tts_index=%s",
            len(self._usable_indexes),
            len(self._raw_keys),
            self.current_text_index,
            self.current_tts_index,
        )
        if env_keys:
            self._save_config()

    @staticmethod
    def _coerce_index(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except Exception:
            return None

    @classmethod
    def _is_usable_key(cls, value: str) -> bool:
        return _is_usable_key_value(value)

    def _load_keys_from_env(self) -> list[str]:
        keys: list[str] = []
        seen: set[str] = set()
        for slot in range(1, 5):
            key = str(os.getenv(f"GEMINI_{slot}_API_KEY", "") or "").strip()
            if slot == 1 and not key:
                key, selected_env, _ambiguous = _select_legacy_env_key()
                if selected_env:
                    logger.info("GEMINI_KEY_SLOT1_FROM_ENV selected=%s", selected_env)
            if self._is_usable_key(key) and key not in seen:
                seen.add(key)
                keys.append(key)
        return keys

    def _load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            logger.warning("[KEY_MANAGER] Arquivo de config nao encontrado: %s", self.config_path)
            return {
                "gemini_api_keys": [],
                "last_working_key_index": 0,
                "last_working_text_key_index": 0,
                "last_working_tts_key_index": 0,
                "last_rotation_timestamp": None,
                "last_text_rotation_timestamp": None,
                "last_tts_rotation_timestamp": None,
                "key_status": {},
            }
        try:
            for attempt in range(3):  # outro processo gravando agora: espera um instante e lê de novo
                try:
                    with self.config_path.open("r", encoding="utf-8") as handle:
                        data = json.load(handle)
                    return data if isinstance(data, dict) else {}
                except ValueError:
                    if attempt == 2:
                        raise
                    time.sleep(0.15)
        except Exception as exc:
            logger.error("[KEY_MANAGER] Erro ao carregar config %s: %s", self.config_path, exc)
            return {
                "gemini_api_keys": [],
                "last_working_key_index": 0,
                "last_working_text_key_index": 0,
                "last_working_tts_key_index": 0,
                "last_rotation_timestamp": None,
                "last_text_rotation_timestamp": None,
                "last_tts_rotation_timestamp": None,
                "key_status": {},
            }

    def _default_lane_status(self) -> dict[str, Any]:
        return {
            "usable": True,
            "cooldown_until": None,
            "last_failure": None,
            "failure_count": 0,
            "last_used": None,
            "last_reason": "",
        }

    def _hydrate_lane_status(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        data = dict(payload or {})
        status = self._default_lane_status()
        status["usable"] = bool(data.get("usable", True))
        status["cooldown_until"] = _parse_iso_datetime(data.get("cooldown_until"))
        status["last_failure"] = _parse_iso_datetime(data.get("last_failure"))
        status["failure_count"] = max(int(data.get("failure_count", 0) or 0), 0)
        status["last_used"] = _parse_iso_datetime(data.get("last_used"))
        status["last_reason"] = str(data.get("last_reason") or "").strip()
        return status

    def _hydrate_key_status(self) -> dict[int, dict[str, dict[str, Any]]]:
        persisted = self.config.get("key_status")
        persisted_map = persisted if isinstance(persisted, dict) else {}
        key_status: dict[int, dict[str, dict[str, Any]]] = {}
        for index in range(len(self._raw_keys)):
            saved_entry = persisted_map.get(str(index))
            if not isinstance(saved_entry, dict):
                saved_entry = {}
            key_status[index] = {
                "text": self._hydrate_lane_status(saved_entry.get("text")),
                "tts": self._hydrate_lane_status(saved_entry.get("tts")),
            }
        return key_status

    def _serialize_key_status(self) -> dict[str, dict[str, dict[str, Any]]]:
        payload: dict[str, dict[str, dict[str, Any]]] = {}
        for index, entry in self.key_status.items():
            payload[str(index)] = {}
            for key_type in _KEY_TYPES:
                lane = entry[key_type]
                payload[str(index)][key_type] = {
                    "usable": bool(lane.get("usable", True)),
                    "cooldown_until": _serialize_datetime(lane.get("cooldown_until")),
                    "last_failure": _serialize_datetime(lane.get("last_failure")),
                    "failure_count": int(lane.get("failure_count", 0) or 0),
                    "last_used": _serialize_datetime(lane.get("last_used")),
                    "last_reason": str(lane.get("last_reason") or ""),
                }
        return payload

    def _save_config(self) -> None:
        now = datetime.now().isoformat()
        self.config["last_working_key_index"] = self.current_text_index
        self.config["last_working_text_key_index"] = self.current_text_index
        self.config["last_working_tts_key_index"] = self.current_tts_index
        self.config["last_rotation_timestamp"] = now
        self.config["last_text_rotation_timestamp"] = now
        self.config["last_tts_rotation_timestamp"] = now
        self.config["key_status"] = self._serialize_key_status()
        self.config["gemini_api_keys"] = list(self._raw_keys)
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            # gravação atômica: quem lê no meio nunca vê o arquivo pela metade (2026-11-02 o backend leu vazio e
            # um save seguinte poderia apagar as chaves)
            temporary = self.config_path.with_suffix(f".json.{os.getpid()}.{threading.get_ident()}.tmp")  # um por processo e thread: duas threads com o mesmo .tmp davam WinError 2
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(self.config, handle, indent=2, ensure_ascii=False)
            for attempt in range(10):  # Windows: outro processo lendo agora segura o arquivo ("já está sendo usado")
                try:
                    os.replace(temporary, self.config_path)
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.05)
            logger.debug(
                "[KEY_MANAGER] Config salva em %s text_index=%s tts_index=%s",
                self.config_path,
                self.current_text_index,
                self.current_tts_index,
            )
        except Exception as exc:
            logger.error("[KEY_MANAGER] Erro ao salvar config %s: %s", self.config_path, exc)

    def _resolve_initial_index(self, *, candidates: tuple[object, ...]) -> int | None:
        for candidate in candidates:
            index = self._coerce_index(candidate)
            if index in self._usable_indexes:
                return index
        return self._usable_indexes[0]

    def _slot_number_for_index(self, index: int | None) -> int | None:
        if index is None:
            return None
        try:
            return self._usable_indexes.index(index) + 1
        except ValueError:
            return None

    def _index_attr_name(self, key_type: KeyType) -> str:
        return "current_tts_index" if key_type == "tts" else "current_text_index"

    def get_current_index(self, key_type: KeyType = "text") -> int | None:
        return getattr(self, self._index_attr_name(key_type))

    def _set_current_index(self, key_type: KeyType, index: int | None) -> None:
        setattr(self, self._index_attr_name(key_type), index)

    @property
    def current_key_index(self) -> int | None:
        return self.current_text_index

    @current_key_index.setter
    def current_key_index(self, value: int | None) -> None:
        self.current_text_index = value

    @property
    def total_keys(self) -> int:
        return len(self._usable_indexes)

    def _preview_key(self, key: str | None) -> str | None:
        if not key:
            return None
        return f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "***"

    def _vision_combinations(self) -> list[tuple[int, str]]:
        return list(product(self._usable_indexes, self.VISION_MODELS))

    def _prune_vision_cooldowns(self) -> None:
        now = time.time()
        expired = [
            combo
            for combo, until in self._vision_cooldowns.items()
            if float(until or 0.0) <= now
        ]
        for combo in expired:
            self._vision_cooldowns.pop(combo, None)

    def _is_vision_combination_in_cooldown(self, key_idx: int, model: str) -> bool:
        self._prune_vision_cooldowns()
        cooldown_until = float(self._vision_cooldowns.get((key_idx, model), 0.0) or 0.0)
        return cooldown_until > time.time()

    def get_next_vision_key_model(self) -> tuple[str, str, int] | None:
        """
        Retorna a proxima combinacao (api_key, model_name, key_index) disponivel.

        Ordem: chave1-modelo1, chave1-modelo2, chave1-modelo3, chave2-modelo1...
        """
        combinations = self._vision_combinations()
        if not combinations:
            logger.warning("[KEY_MANAGER] Nenhuma combinacao Vision configurada")
            return None

        start = self._vision_combination_cursor % len(combinations)
        for step in range(len(combinations)):
            position = (start + step) % len(combinations)
            key_idx, model = combinations[position]
            if self._is_vision_combination_in_cooldown(key_idx, model):
                continue

            self._vision_combination_cursor = (position + 1) % len(combinations)
            api_key = self._raw_keys[key_idx]
            logger.info(
                "[KEY_MANAGER] Vision key+model - key=%d/%d model=%s",
                self._slot_number_for_index(key_idx) or (key_idx + 1),
                self.total_keys,
                model,
            )
            return api_key, model, key_idx

        logger.warning("[KEY_MANAGER] Todas chaves+modelos Vision em cooldown")
        return None

    def mark_vision_combination_cooldown(
        self,
        key_idx: int,
        model: str,
        cooldown_seconds: int = 60,
    ) -> None:
        cooldown_key = (int(key_idx), str(model or "").strip())
        self._vision_cooldowns[cooldown_key] = time.time() + max(int(cooldown_seconds), 1)
        logger.warning(
            "[KEY_MANAGER] Vision cooldown - key=%d model=%s duration=%ds",
            self._slot_number_for_index(key_idx) or (key_idx + 1),
            model,
            cooldown_seconds,
        )

    def _is_key_usable_for(self, index: int | None, key_type: KeyType) -> bool:
        if index is None or index not in self._usable_indexes:
            return False
        lane_status = self.key_status[index][key_type]
        cooldown_until = lane_status.get("cooldown_until")
        if isinstance(cooldown_until, datetime) and datetime.now() < cooldown_until:
            return False
        if isinstance(cooldown_until, datetime) and datetime.now() >= cooldown_until:
            logger.info(
                "[KEY_MANAGER] Chave %s tipo=%s cooldown expirou; reabilitando.",
                index,
                key_type,
            )
            lane_status["usable"] = True
            lane_status["cooldown_until"] = None
        return bool(lane_status.get("usable", True))

    def available_keys(self, key_type: KeyType = "text") -> int:
        return sum(1 for index in self._usable_indexes if self._is_key_usable_for(index, key_type))

    def all_keys_in_cooldown(self, key_type: KeyType = "text") -> bool:
        return self.available_keys(key_type) == 0

    def _ensure_current_key(self, key_type: KeyType) -> int | None:
        index = self.get_current_index(key_type)
        if self._is_key_usable_for(index, key_type):
            return index
        rotated = self.rotate_to_next_key(key_type=key_type, reason=f"{key_type}_current_unavailable")
        if rotated is None:
            return None
        return rotated[1]

    def get_current_key(self) -> str:
        key = self.get_key("text")
        if key is None:
            raise RuntimeError("Nenhuma chave de texto disponivel.")
        return key

    def get_key(self, key_type: KeyType = "text") -> str | None:
        index = self._ensure_current_key(key_type)
        if index is None:
            return None
        self.key_status[index][key_type]["last_used"] = datetime.now()
        return self._raw_keys[index]

    def get_text_key(self) -> str | None:
        return self.get_key("text")

    def get_tts_key(self) -> str | None:
        return self.get_key("tts")

    def apply_current_key(
        self,
        key_type: KeyType = "text",
        *,
        update_env: bool | None = None,
    ) -> str | None:
        key = self.get_key(key_type)
        if key is None:
            return None
        should_update_env = (key_type == "text") if update_env is None else bool(update_env)
        if should_update_env:
            os.environ["GOOGLE_API_KEY"] = key
            logger.debug(
                "[KEY_MANAGER] GOOGLE_API_KEY atualizada para tipo=%s indice=%s",
                key_type,
                self.get_current_index(key_type),
            )
        return key

    def rotate_to_next_key(
        self,
        *,
        key_type: KeyType = "text",
        excluded_indexes: set[int] | None = None,
        reason: str = "manual_rotation",
    ) -> tuple[str, int] | None:
        excluded = set(excluded_indexes or ())
        current_index = self.get_current_index(key_type)
        total_raw = len(self._raw_keys)
        start = current_index if current_index is not None else -1

        for step in range(1, total_raw + 1):
            candidate = (start + step) % total_raw
            if candidate in excluded:
                continue
            if not self._is_key_usable_for(candidate, key_type):
                continue
            self._set_current_index(key_type, candidate)
            key = self.apply_current_key(key_type=key_type, update_env=(key_type == "text"))
            self._save_config()
            logger.warning(
                "[KEY_MANAGER] Rotacao aplicada tipo=%s indice=%s->%s motivo=%s disponiveis=%s/%s",
                key_type,
                current_index,
                candidate,
                reason,
                self.available_keys(key_type),
                len(self._usable_indexes),
            )
            return key or self._raw_keys[candidate], candidate

        self._set_current_index(key_type, None)
        self._save_config()
        logger.error(
            "[KEY_MANAGER] Nenhuma chave utilizavel restante tipo=%s motivo=%s",
            key_type,
            reason,
        )
        return None

    def mark_quota_exceeded(
        self,
        key_type: KeyType,
        *,
        reason: str = "429",
        cooldown_seconds: float | None = None,
    ) -> tuple[str, int] | None:
        index = self.get_current_index(key_type)
        if index is None:
            logger.error("[KEY_MANAGER] Sem indice atual para marcar quota exceeded tipo=%s", key_type)
            return None

        quota_type = detect_quota_type(reason)
        cooldown_minutes = get_cooldown_minutes(quota_type)
        lane_status = self.key_status[index][key_type]
        now = datetime.now()
        lane_status["usable"] = False
        effective_cooldown_seconds = (
            max(float(cooldown_seconds), 1.0)
            if cooldown_seconds is not None
            else float(cooldown_minutes * 60)
        )
        lane_status["cooldown_until"] = now + timedelta(seconds=effective_cooldown_seconds)
        lane_status["last_failure"] = now
        lane_status["failure_count"] = int(lane_status.get("failure_count", 0) or 0) + 1
        lane_status["last_reason"] = str(reason or "").strip() or quota_type
        self._save_config()

        logger.warning(
            "[KEY_MANAGER] Chave indice=%s tipo=%s marcada como quota_exceeded "
            "quota_type=%s cooldown_s=%.1f motivo=%s",
            index,
            key_type,
            quota_type,
            effective_cooldown_seconds,
            reason,
        )
        return self.rotate_to_next_key(
            key_type=key_type,
            excluded_indexes={index},
            reason=f"{key_type}_{quota_type}",
        )

    def handle_quota_error(
        self,
        error_type: KeyType = "tts",
        *,
        reason: str = "429",
        cooldown_seconds: float | None = None,
    ) -> bool:
        """Marca a chave atual em cooldown e tenta trocar para a proxima."""
        rotated = self.mark_quota_exceeded(
            error_type,
            reason=reason,
            cooldown_seconds=cooldown_seconds,
        )
        if rotated is None:
            logger.error(
                "[KEY_MANAGER] Todas as chaves estao em cooldown tipo=%s motivo=%s",
                error_type,
                reason,
            )
            return False
        _, next_index = rotated
        logger.info(
            "[KEY_MANAGER] Rotacao automatica concluida tipo=%s proximo_indice=%s",
            error_type,
            next_index,
        )
        return True

    def mark_key_as_working(self, key_type: KeyType = "text") -> None:
        index = self.get_current_index(key_type)
        if index is None:
            return
        lane_status = self.key_status[index][key_type]
        lane_status["usable"] = True
        lane_status["cooldown_until"] = None
        lane_status["last_reason"] = ""
        lane_status["last_used"] = datetime.now()
        self._save_config()
        logger.info(
            "[KEY_MANAGER] Chave funcional confirmada tipo=%s indice=%s",
            key_type,
            index,
        )

    def has_more_keys(
        self,
        *,
        key_type: KeyType = "text",
        excluded_indexes: set[int] | None = None,
    ) -> bool:
        excluded = set(excluded_indexes or ())
        current_index = self.get_current_index(key_type)
        for index in self._usable_indexes:
            if index == current_index or index in excluded:
                continue
            if self._is_key_usable_for(index, key_type):
                return True
        return False

    def reset_to_first_key(self, key_type: KeyType | None = None) -> None:
        targets = _KEY_TYPES if key_type is None else (key_type,)
        for lane in targets:
            self._set_current_index(lane, self._usable_indexes[0])
            self.key_status[self._usable_indexes[0]][lane]["usable"] = True
            self.key_status[self._usable_indexes[0]][lane]["cooldown_until"] = None
        self.apply_current_key("text", update_env=True)
        self._save_config()
        logger.info("[KEY_MANAGER] Reset para primeira chave utilizavel tipo=%s", key_type or "all")

    def get_status_report(self) -> dict[str, Any]:
        current_slot_number = self._slot_number_for_index(self.current_text_index)
        details: list[dict[str, Any]] = []
        for index in range(len(self._raw_keys)):
            entry = {
                "index": index,
                "configured": index < len(self._raw_keys),
                "usable_in_config": index in self._usable_indexes,
            }
            for key_type in _KEY_TYPES:
                lane = self.key_status[index][key_type]
                entry[key_type] = {
                    "usable": self._is_key_usable_for(index, key_type),
                    "cooldown_until": _serialize_datetime(lane.get("cooldown_until")),
                    "last_failure": _serialize_datetime(lane.get("last_failure")),
                    "failure_count": int(lane.get("failure_count", 0) or 0),
                    "last_reason": str(lane.get("last_reason") or ""),
                    "last_used": _serialize_datetime(lane.get("last_used")),
                }
            details.append(entry)

        text_key = self.get_text_key()
        tts_key = self.get_tts_key()
        return {
            "config_path": str(self.config_path),
            "configured_keys": len(self._raw_keys),
            "usable_keys": len(self._usable_indexes),
            "available_text_keys": self.available_keys("text"),
            "available_tts_keys": self.available_keys("tts"),
            "current_index": self.current_text_index,
            "current_text_index": self.current_text_index,
            "current_tts_index": self.current_tts_index,
            "gemini_current_slot": current_slot_number,
            "gemini_total_slots": len(self._usable_indexes),
            "gemini_key": (
                f"{current_slot_number}/{len(self._usable_indexes)}"
                if current_slot_number is not None and self._usable_indexes
                else f"-/{len(self._usable_indexes)}"
            ),
            "current_key_preview": self._preview_key(text_key),
            "current_text_key_preview": self._preview_key(text_key),
            "current_tts_key_preview": self._preview_key(tts_key),
            "all_text_keys_in_cooldown": self.all_keys_in_cooldown("text"),
            "all_tts_keys_in_cooldown": self.all_keys_in_cooldown("tts"),
            "key_details": details,
        }

    def get_stats(self) -> dict[str, Any]:
        return self.get_status_report()


_key_manager_instance: GeminiKeyManager | None = None


def get_key_manager() -> GeminiKeyManager:
    global _key_manager_instance
    if _key_manager_instance is None:
        _key_manager_instance = GeminiKeyManager()
    return _key_manager_instance


def _safe_get_key_manager(*, logger_override: logging.Logger | None = None) -> GeminiKeyManager | None:
    active_logger = logger_override or logger
    try:
        return get_key_manager()
    except Exception as exc:
        active_logger.warning("[KEY_MANAGER] Gerenciador indisponivel: %s", exc)
        return None


def activate_current_gemini_api_key(
    *,
    logger_override: logging.Logger | None = None,
    reason: str = "startup",
    key_type: KeyType = "text",
    update_env: bool | None = None,
) -> tuple[str | None, int | None]:
    active_logger = logger_override or logger
    manager = _safe_get_key_manager(logger_override=active_logger)
    if manager is not None:
        key = manager.apply_current_key(
            key_type=key_type,
            update_env=update_env,
        )
        index = manager.get_current_index(key_type)
        active_logger.info(
            "[KEY_MANAGER] Chave Gemini ativa tipo=%s indice=%s motivo=%s",
            key_type,
            index,
            reason,
        )
        return key, index

    fallback_key, fallback_env, _ambiguous = _select_legacy_env_key()
    fallback_key = fallback_key or None
    if fallback_key:
        active_logger.warning(
            "[KEY_MANAGER] Usando %s do ambiente sem rotacao tipo=%s motivo=%s",
            fallback_env or "GEMINI_API_KEY/GOOGLE_API_KEY",
            key_type,
            reason,
        )
    return fallback_key, None


def rotate_to_next_usable_gemini_api_key(
    *,
    excluded_indexes: set[int] | None = None,
    logger_override: logging.Logger | None = None,
    reason: str = "rate_limit",
    key_type: KeyType = "text",
) -> tuple[str, int] | None:
    active_logger = logger_override or logger
    manager = _safe_get_key_manager(logger_override=active_logger)
    if manager is None:
        return None
    rotated = manager.rotate_to_next_key(
        key_type=key_type,
        excluded_indexes=excluded_indexes,
        reason=reason,
    )
    if rotated is None:
        active_logger.error(
            "[KEY_MANAGER] Nao ha mais chaves Gemini utilizaveis tipo=%s motivo=%s",
            key_type,
            reason,
        )
        return None
    key, index = rotated
    active_logger.warning(
        "[KEY_MANAGER] Rotacao aplicada tipo=%s indice=%s motivo=%s",
        key_type,
        index,
        reason,
    )
    return key, index


def mark_current_gemini_key_working(
    *,
    logger_override: logging.Logger | None = None,
    key_type: KeyType = "text",
) -> None:
    active_logger = logger_override or logger
    manager = _safe_get_key_manager(logger_override=active_logger)
    if manager is None:
        return
    manager.mark_key_as_working(key_type=key_type)


def mark_current_gemini_key_quota_exceeded(
    *,
    logger_override: logging.Logger | None = None,
    key_type: KeyType = "text",
    reason: str = "429",
) -> tuple[str, int] | None:
    active_logger = logger_override or logger
    manager = _safe_get_key_manager(logger_override=active_logger)
    if manager is None:
        return None
    return manager.mark_quota_exceeded(key_type, reason=reason)


def get_gemini_key_stats(*, logger_override: logging.Logger | None = None) -> dict[str, Any]:
    active_logger = logger_override or logger
    manager = _safe_get_key_manager(logger_override=active_logger)
    if manager is None:
        fallback_key, fallback_env, _ambiguous = _select_legacy_env_key()
        return {
            "config_path": str(DEFAULT_CONFIG_PATH),
            "configured_keys": 1 if fallback_key else 0,
            "usable_keys": 1 if fallback_key else 0,
            "available_text_keys": 1 if fallback_key else 0,
            "available_tts_keys": 1 if fallback_key else 0,
            "current_index": None,
            "current_text_index": None,
            "current_tts_index": None,
            "current_key_preview": "***" if fallback_key else None,
            "current_text_key_preview": "***" if fallback_key else None,
            "current_tts_key_preview": "***" if fallback_key else None,
            "selected_env": fallback_env,
            "all_text_keys_in_cooldown": False,
            "all_tts_keys_in_cooldown": False,
            "key_details": [],
        }
    return manager.get_stats()
