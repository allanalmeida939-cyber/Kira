from __future__ import annotations

import time
import unicodedata
from dataclasses import dataclass, field


def normalize_text(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_text.split())


@dataclass(slots=True, frozen=True)
class VisualUiSnapshot:
    captured_at: float
    window_title: str
    texts: tuple[str, ...] = ()
    menus: tuple[str, ...] = ()
    tabs: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    list_items: tuple[str, ...] = ()
    buttons: tuple[str, ...] = ()
    enabled_buttons: tuple[str, ...] = ()
    progress_values: tuple[int, ...] = ()
    loading_visible: bool = False
    error_visible: bool = False
    success_visible: bool = False

    def contains_text(self, text: str) -> bool:
        needle = normalize_text(text)
        if not needle:
            return False
        return any(needle in value for value in self._iter_all_strings())

    def contains_button(self, text: str, *, enabled_only: bool = False) -> bool:
        needle = normalize_text(text)
        if not needle:
            return False
        haystack = self.enabled_buttons if enabled_only else self.buttons
        return any(needle in value for value in haystack)

    def _iter_all_strings(self) -> tuple[str, ...]:
        return (
            self.window_title,
            *self.texts,
            *self.menus,
            *self.tabs,
            *self.fields,
            *self.list_items,
            *self.buttons,
            *self.enabled_buttons,
        )


@dataclass(slots=True, frozen=True)
class VisualUiContext:
    mode: str = ""
    summary: str = ""
    primary_action: str = ""
    prominent_text: str = ""
    site_name: str = ""
    flags: tuple[str, ...] = ()
    confidence: float = 0.0


@dataclass(slots=True, frozen=True)
class VisualRuntimeSnapshot:
    share_active: bool
    captured_at: float
    frame_token: int
    width: int
    height: int
    change_score: float
    stable_for_seconds: float
    window_title: str
    screen_summary: str
    signature: bytes = b""
    ui: VisualUiSnapshot | None = None
    analysis: VisualUiContext | None = None

    @property
    def frame_age_seconds(self) -> float:
        if self.captured_at <= 0:
            return float("inf")
        return max(time.monotonic() - self.captured_at, 0.0)

    def contains_text(self, text: str) -> bool:
        needle = normalize_text(text)
        if not needle:
            return False
        if needle in normalize_text(self.window_title):
            return True
        if needle in normalize_text(self.screen_summary):
            return True
        if self.ui is None:
            return False
        return self.ui.contains_text(needle)

    def contains_button(self, text: str, *, enabled_only: bool = False) -> bool:
        if self.ui is None:
            return False
        return self.ui.contains_button(text, enabled_only=enabled_only)


@dataclass(slots=True, frozen=True)
class VisualWatchPlan:
    kind: str
    request_text: str
    target_text: str
    description: str
    confirmation_message: str
    success_message: str
    interval_seconds: float = 1.2
    timeout_seconds: float | None = 300.0
    requires_ui: bool = False
    min_change_score: float = 6.5
    stable_seconds: float = 3.0
    dedupe_key: str = ""


@dataclass(slots=True)
class VisualWatcherInfo:
    watcher_id: str
    kind: str
    request_text: str
    target_text: str
    description: str
    interval_seconds: float
    timeout_seconds: float | None
    created_at: float = field(default_factory=time.monotonic)
    status: str = "active"
    last_reason: str = ""
    dedupe_key: str = ""

    @property
    def age_seconds(self) -> int:
        return max(int(time.monotonic() - self.created_at), 0)


@dataclass(slots=True, frozen=True)
class VisualWatcherStartResult:
    watcher: VisualWatcherInfo
    confirmation_message: str
    duplicate: bool = False


@dataclass(slots=True, frozen=True)
class ConditionOutcome:
    triggered: bool = False
    cancel_message: str = ""
    reason: str = ""
