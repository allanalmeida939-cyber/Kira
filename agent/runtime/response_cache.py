from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class CachePolicy(Enum):
    NEVER = -1
    SHORT = 300
    MEDIUM = 1800
    LONG = 86400


class CacheEntry:
    def __init__(self, query: str, response: str, policy: CachePolicy, timestamp: float | None = None):
        self.query = query
        self.response = response
        self.policy = policy
        self.timestamp = timestamp or time.time()
        self.hits = 0

    def is_expired(self) -> bool:
        if self.policy == CachePolicy.NEVER:
            return False
        return (time.time() - self.timestamp) > self.policy.value

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "response": self.response,
            "policy": self.policy.name,
            "timestamp": self.timestamp,
            "hits": self.hits,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CacheEntry":
        entry = cls(
            query=data["query"],
            response=data["response"],
            policy=CachePolicy[data["policy"]],
            timestamp=data.get("timestamp"),
        )
        entry.hits = int(data.get("hits", 0))
        return entry


class ResponseCache:
    def __init__(self, max_size: int = 1000, persistence_file: Path | None = None):
        self.max_size = max(10, int(max_size))
        self.persistence_file = persistence_file
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._hits = 0
        self._misses = 0
        if self.persistence_file and self.persistence_file.exists():
            self._load()
        logger.info("[CACHE] Inicializado - max_size=%d persistence=%s", self.max_size, bool(self.persistence_file))

    def _make_key(self, query: str) -> str:
        normalized = " ".join(str(query or "").strip().lower().split())
        return hashlib.md5(normalized.encode("utf-8")).hexdigest()

    def _detect_policy(self, query: str) -> CachePolicy:
        q = str(query or "").lower()
        if any(k in q for k in ("capital", "defin", "significa", "histori", "quem foi", "quanto e")):
            return CachePolicy.NEVER
        if any(k in q for k in ("preco", "cotacao", "bitcoin", "cripto", "ao vivo")):
            return CachePolicy.SHORT
        if any(k in q for k in ("clima", "tempo", "temperatura", "noticia")):
            return CachePolicy.MEDIUM
        return CachePolicy.LONG

    def get(self, query: str) -> str | None:
        key = self._make_key(query)
        if key not in self._cache:
            self._misses += 1
            logger.debug("[CACHE] MISS - query=%r", str(query)[:80])
            return None
        entry = self._cache[key]
        if entry.is_expired():
            del self._cache[key]
            self._misses += 1
            logger.debug("[CACHE] EXPIRED - query=%r", str(query)[:80])
            return None
        entry.hits += 1
        self._hits += 1
        self._cache.move_to_end(key)
        logger.info("[CACHE] HIT - query=%r policy=%s hits=%d", str(query)[:80], entry.policy.name, entry.hits)
        return entry.response

    def set(self, query: str, response: str, policy: CachePolicy | None = None) -> None:
        key = self._make_key(query)
        pol = policy or self._detect_policy(query)
        self._cache[key] = CacheEntry(query=query, response=response, policy=pol)
        self._cache.move_to_end(key)
        while len(self._cache) > self.max_size:
            self._cache.popitem(last=False)
        logger.info("[CACHE] SET - query=%r policy=%s", str(query)[:80], pol.name)
        if self.persistence_file and len(self._cache) % 10 == 0:
            self._save()

    def _save(self) -> None:
        if not self.persistence_file:
            return
        self.persistence_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "entries": [e.to_dict() for e in self._cache.values()],
            "stats": self.get_stats(),
        }
        self.persistence_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _load(self) -> None:
        try:
            data = json.loads(self.persistence_file.read_text(encoding="utf-8"))
            loaded = 0
            for d in data.get("entries", []):
                entry = CacheEntry.from_dict(d)
                if not entry.is_expired():
                    self._cache[self._make_key(entry.query)] = entry
                    loaded += 1
            logger.info("[CACHE] Carregado - entries=%d", loaded)
        except Exception as exc:
            logger.warning("[CACHE] Falha ao carregar: %s", exc)

    def get_stats(self) -> dict:
        total = self._hits + self._misses
        hit_rate = (self._hits / total * 100.0) if total else 0.0
        return {
            "size": len(self._cache),
            "max_size": self.max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(hit_rate, 1),
        }

