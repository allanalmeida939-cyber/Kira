from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any


MAX_ACTIVE_ITEMS = 2000
MAX_DELETED_HISTORY = 4000
MAX_ACTIVITY_LOG_SIZE_BYTES = 5 * 1024 * 1024
MAX_FOLDER_SNAPSHOTS_PER_FOLDER = 8
MAX_CHILDREN_PER_SNAPSHOT = 500


class NullLocalMemoryStore:
    """No-op store used when cloud memory is the primary provider."""

    def get_last_session_summary(self) -> dict[str, Any]:
        return {}

    def get_user_profile(self) -> dict[str, Any]:
        return {}

    def mark_missing_active_paths_as_deleted(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        if name.startswith(("record_", "update_", "set_", "save_", "append_", "add_")):
            return lambda *args, **kwargs: None
        if name.startswith("get_"):
            return lambda *args, **kwargs: {}
        if name.startswith(("list_", "query_", "search_", "find_")):
            return lambda *args, **kwargs: []
        raise AttributeError(name)


def _cloud_memory_primary() -> bool:
    return os.getenv("KIRA_MEMORY_PROVIDER", "mem0").strip().lower() == "mem0"


def _now_ts() -> float:
    return time.time()


def _safe_name(path: Path) -> str:
    return path.name.strip() or str(path)


def _normalize_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


class LocalMemoryStore:
    def __init__(self, *, base_dir: Path | None = None) -> None:
        default_base = Path(__file__).resolve().parents[2] / "memory" / "local" / "operational"
        self._base_dir = Path(base_dir) if base_dir is not None else default_base
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy_data()
        self._lock = threading.RLock()

        self._files_index_path = self._base_dir / "files_index.json"
        self._folders_index_path = self._base_dir / "folders_index.json"
        self._deleted_history_path = self._base_dir / "deleted_items_history.json"
        self._folder_snapshots_path = self._base_dir / "folder_snapshots.json"
        self._session_context_path = self._base_dir / "session_context.json"
        self._last_session_summary_path = self._base_dir / "last_session_summary.json"
        self._user_profile_path = self._base_dir / "user_profile.json"
        self._activity_log_path = self._base_dir / "activity_log.jsonl"

        self._files_index: dict[str, dict[str, Any]] = {}
        self._folders_index: dict[str, dict[str, Any]] = {}
        self._deleted_history: list[dict[str, Any]] = []
        self._folder_snapshots: dict[str, list[dict[str, Any]]] = {}
        self._session_context: dict[str, Any] = {}
        self._last_session_summary: dict[str, Any] = {}
        self._user_profile: dict[str, Any] = {}
        self._load_all()

    def _migrate_legacy_data(self) -> None:
        legacy_dir = Path(__file__).resolve().parent / "local_memory"
        if not legacy_dir.exists() or legacy_dir.resolve() == self._base_dir.resolve():
            return
        for legacy_file in legacy_dir.iterdir():
            target = self._base_dir / legacy_file.name
            if legacy_file.is_file() and not target.exists():
                try:
                    shutil.copy2(legacy_file, target)
                except Exception:
                    pass

    def _default_session_context(self) -> dict[str, Any]:
        return {
            "session_id": "",
            "started_at": 0.0,
            "ended_at": 0.0,
            "user_id": "",
            "last_project_path": "",
            "last_working_folder": "",
            "recent_actions": [],
            "recent_files": [],
            "recent_folders": [],
            "pending_notes": [],
            "status": "idle",
        }

    def _load_json(self, path: Path, default_value: Any) -> Any:
        if not path.exists():
            return default_value
        try:
            with path.open("r", encoding="utf-8") as fp:
                loaded = json.load(fp)
            return loaded
        except Exception:
            # backup and recreate minimal structure
            try:
                broken = path.with_suffix(path.suffix + ".broken")
                path.replace(broken)
            except Exception:
                pass
            return default_value

    def _atomic_write_json(self, path: Path, payload: Any) -> None:
        tmp = path.with_suffix(path.suffix + f".tmp-{uuid.uuid4().hex}")
        with tmp.open("w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        tmp.replace(path)

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")
        self._rotate_activity_log_if_needed()

    def _rotate_activity_log_if_needed(self) -> None:
        if not self._activity_log_path.exists():
            return
        if self._activity_log_path.stat().st_size <= MAX_ACTIVITY_LOG_SIZE_BYTES:
            return
        try:
            with self._activity_log_path.open("r", encoding="utf-8") as fp:
                lines = fp.readlines()[-2000:]
            self._atomic_write_json(self._activity_log_path.with_suffix(".json.tmp"), {"lines": lines})
            # convert back to jsonl file
            tmp_json = self._activity_log_path.with_suffix(".json.tmp")
            payload = self._load_json(tmp_json, {"lines": []})
            tmp_json.unlink(missing_ok=True)
            with self._activity_log_path.open("w", encoding="utf-8") as fp:
                for line in payload.get("lines", []):
                    fp.write(str(line).rstrip("\n") + "\n")
        except Exception:
            return

    def _load_all(self) -> None:
        with self._lock:
            files_index = self._load_json(self._files_index_path, {})
            self._files_index = files_index if isinstance(files_index, dict) else {}
            folders_index = self._load_json(self._folders_index_path, {})
            self._folders_index = folders_index if isinstance(folders_index, dict) else {}
            deleted_history = self._load_json(self._deleted_history_path, [])
            self._deleted_history = deleted_history if isinstance(deleted_history, list) else []
            folder_snapshots = self._load_json(self._folder_snapshots_path, {})
            self._folder_snapshots = folder_snapshots if isinstance(folder_snapshots, dict) else {}
            session_context = self._load_json(self._session_context_path, self._default_session_context())
            self._session_context = session_context if isinstance(session_context, dict) else self._default_session_context()
            last_summary = self._load_json(self._last_session_summary_path, {})
            self._last_session_summary = last_summary if isinstance(last_summary, dict) else {}
            user_profile = self._load_json(self._user_profile_path, {})
            self._user_profile = user_profile if isinstance(user_profile, dict) else {}
            self._sanitize_indexes()
            self._persist_indexes()

    def _persist_indexes(self) -> None:
        self._atomic_write_json(self._files_index_path, self._files_index)
        self._atomic_write_json(self._folders_index_path, self._folders_index)
        self._atomic_write_json(self._deleted_history_path, self._deleted_history[-MAX_DELETED_HISTORY:])
        self._atomic_write_json(self._folder_snapshots_path, self._folder_snapshots)
        self._atomic_write_json(self._session_context_path, self._session_context)
        self._atomic_write_json(self._last_session_summary_path, self._last_session_summary)
        self._atomic_write_json(self._user_profile_path, self._user_profile)

    def _sanitize_indexes(self) -> None:
        now = _now_ts()
        self._files_index = {
            key: value
            for key, value in self._files_index.items()
            if isinstance(value, dict) and value.get("path")
        }
        self._folders_index = {
            key: value
            for key, value in self._folders_index.items()
            if isinstance(value, dict) and value.get("path")
        }
        self._deleted_history = [
            item for item in self._deleted_history
            if isinstance(item, dict) and item.get("path")
        ][-MAX_DELETED_HISTORY:]
        sanitized_snaps: dict[str, list[dict[str, Any]]] = {}
        for folder, snapshots in self._folder_snapshots.items():
            if not isinstance(snapshots, list):
                continue
            valid = [snap for snap in snapshots if isinstance(snap, dict) and snap.get("folder_path")]
            if valid:
                sanitized_snaps[folder] = valid[-MAX_FOLDER_SNAPSHOTS_PER_FOLDER:]
        self._folder_snapshots = sanitized_snaps
        if not isinstance(self._session_context, dict):
            self._session_context = self._default_session_context()
        self._session_context.setdefault("status", "idle")
        self._session_context.setdefault("recent_actions", [])
        self._session_context.setdefault("recent_files", [])
        self._session_context.setdefault("recent_folders", [])
        self._session_context.setdefault("pending_notes", [])
        self._session_context.setdefault("started_at", now)

    def _record_activity(self, *, event: str, payload: dict[str, Any]) -> None:
        data = {
            "event": event,
            "timestamp": _now_ts(),
            **payload,
        }
        self._append_jsonl(self._activity_log_path, data)

    def _touch_session_action(self, action: str, *, file_path: str = "", folder_path: str = "") -> None:
        actions: list[dict[str, Any]] = list(self._session_context.get("recent_actions") or [])
        actions.append({"action": action, "timestamp": _now_ts(), "file_path": file_path, "folder_path": folder_path})
        self._session_context["recent_actions"] = actions[-80:]
        if file_path:
            files = list(self._session_context.get("recent_files") or [])
            files = [f for f in files if str(f).lower() != file_path.lower()]
            files.insert(0, file_path)
            self._session_context["recent_files"] = files[:40]
        if folder_path:
            folders = list(self._session_context.get("recent_folders") or [])
            folders = [f for f in folders if str(f).lower() != folder_path.lower()]
            folders.insert(0, folder_path)
            self._session_context["recent_folders"] = folders[:30]
            self._session_context["last_working_folder"] = folder_path

    def update_user_profile(self, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                if value is None:
                    continue
                self._user_profile[str(key)] = value
            self._persist_indexes()

    def record_file_action(
        self,
        path: str | Path,
        *,
        action: str,
        origin: str = "runtime",
        relation: str = "",
    ) -> None:
        target = _normalize_path(path)
        parent = str(target.parent)
        ext = target.suffix.lower()
        now = _now_ts()
        with self._lock:
            key = str(target).lower()
            exists = target.exists() and target.is_file()
            if exists:
                self._files_index[key] = {
                    "path": str(target),
                    "name": _safe_name(target),
                    "extension": ext,
                    "type": "file",
                    "last_action": action,
                    "created_at": self._files_index.get(key, {}).get("created_at", now),
                    "updated_at": now,
                    "last_seen_at": now,
                    "exists": True,
                    "origin": origin,
                    "parent_folder": parent,
                    "relation": relation or self._files_index.get(key, {}).get("relation", ""),
                }
                # keep bounded
                if len(self._files_index) > MAX_ACTIVE_ITEMS:
                    oldest_key = sorted(
                        self._files_index,
                        key=lambda k: float(self._files_index[k].get("updated_at", 0.0)),
                    )[0]
                    self._files_index.pop(oldest_key, None)
            else:
                previous = self._files_index.pop(key, None)
                history_item = {
                    "path": str(target),
                    "name": _safe_name(target),
                    "type": "file",
                    "extension": ext,
                    "deleted_at": now,
                    "last_action_before_delete": previous.get("last_action") if isinstance(previous, dict) else action,
                    "origin": origin,
                    "parent_folder": parent,
                    "exists": False,
                }
                self._deleted_history.append(history_item)
                self._deleted_history = self._deleted_history[-MAX_DELETED_HISTORY:]
            self._touch_session_action(action, file_path=str(target), folder_path=parent)
            self._record_activity(event="file_action", payload={"path": str(target), "action": action, "origin": origin})
            self._persist_indexes()

    def record_folder_action(self, path: str | Path, *, action: str, origin: str = "runtime") -> None:
        target = _normalize_path(path)
        now = _now_ts()
        key = str(target).lower()
        with self._lock:
            exists = target.exists() and target.is_dir()
            if exists:
                self._folders_index[key] = {
                    "path": str(target),
                    "name": _safe_name(target),
                    "type": "folder",
                    "last_action": action,
                    "created_at": self._folders_index.get(key, {}).get("created_at", now),
                    "updated_at": now,
                    "last_seen_at": now,
                    "exists": True,
                    "origin": origin,
                    "parent_folder": str(target.parent),
                }
                if len(self._folders_index) > MAX_ACTIVE_ITEMS:
                    oldest_key = sorted(
                        self._folders_index,
                        key=lambda k: float(self._folders_index[k].get("updated_at", 0.0)),
                    )[0]
                    self._folders_index.pop(oldest_key, None)
            else:
                previous = self._folders_index.pop(key, None)
                self._deleted_history.append(
                    {
                        "path": str(target),
                        "name": _safe_name(target),
                        "type": "folder",
                        "deleted_at": now,
                        "last_action_before_delete": previous.get("last_action") if isinstance(previous, dict) else action,
                        "origin": origin,
                        "parent_folder": str(target.parent),
                        "exists": False,
                    }
                )
                self._deleted_history = self._deleted_history[-MAX_DELETED_HISTORY:]
            self._touch_session_action(action, folder_path=str(target))
            self._record_activity(event="folder_action", payload={"path": str(target), "action": action, "origin": origin})
            self._persist_indexes()

    def record_delete(self, path: str | Path, *, item_type: str, origin: str = "runtime", action: str = "deleted") -> None:
        target = _normalize_path(path)
        with self._lock:
            key = str(target).lower()
            previous = self._files_index.pop(key, None) if item_type == "file" else self._folders_index.pop(key, None)
            self._deleted_history.append(
                {
                    "path": str(target),
                    "name": _safe_name(target),
                    "type": item_type,
                    "extension": target.suffix.lower() if item_type == "file" else "",
                    "deleted_at": _now_ts(),
                    "last_action_before_delete": previous.get("last_action") if isinstance(previous, dict) else action,
                    "origin": origin,
                    "parent_folder": str(target.parent),
                    "exists": False,
                }
            )
            self._deleted_history = self._deleted_history[-MAX_DELETED_HISTORY:]
            self._touch_session_action(action, file_path=str(target) if item_type == "file" else "", folder_path=str(target.parent))
            self._record_activity(event="delete", payload={"path": str(target), "type": item_type, "origin": origin})
            self._persist_indexes()

    def record_folder_snapshot(self, folder_path: str | Path, *, origin: str = "runtime") -> None:
        target = _normalize_path(folder_path)
        if not target.exists() or not target.is_dir():
            return
        children: list[dict[str, Any]] = []
        try:
            for child in sorted(target.iterdir(), key=lambda p: p.name.lower())[:MAX_CHILDREN_PER_SNAPSHOT]:
                info = {
                    "name": child.name,
                    "path": str(child.resolve()),
                    "type": "folder" if child.is_dir() else "file",
                    "extension": child.suffix.lower() if child.is_file() else "",
                    "modified_at": child.stat().st_mtime,
                }
                if child.is_file():
                    info["size"] = child.stat().st_size
                children.append(info)
        except Exception:
            return
        snapshot = {
            "folder_path": str(target),
            "timestamp": _now_ts(),
            "origin": origin,
            "children": children,
        }
        key = str(target).lower()
        with self._lock:
            snaps = list(self._folder_snapshots.get(key, []))
            snaps.append(snapshot)
            self._folder_snapshots[key] = snaps[-MAX_FOLDER_SNAPSHOTS_PER_FOLDER:]
            self._touch_session_action("folder_snapshot", folder_path=str(target))
            self._record_activity(event="folder_snapshot", payload={"folder_path": str(target), "children_count": len(children)})
            self._persist_indexes()

    def record_session_start(self, *, user_id: str, session_id: str | None = None) -> str:
        sid = (session_id or f"sess-{uuid.uuid4().hex[:12]}").strip()
        with self._lock:
            self._session_context = self._default_session_context()
            self._session_context["session_id"] = sid
            self._session_context["started_at"] = _now_ts()
            self._session_context["user_id"] = user_id
            self._session_context["status"] = "running"
            self._record_activity(event="session_started", payload={"session_id": sid, "user_id": user_id})
            self._persist_indexes()
        return sid

    def record_session_event(self, *, event: str, payload: dict[str, Any] | None = None) -> None:
        payload = dict(payload or {})
        with self._lock:
            self._touch_session_action(event, file_path=str(payload.get("file_path") or ""), folder_path=str(payload.get("folder_path") or ""))
            self._record_activity(event=event, payload=payload)
            self._persist_indexes()

    def record_session_end(self, *, summary: str = "", status: str = "ended") -> None:
        with self._lock:
            self._session_context["ended_at"] = _now_ts()
            self._session_context["status"] = status
            self._last_session_summary = {
                "session_id": self._session_context.get("session_id", ""),
                "user_id": self._session_context.get("user_id", ""),
                "started_at": self._session_context.get("started_at", 0.0),
                "ended_at": self._session_context.get("ended_at", 0.0),
                "status": status,
                "summary": summary,
                "recent_actions": list(self._session_context.get("recent_actions") or [])[-20:],
                "recent_files": list(self._session_context.get("recent_files") or [])[:12],
                "recent_folders": list(self._session_context.get("recent_folders") or [])[:8],
            }
            self._record_activity(event="session_ended", payload={"session_id": self._session_context.get("session_id", ""), "status": status})
            self._persist_indexes()

    def get_last_session_summary(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._last_session_summary)

    def get_current_session_context(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._session_context)

    def get_last_active_file(self, *, extension: str | None = None) -> dict[str, Any] | None:
        ext = str(extension or "").strip().lower()
        if ext and not ext.startswith("."):
            ext = f".{ext}"
        with self._lock:
            values = list(self._files_index.values())
            if ext:
                values = [item for item in values if str(item.get("extension", "")).lower() == ext]
            values = [item for item in values if item.get("exists")]
            if not values:
                return None
            values.sort(key=lambda item: float(item.get("updated_at", 0.0)), reverse=True)
            return dict(values[0])

    def search_active_files(self, query: str, *, extension: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        needle = str(query or "").strip().lower()
        ext = str(extension or "").strip().lower()
        if ext and not ext.startswith("."):
            ext = f".{ext}"
        with self._lock:
            values = [dict(item) for item in self._files_index.values() if item.get("exists")]
            if ext:
                values = [item for item in values if str(item.get("extension", "")).lower() == ext]
            if needle:
                values = [
                    item for item in values
                    if needle in str(item.get("name", "")).lower()
                    or needle in str(item.get("path", "")).lower()
                ]
            values.sort(key=lambda item: float(item.get("updated_at", 0.0)), reverse=True)
            return values[: max(limit, 1)]

    def get_folder_snapshot(self, folder_path: str | Path) -> dict[str, Any] | None:
        key = str(_normalize_path(folder_path)).lower()
        with self._lock:
            snapshots = self._folder_snapshots.get(key, [])
            if not snapshots:
                return None
            return dict(snapshots[-1])

    def list_active_files(self, *, extension: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        ext = str(extension or "").strip().lower()
        if ext and not ext.startswith("."):
            ext = f".{ext}"
        with self._lock:
            values = [dict(item) for item in self._files_index.values() if item.get("exists")]
            if ext:
                values = [item for item in values if str(item.get("extension", "")).lower() == ext]
            values.sort(key=lambda item: float(item.get("updated_at", 0.0)), reverse=True)
            return values[: max(limit, 1)]

    def list_active_folders(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            values = [dict(item) for item in self._folders_index.values() if item.get("exists")]
            values.sort(key=lambda item: float(item.get("updated_at", 0.0)), reverse=True)
            return values[: max(limit, 1)]

    def list_deleted_items(
        self,
        *,
        item_type: str | None = None,
        extension: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        expected_type = str(item_type or "").strip().lower()
        ext = str(extension or "").strip().lower()
        if ext and not ext.startswith("."):
            ext = f".{ext}"
        with self._lock:
            values = [dict(item) for item in self._deleted_history]
            if expected_type:
                values = [item for item in values if str(item.get("type", "")).lower() == expected_type]
            if ext:
                values = [item for item in values if str(item.get("extension", "")).lower() == ext]
            values.sort(key=lambda item: float(item.get("deleted_at", 0.0)), reverse=True)
            return values[: max(limit, 1)]

    def list_recent_actions(self, *, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            actions = list(self._session_context.get("recent_actions") or [])
            actions.sort(key=lambda item: float(item.get("timestamp", 0.0)), reverse=True)
            return [dict(item) for item in actions[: max(limit, 1)]]

    def get_user_profile(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._user_profile)

    def mark_missing_active_paths_as_deleted(self) -> None:
        with self._lock:
            for key, item in list(self._files_index.items()):
                path = Path(str(item.get("path", "")))
                if not path.exists():
                    self.record_delete(path, item_type="file", origin="reconcile", action="missing_on_disk")
                    self._files_index.pop(key, None)
            for key, item in list(self._folders_index.items()):
                path = Path(str(item.get("path", "")))
                if not path.exists():
                    self.record_delete(path, item_type="folder", origin="reconcile", action="missing_on_disk")
                    self._folders_index.pop(key, None)
            self._persist_indexes()


_STORE_SINGLETON: LocalMemoryStore | NullLocalMemoryStore | None = None
_STORE_LOCK = threading.RLock()


def get_local_memory_store() -> LocalMemoryStore | NullLocalMemoryStore:
    global _STORE_SINGLETON
    with _STORE_LOCK:
        if _STORE_SINGLETON is None:
            # Mem0 é complementar; a camada local permanece disponível como
            # fonte prioritária mesmo quando o provedor remoto está ativo.
            _STORE_SINGLETON = LocalMemoryStore()
        return _STORE_SINGLETON
