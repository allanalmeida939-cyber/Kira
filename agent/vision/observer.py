from __future__ import annotations

import os
import time
from dataclasses import dataclass

from .app_classifier import classify_window_context
from .scene_state import ContextEvent, SceneState


VISUAL_CONFIDENCE_THRESHOLD = max(
    float(os.getenv("KIRA_VISUAL_CONFIDENCE_THRESHOLD", "0.45")),
    0.05,
)
SCREEN_SCENE_CHANGE_THRESHOLD = max(
    float(os.getenv("KIRA_SCREEN_SCENE_CHANGE_THRESHOLD", "8.0")),
    2.0,
)


@dataclass(slots=True)
class ObservationUpdate:
    state: SceneState
    event_kind: str
    event_summary: str
    notable: bool
    importance: float
    raw_payload: dict[str, object]


class ContextObserver:
    def __init__(self) -> None:
        self.state = SceneState()

    def observe_signal(
        self,
        payload: dict[str, object],
        *,
        now: float | None = None,
    ) -> ObservationUpdate | None:
        now = now or time.monotonic()
        signal_type = str(payload.get("type") or "").strip()
        if not signal_type:
            return None

        if signal_type == "desktop_context":
            if not self.state.visual_context_active:
                return None
            return self._observe_desktop_context(payload, now)
        if signal_type == "visual_context_state":
            return self._observe_visual_context_state(payload, now)
        if signal_type in {"chat", "user_transcription"}:
            self.state.last_user_activity_at = now
            return None
        if signal_type == "session_context":
            enabled = bool(payload.get("visual_context_active"))
            self.state.visual_context_active = enabled
            if not enabled:
                self.state.clear_visual_context()
            return None
        return None

    def observe_transcription(self, text: str, *, now: float | None = None) -> None:
        if text.strip():
            self.state.last_user_activity_at = now or time.monotonic()

    def observe_video_frame(
        self,
        *,
        participant_identity: str,
        width: int,
        height: int,
        source: str,
        change_score: float = 0.0,
        now: float | None = None,
    ) -> ObservationUpdate | None:
        now = now or time.monotonic()
        source_label = str(source or "").strip().lower() or "video"
        previous_summary = self.state.screen_summary
        previous_observed_at = self.state.last_observed_at
        self.state.visual_change_score = max(float(change_score), 0.0)
        if source_label == "visual_context":
            self.state.visual_context_active = True
            self.state.screen_dimensions = self._format_dimensions(width, height)
            if not self.state.screen_surface:
                self.state.screen_surface = "monitor"
            if self.state.domain == "unknown":
                self.state.domain = "desktop"

        summary = self._build_frame_summary(
            source_label=source_label,
            width=width,
            height=height,
        )
        self.state.screen_summary = summary
        self.state.last_observed_at = now
        if self.state.domain == "unknown":
            self.state.domain = "desktop"
        self.state.confidence = max(
            self.state.confidence,
            0.62 if source_label == "visual_context" else 0.35,
        )

        high_scene_change = source_label == "visual_context" and change_score >= SCREEN_SCENE_CHANGE_THRESHOLD
        recently_seen_same_summary = (
            summary == previous_summary
            and previous_observed_at
            and now - previous_observed_at < 5.0
        )
        if recently_seen_same_summary and not high_scene_change:
            return None
        if high_scene_change and previous_observed_at and now - previous_observed_at < 2.5:
            return None

        if high_scene_change:
            event_kind = "screen_scene_changed"
            importance = min(0.65 + (min(change_score, 24.0) / 40.0), 0.95)
        else:
            event_kind = "screen_frame_received" if source_label == "visual_context" else "video_frame_received"
            importance = 0.72 if source_label == "visual_context" else 0.35
        event = ContextEvent(
            kind=event_kind,
            summary=f"{summary} via {participant_identity or 'participante'}",
            timestamp=now,
            importance=importance,
        )
        self.state.remember(event)
        return ObservationUpdate(
            state=self.state,
            event_kind=event_kind,
            event_summary=event.summary,
            notable=source_label == "visual_context",
            importance=importance,
            raw_payload={
                "type": event_kind,
                "participant_identity": participant_identity,
                "width": width,
                "height": height,
                "source": source_label,
                "change_score": round(float(change_score), 2),
            },
        )

    def _observe_desktop_context(
        self,
        payload: dict[str, object],
        now: float,
    ) -> ObservationUpdate | None:
        title = str(payload.get("title") or "").strip()
        if not title or title == self.state.active_window_title:
            return None

        previous_domain = self.state.domain
        previous_app = self.state.app_name

        guess = classify_window_context(title, visual_context_active=self.state.visual_context_active)
        self.state.active_window_title = title
        self.state.domain = guess.domain
        self.state.app_name = guess.app_name
        self.state.topic_guess = guess.topic_guess
        self.state.action_guess = guess.action_guess
        self.state.entities = guess.entities
        self.state.confidence = guess.confidence
        self.state.screen_summary = title[:180]

        if not previous_app and guess.app_name:
            event_kind = "app_detected"
        elif previous_app != guess.app_name and guess.app_name:
            event_kind = "app_changed"
        elif previous_domain != guess.domain:
            event_kind = "domain_changed"
        else:
            event_kind = "window_focus_changed"

        notable = (
            event_kind in {"app_detected", "app_changed", "domain_changed"}
            and guess.confidence >= VISUAL_CONFIDENCE_THRESHOLD
        )
        summary = f"janela ativa: {title}"
        importance = 0.85 if guess.action_guess == "erro_visivel" else 0.65 if notable else 0.3

        event = ContextEvent(
            kind=event_kind,
            summary=summary,
            timestamp=now,
            importance=importance,
        )
        self.state.remember(event)
        return ObservationUpdate(
            state=self.state,
            event_kind=event_kind,
            event_summary=summary,
            notable=notable,
            importance=importance,
            raw_payload=payload,
        )

    def _observe_visual_context_state(
        self,
        payload: dict[str, object],
        now: float,
    ) -> ObservationUpdate | None:
        was_enabled = self.state.visual_context_active
        enabled = bool(payload.get("enabled"))
        surface = self._normalize_surface(payload.get("surface"))
        label = str(payload.get("label") or "").strip()
        width = self._coerce_int(payload.get("width"))
        height = self._coerce_int(payload.get("height"))
        track_sid = str(payload.get("track_sid") or "").strip()
        track_source = str(payload.get("source") or "").strip().lower()
        dimensions = self._format_dimensions(width, height)

        metadata_changed = (
            surface != self.state.screen_surface
            or label != self.state.screen_label
            or dimensions != self.state.screen_dimensions
            or track_sid != self.state.screen_track_sid
            or track_source != self.state.screen_track_source
        )
        if enabled == self.state.visual_context_active and not metadata_changed:
            return None

        self.state.visual_context_active = enabled
        if not enabled:
            self.state.clear_visual_context()
        else:
            self.state.screen_surface = surface or self.state.screen_surface or "monitor"
            self.state.screen_label = label
            self.state.screen_dimensions = dimensions
            self.state.screen_track_sid = track_sid
            self.state.screen_track_source = track_source
            self.state.domain = "desktop"
            self.state.confidence = max(self.state.confidence, 0.58)
            self.state.screen_summary = self._build_share_state_summary(
                surface=self.state.screen_surface,
                label=self.state.screen_label,
                dimensions=self.state.screen_dimensions,
            )
        if not enabled:
            event_kind = "visual_context_stopped"
            summary = "modo visao desligado"
            importance = 0.45
        elif not was_enabled:
            event_kind = "visual_context_started"
            summary = self.state.screen_summary or "modo visao ligado"
            importance = 0.7
        else:
            event_kind = "visual_context_updated"
            summary = self.state.screen_summary or "modo visao atualizado"
            importance = 0.62

        event = ContextEvent(
            kind=event_kind,
            summary=summary,
            timestamp=now,
            importance=importance,
        )
        self.state.remember(event)
        return ObservationUpdate(
            state=self.state,
            event_kind=event_kind,
            event_summary=summary,
            notable=True,
            importance=importance,
            raw_payload=payload,
        )

    def _build_frame_summary(
        self,
        *,
        source_label: str,
        width: int,
        height: int,
    ) -> str:
        dimensions = self._format_dimensions(width, height)
        if source_label != "visual_context":
            return f"video remoto {source_label} {dimensions}".strip()

        surface = self.state.screen_surface or "monitor"
        label = self.state.screen_label
        parts = ["modo visao ativo"]
        if surface:
            parts.append(f"fonte {surface}")
        if label:
            parts.append(f"alvo {label}")
        if dimensions:
            parts.append(dimensions)
        return " | ".join(parts)

    def _build_share_state_summary(
        self,
        *,
        surface: str,
        label: str,
        dimensions: str,
    ) -> str:
        parts = ["modo visao ativo"]
        if surface:
            parts.append(f"fonte {surface}")
        if label:
            parts.append(f"alvo {label}")
        if dimensions:
            parts.append(dimensions)
        return " | ".join(parts)

    def _format_dimensions(self, width: int, height: int) -> str:
        if width > 0 and height > 0:
            return f"{width}x{height}"
        return ""

    def _coerce_int(self, value: object) -> int:
        try:
            return max(int(value or 0), 0)
        except (TypeError, ValueError):
            return 0

    def _normalize_surface(self, value: object) -> str:
        normalized = str(value or "").strip().lower()
        mapping = {
            "monitor": "monitor",
            "screen": "monitor",
            "window": "janela",
            "browser": "aba",
            "tab": "aba",
        }
        return mapping.get(normalized, normalized)
