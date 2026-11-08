from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


# Pool de frases para ausência — escolhidas aleatoriamente com anti-repetição
_IDLE_PHRASES: list[str] = [
    "Tem alguem ai?",
    "Saiu?",
    "Oiiiiiiii...",
    "To sozinha mesmo?",
    "Ei, ainda ta comigo?",
    "Sumiu ai, hein?",
    "Vou ficar falando sozinha?",
    "Chefe, ta por ai?",
    "Ainda me ouvindo?",
    "Foi ali e ja volta?",
    "Abandonada de novo?",
    "Tem vida desse lado ai?",
    "To aqui ainda, viu?",
    "Volta aqui rapidinho.",
    "Silencio total por ai...",
    "Acho que voce sumiu mesmo.",
    "Ainda ta na escuta?",
    "To esperando voce voltar.",
    "Oi... alguem responde?",
    "Sera que fui esquecida?",
    "To aqui de plantao.",
    "Posso falar com voce ou sumiu de vez?",
]

# Cooldown mínimo entre falas de ausência (segundos)
_IDLE_SPEECH_COOLDOWN_SECONDS = 90.0
PROACTIVE_IDLE_SECONDS = 600  # 10 minutos - usuario precisa estar ausente 10min


def _pick_idle_phrase(last_phrase: str | None) -> str:
    """Escolhe frase aleatória do pool, evitando repetir a última."""
    pool = [p for p in _IDLE_PHRASES if p != last_phrase]
    return random.choice(pool or _IDLE_PHRASES)


@dataclass(slots=True)
class ProactiveState:
    last_interaction_time: datetime | None = None
    last_proactive_time: datetime | None = None
    interaction_count: int = 0
    silence_duration: float = 0.0
    recent_actions: list[str] = field(default_factory=list)
    pending_count: int = 0
    recent_postpones: int = 0
    recent_completed: int = 0
    last_trigger: str = ""
    last_idle_phrase: str | None = None
    last_idle_speech_time: datetime | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def should_trigger_proactive(
    contexto: dict[str, object] | None,
    estado_sessao: ProactiveState,
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    current_now = now or _utcnow()
    contexto = contexto or {}
    tipo = str(contexto.get("tipo", "") or "").strip().lower()
    intensidade = float(contexto.get("intensidade", 0.0) or 0.0)

    if tipo in {"tecnico", "emocional"} or intensidade >= 0.85:
        return False, "contexto"

    if estado_sessao.last_proactive_time is not None:
        if (current_now - estado_sessao.last_proactive_time) < timedelta(seconds=90):
            return False, "cooldown"

    silence_seconds = 0.0
    if estado_sessao.last_interaction_time is not None:
        silence_seconds = max(0.0, (current_now - estado_sessao.last_interaction_time).total_seconds())
    estado_sessao.silence_duration = silence_seconds

    if silence_seconds < PROACTIVE_IDLE_SECONDS:
        return False, "usuario_ativo"

    return True, "ok"


def generate_proactive_message(
    contexto: dict[str, object] | None,
    estado: ProactiveState,
    *,
    now: datetime | None = None,
) -> tuple[str | None, str]:
    current_now = now or _utcnow()
    del contexto
    silence_seconds = estado.silence_duration

    # Mensagens contextuais (pendências/ações) têm prioridade sobre ausência pura
    if estado.pending_count >= 6:
        return "Chefe, isso aqui ja ta com cara de acumulado. Quer que eu organize rapido?", "pendencias"
    if estado.recent_postpones >= 3:
        return "Voce ta empurrando isso faz tempo... quer resolver agora e encerrar?", "padrao"
    if estado.recent_completed >= 2 and silence_seconds >= 30:
        return "Hoje voce ta rendendo bem. Quer que eu ja puxe o proximo?", "pos_acao"

    # Modo ausência: fala proativa humana com cooldown e anti-repetição
    if silence_seconds >= PROACTIVE_IDLE_SECONDS:
        if estado.last_idle_speech_time is not None:
            elapsed = (current_now - estado.last_idle_speech_time).total_seconds()
            if elapsed < _IDLE_SPEECH_COOLDOWN_SECONDS:
                return None, "idle_cooldown"
        phrase = _pick_idle_phrase(estado.last_idle_phrase)
        estado.last_idle_phrase = phrase
        estado.last_idle_speech_time = current_now
        logger.info(
            "[PROATIVO] trigger=ausencia idle_s=%.1f threshold_s=%s pending=%s",
            silence_seconds,
            PROACTIVE_IDLE_SECONDS,
            estado.pending_count,
        )
        return phrase, "ausencia"

    return None, "none"
