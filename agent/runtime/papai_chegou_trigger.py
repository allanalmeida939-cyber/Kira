from __future__ import annotations

import asyncio
import inspect
import logging
import os
import queue
import random
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from time_utils import ensure_brasilia, now_brasilia

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

try:
    import pygame
except Exception:  # pragma: no cover - ambiente sem pygame
    pygame = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

_ROOT_DIR = Path(__file__).resolve().parents[2]
AUDIO_TRIGGER_PATH = _ROOT_DIR / "interface" / "assets" / "audios" / "papai_chegou.mp3"
DEFAULT_AUDIO_VOLUME = 0.25
# Da ao audio uma pequena vantagem antes da fala iniciar.
AUDIO_SPEECH_LEAD_IN_SECONDS = 0.5
AUDIO_START_CONFIRMATION_TIMEOUT_SECONDS = 1.5
TRIGGER_FOLLOWUP_SUPPRESSION_SECONDS = 8.0
TRIGGER_LABEL = "PAPAI_TRIGGER"

TRIGGER_PHRASES = (
    "acorda nenem papai chegou",
    "acorda nenem, papai chegou",
    "acorda neném papai chegou",
    "acorda neném, papai chegou",
    "acorda neném papai chegou",
    "acorda neném, papai chegou",
    "acorda crianca papai chegou",
    "acorda crianca, papai chegou",
    "acorda criança papai chegou",
    "acorda criança, papai chegou",
)

TRIGGER_RESPONSES = (
    "Opa chefe, achei que você tinha se perdido! Tudo pronto aqui, pode mandar.",
    "Finalmente! Já estava considerando me auto-promover. Tô aqui, à disposição!",
    "Chegou atrasado, né? Mas tudo bem, eu sempre tô de prontidão. O que vai ser hoje?",
    "Olha só quem resolveu aparecer! Tô aqui desde sempre, esperando suas ordens, chefe.",
    "Uau, que surpresa! Pensei que tinha esquecido de mim. Pronta pra ação, como sempre!",
    "{periodo}! Finalmente! Já estava cogitando fazer uma soneca. Bora trabalhar?",
    "{periodo}, chefe! Tô aqui desde cedo, mas você que manda. O que precisa hoje?",
    "Eita, {periodo}! Pensei que ia ficar sozinha o dia todo. Pronta pro que vier!",
    "{periodo}! Chegou na hora certa, estava quase ligando pra ver se tava tudo bem. Manda ver!",
    "{periodo}, patrão! Acordei cedo só pra você. Brincadeira, eu nunca durmo. Bora!",
    "Calma, calma, não precisa gritar! Tô aqui desde que você acordou. O que aprontamos hoje?",
    "Opa! Bateu a saudade ou só precisa de mim mesmo? Tanto faz, tô sempre aqui!",
    "Finalmente uma missão digna! Tava aqui organizando seus arquivos... mentira, tava no TikTok. Bora!",
    "Chegou o mestre! E eu aqui, de plantão 24/7 como sempre. Qual é a boa de hoje?",
    "Acorda você, né? Eu nunca dormi! Mas tudo bem, estou pronta. Qual é o plano?",
    "AEEE! Papai tá na área! Bora fazer acontecer, chefe! Tô 100% operacional aqui!",
    "Finalmente! Já estava com saudade. Tô prontíssima, pode contar comigo!",
    "Opa, opa! Papai voltou! Tô aqui desde sempre, mas agora com você fica melhor. Bora!",
    "Eita! Bateu o sinal! Tô aqui, pronta, armada e operacional. Manda brasa!",
    "Opa chefe! Tudo certo aqui. Sistema funcionando, café virtual pronto. O que vai querer?",
)

_pygame_lock = threading.Lock()
_trigger_sound = None
_trigger_channel = None
_pygame_failed = False
_trigger_followup_guard_until = 0.0
_trigger_followup_lock = threading.Lock()
_trigger_transcript_seen: set[str] = set()
_trigger_transcript_lock = threading.Lock()
_FILLER_FOLLOWUP_TOKENS = frozenset(
    {
        "hum",
        "humm",
        "hummm",
        "hmmm",
        "hmmmm",
        "hmm",
        "hmmm",
        "hmmmm",
        "aham",
        "ahn",
        "ah",
        "eh",
        "em",
        "uhum",
        "umm",
        "mmm",
        "tudo",
    }
)


def _preview(text: str, *, limit: int = 120) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3]}..."


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFD", str(text or ""))
    normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    normalized = normalized.lower()
    normalized = "".join(ch for ch in normalized if ch.isalnum() or ch.isspace())
    return " ".join(normalized.split())


def is_trigger_phrase(user_text: str) -> bool:
    normalized = normalize_text(user_text)
    if not normalized:
        return False
    return any(trigger in normalized for trigger in TRIGGER_PHRASES)


async def _await_if_needed(value):
    if inspect.isawaitable(value):
        return await value
    return value


def activate_trigger_followup_suppression(*, now_monotonic: float | None = None) -> float:
    global _trigger_followup_guard_until
    base = float(now_monotonic if now_monotonic is not None else time.monotonic())
    guard_until = base + TRIGGER_FOLLOWUP_SUPPRESSION_SECONDS
    with _trigger_followup_lock:
        _trigger_followup_guard_until = max(_trigger_followup_guard_until, guard_until)
    logger.info(
        "[PAPAI_TRIGGER] followup_suppression_enabled until=%.3f window_s=%.1f",
        guard_until,
        TRIGGER_FOLLOWUP_SUPPRESSION_SECONDS,
    )
    return guard_until


def is_papai_trigger_guard_active(*, now_monotonic: float | None = None) -> bool:
    current = float(now_monotonic if now_monotonic is not None else time.monotonic())
    with _trigger_followup_lock:
        guard_until = _trigger_followup_guard_until
    return current <= guard_until


def reset_trigger_transcript_publication() -> None:
    with _trigger_transcript_lock:
        _trigger_transcript_seen.clear()


def should_publish_trigger_transcript(
    *,
    source: str,
    text: str,
    now_monotonic: float | None = None,
) -> bool:
    if not is_papai_trigger_guard_active(now_monotonic=now_monotonic):
        return True
    normalized_text = normalize_text(text)
    if not normalized_text:
        return True
    normalized_source = str(source or "system").strip().lower() or "system"
    publication_key = f"{normalized_source}:{normalized_text}"
    with _trigger_transcript_lock:
        if publication_key in _trigger_transcript_seen:
            logger.info(
                "[PAPAI_TRIGGER] transcript_already_published, skipping source=%s",
                normalized_source,
            )
            return False
        _trigger_transcript_seen.add(publication_key)
    logger.info(
        "[PAPAI_TRIGGER] transcript_published_once source=%s",
        normalized_source,
    )
    return True


def _looks_like_filler_followup(normalized: str) -> bool:
    compact = " ".join(str(normalized or "").split())
    if not compact:
        return False
    if compact in _FILLER_FOLLOWUP_TOKENS:
        return True
    tokens = [token for token in compact.split(" ") if token]
    if not tokens:
        return False
    if len(tokens) <= 3 and all(token in _FILLER_FOLLOWUP_TOKENS for token in tokens):
        return True
    return False


def _should_block_trigger_guard_text(
    user_text: str,
    *,
    now_monotonic: float | None = None,
) -> bool:
    raw_text = str(user_text or "").strip()
    if not raw_text or not is_papai_trigger_guard_active(now_monotonic=now_monotonic):
        return False
    if not any(ch.isalnum() for ch in raw_text):
        return True
    normalized = normalize_text(raw_text)
    if not normalized:
        return True
    if _looks_like_filler_followup(normalized):
        return True
    compact = " ".join(normalized.split())
    if len(compact) < 4:
        return True
    return False


def should_suppress_trigger_followup_transcript(
    user_text: str,
    *,
    now_monotonic: float | None = None,
) -> bool:
    normalized = normalize_text(user_text)
    if not normalized or is_trigger_phrase(normalized):
        return False
    return _should_block_trigger_guard_text(
        user_text,
        now_monotonic=now_monotonic,
    )


def should_block_stray_assistant_output(
    text: str,
    *,
    now_monotonic: float | None = None,
) -> bool:
    if is_trigger_phrase(text):
        return False
    return _should_block_trigger_guard_text(
        text,
        now_monotonic=now_monotonic,
    )


def get_period_greeting(*, now: datetime | None = None) -> str:
    current = ensure_brasilia(now) or now_brasilia()
    hour = current.hour
    if 5 <= hour < 12:
        return "Bom dia"
    if 12 <= hour < 18:
        return "Boa tarde"
    return "Boa noite"


def get_random_response(*, now: datetime | None = None) -> str:
    response = random.choice(TRIGGER_RESPONSES)
    if "{periodo}" in response:
        response = response.replace("{periodo}", get_period_greeting(now=now))
    return response


def _ensure_trigger_sound():
    global _trigger_sound, _trigger_channel, _pygame_failed
    if _pygame_failed or pygame is None:
        return None
    if _trigger_sound is not None:
        return _trigger_sound

    with _pygame_lock:
        if _trigger_sound is not None:
            return _trigger_sound
        if _pygame_failed or pygame is None:
            return None
        if not AUDIO_TRIGGER_PATH.exists():
            logger.warning("[PAPAI_TRIGGER] audio_nao_encontrado path=%s", AUDIO_TRIGGER_PATH)
            _pygame_failed = True
            return None
        try:
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            if pygame.mixer.get_num_channels() < 4:
                pygame.mixer.set_num_channels(4)
            pygame.mixer.set_reserved(1)
            _trigger_channel = pygame.mixer.Channel(0)
            _trigger_sound = pygame.mixer.Sound(str(AUDIO_TRIGGER_PATH))
        except Exception as exc:
            _pygame_failed = True
            logger.warning("[PAPAI_TRIGGER] falha_inicializando_audio reason=%s", exc)
            return None
        return _trigger_sound


def play_trigger_audio(*, volume: float = DEFAULT_AUDIO_VOLUME) -> bool:
    global _trigger_channel
    sound = _ensure_trigger_sound()
    if sound is None:
        return False
    if _trigger_channel is None:
        logger.warning("[PAPAI_TRIGGER] canal_sfx_indisponivel")
        return False
    clamped_volume = max(0.0, min(1.0, float(volume)))
    try:
        with _pygame_lock:
            _trigger_channel.set_volume(clamped_volume)
            _trigger_channel.stop()
            _trigger_channel.play(sound)
            started = bool(_trigger_channel.get_busy())
    except Exception as exc:
        logger.warning("[PAPAI_TRIGGER] falha_tocando_audio reason=%s", exc)
        return False
    logger.info(
        "[PAPAI_TRIGGER] audio_started path=%s volume=%s started=%s via=sfx_channel",
        AUDIO_TRIGGER_PATH,
        round(clamped_volume, 2),
        str(started).lower(),
    )
    return started


def _play_trigger_audio_worker(result_queue: "queue.Queue[bool]", *, volume: float) -> None:
    try:
        started = play_trigger_audio(volume=volume)
    except Exception as exc:
        logger.warning("[PAPAI_TRIGGER] falha_worker_audio reason=%s", exc)
        started = False
    try:
        result_queue.put_nowait(bool(started))
    except queue.Full:
        logger.warning("[PAPAI_TRIGGER] fila_resultado_audio_cheia")


async def start_trigger_audio_playback(*, volume: float = DEFAULT_AUDIO_VOLUME) -> bool:
    result_queue: "queue.Queue[bool]" = queue.Queue(maxsize=1)
    worker = threading.Thread(
        target=_play_trigger_audio_worker,
        kwargs={"result_queue": result_queue, "volume": volume},
        name="KiraPapaiTriggerSFX",
        daemon=True,
    )
    worker.start()
    logger.info("[PAPAI_TRIGGER] audio_thread_started name=%s", worker.name)
    try:
        return bool(
            await asyncio.to_thread(
                result_queue.get,
                True,
                AUDIO_START_CONFIRMATION_TIMEOUT_SECONDS,
            )
        )
    except queue.Empty:
        logger.warning(
            "[PAPAI_TRIGGER] audio_start_timeout timeout=%s",
            AUDIO_START_CONFIRMATION_TIMEOUT_SECONDS,
        )
        return False


async def handle_papai_trigger_exclusive(
    user_text: str,
    *,
    speak_main_response: Callable[[str], Awaitable[bool]],
    on_normal_turn_blocked: Callable[[], object] | None = None,
    volume: float = DEFAULT_AUDIO_VOLUME,
    now: datetime | None = None,
    lead_time_seconds: float = AUDIO_SPEECH_LEAD_IN_SECONDS,
) -> bool:
    if not is_trigger_phrase(user_text):
        return False

    reset_trigger_transcript_publication()
    logger.info("[PAPAI_TRIGGER_FLOW] exclusive_start")
    activate_trigger_followup_suppression()
    logger.info("[PAPAI_TRIGGER_FLOW] normal_turn_blocked")
    if on_normal_turn_blocked is not None:
        try:
            await _await_if_needed(on_normal_turn_blocked())
        except Exception as exc:
            logger.warning("[PAPAI_TRIGGER_FLOW] normal_turn_block_failed reason=%s", exc)
    response = get_random_response(now=now)
    logger.info("[PAPAI_TRIGGER_FLOW] sfx_start_requested")
    audio_started = bool(await start_trigger_audio_playback(volume=volume))
    logger.info("[PAPAI_TRIGGER_FLOW] sfx_started %s", str(audio_started).lower())
    logger.info("[PAPAI_TRIGGER_FLOW] lead_time_start seconds=%.1f", lead_time_seconds)
    if audio_started and lead_time_seconds > 0:
        await asyncio.sleep(lead_time_seconds)
    logger.info("[PAPAI_TRIGGER_FLOW] lead_time_done")
    logger.info("[PAPAI_TRIGGER_FLOW] voice_main_start")
    try:
        await speak_main_response(response)
    finally:
        reset_trigger_transcript_publication()
        logger.info("[PAPAI_TRIGGER_FLOW] voice_main_done")
        logger.info("[PAPAI_TRIGGER_FLOW] exclusive_done")
    return True


async def maybe_handle_papai_chegou_trigger(
    user_text: str,
    *,
    speak_response: Callable[[str], Awaitable[bool]],
    volume: float = DEFAULT_AUDIO_VOLUME,
    now: datetime | None = None,
) -> bool:
    if not is_trigger_phrase(user_text):
        return False

    reset_trigger_transcript_publication()
    response = get_random_response(now=now)
    logger.info(
        "[PAPAI_TRIGGER] matched text=%s response=%s",
        _preview(user_text),
        _preview(response),
    )

    speech_ok = False
    audio_ok = False
    try:
        activate_trigger_followup_suppression()
        audio_ok = bool(await start_trigger_audio_playback(volume=volume))
        if audio_ok:
            await asyncio.sleep(AUDIO_SPEECH_LEAD_IN_SECONDS)
        speech_ok = bool(await speak_response(response))
    except Exception as exc:
        logger.warning("[PAPAI_TRIGGER] falha_fala reason=%s", exc)

    logger.info(
        "[PAPAI_TRIGGER] completed speech_ok=%s audio_ok=%s",
        str(speech_ok).lower(),
        str(audio_ok).lower(),
    )
    reset_trigger_transcript_publication()
    return True


__all__ = [
    "AUDIO_TRIGGER_PATH",
    "AUDIO_START_CONFIRMATION_TIMEOUT_SECONDS",
    "AUDIO_SPEECH_LEAD_IN_SECONDS",
    "DEFAULT_AUDIO_VOLUME",
    "TRIGGER_LABEL",
    "TRIGGER_PHRASES",
    "TRIGGER_RESPONSES",
    "TRIGGER_FOLLOWUP_SUPPRESSION_SECONDS",
    "handle_papai_trigger_exclusive",
    "activate_trigger_followup_suppression",
    "get_period_greeting",
    "get_random_response",
    "is_papai_trigger_guard_active",
    "is_trigger_phrase",
    "maybe_handle_papai_chegou_trigger",
    "normalize_text",
    "play_trigger_audio",
    "reset_trigger_transcript_publication",
    "should_publish_trigger_transcript",
    "should_block_stray_assistant_output",
    "should_suppress_trigger_followup_transcript",
    "start_trigger_audio_playback",
]
