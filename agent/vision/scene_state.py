from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ContextEvent:
    kind: str
    summary: str
    timestamp: float
    importance: float = 0.5


@dataclass(slots=True)
class SceneState:
    domain: str = "desktop"
    app_name: str = ""
    topic_guess: str = ""
    action_guess: str = ""
    active_window_title: str = ""
    screen_summary: str = ""
    screen_surface: str = ""
    screen_label: str = ""
    screen_dimensions: str = ""
    screen_track_sid: str = ""
    screen_track_source: str = ""
    ui_summary: str = ""
    ui_mode: str = ""
    ui_primary_action: str = ""
    ui_prominent_text: str = ""
    ui_flags: list[str] = field(default_factory=list)
    visual_change_score: float = 0.0
    entities: list[str] = field(default_factory=list)
    confidence: float = 0.0
    visual_context_active: bool = False
    last_user_activity_at: float = 0.0
    last_observed_at: float = 0.0
    recent_events: deque[ContextEvent] = field(default_factory=lambda: deque(maxlen=24))

    def remember(self, event: ContextEvent) -> None:
        self.recent_events.append(event)
        self.last_observed_at = event.timestamp

    def clear_visual_context(self) -> None:
        self.domain = "unknown"
        self.app_name = ""
        self.topic_guess = ""
        self.action_guess = ""
        self.active_window_title = ""
        self.screen_summary = ""
        self.screen_surface = ""
        self.screen_label = ""
        self.screen_dimensions = ""
        self.screen_track_sid = ""
        self.screen_track_source = ""
        self.ui_summary = ""
        self.ui_mode = ""
        self.ui_primary_action = ""
        self.ui_prominent_text = ""
        self.ui_flags.clear()
        self.visual_change_score = 0.0
        self.entities.clear()
        self.confidence = 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "app_name": self.app_name,
            "topic_guess": self.topic_guess,
            "action_guess": self.action_guess,
            "active_window_title": self.active_window_title,
            "screen_summary": self.screen_summary,
            "screen_surface": self.screen_surface,
            "screen_label": self.screen_label,
            "screen_dimensions": self.screen_dimensions,
            "screen_track_sid": self.screen_track_sid,
            "screen_track_source": self.screen_track_source,
            "ui_summary": self.ui_summary,
            "ui_mode": self.ui_mode,
            "ui_primary_action": self.ui_primary_action,
            "ui_prominent_text": self.ui_prominent_text,
            "ui_flags": list(self.ui_flags),
            "visual_change_score": round(self.visual_change_score, 2),
            "entities": list(self.entities),
            "confidence": round(self.confidence, 3),
            "visual_context_active": self.visual_context_active,
        }

    def compact_summary(self) -> str:
        parts = [
            f"dominio={self.domain}",
            f"app={self.app_name or 'desconhecido'}",
        ]
        if self.topic_guess:
            parts.append(f"topico={self.topic_guess}")
        if self.action_guess:
            parts.append(f"acao={self.action_guess}")
        if self.visual_context_active:
            parts.append("visao=ativo")
        if self.screen_summary:
            parts.append(f"tela={self.screen_summary[:80]}")
        if self.screen_surface:
            parts.append(f"fonte={self.screen_surface}")
        if self.screen_label:
            parts.append(f"alvo={self.screen_label[:60]}")
        if self.screen_dimensions:
            parts.append(f"resolucao={self.screen_dimensions}")
        if self.screen_track_source:
            parts.append(f"track_source={self.screen_track_source}")
        if self.screen_track_sid:
            parts.append(f"track_sid={self.screen_track_sid}")
        if self.ui_mode:
            parts.append(f"ui_modo={self.ui_mode}")
        if self.ui_primary_action:
            parts.append(f"ui_acao={self.ui_primary_action[:40]}")
        if self.ui_prominent_text:
            parts.append(f"ui_texto={self.ui_prominent_text[:60]}")
        if self.ui_summary:
            parts.append(f"ui={self.ui_summary[:100]}")
        if self.ui_flags:
            parts.append("ui_flags=" + ", ".join(self.ui_flags[:5]))
        if self.visual_change_score > 0:
            parts.append(f"mudanca_visual={self.visual_change_score:.1f}")
        if self.entities:
            parts.append("entidades=" + ", ".join(self.entities[:4]))
        parts.append(f"confianca={self.confidence:.2f}")
        return "; ".join(parts)
