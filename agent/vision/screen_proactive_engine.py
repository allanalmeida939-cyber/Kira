from __future__ import annotations

import os
import time
from dataclasses import dataclass
from difflib import SequenceMatcher


PROACTIVE_GLOBAL_COOLDOWN_SECONDS = max(
    float(os.getenv("KIRA_SCREEN_PROACTIVE_GLOBAL_COOLDOWN_SECONDS", "20.0")),
    3.0,
)
PROACTIVE_CONTEXT_COOLDOWN_SECONDS = max(
    float(os.getenv("KIRA_SCREEN_PROACTIVE_CONTEXT_COOLDOWN_SECONDS", "35.0")),
    5.0,
)
PROACTIVE_LOADING_PERSIST_SECONDS = max(
    float(os.getenv("KIRA_SCREEN_PROACTIVE_LOADING_PERSIST_SECONDS", "8.0")),
    2.0,
)


@dataclass(slots=True)
class ScreenProactiveDecision:
    should_notify: bool
    reason: str
    screen_context: str
    priority: str
    cooldown_blocked: bool
    timestamp: float
    confidence: float
    subtype: str
    text_excerpt: str
    source_keywords: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "should_notify": self.should_notify,
            "reason": self.reason,
            "screen_context": self.screen_context,
            "priority": self.priority,
            "cooldown_blocked": self.cooldown_blocked,
            "timestamp": self.timestamp,
            "confidence": self.confidence,
            "subtype": self.subtype,
            "text_excerpt": self.text_excerpt,
            "source_keywords": list(self.source_keywords),
        }


class ScreenProactiveEngine:
    def __init__(self) -> None:
        self._last_global_notify_at = 0.0
        self._last_notify_by_context: dict[str, float] = {}
        self._last_excerpt_by_context: dict[str, str] = {}
        self._loading_since = 0.0
        self._loading_last_seen = 0.0

    def evaluate_screen_proactivity(self, screen_context_event: dict[str, object], *, now: float | None = None) -> ScreenProactiveDecision:
        current_now = float(now or time.monotonic())
        context = str(screen_context_event.get("screen_context") or "neutro").strip().lower() or "neutro"
        subtype = str(screen_context_event.get("subtype") or "desconhecido").strip().lower() or "desconhecido"
        confidence = float(screen_context_event.get("confidence") or 0.0)
        excerpt = str(screen_context_event.get("text_excerpt") or "").strip().lower()
        keywords = [str(item) for item in screen_context_event.get("keywords", []) if str(item).strip()]

        priority = self.get_screen_event_priority(context=context, subtype=subtype)
        reason = self._build_reason(context=context, subtype=subtype, priority=priority)

        blocked, block_reason = self.should_block_screen_notification(
            context=context,
            subtype=subtype,
            confidence=confidence,
            excerpt=excerpt,
            now=current_now,
        )
        if blocked:
            return self.build_screen_proactive_event(
                should_notify=False,
                reason=block_reason,
                context=context,
                priority=priority,
                cooldown_blocked="cooldown" in block_reason,
                timestamp=current_now,
                confidence=confidence,
                subtype=subtype,
                excerpt=excerpt,
                keywords=keywords,
            )

        self._last_global_notify_at = current_now
        self._last_notify_by_context[context] = current_now
        self._last_excerpt_by_context[context] = excerpt
        return self.build_screen_proactive_event(
            should_notify=True,
            reason=reason,
            context=context,
            priority=priority,
            cooldown_blocked=False,
            timestamp=current_now,
            confidence=confidence,
            subtype=subtype,
            excerpt=excerpt,
            keywords=keywords,
        )

    def get_screen_event_priority(self, *, context: str, subtype: str) -> str:
        if context in {"erro", "confirmacao", "login"}:
            return "high"
        if context in {"formulario", "carregamento"}:
            return "medium"
        return "low"

    def should_block_screen_notification(
        self,
        *,
        context: str,
        subtype: str,
        confidence: float,
        excerpt: str,
        now: float,
    ) -> tuple[bool, str]:
        if context == "neutro":
            return True, "contexto_fraco_neutro"
        if confidence < 0.52:
            return True, "contexto_fraco_confidence"

        # loading precisa persistir antes de gerar aviso.
        if context == "carregamento":
            if self._loading_since <= 0.0:
                self._loading_since = now
                self._loading_last_seen = now
                return True, "loading_ainda_nao_persistente"
            self._loading_last_seen = now
            if (now - self._loading_since) < PROACTIVE_LOADING_PERSIST_SECONDS:
                return True, "loading_ainda_nao_persistente"
        else:
            self._loading_since = 0.0
            self._loading_last_seen = 0.0

        # cooldown global
        if self._last_global_notify_at > 0 and (now - self._last_global_notify_at) < PROACTIVE_GLOBAL_COOLDOWN_SECONDS:
            return True, "cooldown_global"

        # cooldown por contexto
        last_context_at = self._last_notify_by_context.get(context, 0.0)
        if last_context_at > 0 and (now - last_context_at) < PROACTIVE_CONTEXT_COOLDOWN_SECONDS:
            previous_excerpt = self._last_excerpt_by_context.get(context, "")
            similarity = SequenceMatcher(None, excerpt, previous_excerpt).ratio() if excerpt and previous_excerpt else 0.0
            if similarity >= 0.94:
                return True, "cooldown_context_repeticao"
            return True, "cooldown_context"

        return False, ""

    def build_screen_proactive_event(
        self,
        *,
        should_notify: bool,
        reason: str,
        context: str,
        priority: str,
        cooldown_blocked: bool,
        timestamp: float,
        confidence: float,
        subtype: str,
        excerpt: str,
        keywords: list[str],
    ) -> ScreenProactiveDecision:
        return ScreenProactiveDecision(
            should_notify=bool(should_notify),
            reason=str(reason or ""),
            screen_context=str(context or "neutro"),
            priority=str(priority or "low"),
            cooldown_blocked=bool(cooldown_blocked),
            timestamp=float(timestamp),
            confidence=round(float(confidence), 2),
            subtype=str(subtype or "desconhecido"),
            text_excerpt=str(excerpt or "")[:180],
            source_keywords=list(keywords),
        )

    def _build_reason(self, *, context: str, subtype: str, priority: str) -> str:
        if context == "erro":
            return "erro_importante"
        if context == "confirmacao":
            return "confirmacao_importante"
        if context == "login":
            return "login_senha_detectado"
        if context == "carregamento":
            return "carregamento_persistente"
        if context == "sucesso":
            return "sucesso_relevante"
        if context == "formulario":
            return "formulario_relevante"
        return f"contexto_{context}_{priority}"

