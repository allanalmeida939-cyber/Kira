from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path


TEXT_EDITABLE_EXTENSIONS = {
    ".txt",
    ".bat",
    ".cmd",
    ".ps1",
    ".py",
    ".html",
    ".css",
    ".js",
    ".json",
    ".md",
    ".csv",
    ".yaml",
    ".yml",
    ".xml",
    ".ini",
    ".log",
}


@dataclass(slots=True)
class RecentFileEntry:
    path: str
    name: str
    extension: str
    action: str
    timestamp: float


class RecentFileRegistry:
    def __init__(self, *, max_entries: int = 120) -> None:
        self._entries: deque[RecentFileEntry] = deque(maxlen=max_entries)

    def remember(self, path: Path | str, *, action: str) -> None:
        target = Path(path).expanduser()
        if not target.exists() or not target.is_file():
            return
        resolved = target.resolve()
        key = str(resolved).lower()
        self._entries = deque(
            (entry for entry in self._entries if entry.path.lower() != key),
            maxlen=self._entries.maxlen,
        )
        self._entries.appendleft(
            RecentFileEntry(
                path=str(resolved),
                name=resolved.name,
                extension=resolved.suffix.lower(),
                action=str(action or "touched").strip().lower() or "touched",
                timestamp=time.time(),
            )
        )

    def recent(self, *, extension: str | None = None, limit: int = 8) -> list[Path]:
        wanted = str(extension or "").strip().lower()
        if wanted and not wanted.startswith("."):
            wanted = f".{wanted}"
        collected: list[Path] = []
        for entry in list(self._entries):
            if wanted and entry.extension != wanted:
                continue
            path = Path(entry.path)
            if path.exists() and path.is_file():
                collected.append(path)
            if len(collected) >= max(limit, 1):
                break
        return collected

    def resolve_last(self, *, extension: str | None = None) -> Path | None:
        recent = self.recent(extension=extension, limit=1)
        return recent[0] if recent else None


def resolve_search_roots(*, include_project: bool = True) -> list[Path]:
    home = Path.home()
    roots = [
        Path.cwd(),
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
        home / "Pictures",
        home / "Documents" / "Kira",
    ]
    if not include_project:
        roots = roots[1:]
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).lower()
        if key in seen:
            continue
        seen.add(key)
        if root.exists():
            unique.append(root)
    return unique
