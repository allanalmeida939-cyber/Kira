"""Persistencia pequena e atômica da preferência de mídia da UI."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from threading import Lock


def _flag(value: object, default: bool = True) -> bool:
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off", "nao", "não"}


class UiMediaSettings:
    """Lê a preferência a cada consulta e troca o arquivo com ``os.replace``."""

    def __init__(self, path: str | os.PathLike[str], *, environment: dict[str, str] | None = None) -> None:
        self.path = Path(path)
        self.environment = environment if environment is not None else os.environ
        self._write_lock = Lock()

    @property
    def enabled(self) -> bool:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("enabled"), bool):
                return payload["enabled"]
        except (OSError, ValueError, TypeError):
            pass
        return _flag(self.environment.get("KIRA_MEDIA_ENABLED"), True)

    def set_enabled(self, enabled: bool) -> bool:
        if not isinstance(enabled, bool):
            raise ValueError("enabled deve ser booleano")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"enabled": enabled}
        with self._write_lock:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".ui_media-", suffix=".json", dir=self.path.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, self.path)
            except Exception:
                try:
                    os.unlink(temporary_name)
                except OSError:
                    pass
                raise
        return enabled


__all__ = ["UiMediaSettings"]
