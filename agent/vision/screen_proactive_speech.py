from __future__ import annotations

import os
import time
from dataclasses import dataclass
from difflib import SequenceMatcher


SCREEN_SPEECH_GLOBAL_COOLDOWN_SECONDS = max(
    float(os.getenv("KIRA_SCREEN_SPEECH_GLOBAL_COOLDOWN_SECONDS", "26.0")),
    5.0,
)
SCREEN_SPEECH_CONTEXT_COOLDOWN_SECONDS = max(
    float(os.getenv("KIRA_SCREEN_SPEECH_CONTEXT_COOLDOWN_SECONDS", "38.0")),
    8.0,
)
SCREEN_SPEECH_SUCCESS_MIN_CONFIDENCE = max(
    float(os.getenv("KIRA_SCREEN_SPEECH_SUCCESS_MIN_CONFIDENCE", "0.72")),
    0.5,
)


@dataclass(slots=True)
class ScreenSpeechDecision:
    emitted: bool
    message: str
    reason: str
    context: str
    voice_attempted: bool


class ScreenProactiveSpeech:
    def __init__(self) -> None:
        self._last_emit_at = 0.0
        self._last_by_context: dict[str, float] = {}
        self._last_message_by_context: dict[str, str] = {}
        self._variant_counter: dict[str, int] = {}

    def should_emit_screen_speech(
        self,
        event: dict[str, object],
        *,
        now: float,
        visual_context_active: bool,
        agent_speaking: bool,
        user_context_type: str,
    ) -> tuple[bool, str]:
        if not visual_context_active:
            return False, "visual_context_off"
        if agent_speaking:
            return False, "agent_speaking"
        if user_context_type in {"tecnico_intenso", "technical_intense", "focado"}:
            return False, "user_technical_intense"

        if not bool(event.get("should_notify")):
            return False, "proactive_engine_blocked"

        context = str(event.get("screen_context") or "neutro").strip().lower()
        priority = str(event.get("priority") or "low").strip().lower()
        confidence = float(event.get("confidence") or 0.0)
        excerpt = str(event.get("text_excerpt") or "").strip().lower()

        if context == "neutro":
            return False, "neutral_context"
        if priority == "low" and context == "sucesso" and confidence < SCREEN_SPEECH_SUCCESS_MIN_CONFIDENCE:
            return False, "low_priority_success_held"

        if self._last_emit_at > 0 and (now - self._last_emit_at) < SCREEN_SPEECH_GLOBAL_COOLDOWN_SECONDS:
            return False, "global_cooldown"

        last_context_at = self._last_by_context.get(context, 0.0)
        if last_context_at > 0 and (now - last_context_at) < SCREEN_SPEECH_CONTEXT_COOLDOWN_SECONDS:
            previous = self._last_message_by_context.get(context, "")
            similarity = SequenceMatcher(None, excerpt, previous).ratio() if excerpt and previous else 0.0
            if similarity >= 0.92:
                return False, "context_repeat"
            return False, "context_cooldown"

        return True, "ok"

    def build_screen_proactive_message(self, event: dict[str, object]) -> str:
        context = str(event.get("screen_context") or "neutro").strip().lower()
        subtype = str(event.get("subtype") or "").strip().lower()
        reason = str(event.get("reason") or "").strip().lower()

        templates = {
            "erro": [
                "Chefe... isso aqui parece um erro.",
                "Chefe, apareceu um erro aqui na tela.",
                "Chefe, isso aqui deu cara de falha.",
            ],
            "login": [
                "Chefe, isso aqui parece tela de login.",
                "Chefe, apareceu um pedido de login e senha aqui.",
                "Chefe, parece que essa parte pede autenticação.",
            ],
            "confirmacao": [
                "Chefe, apareceu uma confirmação aqui. Quer que eu acompanhe?",
                "Chefe, isso aqui está pedindo confirmação.",
                "Chefe, tem uma decisão de confirmar ou cancelar nessa tela.",
            ],
            "carregamento": [
                "Chefe, isso ainda está carregando há um tempo.",
                "Chefe, esse carregamento parece persistente.",
                "Chefe, essa tela continua processando sem concluir.",
            ],
            "sucesso": [
                "Chefe, parece que isso foi concluído com sucesso.",
                "Chefe, deu sinal de sucesso aqui na tela.",
                "Chefe, isso aqui parece ter sido salvo.",
            ],
        }

        if context == "erro" and subtype == "falha_login":
            templates["erro"] = [
                "Chefe, parece erro de login ou senha.",
                "Chefe, isso aqui está com falha na autenticação.",
                "Chefe, deu erro justo na parte de login.",
            ]
        if context == "confirmacao" and reason == "confirmacao_importante":
            templates["confirmacao"] = [
                "Chefe, essa confirmação parece importante. Quer que eu fique de olho?",
                "Chefe, apareceu uma confirmação crítica aqui.",
                "Chefe, essa tela está pedindo uma decisão de confirmação.",
            ]

        options = templates.get(context, [])
        if not options:
            return ""
        idx = self._variant_counter.get(context, 0) % len(options)
        self._variant_counter[context] = idx + 1
        return options[idx]

    def remember_emit(self, event: dict[str, object], message: str, *, now: float) -> None:
        context = str(event.get("screen_context") or "neutro").strip().lower()
        excerpt = str(event.get("text_excerpt") or "").strip().lower()
        self._last_emit_at = now
        self._last_by_context[context] = now
        self._last_message_by_context[context] = excerpt or message.lower().strip()

