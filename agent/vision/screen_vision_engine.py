from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable


VISION_SAMPLE_SECONDS = max(
    float(os.getenv("KIRA_SCREEN_VISION_INTERVAL_SECONDS", "3.0")),
    1.0,
)
VISION_TEXT_EXCERPT_MAX = max(int(os.getenv("KIRA_SCREEN_VISION_EXCERPT_MAX", "180")), 80)
VISION_DUPLICATE_COOLDOWN_SECONDS = max(
    float(os.getenv("KIRA_SCREEN_VISION_DUPLICATE_COOLDOWN_SECONDS", "8.0")),
    1.0,
)
VISION_SIMILARITY_THRESHOLD = min(
    max(float(os.getenv("KIRA_SCREEN_VISION_SIMILARITY_THRESHOLD", "0.96")), 0.75),
    0.999,
)

_KEYWORD_MAP: dict[str, tuple[str, ...]] = {
    "erro": ("erro", "error", "falha", "failed"),
    "login": ("login", "sign in", "entrar"),
    "senha": ("senha", "password"),
    "confirmar": ("confirmar", "confirm", "ok", "aceitar"),
    "aviso": ("aviso", "warning", "alerta", "notice"),
    "carregando": ("carregando", "loading", "aguarde", "processando"),
    "sucesso": ("sucesso", "success", "concluido", "concluida"),
    "falha": ("falha", "failed", "erro", "error"),
}


def _normalize_text(value: str) -> str:
    text = str(value or "").lower()
    table = str.maketrans(
        {
            "á": "a",
            "à": "a",
            "â": "a",
            "ã": "a",
            "é": "e",
            "ê": "e",
            "í": "i",
            "ó": "o",
            "ô": "o",
            "õ": "o",
            "ú": "u",
            "ç": "c",
        }
    )
    return " ".join(text.translate(table).split())


@dataclass(slots=True)
class ScreenVisionResult:
    event: dict[str, object] | None
    duplicate_skipped: bool
    text_excerpt: str
    keywords: list[str]


class ScreenVisionEngine:
    def __init__(self) -> None:
        self._last_processed_at = 0.0
        self._last_fingerprint = ""
        self._last_excerpt = ""
        self._last_event_at = 0.0

    def should_process(self, now: float | None = None) -> bool:
        current_now = now or time.monotonic()
        return (current_now - self._last_processed_at) >= VISION_SAMPLE_SECONDS

    def process_screen_frame(
        self,
        frame,
        *,
        text_sources: Iterable[str] | None = None,
        now: float | None = None,
    ) -> ScreenVisionResult:
        current_now = now or time.monotonic()
        self._last_processed_at = current_now

        width = int(getattr(frame, "width", 0) or 0)
        height = int(getattr(frame, "height", 0) or 0)
        print(f"[KIRA VISION] frame_received width={width} height={height}", flush=True)

        extracted = self.extract_screen_text(frame, text_sources=text_sources)
        excerpt = extracted[:VISION_TEXT_EXCERPT_MAX]
        print(f"[KIRA VISION] text_extracted=\"{excerpt}\"", flush=True)

        keywords = self.detect_screen_keywords(extracted)
        print(f"[KIRA VISION] keywords={keywords}", flush=True)
        if not keywords:
            return ScreenVisionResult(event=None, duplicate_skipped=False, text_excerpt=excerpt, keywords=[])

        event = self.build_screen_event(extracted, keywords, now=current_now)
        is_duplicate = self.is_duplicate_screen_event(event, now=current_now)
        print(f"[KIRA VISION] duplicate_skipped={is_duplicate}", flush=True)
        if is_duplicate:
            return ScreenVisionResult(event=None, duplicate_skipped=True, text_excerpt=excerpt, keywords=keywords)

        self._last_event_at = current_now
        print(f"[KIRA VISION] event_generated type={event['type']}", flush=True)
        return ScreenVisionResult(event=event, duplicate_skipped=False, text_excerpt=excerpt, keywords=keywords)

    def extract_screen_text(self, frame, *, text_sources: Iterable[str] | None = None) -> str:
        candidates: list[str] = []
        for value in text_sources or ():
            normalized = _normalize_text(str(value or ""))
            if normalized and normalized not in candidates:
                candidates.append(normalized)
        return " | ".join(candidates)

    def detect_screen_keywords(self, text: str) -> list[str]:
        normalized = _normalize_text(text)
        if not normalized:
            return []
        hits: list[str] = []
        for key, variants in _KEYWORD_MAP.items():
            if any(variant in normalized for variant in variants):
                hits.append(key)
        return hits

    def build_screen_event(
        self,
        text: str,
        keywords: list[str],
        *,
        now: float | None = None,
    ) -> dict[str, object]:
        normalized = _normalize_text(text)
        excerpt = normalized[:VISION_TEXT_EXCERPT_MAX]
        confidence = min(0.55 + (0.07 * len(keywords)), 0.92)
        return {
            "type": "screen_text_detected",
            "keywords": keywords,
            "text_excerpt": excerpt,
            "confidence": round(confidence, 2),
            "timestamp": now or time.monotonic(),
        }

    def is_duplicate_screen_event(self, event: dict[str, object], *, now: float | None = None) -> bool:
        current_now = now or time.monotonic()
        excerpt = _normalize_text(str(event.get("text_excerpt") or ""))
        keywords = ",".join(sorted(str(item) for item in event.get("keywords", [])))
        fingerprint = hashlib.sha1(f"{keywords}|{excerpt}".encode("utf-8")).hexdigest()

        if not self._last_fingerprint:
            self._last_fingerprint = fingerprint
            self._last_excerpt = excerpt
            return False

        if fingerprint == self._last_fingerprint and (current_now - self._last_event_at) < VISION_DUPLICATE_COOLDOWN_SECONDS:
            return True

        similarity = SequenceMatcher(None, excerpt, self._last_excerpt).ratio() if excerpt and self._last_excerpt else 0.0
        if similarity >= VISION_SIMILARITY_THRESHOLD and (current_now - self._last_event_at) < VISION_DUPLICATE_COOLDOWN_SECONDS:
            return True

        self._last_fingerprint = fingerprint
        self._last_excerpt = excerpt
        return False

