from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class PersonMemory:
    person_id: str
    person_name: str
    content: str
    timestamp: str
    context: str | None = None

    def to_dict(self) -> dict:
        return {
            "person_id": self.person_id,
            "person_name": self.person_name,
            "content": self.content,
            "timestamp": self.timestamp,
            "context": self.context,
        }


class PersonMemoryManager:
    def __init__(self, storage_file: Path):
        self.storage_file = Path(storage_file)
        self.storage_file.parent.mkdir(parents=True, exist_ok=True)
        self._memories: list[PersonMemory] = []
        self._load()

    def _load(self) -> None:
        if not self.storage_file.exists():
            return
        try:
            data = json.loads(self.storage_file.read_text(encoding="utf-8"))
            self._memories = [PersonMemory(**item) for item in data.get("memories", [])]
        except Exception as exc:
            logger.warning("[PERSON_MEMORY] Falha ao carregar: %s", exc)

    def _save(self) -> None:
        payload = {
            "memories": [m.to_dict() for m in self._memories],
            "last_updated": datetime.now().isoformat(),
        }
        self.storage_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def add_memory(self, person_id: str, person_name: str, content: str, context: str | None = None) -> None:
        self._memories.append(
            PersonMemory(
                person_id=person_id,
                person_name=person_name,
                content=str(content or "").strip(),
                timestamp=datetime.now().isoformat(),
                context=context,
            )
        )
        self._save()

    def get_memories(self, person_id: str | None = None, person_name: str | None = None, limit: int = 50) -> list[PersonMemory]:
        data = self._memories
        if person_id:
            data = [m for m in data if m.person_id == person_id]
        if person_name:
            data = [m for m in data if m.person_name == person_name]
        return list(sorted(data, key=lambda m: m.timestamp, reverse=True))[:limit]

