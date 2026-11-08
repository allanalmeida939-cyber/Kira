from __future__ import annotations

from dataclasses import dataclass, replace
import logging
import re
import threading
import time
from typing import Any
import unicodedata
from uuid import uuid4

logger = logging.getLogger(__name__)


def _normalize_draft_text(text: str) -> str:
    value = unicodedata.normalize("NFD", str(text or "").lower().strip())
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = re.sub(r"[^\w\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


@dataclass
class CreationTaskDraft:
    draft_id: str
    command: Any
    source: str
    original_text: str
    refinements: list[str]
    expires_at: float

    def final_text(self) -> str:
        return " ".join([self.original_text, *self.refinements]).strip()

    def final_command(self) -> Any:
        text = self.final_text()
        normalized = _normalize_draft_text(text)
        mode = str(getattr(self.command, "destination_mode", "ask_later") or "ask_later")
        hint = str(getattr(self.command, "destination_hint", "") or "")
        if any(term in normalized for term in ("pasta padrao", "no padrao", "pasta de pdfs", "pasta de textos", "pasta de slides", "pasta de prompts")):
            mode, hint = "default", str(getattr(self.command, "task_type", "") or "")
        has_image = bool(getattr(self.command, "has_image", False) or re.search(r"\b(imagem|foto|figura|ilustracao|capa)\b", normalized))
        return replace(
            self.command, raw_text=text, normalized_text=normalized,
            destination_mode=mode, destination_hint=hint, has_image=has_image,
        )


class CreationTaskDraftStore:
    def __init__(self) -> None:
        self._drafts: dict[str, CreationTaskDraft] = {}
        self._notice_keys: set[str] = set()
        self._lock = threading.Lock()

    def create(self, command: Any, source: str, text: str, timeout_s: float = 12.0) -> CreationTaskDraft:
        draft = CreationTaskDraft(
            draft_id=f"draft_{uuid4().hex[:10]}",
            command=command,
            source=str(source or "unknown"),
            original_text=str(text or "").strip(),
            refinements=[],
            expires_at=time.monotonic() + float(timeout_s),
        )
        with self._lock:
            self._drafts[draft.draft_id] = draft
        logger.info("CREATION_TASK_DRAFT_CREATED kind=%s timeout_s=%s", command.task_type, int(timeout_s))
        return draft

    def active(self, source: str | None = None) -> CreationTaskDraft | None:
        with self._lock:
            drafts = list(self._drafts.values())
        if source:
            same_source = [d for d in drafts if d.source == source]
            drafts = same_source or drafts
        return max(drafts, key=lambda d: d.expires_at) if drafts else None

    def add_refinement(self, draft_id: str, text: str) -> CreationTaskDraft | None:
        with self._lock:
            draft = self._drafts.get(draft_id)
            if draft:
                draft.refinements.append(str(text or "").strip())
        if draft:
            logger.info("CREATION_TASK_DRAFT_REFINEMENT_ADDED text_chars=%d", len(str(text or "").strip()))
        return draft

    def take(self, draft_id: str, reason: str) -> CreationTaskDraft | None:
        with self._lock:
            draft = self._drafts.pop(draft_id, None)
        if draft:
            logger.info("CREATION_TASK_DRAFT_SUBMIT_NOW reason=%s", reason)
            logger.info("CREATION_TASK_DRAFT_FINAL_PROMPT_BUILT chars=%d", len(draft.final_text()))
        return draft

    def mark_notice_once(self, key: str, *, event: str = "CREATION_TASK_NOTICE_DEDUPED") -> bool:
        normalized = str(key or "").strip()
        if not normalized:
            return True
        with self._lock:
            if normalized in self._notice_keys:
                logger.info("%s key=%s", event, normalized)
                return False
            self._notice_keys.add(normalized)
        return True

    def clear(self) -> None:
        with self._lock:
            self._drafts.clear()
            self._notice_keys.clear()


creation_task_draft_store = CreationTaskDraftStore()
