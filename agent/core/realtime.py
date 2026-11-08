from __future__ import annotations

from pathlib import Path
import asyncio
import weakref
import contextlib
import logging
import os
import re
import json
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from enum import Enum
import numpy as np

from google.genai import types as genai_types
from livekit import rtc
from livekit.agents import APIConnectOptions, APIConnectionError, llm, utils
from livekit.plugins import google
from livekit.plugins.google.realtime import realtime_api as google_realtime_api

if __package__ and __package__.startswith("agent"):
    from agent.gemini_key_manager import (
        activate_current_gemini_api_key,
        get_key_manager,
        mark_current_gemini_key_working,
        rotate_to_next_usable_gemini_api_key,
    )
else:  # pragma: no cover - suporte a execucao direta em agent/
    from gemini_key_manager import (
        activate_current_gemini_api_key,
        get_key_manager,
        mark_current_gemini_key_working,
        rotate_to_next_usable_gemini_api_key,
    )
from resilience import is_rate_limit_error
from runtime.audio_denoiser import AudioDenoiser
from runtime.audio_enhancer import AudioEnhancer
from runtime.security_actions import escalate, show_voice_enrollment, show_voice_label
from runtime.speaker_guard import SpeakerGuard, clean_name
from runtime.transcript_filter import is_empty_user_input_with_reason
from runtime.voice_enrollment import TARGET_SECONDS as ENROLL_TARGET_SECONDS, TIMEOUT_SECONDS as ENROLL_TIMEOUT_SECONDS, VoiceEnrollment
ENROLL_RESAVE_SECONDS = 120.0  # logo depois de salvar, o Gemini às vezes pedia outra gravação da mesma pessoa
NOTE_PREFIX = "Nota do sistema (não é fala do usuário; não leia em voz alta, não repita estas palavras): "
# "cancela"/"para de gravar" durante a gravação da voz
ENROLL_CANCEL_RE = re.compile(r"\b(?:cancela|cancelar|cancele|desiste|para de gravar|pare de gravar|parar de gravar)\b", re.IGNORECASE)
ENROLLING_TOOL_OUTPUT = "Ignorado: a Kira está gravando a voz da pessoa. Não faça nada até a nota do sistema."
# Impressão da própria voz da Kira (eco): os primeiros segundos de áudio que ela gera na sessão
OWN_VOICE_SAMPLE_SECONDS = 4.0
from runtime.voice_activity_detector import VoiceActivityDetector
from runtime.voice_watchdog import note_tool_call, note_tool_response, tool_wait_state
try:
    from .realtime_config import (
        DEFAULT_GEMINI_REALTIME_TEMPERATURE,
        DEFAULT_GEMINI_REALTIME_VOICE,
        get_gemini_realtime_config,
    )
except Exception:  # pragma: no cover - suporte a execucao direta em agent/core
    from realtime_config import (
        DEFAULT_GEMINI_REALTIME_TEMPERATURE,
        DEFAULT_GEMINI_REALTIME_VOICE,
        get_gemini_realtime_config,
    )


logger = logging.getLogger(__name__)
# Rastreio do turno de voz: fica no log da sessão (core.realtime só registra avisos).
trace_logger = logging.getLogger("runtime.gemini_trace")
SENTENCE_END_RE = re.compile(r"[.!?…][\s\"')\]»]*$")
# Tools de interface não pausam a fala: o Gemini continua narrando enquanto a UI muda
# (bloqueantes, partiam o turno e a continuação vinha curta, vazia ou falando o nome da tool).
UI_SILENT_TOOLS = frozenset(
    {"show_media", "plan_visuals", "show_videos", "briefing_section", "recall_image", "close_panels", "focus_media"}
)
UI_TOOLS_NON_BLOCKING = os.getenv("KIRA_GEMINI_UI_TOOLS_NON_BLOCKING", "1").strip().lower() not in {"0", "false", "no", "off"}
TURN_COMPLETE_GRACE_SECONDS = 3.0
LATE_UI_TOOL_SPOKEN_CHARS = 120
REPEAT_PRONE_UI_TOOLS = frozenset({"plan_visuals", "show_media", "show_videos"})
WORD_RE = re.compile(r"[^\W\d_]{2,}")
# O Gemini transcreve ruído como "<noise>": a etiqueta não é palavra dita (20:13 de 2026-10-21 desligou o microfone).
TRANSCRIPT_TAG_RE = re.compile(r"<[^<>]{1,20}>")
AGENT_ANSWER_DEDUP_SECONDS = 45.0
# Medição de latência: quadro do microfone acima disto conta como voz (~-44 dBFS; o microfone do usuário capta baixo).
VOICE_RMS_THRESHOLD = 0.006
VOICE_TURN_WINDOW_SECONDS = 6.0
# Resposta automática do Gemini a uma fala: por esta janela o agente não pede outra resposta para a mesma fala.
AUTO_REPLY_OWNS_VOICE_SECONDS = 12.0
NOISE_TOOL_OUTPUT = "Nenhum pedido foi ouvido (só ruído); nada foi feito. Não responda."
# Voz guardada para reconhecer quem falou: só trechos com voz, dos últimos segundos (antes era tudo, sem limite).
VOICED_KEEP_SECONDS = 8.0
# Silêncio que fecha um trecho de fala: cada trecho é identificado (quem falou) assim que termina.
SEGMENT_END_SILENCE_SECONDS = 0.7
# Depois da última amostra de áudio da Kira, o fim da fala dela ainda sai do alto-falante (rede, buffer de suavização):
# esse trecho não entra no reconhecimento de quem fala.
OWN_SPEECH_TAIL_SECONDS = 0.8
# Aviso de desconexão do servidor (go_away, a cada ~10-15 min): sobra tempo para terminar a ferramenta e a fala.
GO_AWAY_RECONNECT_MARGIN_SECONDS = 10.0
GENERATION_CREATED_TIMEOUT_SECONDS = max(float(os.getenv("KIRA_GEMINI_GENERATION_CREATED_TIMEOUT_SECONDS", "15") or 15), 5.0)


def is_gemini_live_session_not_found_error(error: object) -> bool:
    message = str(error or "").strip().lower()
    if not message:
        return False
    session_missing_fragments = (
        "bidigeneratecontent session not found",
        "session not found",
        "session missing",
        "gemini session missing",
        "gemini session not found",
        "live session not found",
    )
    return "1008" in message and any(fragment in message for fragment in session_missing_fragments)


class GeminiRealtimeState(str, Enum):
    CONNECTING = "connecting"
    READY = "ready"
    RECONNECTING = "reconnecting"
    FAILED = "failed"
    COOLDOWN = "cooldown"


def _browser_secret_expected() -> bool:
    """O navegador da Kira está esperando a senha do Chefe: a próxima fala dele é a senha."""
    try:
        try:
            from agent.core import browser_task_state as bts
        except ImportError:  # execução de dentro de agent/ sem o pacote
            from core import browser_task_state as bts
        return bool(bts.is_browser_secret_expected())
    except Exception:
        return False


def _heard_for_log(text: str) -> str:
    """O que o Gemini ouviu, para o log. Com o navegador esperando senha, a fala é a senha: não vai para o log."""
    return "[senha]" if _browser_secret_expected() else text


# Com a senha esperada, só as ferramentas do navegador rodam: no teste o Gemini mandou a senha falada para a busca na
# internet (2026-10-25), como já tinha feito com o texto lido na gravação da voz.
SECRET_SAFE_TOOLS = frozenset({"navegador_responder", "navegador_parar"})
# Mexer na tela é inofensivo: passa mesmo quando a transcrição da fala veio vazia ("."). O Gemini entende o áudio e a
# transcrição às vezes falha (2026-10-31: "oculta as APIs" chegou como "." e a ação era jogada fora como ruído).
# Thinking Orbs: estágio real mostrado na tela enquanto a ferramenta roda (prefixo do nome -> estado, segundos)
KIRA_LOCK_FLAG = Path(__file__).resolve().parents[2] / "runtime" / "kira_locked"
ACTIVITY_BY_TOOL = (
    (("web_search", "pesquisa", "rota", "show_media", "show_videos", "recall_image", "descrever_tela", "locate_on_screen", "instagram"), "buscando", 90),
    (("whatsapp", "casa", "celular", "tocar_youtube", "music", "enviar_aviso"), "conectando", 180),
    (("estudo", "documento", "criar", "creation", "trabalho", "excluir", "mover", "copiar"), "trabalhando", 240),
    (("plan_visuals", "briefing", "get_briefing"), "trancando", 60),
    (("aparencia", "painel", "close_panels", "move_panel", "open_window", "controlar_janela"), "moldando", 20),
)
# Os segundos são só um teto de segurança: o fim real vem de _announce_activity_done quando a resposta da ferramenta sai.


def _post_activity(payload: dict) -> None:
    try:
        from runtime.media_tools import _post_local
    except ImportError:  # pragma: no cover - execução como pacote
        from agent.runtime.media_tools import _post_local

    async def send() -> None:  # aviso de tela: falha de rede não importa
        try:
            await _post_local("/api/ui/activity", payload)
        except Exception:
            pass

    try:
        asyncio.get_running_loop().create_task(send())
    except RuntimeError:
        pass


def _activity_for(name: str):
    for prefixes, state, seconds in ACTIVITY_BY_TOOL:
        if name.startswith(prefixes):
            return state, seconds
    return None


def _announce_activity(names: list[str]) -> None:
    for name in names:
        hit = _activity_for(name)
        if hit:
            _post_activity({"state": hit[0], "label": name, "seconds": hit[1]})
            return


def _announce_activity_done() -> None:
    _post_activity({"state": "pronto", "done": True})


NOISE_SAFE_TOOLS = frozenset({"close_panels", "list_active_panels", "focus_media", "painel", "aparencia"})
# Não há pré-confirmação para tools de tela: "ok" só pode ser dito depois que a própria tool
# retorna o resultado real (inclusive ACK da interface para aparência).
QUICK_SPOKEN_TOOLS = frozenset()
ONE_AT_A_TIME_TOOLS = frozenset({"documento", "criar_imagem", "create_image", "criar_video"})
SECRET_TOOL_OUTPUT = "A fala do Chefe era a senha que o navegador pediu; nada foi feito com ela. Não repita a senha."


def _env_float(*names: str, default: float, minimum: float) -> float:
    for name in names:
        raw = os.getenv(name)
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        try:
            return max(float(text), minimum)
        except (TypeError, ValueError):
            logger.warning(
                "REALTIME_ENV_FLOAT_WARNING name=%s value=%r default=%.1f",
                name,
                raw,
                default,
            )
    return max(float(default), minimum)

GEMINI_BACKEND_DEVELOPER = "developer"
GEMINI_BACKEND_VERTEX = "vertexai"
DEFAULT_GEMINI_LIVE_DEVELOPER_MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"
DEFAULT_GEMINI_LIVE_VERTEX_MODEL = "gemini-live-2.5-flash-native-audio"
DEFAULT_GEMINI_LIVE_VOICE = DEFAULT_GEMINI_REALTIME_VOICE
DEFAULT_GEMINI_LIVE_TEMPERATURE = DEFAULT_GEMINI_REALTIME_TEMPERATURE
DEFAULT_PROACTIVE_TTS_PROVIDER = "google"
DEFAULT_CONSOLE_STT_PROVIDER = "auto"

GEMINI_LIVE_MAX_RETRIES = max(int(os.getenv("GEMINI_LIVE_MAX_RETRIES", "10")), 0)
GEMINI_LIVE_RETRY_BASE_DELAY_SECONDS = max(
    float(os.getenv("GEMINI_LIVE_RETRY_BASE_DELAY_SECONDS", "2")),
    0.1,
)
GEMINI_LIVE_RETRY_MAX_DELAY_SECONDS = max(
    float(os.getenv("GEMINI_LIVE_RETRY_MAX_DELAY_SECONDS", "60")),
    GEMINI_LIVE_RETRY_BASE_DELAY_SECONDS,
)
GEMINI_LIVE_RETRY_BACKOFF_MULTIPLIER = max(
    float(os.getenv("GEMINI_LIVE_RETRY_BACKOFF_MULTIPLIER", "2")),
    1.0,
)
GEMINI_LIVE_CONNECT_TIMEOUT_SECONDS = max(
    float(os.getenv("GEMINI_LIVE_CONNECT_TIMEOUT_SECONDS", "20")),
    5.0,
)
GEMINI_LIVE_WS_OPEN_TIMEOUT_SECONDS = max(
    float(os.getenv("GEMINI_LIVE_WS_OPEN_TIMEOUT_SECONDS", "12")),
    5.0,
)
GEMINI_LIVE_WS_CLOSE_TIMEOUT_SECONDS = max(
    float(os.getenv("GEMINI_LIVE_WS_CLOSE_TIMEOUT_SECONDS", "10")),
    1.0,
)
GEMINI_LIVE_READY_TIMEOUT_SECONDS = max(
    float(os.getenv("GEMINI_LIVE_READY_TIMEOUT_SECONDS", "20")),
    1.0,
)
GEMINI_LIVE_READY_POLL_SECONDS = max(
    float(os.getenv("GEMINI_LIVE_READY_POLL_SECONDS", "0.05")),
    0.01,
)
GEMINI_LIVE_MAX_UNRECOVERABLE_ERRORS = max(
    int(os.getenv("GEMINI_LIVE_MAX_UNRECOVERABLE_ERRORS", "8")),
    1,
)
GEMINI_HEARTBEAT_FRAME_MS = max(
    int(os.getenv("GEMINI_HEARTBEAT_FRAME_MS", "20")),
    10,
)
GEMINI_GENERATE_REPLY_TIMEOUT_SECONDS = max(
    _env_float(
        "GENERATION_TIMEOUT_S",
        "KIRA_GEMINI_GENERATE_REPLY_TIMEOUT_SECONDS",
        default=30.0,
        minimum=1.0,
    ),
    1.0,
)
GEMINI_GENERATE_REPLY_MAX_ATTEMPTS = max(
    int(os.getenv("KIRA_GEMINI_GENERATE_REPLY_MAX_ATTEMPTS", "2")),
    1,
)
GEMINI_GENERATE_REPLY_STALE_QUEUE_SECONDS = max(
    float(os.getenv("KIRA_GEMINI_GENERATE_REPLY_STALE_QUEUE_SECONDS", "15.0")),
    1.0,
)
GEMINI_DISCONNECT_WARNING_SECONDS = max(
    int(os.getenv("KIRA_GEMINI_DISCONNECT_WARNING_SECONDS", "60")),
    1,
)
GEMINI_LIVE_RETRY_INITIAL_CONNECTION_FOREVER = os.getenv(
    "GEMINI_LIVE_RETRY_INITIAL_CONNECTION_FOREVER",
    "1",
).strip().lower() not in {"0", "false", "no", "off"}

_DURATION_SECONDS_RE = re.compile(
    r"^\s*([+-]?\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)?\s*$",
    re.IGNORECASE,
)


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, *, minimum: int) -> int:
    try:
        return max(int(os.getenv(name, "").strip() or default), minimum)
    except ValueError:
        return default


def parse_duration_seconds(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value or "").strip()
    if not text:
        return None
    match = _DURATION_SECONDS_RE.match(text)
    if not match:
        logger.warning("REALTIME_DURATION_PARSE_WARNING raw=%r", value)
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError) as exc:
        logger.warning("REALTIME_DURATION_PARSE_WARNING raw=%r reason=%s", value, exc)
        return None


def resolve_gemini_backend() -> str:
    configured = os.getenv("KIRA_GEMINI_BACKEND", "").strip().lower()
    if configured in {GEMINI_BACKEND_DEVELOPER, GEMINI_BACKEND_VERTEX}:
        return configured
    if _env_flag("GOOGLE_GENAI_USE_VERTEXAI", default=False):
        return GEMINI_BACKEND_VERTEX
    return GEMINI_BACKEND_DEVELOPER


def resolve_gemini_live_model() -> str:
    if resolve_gemini_backend() == GEMINI_BACKEND_VERTEX:
        default_model = DEFAULT_GEMINI_LIVE_VERTEX_MODEL
    else:
        default_model = DEFAULT_GEMINI_LIVE_DEVELOPER_MODEL
    return get_gemini_realtime_config(default_model=default_model, logger=logger).model


def resolve_gemini_live_voice() -> str:
    return get_gemini_realtime_config(default_model=resolve_gemini_live_model(), logger=logger).voice


def resolve_gemini_live_temperature() -> float:
    return get_gemini_realtime_config(default_model=resolve_gemini_live_model(), logger=logger).temperature


def should_use_gemini_native_audio() -> bool:
    return _env_flag("KIRA_GEMINI_NATIVE_AUDIO", default=True)


def should_use_console_stt_bridge() -> bool:
    return _env_flag("KIRA_USE_CONSOLE_STT_BRIDGE", default=True)


def resolve_console_stt_provider() -> str:
    provider = os.getenv("KIRA_CONSOLE_STT_PROVIDER", DEFAULT_CONSOLE_STT_PROVIDER).strip().lower()
    return provider or DEFAULT_CONSOLE_STT_PROVIDER


def resolve_proactive_tts_provider() -> str:
    provider = os.getenv("KIRA_PROACTIVE_TTS_PROVIDER", DEFAULT_PROACTIVE_TTS_PROVIDER).strip().lower()
    return provider or DEFAULT_PROACTIVE_TTS_PROVIDER


def should_use_fallback_tts_for_proactive() -> bool:
    if "KIRA_TTS_FALLBACK_ENABLED" in os.environ:
        return _env_flag("KIRA_TTS_FALLBACK_ENABLED", default=True)
    if "KIRA_USE_FALLBACK_TTS_FOR_PROACTIVE" in os.environ:
        return _env_flag("KIRA_USE_FALLBACK_TTS_FOR_PROACTIVE", default=True)
    if "KIRA_USE_FALLBACK_TTS" in os.environ:
        return _env_flag("KIRA_USE_FALLBACK_TTS", default=False)
    if "KIRA_ENABLE_DEDICATED_TTS" in os.environ:
        return _env_flag("KIRA_ENABLE_DEDICATED_TTS", default=False)
    return not should_use_gemini_native_audio()


def should_enable_gemini_proactivity() -> bool:
    return _env_flag("KIRA_GEMINI_PROACTIVITY", default=False)


def should_enable_gemini_affective_dialog() -> bool:
    return _env_flag("KIRA_GEMINI_AFFECTIVE_DIALOG", default=False)


def should_use_fallback_tts() -> bool:
    return should_use_fallback_tts_for_proactive()


def should_prefer_native_audio_for_kira_messages() -> bool:
    return should_use_gemini_native_audio()


def complete_transcript(published: str, full: str) -> str:
    """O transcript sincronizado do LiveKit perde o fim quando a transcrição chega depois do áudio;
    se ele é um prefixo do texto gerado, vale o texto completo."""
    squash = lambda value: " ".join(str(value or "").split())
    short, whole = squash(published), squash(full)
    if short and len(whole) > len(short) and whole.startswith(short):
        return whole
    return short


def build_activity_detection_config() -> genai_types.RealtimeInputConfig | None:
    """Barge-in real: a Kira para quando o usuário fala (>= prefix_padding_ms de fala).

    Sensibilidade LOW + 400 ms fazia o Gemini ignorar quase toda fala em volume normal (sessão de
    2026-10-21: 1 de ~10 falas transcritas); o eco da própria Kira já sai pelo AEC do navegador."""
    sensitivity = os.getenv("KIRA_GEMINI_START_SENSITIVITY", "high").strip().lower()
    if sensitivity == "default":
        return None
    barge_in = os.getenv("KIRA_GEMINI_BARGE_IN", "1").strip().lower() not in {"0", "false", "no", "off"}
    return genai_types.RealtimeInputConfig(
        activity_handling=(
            genai_types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS
            if barge_in
            else genai_types.ActivityHandling.NO_INTERRUPTION
        ),
        automatic_activity_detection=genai_types.AutomaticActivityDetection(
            start_of_speech_sensitivity=(
                genai_types.StartSensitivity.START_SENSITIVITY_LOW
                if sensitivity == "low"
                else genai_types.StartSensitivity.START_SENSITIVITY_HIGH
            ),
            prefix_padding_ms=_env_int("KIRA_GEMINI_BARGE_IN_MIN_SPEECH_MS", 200, minimum=100),
            # 700 -> 500 -> 400 ms: cada 100 ms de silêncio exigido somam ~100 ms antes da resposta. 2026-10-31: a sessão
            # media 0,8-1,9 s entre o fim da fala e o Gemini perceber; com fim de fala sensível ele fecha o turno antes.
            end_of_speech_sensitivity=(
                genai_types.EndSensitivity.END_SENSITIVITY_LOW
                if os.getenv("KIRA_GEMINI_END_SENSITIVITY", "high").strip().lower() == "low"
                else genai_types.EndSensitivity.END_SENSITIVITY_HIGH
            ),
            silence_duration_ms=_env_int("KIRA_GEMINI_END_SILENCE_MS", 300, minimum=150),
        ),
    )


def build_gemini_realtime_input_config(*, external_stt_enabled: bool) -> genai_types.RealtimeInputConfig | None:
    if not external_stt_enabled:
        return None
    return genai_types.RealtimeInputConfig(
        automatic_activity_detection=genai_types.AutomaticActivityDetection(
            disabled=True,
        )
    )


@dataclass(frozen=True)
class ExponentialBackoffConnectOptions(APIConnectOptions):
    max_retry_interval: float = GEMINI_LIVE_RETRY_MAX_DELAY_SECONDS
    backoff_multiplier: float = GEMINI_LIVE_RETRY_BACKOFF_MULTIPLIER

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.max_retry_interval < 0:
            raise ValueError("max_retry_interval must be greater than or equal to 0")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be greater than or equal to 1")

    def _interval_for_retry(self, num_retries: int) -> float:
        if num_retries <= 0:
            return 0.1

        interval = self.retry_interval * (self.backoff_multiplier ** (num_retries - 1))
        return min(interval, self.max_retry_interval)


def build_gemini_http_options(
    base_options: genai_types.HttpOptions | None = None,
) -> genai_types.HttpOptions:
    base = base_options or genai_types.HttpOptions()
    headers = dict(base.headers or {}) or None
    client_args = dict(base.client_args or {}) or None
    async_client_args = dict(base.async_client_args or {})
    async_client_args.setdefault("open_timeout", GEMINI_LIVE_WS_OPEN_TIMEOUT_SECONDS)
    async_client_args.setdefault("close_timeout", GEMINI_LIVE_WS_CLOSE_TIMEOUT_SECONDS)

    return genai_types.HttpOptions(
        base_url=base.base_url,
        base_url_resource_scope=base.base_url_resource_scope,
        api_version=base.api_version,
        headers=headers,
        timeout=base.timeout or int(GEMINI_LIVE_CONNECT_TIMEOUT_SECONDS * 1000),
        client_args=client_args,
        async_client_args=async_client_args or None,
        extra_body=base.extra_body,
        retry_options=base.retry_options,
        httpx_client=base.httpx_client,
        httpx_async_client=base.httpx_async_client,
        aiohttp_client=base.aiohttp_client,
    )


def resolve_current_gemini_api_key() -> str | None:
    if resolve_gemini_backend() == GEMINI_BACKEND_VERTEX:
        return None
    api_key, key_index = activate_current_gemini_api_key(
        logger_override=logger,
        reason="build_realtime_model",
        key_type="text",
    )
    if key_index is not None:
        logger.info("[GEMINI][LIVE] Preparando realtime com chave índice=%s", key_index)
    return api_key


def _current_gemini_key_slot_for_log() -> str:
    if resolve_gemini_backend() == GEMINI_BACKEND_VERTEX:
        return "vertex"
    try:
        return str(get_key_manager().current_text_index)
    except Exception:
        return "unknown"


@dataclass(slots=True)
class GeminiDisconnectWarningEvent:
    time_left_seconds: int


@dataclass(slots=True)
class _QueuedGenerationRequest:
    result_fut: asyncio.Future[llm.GenerationCreatedEvent]
    instructions: object
    queued_at: float


_active_session_ref: "weakref.ReferenceType[KiraRealtimeSession] | None" = None


def active_session() -> "KiraRealtimeSession | None":
    """Sessão Gemini Live atual (o envio da tela compartilhada usa)."""
    return _active_session_ref() if _active_session_ref is not None else None


class KiraRealtimeSession(google_realtime_api.RealtimeSession):
    def __init__(self, *args, **kwargs) -> None:
        global _active_session_ref
        super().__init__(*args, **kwargs)
        _active_session_ref = weakref.ref(self)
        self._generation_request_queue: asyncio.Queue[_QueuedGenerationRequest] = (
            asyncio.Queue()
        )
        self._generation_worker_task: asyncio.Task | None = None
        self._generation_in_progress = False
        self._last_client_content_at = 0.0
        self._synthetic_speech_start = False
        self._model_turn_open_after_tool = False
        self._starting_continuation = False
        self._generation_complete_ids: OrderedDict[str, None] = OrderedDict()
        self._turn_complete_timer: asyncio.TimerHandle | None = None
        self._preanswered_calls: OrderedDict[str, None] = OrderedDict()
        self._barged_in: OrderedDict[str, None] = OrderedDict()
        self._speech_expected_until = 0.0
        self._audio_received_s: dict[str, float] = {}
        self._turn_input_text = ""
        self._turn_has_client_text = False
        self._agent_answered_at = 0.0
        self._heard_since_answer = ""
        self._mute_next_continuation = False
        self._muted_generations: set[str] = set()
        self._late_calls: list[tuple[str, str]] = []
        self._tool_continuations: OrderedDict[str, None] = OrderedDict()
        self._completed_texts: OrderedDict[str, str] = OrderedDict()
        self._proactive_reconnect_task: asyncio.Task | None = None
        self._denoise_frame_count = 0
        self._denoise_sample_rate = int(os.getenv("AUDIO_SAMPLE_RATE", "48000"))
        self._denoise_enabled = _env_flag("ENABLE_AUDIO_DENOISING", default=True)
        self._audio_enhancer_enabled = _env_flag("ENABLE_AUDIO_ENHANCER", default=True)
        self._denoiser = AudioDenoiser(
            sample_rate=self._denoise_sample_rate,
            enabled=self._denoise_enabled,
        )
        stats = self._denoiser.get_stats()
        logger.info(
            "[STARTUP] Audio Denoiser - enabled=%s sample_rate=%s frame_size=%s backend=%s",
            stats["enabled"],
            stats["sample_rate"],
            stats["frame_size"],
            stats.get("backend", "unknown"),
        )
        self._audio_enhancer = AudioEnhancer(
            sample_rate=self._denoise_sample_rate,
            target_rms=float(os.getenv("AUDIO_AGC_TARGET_RMS", "0.1")),
            noise_gate_threshold_db=float(os.getenv("AUDIO_NOISE_GATE_DB", "-50.0")),
            ring_buffer_seconds=float(os.getenv("AUDIO_RING_BUFFER_SECONDS", "2.0")),
            enabled=self._audio_enhancer_enabled,
        )
        enhancer_stats = self._audio_enhancer.get_stats()
        logger.info(
            "[STARTUP] Audio Enhancer - enabled=%s sample_rate=%s ring_buffer=%.1fs target_rms=%s",
            enhancer_stats["enabled"],
            enhancer_stats["sample_rate"],
            float(enhancer_stats["ring_buffer_seconds"]),
            enhancer_stats["target_rms"],
        )
        # KIRA_CUSTOM_VAD_ENABLED controla o VAD local da Kira (Silero).
        # Por padrao False: usa endpointing nativo do LiveKit/Gemini.
        # Manter True apenas se houver ruído de ambiente muito alto.
        _kira_vad_enabled = _env_flag("KIRA_CUSTOM_VAD_ENABLED", default=False)
        # ENABLE_VAD mantido como override legado; KIRA_CUSTOM_VAD_ENABLED tem prioridade.
        if "ENABLE_VAD" in os.environ:
            _kira_vad_enabled = _env_flag("ENABLE_VAD", default=False)
        logger.info(
            "KIRA_CUSTOM_VAD_CONFIG enabled=%s reason=%s",
            _kira_vad_enabled,
            "env_override" if "KIRA_CUSTOM_VAD_ENABLED" in os.environ or "ENABLE_VAD" in os.environ
            else "use_livekit_gemini_default",
        )
        if not _kira_vad_enabled:
            logger.info("KIRA_CUSTOM_VAD_DISABLED")
            logger.info("KIRA_USING_DEFAULT_ENDPOINTING provider=livekit_gemini")
        self._vad = VoiceActivityDetector(
            sample_rate=self._denoise_sample_rate,
            threshold_start=float(os.getenv("VAD_THRESHOLD_START", os.getenv("VAD_THRESHOLD", "0.30"))),
            threshold_continue=float(os.getenv("VAD_THRESHOLD_CONTINUE", os.getenv("VAD_THRESHOLD", "0.20"))),
            min_speech_duration_ms=int(os.getenv("VAD_MIN_SPEECH_MS", "250")),
            min_silence_duration_ms=int(os.getenv("VAD_MIN_SILENCE_MS", "600")),
            speech_pad_ms=int(os.getenv("VAD_SPEECH_PAD_MS", "300")),
            enabled=_kira_vad_enabled,
        )
        self._assistant = None
        self._session_active = True
        self._last_activity_at = time.monotonic()
        self._warmup_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._economy_standby = False
        self._economy_standby_changed = asyncio.Event()
        self._voiced_audio: deque[tuple[float, np.ndarray]] = deque()
        self._segment_started_at: float | None = None
        self.speaker_guard: SpeakerGuard | None = None
        self._ask_speaker = False
        self._enrollment: VoiceEnrollment | None = None
        self._enrollment_timer: asyncio.TimerHandle | None = None
        self._own_voice_chunks: list[np.ndarray] = []
        self._own_voice_samples = 0
        self._own_voice_done = False
        self._enrolled_at: dict[str, float] = {}
        self._kira_realtime_state = GeminiRealtimeState.CONNECTING
        self._kira_realtime_degraded = False
        self._voice_audio_degraded = False
        self._voice_audio_degraded_reason = ""
        self._live_session_dead = False
        self._live_session_dead_reason = ""
        self._voice_refresh_in_progress = False
        self._gemini_restart_lock = asyncio.Lock()
        self.gemini_restart_in_progress = False
        self._live_session_epoch = 0
        self._kira_consecutive_connect_failures = 0
        self._kira_fast_reconnect_limit = 2
        self._gemini_1008_recovery_in_progress = False
        self._reconnect_started_at = 0.0
        self._last_go_away_at = 0.0
        # 1007 'invalid argument': algo no histórico reenviado na reconexão o Gemini recusa, e cada reconexão reenviava o
        # mesmo histórico (teste de 2026-10-30: 16 erros seguidos, Kira muda por minutos). Na próxima conexão vai sem histórico.
        self._drop_history_on_connect = False
        logger.info(
            "[AUDIO] VAD inicializado - enabled=%s threshold_start=%.2f threshold_continue=%.2f min_speech_ms=%d speech_pad=%dms",
            _kira_vad_enabled,
            self._vad.threshold_start,
            self._vad.threshold_continue,
            self._vad.min_speech_duration_ms,
            self._vad.speech_pad_ms,
        )

    def bind_assistant(self, assistant) -> None:
        self._assistant = assistant
        book = getattr(assistant, "_voice_book", None)
        if book is not None and book.enabled:
            self.speaker_guard = SpeakerGuard(book, on_label=show_voice_label, on_escalate=escalate)

    @property
    def kira_realtime_state(self) -> str:
        return self._kira_realtime_state.value

    @property
    def kira_realtime_degraded(self) -> bool:
        return bool(self._kira_realtime_degraded)

    @property
    def voice_audio_degraded(self) -> bool:
        return bool(self._voice_audio_degraded)

    @property
    def voice_audio_degraded_reason(self) -> str:
        return str(self._voice_audio_degraded_reason or "")

    @property
    def live_session_dead(self) -> bool:
        return bool(self._live_session_dead)

    @property
    def live_session_dead_reason(self) -> str:
        return str(self._live_session_dead_reason or "")

    def mark_voice_audio_degraded(self, reason: str) -> None:
        self._voice_audio_degraded = True
        self._voice_audio_degraded_reason = str(reason or "gemini_audio_degraded")
        logger.warning(
            "VOICE_AVAILABLE_FALSE reason=gemini_audio_degraded detail=%s",
            self._voice_audio_degraded_reason,
        )

    def clear_pending_voice_degradation(self, reason: str) -> bool:
        """Desfaz só a degradação que esperava a próxima fala para ser checada ("pre_turn_pending:..."), quando o áudio
        da Kira já voltou a tocar. Sessão morta, reconexão ou outra degradação continuam com o caminho de recuperação."""
        if not self._voice_audio_degraded or self._live_session_dead:
            return False
        if not str(self._voice_audio_degraded_reason or "").startswith("pre_turn_pending"):
            return False
        previous = self._voice_audio_degraded_reason
        self._voice_audio_degraded = False
        self._voice_audio_degraded_reason = ""
        logger.info("VOICE_AVAILABLE_TRUE reason=%s previous=%s", reason, previous)
        return True

    def mark_gemini_session_dead(self, reason: str) -> None:
        normalized_reason = str(reason or "session_not_found").strip() or "session_not_found"
        self._live_session_dead = True
        self._live_session_dead_reason = normalized_reason
        self._voice_audio_degraded = True
        self._voice_audio_degraded_reason = normalized_reason
        self._kira_realtime_degraded = True
        self._mark_live_ready(False)
        self._set_live_ready_failure(RuntimeError(normalized_reason))
        self._set_kira_realtime_state(GeminiRealtimeState.FAILED, reason=normalized_reason)
        logger.error("GEMINI_LIVE_SESSION_NOT_FOUND_CLASSIFIED reason=%s", normalized_reason)
        logger.error("GEMINI_SESSION_DEAD_MARKED reason=%s", normalized_reason)
        logger.warning("TEXT_TURN_NOT_BLOCKED_BY_DEAD_GEMINI reason=%s", normalized_reason)

    def _set_kira_realtime_state(self, state: GeminiRealtimeState, *, reason: str = "") -> None:
        old = self._kira_realtime_state
        if old == state and (state != GeminiRealtimeState.FAILED or self._kira_realtime_degraded):
            return
        self._kira_realtime_state = state
        if state == GeminiRealtimeState.RECONNECTING:
            self._reconnect_started_at = time.monotonic()
        elif state in {GeminiRealtimeState.READY, GeminiRealtimeState.FAILED, GeminiRealtimeState.COOLDOWN}:
            self._reconnect_started_at = 0.0
        self._kira_realtime_degraded = state in {GeminiRealtimeState.RECONNECTING, GeminiRealtimeState.FAILED, GeminiRealtimeState.COOLDOWN}
        logger.info(
            "GEMINI_STATUS_PUBLISHED state=%s degraded=%s reason=%s",
            state.value,
            str(self._kira_realtime_degraded).lower(),
            reason or "none",
        )
        if self._kira_realtime_degraded:
            logger.warning("GEMINI_REALTIME_DEGRADED state=%s reason=%s", state.value, reason or "unknown")

    def _is_ready_for_generation(self) -> bool:
        return (
            self.is_live_connected
            and self._kira_realtime_state == GeminiRealtimeState.READY
            and not self._kira_realtime_degraded
            and not self._voice_audio_degraded
            and not self.gemini_restart_in_progress
        )

    def turn_voice_audio(self, since: float = 0.0) -> np.ndarray:
        """Voz (16 kHz) dos últimos segundos (ou desde `since`): a fala do turno atual, para saber quem pediu."""
        chunks = [chunk for at, chunk in self._voiced_audio if at >= since]
        return np.concatenate(chunks).astype(np.float32, copy=False) if chunks else np.array([], dtype=np.float32)

    def _identify_speaker(self, since: float) -> None:
        guard = getattr(self, "speaker_guard", None)
        if guard is None or not guard.active:
            return
        audio = self.turn_voice_audio(since)
        loop = asyncio.get_running_loop()

        def done(future: asyncio.Future) -> None:
            verdict = None if future.cancelled() or future.exception() else future.result()
            if verdict is not None and guard.should_ask() is not None:
                self._ask_speaker = True

        loop.run_in_executor(None, guard.identify, audio).add_done_callback(done)

    def start_voice_enrollment(self, name: str) -> str:
        """registrar_voz: começa a gravar a voz da pessoa (a barra aparece na tela). A frase do pedido já conta."""
        guard = self.speaker_guard
        if guard is None:
            return "O reconhecimento de voz não está disponível agora."
        if self._enrollment is not None:
            # O Gemini chamava de novo a cada frase e cada chamada zerava a barra (sessão de teste de 2026-10-23).
            return (f"Já estou gravando a voz de {self._enrollment.name} ({self._enrollment.progress:.0%} da barra). Não "
                    "chame de novo nem fale: fique em silêncio até a nota do sistema dizer se a voz foi salva.")
        problem = guard.enroll_problem(name)
        if problem:
            return problem
        clean = clean_name(name).title()
        if time.monotonic() - self._enrolled_at.get(clean, -ENROLL_RESAVE_SECONDS) < ENROLL_RESAVE_SECONDS:
            return f"A voz de {clean} acabou de ser salva. Não precisa gravar de novo: só confirme em uma frase curta."
        self._cancel_enrollment_timer()
        enrollment = VoiceEnrollment(clean, on_progress=lambda progress: show_voice_enrollment("gravando", clean, progress))
        self._enrollment = enrollment
        self._enrollment_timer = asyncio.get_running_loop().call_later(ENROLL_TIMEOUT_SECONDS, self._expire_enrollment, enrollment)
        trace_logger.info("VOICE_ENROLL_STARTED name=%s", clean)
        if enrollment.feed(self.turn_voice_audio()):
            self._finish_enrollment()
        return (f"Gravando a voz de {clean}: uma barra na tela enche enquanto a pessoa fala (uns "
                f"{ENROLL_TARGET_SECONDS:.0f} segundos de fala no total, em várias frases) e mostra o que falar: frase normal, "
                "comandos, mais alto, mais baixo. Diga em UMA frase curta para ela seguir o que aparece na barra até encher. "
                "Enquanto grava você fica em silêncio (o sistema não toca nada do que você disser). Quando encher, chega "
                "uma nota do sistema dizendo se a voz foi salva; só então fale. Nunca diga que salvou antes disso e não "
                "chame registrar_voz de novo enquanto grava.")

    def _feed_enrollment(self, audio_16k: np.ndarray) -> None:
        if self._enrollment is not None and self._enrollment.feed(audio_16k):
            self._finish_enrollment()

    def _cancel_enrollment_timer(self) -> None:
        if self._enrollment_timer is not None:
            self._enrollment_timer.cancel()
            self._enrollment_timer = None

    def _finish_enrollment(self) -> None:
        """Barra cheia: para de gravar, salva numa thread e avisa o Gemini (que dá o ok em voz)."""
        enrollment, self._enrollment = self._enrollment, None
        self._cancel_enrollment_timer()
        guard = self.speaker_guard
        if enrollment is None or guard is None:
            return
        show_voice_enrollment("salvando", enrollment.name, 1.0)
        loop = asyncio.get_running_loop()

        def done(future: asyncio.Future) -> None:
            if future.cancelled() or future.exception() is not None:
                ok, message = False, "Deu um erro ao salvar a voz; peça para tentar de novo."
                logger.warning("VOICE_ENROLL_FAILED error=%s", None if future.cancelled() else future.exception())
            else:
                ok, message = future.result()
            show_voice_enrollment("salva" if ok else "falhou", enrollment.name, 1.0, message)
            if ok:
                self._enrolled_at[enrollment.name] = time.monotonic()
            trace_logger.info("VOICE_ENROLL_DONE name=%s ok=%s", enrollment.name, ok)
            self._send_enrollment_note(
                f"a gravação terminou e a voz FOI SALVA. {message} A pessoa pode parar de falar: diga do seu jeito, em uma "
                "frase curta, que deu certo. Não chame registrar_voz de novo."
                if ok else f"a gravação terminou e a voz NÃO foi salva. {message} Explique em uma frase curta e ofereça tentar de novo."
            )

        loop.run_in_executor(None, guard.enroll, enrollment.name, enrollment.audio()).add_done_callback(done)

    def _expire_enrollment(self, enrollment: VoiceEnrollment) -> None:
        if self._enrollment is not enrollment:
            return
        self._enrollment = None
        self._enrollment_timer = None
        show_voice_enrollment("cancelado", enrollment.name, enrollment.progress)
        trace_logger.info("VOICE_ENROLL_EXPIRED name=%s progress=%.2f", enrollment.name, enrollment.progress)
        self._send_enrollment_note(
            f"o cadastro da voz de {enrollment.name} parou por tempo e a voz NÃO foi salva: a pessoa não falou o "
            f"bastante ({enrollment.progress:.0%} da barra). Diga em uma frase curta e ofereça tentar de novo."
        )

    def _cancel_enrollment(self) -> None:
        """A pessoa disse "cancela" durante a gravação: para de gravar e a Kira confirma."""
        enrollment, self._enrollment = self._enrollment, None
        self._cancel_enrollment_timer()
        if enrollment is None:
            return
        show_voice_enrollment("cancelado", enrollment.name, enrollment.progress)
        trace_logger.info("VOICE_ENROLL_CANCELLED name=%s progress=%.2f", enrollment.name, enrollment.progress)
        self._send_enrollment_note(
            f"a pessoa pediu para cancelar a gravação da voz de {enrollment.name}; nada foi salvo. Confirme em uma frase curta."
        )

    def _send_enrollment_note(self, text: str) -> None:
        super()._send_client_event(genai_types.LiveClientContent(
            turns=[genai_types.Content(role="user", parts=[genai_types.Part(text=NOTE_PREFIX + text)])],
            turn_complete=True,
        ))

    def _collect_own_voice(self, server_content) -> None:
        """Primeiros segundos de fala da Kira na sessão viram a impressão da voz dela (para reconhecer o eco)."""
        guard = self.speaker_guard
        if guard is None or self._own_voice_done:
            return
        for part in server_content.model_turn.parts or []:
            data = part.inline_data.data if part.inline_data is not None else b""
            if data:
                pcm = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32767.0
                chunk = self._resample_to_16k(pcm, source_sample_rate=google_realtime_api.OUTPUT_AUDIO_SAMPLE_RATE)
                self._own_voice_chunks.append(chunk)
                self._own_voice_samples += chunk.size
        if self._own_voice_samples >= OWN_VOICE_SAMPLE_SECONDS * 16000:
            self._own_voice_done = True
            audio, self._own_voice_chunks = np.concatenate(self._own_voice_chunks), []
            asyncio.get_running_loop().run_in_executor(None, guard.set_own_voice, audio)

    def _ask_who_is_speaking(self, heard: str = "") -> None:
        """Voz desconhecida conversando: depois da resposta, a Kira pergunta quem é."""
        self._ask_speaker = False
        verdict = self.speaker_guard.current() if self.speaker_guard is not None else None
        # Ruído ou fala em outra língua não é alguém conversando (2026-10-24: som do ambiente virou "ไป เลย" e a Kira
        # perguntou "quem tá falando?" do nada); e se a última voz já é de alguém conhecido, não pergunta.
        garbage, why = is_empty_user_input_with_reason(heard)
        if garbage or (verdict is not None and verdict.role != "unknown"):
            if self.speaker_guard is not None:
                self.speaker_guard.cancel_ask()
            trace_logger.info("SPEAKER_ASK_WHO_SKIPPED reason=%s", why if garbage else f"last_voice_{verdict.role}")
            return
        kind = f" Parece uma voz {verdict.kind}." if verdict is not None and verdict.kind else ""
        super()._send_client_event(genai_types.LiveClientContent(
            turns=[genai_types.Content(role="user", parts=[genai_types.Part(text=(
                f"{NOTE_PREFIX}a última voz não é a do Chefe nem de ninguém cadastrado.{kind} "
                "Pergunte em uma frase curta, do seu jeito, quem está falando. Se a pessoa disser o nome, chame registrar_voz."
            ))])],
            turn_complete=True,
        ))
        trace_logger.info("SPEAKER_ASK_WHO_SENT")

    def _audio_frame_to_mono_float32(self, frame: rtc.AudioFrame) -> np.ndarray:
        channels = int(getattr(frame, "num_channels", 1) or 1)
        samples_per_channel = int(getattr(frame, "samples_per_channel", 0) or 0)
        if samples_per_channel <= 0:
            return np.array([], dtype=np.float32)
        pcm = np.frombuffer(frame.data, dtype=np.int16)
        if pcm.size != samples_per_channel * max(channels, 1):
            return np.array([], dtype=np.float32)
        if channels > 1:
            mono = pcm.reshape(samples_per_channel, channels).mean(axis=1)
        else:
            mono = pcm.astype(np.float32, copy=False)
        return (mono.astype(np.float32, copy=False) / 32767.0).clip(-1.0, 1.0)

    def _resample_to_16k(self, audio: np.ndarray, *, source_sample_rate: int) -> np.ndarray:
        if audio.size == 0:
            return np.array([], dtype=np.float32)
        if source_sample_rate <= 0 or source_sample_rate == 16000:
            return audio.astype(np.float32, copy=False)
        duration_s = len(audio) / float(source_sample_rate)
        if duration_s <= 0.0:
            return np.array([], dtype=np.float32)
        target_len = max(int(round(duration_s * 16000)), 1)
        source_x = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
        target_x = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
        resampled = np.interp(target_x, source_x, audio).astype(np.float32, copy=False)
        return resampled

    def _send_client_event(self, event) -> None:
        if isinstance(event, genai_types.LiveClientRealtimeInput) and any(
            str(chunk.mime_type or "").startswith("image/") for chunk in (event.media_chunks or [])
        ):
            self._video_frames_sent = getattr(self, "_video_frames_sent", 0) + 1
        # Conteúdo de cliente enviado durante uma geração faz o Gemini interromper a fala;
        # registra só o formato (papéis, tamanho), nunca o texto.
        if isinstance(event, genai_types.LiveClientContent):
            turns = event.turns or []
            roles = ",".join(str(getattr(turn, "role", "") or "?") for turn in turns)
            chars = sum(len(part.text or "") for turn in turns for part in (turn.parts or []))
            self._last_client_content_at = time.monotonic()
            self._turn_has_client_text = True
            trace_logger.info(
                "GEMINI_CLIENT_CONTENT_SENT turns=%s roles=%s chars=%s turn_complete=%s generating=%s",
                len(turns), roles, chars, str(bool(event.turn_complete)).lower(),
                str(bool(self._current_generation and not self._current_generation._done)).lower(),
            )
        elif isinstance(event, genai_types.LiveClientToolResponse):
            pending = getattr(self, "_activity_pending", None)
            if pending:
                pending.difference_update(str(r.id) for r in (event.function_responses or []) if r.id)
                if not pending:
                    _announce_activity_done()  # orbe e radar seguem o fim real da ferramenta, não um timer
            late = [r for r in (event.function_responses or []) if r.id and r.id in self._preanswered_calls]
            if late:
                for response in late:
                    self._preanswered_calls.pop(response.id, None)
                event.function_responses = [r for r in event.function_responses or [] if r not in late]
                if not event.function_responses:
                    return
            answered = sum(1 for r in (event.function_responses or []) if not (UI_TOOLS_NON_BLOCKING and r.name in UI_SILENT_TOOLS))
            if answered:
                note_tool_response(answered)
            if UI_TOOLS_NON_BLOCKING:
                for response in event.function_responses or []:
                    if response.name in UI_SILENT_TOOLS:
                        response.scheduling = genai_types.FunctionResponseScheduling.SILENT
            trace_logger.info(
                "GEMINI_TOOL_RESPONSE_SENT names=%s",
                ",".join(str(r.name or "?") for r in (event.function_responses or [])),
            )
        super()._send_client_event(event)

    def _start_new_generation(self) -> None:
        # O plugin emite input_speech_started antes de toda geração que o usuário não pediu (continuação depois
        # de uma tool, por exemplo). O LiveKit lia isso como "usuário falou": cortava a fala em andamento e o agente
        # segurava a continuação esperando uma transcrição que nunca vinha. Só o "interrupted" real do servidor conta.
        # Decidido antes do super(): o plugin emite generation_created (e o agente decide) dentro da chamada.
        self._starting_continuation = self._model_turn_open_after_tool
        pending = getattr(self, "_pending_generation_fut", None)
        manual = pending is not None and not pending.done()
        if not manual and not self._model_turn_open_after_tool:
            self._auto_reply_started_at = time.monotonic()
        self._synthetic_speech_start = True
        try:
            super()._start_new_generation()
        finally:
            self._synthetic_speech_start = False
            self._starting_continuation = False
        generation = self._current_generation
        self._mute_during_enrollment(generation)
        if generation is not None and not self._model_turn_open_after_tool:
            # Linha do tempo do turno: fim da voz do usuário -> geração criada -> primeiro áudio da resposta.
            self._turn_latency = {"rid": generation.response_id, "speech_end": getattr(self, "_last_voice_at", 0.0), "created": time.monotonic()}
        if self._model_turn_open_after_tool and generation is not None:
            self._tool_continuations[generation.response_id] = None
            while len(self._tool_continuations) > 20:
                self._tool_continuations.popitem(last=False)
            trace_logger.info("GEMINI_TOOL_CONTINUATION response_id=%s", generation.response_id)
            if getattr(self, "_mute_next_continuation", False):
                # A tool de interface chegou depois da resposta já falada; o Gemini continua sozinho e repete tudo
                # (mesmo sem resposta da tool). A continuação segue até o fim, mas sem áudio nem texto.
                self._mute_next_continuation = False
                self._muted_generations.add(generation.response_id)
                trace_logger.info("GEMINI_LATE_TOOL_CONTINUATION_MUTED response_id=%s", generation.response_id)

    def _close_after_missing_turn_complete(self, response_id: str) -> None:
        """O Gemini terminou o áudio (generation_complete) e o turn_complete demora: fecha só o áudio para a Kira sair
        de "falando" (sem isso o destravamento cortava e o pipeline acionava recuperação). O texto continua aberto:
        o fim da transcrição costuma chegar junto com o turn_complete."""
        self._turn_complete_timer = None
        generation = self._current_generation
        if generation is not None and not generation._done and generation.response_id == response_id and not generation.audio_ch.closed:
            trace_logger.info("GEMINI_AUDIO_CLOSED_AWAITING_TURN_COMPLETE response_id=%s grace_s=%.1f", response_id, TURN_COMPLETE_GRACE_SECONDS)
            generation.audio_ch.close()

    def send_screen_image(self, jpeg: bytes, caption: str = "Imagem da tela compartilhada agora (contexto visual; não responda a esta mensagem).") -> bool:
        """Quadro atual da tela compartilhada como imagem na conversa. O modelo de áudio nativo ignora o vídeo em tempo
        real (medido: "não estou recebendo imagem"), mas enxerga imagem dentro do turno. Só com a Kira quieta: conteúdo
        de cliente durante uma geração interrompe a fala; turn_complete=False não pede resposta."""
        generating = self._current_generation is not None and not self._current_generation._done
        user_speaking = time.monotonic() - getattr(self, "_last_voice_at", 0.0) < 1.5
        if generating or user_speaking or self.speech_playing(margin=0.5):
            return False
        # Envio direto do plugin: pelo _send_client_event da Kira o turno seguinte contaria como "texto do cliente" e
        # perderia a proteção contra ruído.
        google_realtime_api.RealtimeSession._send_client_event(self, genai_types.LiveClientContent(
            turns=[genai_types.Content(role="user", parts=[
                genai_types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"),
                genai_types.Part(text=caption),
            ])],
            turn_complete=False,
        ))
        trace_logger.info("SCREEN_IMAGE_SENT bytes=%d", len(jpeg))
        return True

    def speech_playing(self, *, margin: float = 1.0) -> bool:
        """Há áudio da Kira recebido e ainda tocando (soma da duração do áudio do Gemini); sinal real, não estado."""
        return time.monotonic() < self._speech_expected_until + margin

    def gemini_answering_voice(self) -> bool:
        """O Gemini já começou a responder sozinho à fala do usuário (antes de a transcrição chegar ao agente)."""
        started = getattr(self, "_auto_reply_started_at", 0.0)
        # A resposta precisa ser desta fala: começou depois da última voz do usuário (com folga de 1 s).
        after_this_speech = started >= getattr(self, "_last_voice_at", 0.0) - 1.0
        return bool(started) and after_this_speech and time.monotonic() - started < AUTO_REPLY_OWNS_VOICE_SECONDS

    def mute_generation(self, response_id: str) -> None:
        """Geração que não deve ser ouvida (resposta duplicada): áudio e texto descartados até o fim dela."""
        if response_id:
            if not hasattr(self, "_muted_generations"):
                self._muted_generations = set()
            self._muted_generations.add(response_id)
            trace_logger.info("GEMINI_GENERATION_MUTED response_id=%s", response_id)

    def duplicates_agent_answer(self) -> bool:
        """Geração automática para uma fala que o agente já respondeu (portão de memória, comando...).

        O Gemini responde sozinho ao áudio; quando o agente também responde à transcrição, a resposta automática
        (ou a continuação dela depois de uma tool) virava uma segunda fala (sessão de 2026-10-21)."""
        answered = getattr(self, "_agent_answered_at", 0.0)
        return bool(answered) and time.monotonic() - answered < AGENT_ANSWER_DEDUP_SECONDS and not WORD_RE.search(
            getattr(self, "_heard_since_answer", "")
        )

    def _request_generation(self, *, instructions: object = google_realtime_api.NOT_GIVEN) -> asyncio.Future[llm.GenerationCreatedEvent]:
        """Igual ao generate_reply do plugin, mas o limite para o Gemini começar a resposta é configurável: os 5 s fixos
        do plugin estouravam quando o modelo pensava ou chamava uma tool antes de falar, e a resposta chegava sem vínculo
        com o pedido (o agente bloqueava como "resposta automática")."""
        if self._pending_generation_fut and not self._pending_generation_fut.done():
            self._pending_generation_fut.cancel("Superseded by new generate_reply call")
        if WORD_RE.search(getattr(self, "_turn_input_text", "")):
            self._agent_answered_at = time.monotonic()
            self._heard_since_answer = ""
        fut: asyncio.Future[llm.GenerationCreatedEvent] = asyncio.Future()
        self._pending_generation_fut = fut
        if self._in_user_activity:
            self._send_client_event(genai_types.LiveClientRealtimeInput(activity_end=genai_types.ActivityEnd()))
            self._in_user_activity = False
        # O plugin mandava as instruções como fala do próprio modelo e fechava com um usuário dizendo ".";
        # o modelo de áudio respondia vazio (saudação muda, turno sem resposta). A instrução vai como nota
        # interna do turno do usuário, que é o turno que o Gemini responde.
        text = (
            f"[Nota interna para esta resposta, não leia em voz alta]\n{instructions}"
            if google_realtime_api.is_given(instructions) and str(instructions).strip()
            else "."
        )
        turns = [genai_types.Content(parts=[genai_types.Part(text=text)], role="user")]
        self._send_client_event(genai_types.LiveClientContent(turns=turns, turn_complete=True))

        def on_timeout() -> None:
            if not fut.done():
                fut.set_exception(llm.RealtimeError("generate_reply timed out waiting for generation_created event."))
                if self._pending_generation_fut is fut:
                    self._pending_generation_fut = None

        handle = asyncio.get_event_loop().call_later(GENERATION_CREATED_TIMEOUT_SECONDS, on_timeout)
        fut.add_done_callback(lambda _: handle.cancel())
        return fut

    def is_tool_continuation(self, response_id: str) -> bool:
        """Geração aberta pelo Gemini para continuar a mesma resposta depois de uma tool (não é resposta nova)."""
        if response_id in self._tool_continuations:
            return True
        generation = self._current_generation
        return self._starting_continuation and generation is not None and generation.response_id == response_id

    def _handle_input_speech_started(self) -> None:
        if self._synthetic_speech_start:
            trace_logger.info("GEMINI_SYNTHETIC_SPEECH_START_SUPPRESSED reason=agent_initiated_generation")
            return
        trace_logger.info(
            "BARGE_IN source=server_activity generating=%s",
            str(bool(self._current_generation and not self._current_generation._done)).lower(),
        )
        if self._current_generation is not None:
            self._barged_in[self._current_generation.response_id] = None
            while len(self._barged_in) > 20:
                self._barged_in.popitem(last=False)
        super()._handle_input_speech_started()

    def _mark_current_generation_done(self) -> None:
        generation = self._current_generation
        if generation is not None and not generation._done:
            if generation.output_text:
                self._completed_texts[generation.response_id] = generation.output_text
                while len(self._completed_texts) > 20:
                    self._completed_texts.popitem(last=False)
            getattr(self, "_muted_generations", set()).discard(generation.response_id)
            trace_logger.info(
                "GEMINI_AUDIO_RECEIVED response_id=%s seconds=%.2f chars=%s",
                generation.response_id,
                self._audio_received_s.pop(generation.response_id, 0.0),
                len(generation.output_text or ""),
            )
        super()._mark_current_generation_done()

    def full_output_text(self, response_id: str) -> str:
        return self._completed_texts.get(response_id, "")

    def _mute_during_enrollment(self, generation) -> None:
        """Gravando a voz: a pessoa fala para encher a barra e a Kira não responde (ela respondia, inventava "voz salva"
        antes da hora e o eco dela entrava na gravação; sessão de 2026-10-24 12:22). A continuação da própria ferramenta
        ("pode falar") e a nota final falam normalmente."""
        if generation is not None and getattr(self, "_enrollment", None) is not None and not self._model_turn_open_after_tool:
            self.mute_generation(generation.response_id)

    def was_barged_in(self, response_id: str) -> bool:
        """A resposta foi interrompida pela fala real do usuário (o texto publicado fica só com o que foi dito)."""
        return response_id in self._barged_in

    def _answer_late_calls(self) -> None:
        late = getattr(self, "_late_calls", [])
        if not late:
            return
        self._late_calls = []
        trace_logger.info("GEMINI_LATE_UI_TOOL_ANSWERED names=%s", ",".join(name for _, name in late))
        super()._send_client_event(genai_types.LiveClientToolResponse(function_responses=[
            genai_types.FunctionResponse(id=call_id, name=name, response={"output": "ok"}, scheduling=genai_types.FunctionResponseScheduling.SILENT)
            for call_id, name in late
        ]))

    def _heard_only_noise(self) -> bool:
        """O turno veio da voz e tudo o que o Gemini transcreveu não tem palavra (".", estalo, eco)."""
        heard = getattr(self, "_turn_input_text", "")
        return bool(heard.strip()) and not getattr(self, "_turn_has_client_text", False) and not WORD_RE.search(TRANSCRIPT_TAG_RE.sub(" ", heard))

    def _handle_tool_calls(self, tool_call) -> None:
        trace_logger.info(
            "GEMINI_TOOL_CALL names=%s generating=%s",
            ",".join(str(c.name or "?") for c in (tool_call.function_calls or [])),
            str(bool(self._current_generation and not self._current_generation._done)).lower(),
        )
        # Mesma ferramenta com os mesmos argumentos em poucos segundos (continuação + retomada do turno) executava
        # e confirmava duas vezes ("mandei pro Breno" repetido; controlar_janela 2x às 21:39:02). A cópia é respondida
        # sem executar. Ações de leitura repetidas de propósito ficam de fora pela janela curta.
        now = time.monotonic()
        recent = getattr(self, "_recent_tool_sigs", None)
        if recent is None:
            recent = self._recent_tool_sigs = {}
        for sig, at in list(recent.items()):
            if now - at > (30.0 if sig.startswith("ONE|") else 8.0):
                recent.pop(sig, None)
        dupes, fresh_calls = [], []
        for c in (tool_call.function_calls or []):
            if str(c.name or "") in ONE_AT_A_TIME_TOOLS:
                # criar trabalho/arquivo: 2ª chamada em 30 s, mesmo com texto diferente, era o mesmo pedido (2 PDFs
                # de Chernobyl e 2 avisos em 2026-11-05 07:17)
                sig = f"ONE|{c.name}"
            else:
                try:
                    sig = f"{c.name}|{json.dumps(c.args or {}, sort_keys=True, ensure_ascii=False, default=str)}"
                except Exception:
                    sig = f"{c.name}|{c.args!r}"
            if c.id and sig in recent:
                dupes.append(c)
            else:
                recent[sig] = now
                fresh_calls.append(c)
        if dupes:
            trace_logger.info("GEMINI_TOOL_CALL_DUPLICATE_SKIPPED names=%s", ",".join(str(c.name) for c in dupes))
            super()._send_client_event(genai_types.LiveClientToolResponse(function_responses=[
                genai_types.FunctionResponse(id=c.id, name=c.name, response={"output": "Chamada repetida ignorada: essa ação já foi feita há instantes. Não confirme de novo."},
                                             scheduling=genai_types.FunctionResponseScheduling.SILENT)
                for c in dupes
            ]))
            if not fresh_calls:
                return
            tool_call = tool_call.model_copy(update={"function_calls": fresh_calls})
        if self._heard_only_noise() and not all(str(c.name) in NOISE_SAFE_TOOLS for c in (tool_call.function_calls or [])):
            # Sensibilidade alta: ruído vira "fala" e o modelo inventava ações (placar, música, câmera).
            calls = [c for c in (tool_call.function_calls or []) if c.id]
            trace_logger.info("GEMINI_TOOL_CALL_REJECTED_NOISE names=%s heard=%r", ",".join(str(c.name) for c in calls), _heard_for_log(self._turn_input_text[:40]))
            # A atividade era anunciada antes do filtro e deixava o mini-orbe em "Conectando"
            # mesmo sem nenhuma ação. Silencia a continuação e não cria atividade para ruído.
            current_generation = self._current_generation
            if current_generation is not None and not current_generation._done:
                self.mute_generation(current_generation.response_id)
            self._mute_next_continuation = True
            for call in calls:
                self._preanswered_calls[call.id] = None
            super()._send_client_event(genai_types.LiveClientToolResponse(function_responses=[
                genai_types.FunctionResponse(id=c.id, name=c.name, response={"output": NOISE_TOOL_OUTPUT}, scheduling=genai_types.FunctionResponseScheduling.SILENT)
                for c in calls
            ]))
            return
        # Kira trancada (Fase 5): nenhuma ferramenta roda, só bloquear o PC. Voz nunca desbloqueia.
        if KIRA_LOCK_FLAG.exists():
            calls = [c for c in (tool_call.function_calls or []) if c.id]
            blocked = [c for c in calls if not (str(c.name) == "bloqueio" and "pc" in str(c.args or {}))]
            if blocked:
                for call in blocked:
                    self._preanswered_calls[call.id] = None
                super()._send_client_event(genai_types.LiveClientToolResponse(function_responses=[
                    genai_types.FunctionResponse(id=c.id, name=c.name, response={"output": "A Kira está bloqueada: nada é feito até digitarem a senha na tela. Diga isso em uma frase."})
                    for c in blocked
                ]))
                if len(blocked) == len(calls):
                    return
        if _browser_secret_expected():
            blocked_secret = [c for c in (tool_call.function_calls or []) if c.id and str(c.name) not in SECRET_SAFE_TOOLS]
            if blocked_secret:
                for call in blocked_secret:
                    self._preanswered_calls[call.id] = None
                trace_logger.info("GEMINI_TOOL_CALL_BLOCKED_SECRET names=%s", ",".join(str(c.name) for c in blocked_secret))
                super()._send_client_event(genai_types.LiveClientToolResponse(function_responses=[
                    genai_types.FunctionResponse(id=c.id, name=c.name, response={"output": SECRET_TOOL_OUTPUT}, scheduling=genai_types.FunctionResponseScheduling.SILENT)
                    for c in blocked_secret
                ]))
                kept = [c for c in (tool_call.function_calls or []) if all(c is not b for b in blocked_secret)]
                if not kept:
                    return
                tool_call = tool_call.model_copy(update={"function_calls": kept})
        generation = self._current_generation
        if getattr(self, "_enrollment", None) is not None and generation is not None and generation.response_id in self._muted_generations:
            # Gravando a voz, a resposta à fala da pessoa já está muda; a ferramenta dela também não roda (no teste o
            # Gemini pesquisou na internet o texto que a pessoa lia para encher a barra).
            calls = [c for c in (tool_call.function_calls or []) if c.id]
            for call in calls:
                self._preanswered_calls[call.id] = None
            trace_logger.info("GEMINI_TOOL_CALL_SKIPPED_ENROLLING names=%s", ",".join(str(c.name) for c in calls))
            super()._send_client_event(genai_types.LiveClientToolResponse(function_responses=[
                genai_types.FunctionResponse(id=c.id, name=c.name, response={"output": ENROLLING_TOOL_OUTPUT}, scheduling=genai_types.FunctionResponseScheduling.SILENT)
                for c in calls
            ]))
            return
        guard = getattr(self, "speaker_guard", None)
        if guard is not None:
            typed = bool(getattr(self, "_turn_has_client_text", False))
            blocked = [(c, v) for c in (tool_call.function_calls or []) if c.id and (v := guard.blocks(str(c.name), typed=typed))]
            if blocked:
                level = guard.note_blocked(blocked[0][1])  # insistência sobe o tom: câmera no 3, bloqueio no 4
                trace_logger.info("SPEAKER_TOOL_BLOCKED names=%s role=%s level=%d", ",".join(str(c.name) for c, _ in blocked), blocked[0][1].role, level)
                self._ask_speaker = False  # a recusa já pergunta quem é
                super()._send_client_event(genai_types.LiveClientToolResponse(function_responses=[
                    genai_types.FunctionResponse(id=c.id, name=c.name, response={"output": guard.refusal(v, level)}) for c, v in blocked
                ]))
                kept = [c for c in (tool_call.function_calls or []) if all(c is not b for b, _ in blocked)]
                if not kept:
                    return
                tool_call = tool_call.model_copy(update={"function_calls": kept})
        self._model_turn_open_after_tool = True
        calls = tool_call.function_calls or []
        waiting = sum(1 for c in calls if not (UI_TOOLS_NON_BLOCKING and c.name in UI_SILENT_TOOLS))
        if waiting:
            note_tool_call(waiting)
        if UI_TOOLS_NON_BLOCKING:
            # Tool de interface: o Gemini recebe "ok" na hora e segue narrando; o LiveKit executa a tool normalmente
            # e a resposta dele (que só sai depois de tocar o áudio) é descartada. Antes, a fala parava ~25 s esperando.
            ui_calls = [c for c in calls if c.name in UI_SILENT_TOOLS and c.id]
            if ui_calls:
                for call in ui_calls:
                    self._preanswered_calls[call.id] = None
                while len(self._preanswered_calls) > 40:
                    self._preanswered_calls.popitem(last=False)
                # Tool chamada depois de a resposta já ter sido falada: qualquer resposta (mesmo "não repita") fazia o
                # Gemini abrir uma continuação repetindo a história inteira (sessões de 2026-10-21). Sem resposta, a
                # tool é declarada NON_BLOCKING e o turno termina; o efeito (imagens) roda do mesmo jeito.
                spoken = len((self._current_generation.output_text or "") if self._current_generation is not None else "")
                # Só as tools que abrem imagens de uma história são "tardias"; briefing_section avança a narração e a
                # continuação dela é o resto do briefing (silenciá-la cortava notícias e lembretes).
                late = spoken >= LATE_UI_TOOL_SPOKEN_CHARS and all(c.name in REPEAT_PRONE_UI_TOOLS for c in ui_calls)
                trace_logger.info(
                    "GEMINI_UI_TOOL_PREANSWERED names=%s spoken_chars=%s response=%s",
                    ",".join(str(c.name) for c in ui_calls), spoken, "none_late" if late else "ok",
                )
                if late:
                    # Responde só quando o turno fechar: sem resposta o Gemini fica esperando e ignora a voz do
                    # usuário (teste de 2026-10-21: nenhuma fala transcrita depois da história).
                    self._mute_next_continuation = True
                    self._late_calls.extend((str(c.id), str(c.name)) for c in ui_calls)
                else:
                    super()._send_client_event(genai_types.LiveClientToolResponse(function_responses=[
                        genai_types.FunctionResponse(id=c.id, name=c.name, response={"output": "enviado para a tela, ainda sem confirmacao de exibicao; nao afirme que ja apareceu"}, scheduling=genai_types.FunctionResponseScheduling.SILENT)
                        for c in ui_calls
                    ]))
        # Só anuncia uma atividade depois de todos os filtros (ruído, segredo, lock,
        # enrollment e voz) permitirem a execução da chamada.
        _announce_activity([str(c.name or "") for c in calls])
        pending = getattr(self, "_activity_pending", None)
        if pending is None:
            pending = self._activity_pending = set()
        pending.update(str(c.id) for c in calls if c.id and _activity_for(str(c.name or "")))
        quick = [c for c in calls if c.name in QUICK_SPOKEN_TOOLS and c.id and c.id not in self._preanswered_calls]
        if quick:
            for call in quick:
                self._preanswered_calls[call.id] = None
            super()._send_client_event(genai_types.LiveClientToolResponse(function_responses=[
                genai_types.FunctionResponse(id=c.id, name=c.name, response={"output": "Feito na tela. Confirme em voz numa frase bem curta."})
                for c in quick
            ]))
        super()._handle_tool_calls(tool_call)

    def _handle_server_content(self, server_content) -> None:
        generation = self._current_generation
        if generation is not None and generation.response_id in getattr(self, "_muted_generations", ()):
            server_content = server_content.model_copy(update={"model_turn": None, "output_transcription": None})
        transcription = getattr(server_content, "input_transcription", None)
        if transcription is not None and transcription.text:
            self._turn_input_text = (getattr(self, "_turn_input_text", "") + transcription.text)[-400:]
            if getattr(self, "_enrollment", None) is not None and ENROLL_CANCEL_RE.search(self._turn_input_text):
                self._cancel_enrollment()
            if getattr(self, "_agent_answered_at", 0.0):
                self._heard_since_answer = (getattr(self, "_heard_since_answer", "") + transcription.text)[-200:]
        if server_content.turn_complete or server_content.interrupted:
            heard = getattr(self, "_turn_input_text", "")
            self._model_turn_open_after_tool = False
            self._turn_input_text = ""
            self._turn_has_client_text = False
            self._mute_next_continuation = False
            self._answer_late_calls()
            if server_content.turn_complete and getattr(self, "_ask_speaker", False):
                self._ask_who_is_speaking(heard)
        if server_content.interrupted:
            self._speech_expected_until = 0.0
        audio_bytes = sum(
            len(part.inline_data.data or b"")
            for part in ((server_content.model_turn.parts or []) if server_content.model_turn else [])
            if part.inline_data is not None
        )
        if audio_bytes:
            if not getattr(self, "_own_voice_done", True):
                self._collect_own_voice(server_content)
            now = time.monotonic()
            seconds = audio_bytes / (2 * google_realtime_api.OUTPUT_AUDIO_SAMPLE_RATE)
            self._speech_expected_until = max(self._speech_expected_until, now) + seconds
            if self._current_generation is not None:
                response_id = self._current_generation.response_id
                self._audio_received_s[response_id] = self._audio_received_s.get(response_id, 0.0) + seconds
                self._log_turn_latency(response_id, now)
        if self._turn_complete_timer is not None:
            self._turn_complete_timer.cancel()
            self._turn_complete_timer = None
        if server_content.generation_complete and self._current_generation is not None:
            response_id = self._current_generation.response_id
            if self._current_generation.output_text:
                self._completed_texts[response_id] = self._current_generation.output_text
            self._generation_complete_ids[response_id] = None
            while len(self._generation_complete_ids) > 20:
                self._generation_complete_ids.popitem(last=False)
            if not server_content.turn_complete:
                with contextlib.suppress(RuntimeError):
                    self._turn_complete_timer = asyncio.get_running_loop().call_later(
                        TURN_COMPLETE_GRACE_SECONDS, self._close_after_missing_turn_complete, response_id
                    )
        if server_content.generation_complete or server_content.turn_complete:
            generation = self._current_generation
            trace_logger.info(
                "GEMINI_SERVER_COMPLETE generation_complete=%s turn_complete=%s response_id=%s output_chars=%s",
                str(bool(server_content.generation_complete)).lower(),
                str(bool(server_content.turn_complete)).lower(),
                getattr(generation, "response_id", "none"),
                len(getattr(generation, "output_text", "") or ""),
            )
            text = str(getattr(generation, "output_text", "") or "").strip()
            if server_content.turn_complete and text and not SENTENCE_END_RE.search(text):
                trace_logger.warning(
                    "SPEECH_CUT reason=server_complete_mid_sentence turn=%s words_spoken=%s tail=%r",
                    getattr(generation, "response_id", "none"), len(text.split()), text[-60:],
                )
        super()._handle_server_content(server_content)

    def _handle_usage_metadata(self, usage_metadata) -> None:
        details = ",".join(
            f"{getattr(d.modality, 'value', d.modality)}:{d.token_count}"
            for d in (usage_metadata.response_tokens_details or [])
        )
        trace_logger.info(
            "GEMINI_USAGE response_tokens=%s thoughts_tokens=%s prompt_tokens=%s total_tokens=%s detail=%s",
            usage_metadata.response_token_count, usage_metadata.thoughts_token_count,
            usage_metadata.prompt_token_count, usage_metadata.total_token_count, details or "-",
        )
        super()._handle_usage_metadata(usage_metadata)

    def push_video(self, frame) -> None:
        """Quadros ao vivo NÃO vão para o Gemini: o modelo de áudio nativo ignora vídeo (a visão da Kira manda imagens
        pela conversa) e converter cada quadro da tela cheia em RGBA estourou a memória e derrubou o agente
        (MemoryError em 2026-11-05 12:43, com o compartilhamento de tela ligado). KIRA_FORWARD_LIVE_VIDEO=1 religa,
        limitado a 1 quadro a cada 2 s."""
        if os.getenv("KIRA_FORWARD_LIVE_VIDEO", "0").strip().lower() not in {"1", "true", "on", "yes"}:
            return
        now = time.monotonic()
        if now - getattr(self, "_last_video_push", 0.0) < 2.0:
            return
        self._last_video_push = now
        try:
            super().push_video(frame)
        except MemoryError:
            logger.warning("LIVE_VIDEO_FRAME_DROPPED reason=memory")

    def push_audio(self, frame: rtc.AudioFrame) -> None:
        logger.debug(
            "[DEBUG_AUDIO] push_audio called - sample_rate=%s channels=%s samples_per_channel=%s",
            int(getattr(frame, "sample_rate", 0) or 0),
            int(getattr(frame, "num_channels", 0) or 0),
            int(getattr(frame, "samples_per_channel", 0) or 0),
        )
        denoised_frame = frame
        try:
            denoised_frame = self._maybe_denoise_audio_frame(frame)
        except Exception as exc:
            logger.warning("[DENOISER] Falha no hook de push_audio: %s", exc)
        enhanced_frame = denoised_frame
        try:
            enhanced_frame = self._maybe_enhance_audio_frame(denoised_frame)
        except Exception as exc:
            logger.warning("[AUDIO_ENHANCER] Falha no hook de push_audio: %s", exc)
        mono_audio = self._audio_frame_to_mono_float32(enhanced_frame)
        voiced = self._note_input_level(mono_audio)
        audio_16k = self._resample_to_16k(
            mono_audio,
            source_sample_rate=int(getattr(enhanced_frame, "sample_rate", 0) or 0),
        )
        now = time.monotonic()
        if voiced and audio_16k.size and not self.speech_playing(margin=OWN_SPEECH_TAIL_SECONDS):
            # Enquanto a Kira fala, o que o microfone pega pode ser o eco dela: não entra no reconhecimento.
            if self._segment_started_at is None:
                self._segment_started_at = now
            self._voiced_audio.append((now, audio_16k))
            while self._voiced_audio and now - self._voiced_audio[0][0] > VOICED_KEEP_SECONDS:
                self._voiced_audio.popleft()
            self._feed_enrollment(audio_16k)
        elif self._segment_started_at is not None and now - getattr(self, "_last_voice_at", now) >= SEGMENT_END_SILENCE_SECONDS:
            started, self._segment_started_at = self._segment_started_at, None
            self._identify_speaker(since=started)
        maybe_vad_frame, speech_started, is_speech, speech_ended = self._maybe_vad_gate_audio_frame(enhanced_frame)
        logger.debug(
            "[DEBUG_VAD] VAD result - is_speech=%s speech_started=%s audio_to_send=%s",
            bool(is_speech),
            bool(speech_started),
            "YES" if maybe_vad_frame is not None else "NO",
        )
        if speech_started:
            logger.info("[AUDIO] Fala detectada - iniciando captura com pre-buffer")
        if maybe_vad_frame is not None:
            try:
                maybe_vad_frame = self._noise_gate(maybe_vad_frame, mono_audio)
            except Exception as exc:  # portão nunca pode derrubar o áudio
                logger.debug("NOISE_GATE_FAILED %s", exc)
            try:
                logger.debug(
                    "GEMINI_INPUT_PATH source=livekit_audio transcript_dependency=false"
                )
                super().push_audio(maybe_vad_frame)
                logger.debug("[DEBUG_AUDIO] Audio ENVIADO para LiveKit")
            except Exception as exc:
                logger.error("[AUDIO] ERRO ao enviar audio para LiveKit: %s", exc, exc_info=True)

    def _noise_gate(self, frame: rtc.AudioFrame, mono_audio: np.ndarray) -> rtc.AudioFrame:
        """Portão de ruído adaptativo. Som de fundo constante (TV, vídeo, ventilador) mantinha o detector de fala do
        Gemini achando que o Chefe ainda falava: o turno só fechava num silêncio e a resposta vinha 20-60 s depois
        (sessão 2026-11-05 07:12: -37..-44 dBFS contínuos, voz 'feminina' da TV). Quadros no nível do fundo viram
        silêncio; a voz perto do microfone (bem acima do fundo) passa. Sala quieta: não faz nada."""
        if os.getenv("KIRA_NOISE_GATE", "1").strip().lower() in {"0", "false", "off", "no"} or not mono_audio.size:
            return frame
        rms = float(np.sqrt(np.mean(np.square(mono_audio, dtype=np.float64))))
        gate = self.__dict__.setdefault("_gate", {"hist": deque(maxlen=600), "floor": 0.0, "n": 0, "open_until": 0.0, "closed": 0})
        gate["hist"].append(rms)
        gate["n"] += 1
        if gate["n"] % 25 == 0 and len(gate["hist"]) >= 100:  # piso = percentil 30 dos últimos ~6 s
            gate["floor"] = float(np.percentile(np.fromiter(gate["hist"], dtype=np.float64), 30))
        floor = gate["floor"]
        now = time.monotonic()
        if floor < 0.004:  # fundo abaixo de ~-48 dBFS: sala quieta, deixa tudo passar
            return frame
        if rms >= max(floor * 2.8, 0.02):  # ~+9 dB acima do fundo = alguém falando perto do microfone
            gate["open_until"] = now + 0.45  # rabicho: fim de palavra não é cortado
            return frame
        if now < gate["open_until"]:
            return frame
        gate["closed"] += 1
        if gate["closed"] % 1000 == 1:
            trace_logger.info("NOISE_GATE_ACTIVE floor_dbfs=%.1f", 20.0 * np.log10(max(floor, 1e-6)))
        silent = bytes(len(bytes(frame.data.cast("B")) if hasattr(frame.data, "cast") else bytes(frame.data)))
        return rtc.AudioFrame(data=silent, sample_rate=frame.sample_rate, num_channels=frame.num_channels,
                              samples_per_channel=frame.samples_per_channel)

    def _log_turn_latency(self, response_id: str, first_audio_at: float) -> None:
        timeline = getattr(self, "_turn_latency", None)
        if not timeline or timeline.get("rid") != response_id or "first_audio" in timeline:
            return
        timeline["first_audio"] = first_audio_at
        # A linha sai quando o primeiro quadro vai para a sala (note_playback_started) ou, sem isso, em 3 s.
        asyncio.get_running_loop().call_later(3.0, self._flush_turn_latency, response_id, None)

    def note_playback_started(self) -> None:
        """Primeiro quadro da resposta publicado na sala do LiveKit: fecha a linha TURN_LATENCY com playout_ms."""
        timeline = getattr(self, "_turn_latency", None)
        if timeline and "first_audio" in timeline:
            self._flush_turn_latency(timeline["rid"], time.monotonic())

    def _flush_turn_latency(self, response_id: str, playout_at: float | None) -> None:
        timeline = getattr(self, "_turn_latency", None)
        if not timeline or timeline.get("rid") != response_id or "first_audio" not in timeline:
            return
        self._turn_latency = None
        created, speech_end, first_audio_at = timeline["created"], timeline["speech_end"], timeline["first_audio"]
        spoken = bool(speech_end) and created - speech_end < VOICE_TURN_WINDOW_SECONDS
        trace_logger.info(
            "TURN_LATENCY source=%s user_end_to_first_audio_ms=%s vad_ms=%s gemini_ms=%d playout_ms=%s response_id=%s",
            "voice" if spoken else "text_or_auto",
            int((first_audio_at - speech_end) * 1000) if spoken else "-",
            int((created - speech_end) * 1000) if spoken else "-",
            int((first_audio_at - created) * 1000),
            int((playout_at - first_audio_at) * 1000) if playout_at else "-",
            response_id,
        )

    def _note_input_level(self, mono_audio: np.ndarray) -> bool:
        """A cada 10 s: quantos quadros do microfone chegaram e o volume (prova se o áudio chega ao Gemini).
        Devolve se o quadro tem voz."""
        stats = self.__dict__.setdefault("_input_stats", {"since": time.monotonic(), "frames": 0, "peak": 0.0, "sum": 0.0, "n": 0})
        voiced = False
        if mono_audio.size:
            rms = float(np.sqrt(np.mean(np.square(mono_audio, dtype=np.float64))))
            if rms >= VOICE_RMS_THRESHOLD:
                self._last_voice_at = time.monotonic()
                voiced = True
            stats["frames"] += 1
            stats["peak"] = max(stats["peak"], rms)
            stats["sum"] += rms
            stats["n"] += 1
        if time.monotonic() - stats["since"] >= 10.0:
            to_db = lambda value: 20.0 * np.log10(max(value, 1e-6))
            if stats["frames"] and stats["peak"] <= 1e-6:
                trace_logger.warning("GEMINI_INPUT_DIGITAL_SILENCE seconds=10 hint=microfone_mudo_ou_ocupado_por_outro_app")
            trace_logger.info(
                "GEMINI_INPUT_AUDIO frames=%s avg_dbfs=%.1f peak_dbfs=%.1f send_queue=%s video_frames=%s",
                stats["frames"], to_db(stats["sum"] / max(stats["n"], 1)), to_db(stats["peak"]), self._msg_ch.qsize(),
                getattr(self, "_video_frames_sent", 0),
            )
            self._video_frames_sent = 0
            self._input_stats = {"since": time.monotonic(), "frames": 0, "peak": 0.0, "sum": 0.0, "n": 0}
        return voiced

    def _maybe_vad_gate_audio_frame(self, frame: rtc.AudioFrame) -> tuple[rtc.AudioFrame | None, bool, bool, bool]:
        if not getattr(self._vad, "enabled", False):
            return frame, False, True, False
        sample_rate = int(getattr(frame, "sample_rate", 0) or 0)
        channels = int(getattr(frame, "num_channels", 1) or 1)
        samples_per_channel = int(getattr(frame, "samples_per_channel", 0) or 0)
        if sample_rate <= 0 or samples_per_channel <= 0:
            return frame, False, False, False
        pcm = np.frombuffer(frame.data, dtype=np.int16)
        if pcm.size != samples_per_channel * max(channels, 1):
            return frame, False, False, False
        if channels > 1:
            mono = pcm.reshape(samples_per_channel, channels).mean(axis=1).astype(np.int16)
        else:
            mono = pcm.copy()
        was_speaking = bool(getattr(self._vad, "_is_speaking", False))
        chunk_to_send, _is_speech, speech_started = self._vad.process_chunk(mono)
        if speech_started and chunk_to_send is not None:
            chunk_to_send = self._maybe_expand_speech_start_chunk(
                chunk_to_send,
                source_sample_rate=sample_rate,
                dtype=mono.dtype,
            )
        speech_ended = was_speaking and not bool(getattr(self._vad, "_is_speaking", False)) and not speech_started
        if chunk_to_send is None:
            return None, speech_started, bool(_is_speech), speech_ended
        out_chunk = chunk_to_send.astype(np.int16, copy=False)
        out_samples_per_channel = int(len(out_chunk))
        if out_samples_per_channel <= 0:
            return None, speech_started, bool(_is_speech), speech_ended
        if channels > 1:
            interleaved = np.repeat(out_chunk[:, None], channels, axis=1).reshape(-1)
        else:
            interleaved = out_chunk
        out = rtc.AudioFrame.create(sample_rate, channels, out_samples_per_channel)
        out_view = np.frombuffer(out.data, dtype=np.int16)
        if out_view.size != interleaved.size:
            logger.warning(
                "[AUDIO] Frame VAD size mismatch out=%s in=%s",
                out_view.size,
                interleaved.size,
            )
            return frame, False, bool(_is_speech), speech_ended
        out_view[:] = interleaved
        return out, speech_started, bool(_is_speech), speech_ended

    def _maybe_denoise_audio_frame(self, frame: rtc.AudioFrame) -> rtc.AudioFrame:
        if not getattr(self._denoiser, "enabled", False):
            return frame
        sample_rate = int(getattr(frame, "sample_rate", 0) or 0)
        channels = int(getattr(frame, "num_channels", 1) or 1)
        samples_per_channel = int(getattr(frame, "samples_per_channel", 0) or 0)
        if sample_rate <= 0 or samples_per_channel <= 0:
            return frame
        if channels <= 0:
            channels = 1

        pcm = np.frombuffer(frame.data, dtype=np.int16)
        if pcm.size != samples_per_channel * channels:
            return frame
        if channels > 1:
            audio = pcm.reshape(samples_per_channel, channels).mean(axis=1).astype(np.int16)
        else:
            audio = pcm.copy()

        clean = self._denoiser.process_chunk(audio)
        if clean.dtype != np.int16:
            clean = np.clip(clean, -32768, 32767).astype(np.int16)

        if channels > 1:
            clean_interleaved = np.repeat(clean[:, None], channels, axis=1).reshape(-1)
        else:
            clean_interleaved = clean

        out = rtc.AudioFrame.create(sample_rate, channels, samples_per_channel)
        out_view = np.frombuffer(out.data, dtype=np.int16)
        if out_view.size != clean_interleaved.size:
            logger.warning(
                "[AUDIO] Frame denoise size mismatch out=%s in=%s",
                out_view.size,
                clean_interleaved.size,
            )
            return frame
        out_view[:] = clean_interleaved
        self._denoise_frame_count += 1
        if self._denoise_frame_count % 100 == 0:
            logger.debug("[AUDIO] Denoising ativo - frames processados=%d", self._denoise_frame_count)
        return out

    def _maybe_enhance_audio_frame(self, frame: rtc.AudioFrame) -> rtc.AudioFrame:
        if not getattr(self._audio_enhancer, "enabled", False):
            return frame
        sample_rate = int(getattr(frame, "sample_rate", 0) or 0)
        channels = int(getattr(frame, "num_channels", 1) or 1)
        samples_per_channel = int(getattr(frame, "samples_per_channel", 0) or 0)
        if sample_rate <= 0 or samples_per_channel <= 0:
            return frame
        if channels <= 0:
            channels = 1

        pcm = np.frombuffer(frame.data, dtype=np.int16)
        if pcm.size != samples_per_channel * channels:
            return frame
        if channels > 1:
            audio = pcm.reshape(samples_per_channel, channels).mean(axis=1).astype(np.int16)
        else:
            audio = pcm.copy()

        enhanced = self._audio_enhancer.process_chunk(audio)
        if enhanced.dtype != np.int16:
            enhanced = np.clip(enhanced, -32768, 32767).astype(np.int16)

        if channels > 1:
            enhanced_interleaved = np.repeat(enhanced[:, None], channels, axis=1).reshape(-1)
        else:
            enhanced_interleaved = enhanced

        out = rtc.AudioFrame.create(sample_rate, channels, samples_per_channel)
        out_view = np.frombuffer(out.data, dtype=np.int16)
        if out_view.size != enhanced_interleaved.size:
            logger.warning(
                "[AUDIO] Frame enhancer size mismatch out=%s in=%s",
                out_view.size,
                enhanced_interleaved.size,
            )
            return frame
        out_view[:] = enhanced_interleaved
        return out

    def _maybe_expand_speech_start_chunk(
        self,
        chunk_to_send: np.ndarray,
        *,
        source_sample_rate: int,
        dtype: np.dtype,
    ) -> np.ndarray:
        if not getattr(self._audio_enhancer, "enabled", False):
            return chunk_to_send
        enhancer_sample_rate = int(getattr(self._audio_enhancer, "sample_rate", 0) or 0)
        if enhancer_sample_rate != source_sample_rate:
            return chunk_to_send
        target_dtype = np.int16 if np.issubdtype(dtype, np.integer) else np.float32
        try:
            lookback = self._audio_enhancer.get_lookback_audio(dtype=target_dtype)
        except Exception as exc:
            logger.warning("[AUDIO_ENHANCER] Falha ao obter lookback: %s", exc)
            return chunk_to_send
        if lookback.size <= len(chunk_to_send):
            return chunk_to_send
        logger.info(
            "[AUDIO_ENHANCER] Lookback anexado - samples=%d duration=%.2fs",
            len(lookback),
            len(lookback) / float(source_sample_rate),
        )
        return lookback.astype(dtype, copy=False)

    def _build_genai_client(self):
        api_version = self._opts.api_version
        if (
            not api_version
            and (self._opts.enable_affective_dialog or self._opts.proactivity)
            and not self._opts.vertexai
        ):
            api_version = "v1alpha"

        http_options = self._opts.http_options or genai_types.HttpOptions(
            timeout=int(self._opts.conn_options.timeout * 1000)
        )
        if api_version:
            http_options.api_version = api_version
        if not http_options.headers:
            http_options.headers = {}
        http_options.headers["x-goog-api-client"] = (
            f"livekit-agents/{google_realtime_api.__version__}"
        )

        return google_realtime_api.GenAIClient(
            api_key=self._opts.api_key,
            vertexai=self._opts.vertexai,
            project=self._opts.project,
            location=self._opts.location,
            http_options=http_options,
        )

    def update_api_key(self, api_key: str, *, reconnect: bool = True) -> None:
        self._opts.api_key = api_key
        if hasattr(self, "_client"):
            self._client = self._build_genai_client()
        logger.warning("[GEMINI][LIVE] API key atualizada para a sessão ativa.")
        if reconnect:
            self._session_should_close.set()

    def _rotate_api_key_on_rate_limit(
        self,
        *,
        exhausted_indexes: set[int],
        reason: str,
    ) -> bool:
        rotated = rotate_to_next_usable_gemini_api_key(
            excluded_indexes=exhausted_indexes,
            logger_override=logger,
            reason=reason,
            key_type="text",
        )
        if rotated is None:
            return False
        new_key, new_index = rotated
        self.update_api_key(new_key, reconnect=False)
        logger.warning("[GEMINI][LIVE] Rotacionando sessão realtime para chave índice=%s", new_index)
        return True

    def _live_ready_event(self) -> asyncio.Event:
        event = getattr(self, "_kira_live_ready_event", None)
        if event is None:
            event = asyncio.Event()
            self._kira_live_ready_event = event
        return event

    def _live_ready_failure(self) -> BaseException | None:
        return getattr(self, "_kira_live_ready_failure", None)

    def _set_live_ready_failure(self, error: BaseException | None) -> None:
        if error is None:
            if hasattr(self, "_kira_live_ready_failure"):
                self._kira_live_ready_failure = None
            return
        self._kira_live_ready_failure = error

    def fail_live_ready(self, error: BaseException) -> None:
        self._set_live_ready_failure(error)
        self._live_ready_event().set()

    def _mark_live_ready(self, ready: bool) -> None:
        event = self._live_ready_event()
        if ready:
            self._set_live_ready_failure(None)
            event.set()
            return
        event.clear()

    @property
    def is_live_connected(self) -> bool:
        return self._active_session is not None and not self._session_should_close.is_set()

    @property
    def live_session_epoch(self) -> int:
        return int(getattr(self, "_live_session_epoch", 0) or 0)

    @property
    def economy_standby(self) -> bool:
        return bool(self._economy_standby)

    async def enter_economy_standby(self) -> dict[str, object]:
        if self._economy_standby:
            return {"ok": True, "state": "standby", "already": True}
        logger.info("GEMINI_ECONOMY_STANDBY_ENTER_REQUESTED")
        self._economy_standby = True
        self._economy_standby_changed.set()
        self._session_should_close.set()
        if self._warmup_task is not None and not self._warmup_task.done():
            self._warmup_task.cancel()
            await asyncio.gather(self._warmup_task, return_exceptions=True)
        if self._keepalive_task is not None and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            await asyncio.gather(self._keepalive_task, return_exceptions=True)
        deadline = time.monotonic() + 10.0
        while self.is_live_connected and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if self.is_live_connected:
            raise TimeoutError("gemini_session_close_timeout")
        self._set_kira_realtime_state(GeminiRealtimeState.COOLDOWN, reason="economy_standby")
        logger.info("GEMINI_ECONOMY_STANDBY_ENTERED")
        return {"ok": True, "state": "standby"}

    async def wake_from_economy_standby(self, *, timeout_seconds: float = 20.0) -> dict[str, object]:
        if not self._economy_standby:
            if self.is_live_connected:
                return {"ok": True, "state": "ready", "already": True}
        logger.info("GEMINI_ECONOMY_WAKE_REQUESTED")
        self._economy_standby = False
        self._economy_standby_changed.set()
        self._session_should_close.set()
        await self.wait_until_live_ready(timeout=timeout_seconds)
        logger.info("GEMINI_ECONOMY_WAKE_READY")
        return {"ok": True, "state": "ready"}

    async def refresh_after_voice_degradation(
        self,
        *,
        reason: str = "voice_degradation",
        timeout_seconds: float = 30.0,
    ) -> dict[str, object]:
        refresh_reason = str(reason or "voice_degradation")
        if self._economy_standby:
            logger.warning("VOICE_DEGRADATION_RECOVERY_FAILED reason=economy_standby")
            return {"ok": False, "reason": "economy_standby"}
        if self._voice_refresh_in_progress:
            logger.info("VOICE_DEGRADATION_RECOVERY_BEGIN reason=%s already_in_progress=true", refresh_reason)
            try:
                await self.wait_until_live_ready(timeout=timeout_seconds)
                return {"ok": True, "reason": "already_in_progress"}
            except Exception as exc:
                logger.warning("VOICE_DEGRADATION_RECOVERY_FAILED reason=%s", exc)
                return {"ok": False, "reason": str(exc)}

        self._voice_refresh_in_progress = True
        self.mark_voice_audio_degraded(refresh_reason)
        logger.warning("VOICE_DEGRADATION_RECOVERY_BEGIN reason=%s", refresh_reason)
        logger.warning("VOICE_DEGRADATION_RECOVERY_CLOSE_GEMINI")
        logger.warning("VOICE_DEGRADATION_RECOVERY_RECREATE_GEMINI")
        self._set_kira_realtime_state(GeminiRealtimeState.RECONNECTING, reason=refresh_reason)
        self._session_should_close.set()
        try:
            await self.wait_until_live_ready(timeout=max(timeout_seconds, 0.1))
        except Exception as exc:
            logger.warning("VOICE_DEGRADATION_RECOVERY_FAILED reason=%s", exc)
            self._voice_refresh_in_progress = False
            return {"ok": False, "reason": str(exc)}
        self._voice_audio_degraded = False
        self._voice_audio_degraded_reason = ""
        self._voice_refresh_in_progress = False
        logger.warning("VOICE_DEGRADATION_RECOVERY_READY")
        return {"ok": True, "state": "ready", "reason": refresh_reason}

    async def restart_gemini_session(
        self,
        *,
        reason: str = "unknown",
        timeout_seconds: float = 15.0,
    ) -> bool:
        restart_reason = str(reason or "unknown").strip() or "unknown"
        start = time.monotonic()
        if self._gemini_restart_lock.locked():
            logger.info(
                "GEMINI_SESSION_RESTART_SKIPPED_ALREADY_RUNNING reason=%s",
                restart_reason,
            )
            return False

        async with self._gemini_restart_lock:
            ok = False
            self.gemini_restart_in_progress = True
            self._voice_refresh_in_progress = True
            logger.warning("GEMINI_SESSION_RESTART_BEGIN reason=%s", restart_reason)
            try:
                if self._economy_standby:
                    logger.warning("GEMINI_SESSION_RESTART_FAILED reason=economy_standby")
                    return False

                self.mark_voice_audio_degraded(restart_reason)
                self._set_kira_realtime_state(GeminiRealtimeState.RECONNECTING, reason=restart_reason)
                self._set_live_ready_failure(None)
                self._mark_live_ready(False)
                self._session_should_close.set()
                logger.warning("GEMINI_SESSION_RESTART_CLOSE_OLD_OK")
                logger.warning("GEMINI_SESSION_RESTART_CREATE_NEW_BEGIN")

                previous_epoch = self.live_session_epoch
                await self.wait_until_live_ready(
                    timeout=max(float(timeout_seconds), 0.1),
                    min_session_epoch=previous_epoch + 1,
                )
                live_connected = bool(self.is_live_connected)
                ready_state = self._kira_realtime_state == GeminiRealtimeState.READY
                new_epoch = self.live_session_epoch
                if not (live_connected and ready_state and new_epoch > previous_epoch):
                    logger.warning(
                        "GEMINI_SESSION_RESTART_FAILED reason=not_ready live_connected=%s state=%s previous_epoch=%s new_epoch=%s",
                        str(live_connected).lower(),
                        self._kira_realtime_state.value,
                        previous_epoch,
                        new_epoch,
                    )
                    return False

                self._voice_audio_degraded = False
                self._voice_audio_degraded_reason = ""
                self._kira_realtime_degraded = False
                ok = True
                logger.warning(
                    "GEMINI_SESSION_RESTART_READY previous_epoch=%s new_epoch=%s",
                    previous_epoch,
                    new_epoch,
                )
                return True
            except Exception as exc:
                logger.warning("GEMINI_SESSION_RESTART_FAILED reason=%s", exc)
                return False
            finally:
                self._voice_refresh_in_progress = False
                self.gemini_restart_in_progress = False
                logger.warning(
                    "GEMINI_SESSION_RESTART_END ok=%s elapsed_s=%.2f",
                    str(ok).lower(),
                    time.monotonic() - start,
                )

    @property
    def generation_in_progress(self) -> bool:
        return self._generation_in_progress

    async def wait_until_live_ready(
        self,
        *,
        timeout: float = GEMINI_LIVE_READY_TIMEOUT_SECONDS,
        min_session_epoch: int | None = None,
    ) -> bool:
        min_epoch = int(min_session_epoch or 0)
        if self.is_live_connected and self.live_session_epoch >= min_epoch:
            self._mark_live_ready(True)
            self._set_kira_realtime_state(GeminiRealtimeState.READY, reason="live_connected")
            return True

        deadline = asyncio.get_running_loop().time() + max(timeout, 0.1)
        while True:
            failure = self._live_ready_failure()
            if failure is not None:
                logger.error(
                    "GEMINI_LIVE_READY_WAIT_FAILED type=%s reason=%s",
                    failure.__class__.__name__,
                    failure,
                )
                raise failure
            if self.is_live_connected and self.live_session_epoch >= min_epoch:
                self._mark_live_ready(True)
                return True

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                error = TimeoutError("Gemini Live nao ficou pronto a tempo")
                self.fail_live_ready(error)
                logger.error(
                    "GEMINI_LIVE_READY_WAIT_TIMEOUT model=%s backend=%s native_audio=%s key_slot=%s",
                    self._opts.model,
                    resolve_gemini_backend(),
                    should_use_gemini_native_audio(),
                    _current_gemini_key_slot_for_log(),
                )
                raise error

            try:
                await asyncio.wait_for(
                    self._live_ready_event().wait(),
                    timeout=min(GEMINI_LIVE_READY_POLL_SECONDS, remaining),
                )
            except TimeoutError:
                continue
            return True

    def generate_reply(
        self,
        *,
        instructions: object = google_realtime_api.NOT_GIVEN,
    ) -> asyncio.Future[llm.GenerationCreatedEvent]:
        result_fut: asyncio.Future[llm.GenerationCreatedEvent] = asyncio.get_running_loop().create_future()
        if not self._is_ready_for_generation():
            logger.warning(
                "GEMINI_GENERATE_BLOCKED_NOT_READY state=%s connected=%s degraded=%s",
                self._kira_realtime_state.value,
                str(self.is_live_connected).lower(),
                str(self._kira_realtime_degraded).lower(),
            )
            result_fut.set_exception(
                llm.RealtimeError("A conexão com o Gemini caiu. Estou tentando reconectar.")
            )
            return result_fut
        # Existe uma única geração ativa por sessão. Uma nova entrada do
        # usuário tem prioridade: interrompe a fala corrente e descarta itens
        # ainda aguardando na fila, evitando que respostas antigas bloqueiem a
        # mensagem atual.
        if self._generation_in_progress:
            try:
                self.interrupt()
                logger.info("GEMINI_ACTIVE_GENERATION_INTERRUPTED reason=new_user_request")
            except Exception as exc:
                logger.warning("GEMINI_ACTIVE_GENERATION_INTERRUPT_FAILED reason=%s", exc)
        dropped = 0
        while True:
            try:
                stale_request = self._generation_request_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not stale_request.result_fut.done():
                stale_request.result_fut.set_exception(llm.RealtimeError("CANCELLED_BY_NEWER_USER_TURN"))
                dropped += 1
        if dropped:
            logger.info("GEMINI_QUEUED_GENERATIONS_CANCELLED count=%s reason=new_user_request", dropped)
        stale = self._current_generation
        if stale is not None and not stale._done and stale.response_id in self._generation_complete_ids:
            # O Gemini terminou de gerar mas não mandou turn_complete; sem fechar aqui, a resposta ao pedido novo
            # entrava na geração velha e o generate_reply esperava generation_created até estourar.
            trace_logger.info("GEMINI_STALE_GENERATION_CLOSED response_id=%s reason=new_user_request", stale.response_id)
            self._mark_current_generation_done()
        self._model_turn_open_after_tool = False
        self._last_activity_at = time.monotonic()
        request = _QueuedGenerationRequest(
            result_fut=result_fut,
            instructions=instructions,
            queued_at=asyncio.get_running_loop().time(),
        )
        self._generation_request_queue.put_nowait(request)
        self._ensure_generation_worker()
        return result_fut

    def _ensure_generation_worker(self) -> None:
        task = self._generation_worker_task
        if task is not None and not task.done():
            return
        self._generation_worker_task = asyncio.create_task(
            self._process_generation_queue(),
            name="kira-gemini-generation-queue",
        )

    async def _process_generation_queue(self) -> None:
        try:
            while not self._msg_ch.closed:
                request = await self._generation_request_queue.get()
                if request.result_fut.done():
                    continue
                queued_for = asyncio.get_running_loop().time() - request.queued_at
                if queued_for > GEMINI_GENERATE_REPLY_STALE_QUEUE_SECONDS:
                    logger.error(
                        "DROPPED_STALE_SPEECH queued_for=%.1fs generation_in_progress=%s",
                        queued_for,
                        self._generation_in_progress,
                    )
                    request.result_fut.set_exception(
                        llm.RealtimeError("DROPPED_STALE_SPEECH")
                    )
                    continue
                await self._run_generation_request(request)
        except asyncio.CancelledError:
            raise

    async def drain_stale_generation_queue(
        self,
        *,
        max_age_s: float = GEMINI_GENERATE_REPLY_STALE_QUEUE_SECONDS,
    ) -> int:
        dropped = 0
        kept: list[_QueuedGenerationRequest] = []
        while True:
            try:
                request = self._generation_request_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            queued_for = asyncio.get_running_loop().time() - request.queued_at
            if request.result_fut.done():
                continue
            if queued_for > max_age_s:
                dropped += 1
                logger.error(
                    "DROPPED_STALE_SPEECH queued_for=%.1fs generation_in_progress=%s",
                    queued_for,
                    self._generation_in_progress,
                )
                request.result_fut.set_exception(
                    llm.RealtimeError("DROPPED_STALE_SPEECH")
                )
                continue
            kept.append(request)
        for request in kept:
            self._generation_request_queue.put_nowait(request)
        return dropped

    async def _run_generation_request(self, request: _QueuedGenerationRequest) -> None:
        self._generation_in_progress = True
        self._last_activity_at = time.monotonic()
        try:
            await self.wait_until_live_ready(timeout=GEMINI_LIVE_READY_TIMEOUT_SECONDS)
            generation_fut = self._request_generation(instructions=request.instructions)
            logger.info(
                "VOICE_GENERATION_ACTIVE_SET source=gemini_realtime reason=generate_reply_start timeout_s=%.1f",
                GEMINI_GENERATE_REPLY_TIMEOUT_SECONDS,
            )
            try:
                generation_ev = await asyncio.wait_for(
                    asyncio.shield(generation_fut),
                    timeout=GEMINI_GENERATE_REPLY_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "generate_reply timeout tentativa 1/%s",
                    GEMINI_GENERATE_REPLY_MAX_ATTEMPTS,
                )
                logger.warning("GENERATE_REPLY_NO_GENERATION_CREATED_CLEANUP")
                self._set_kira_realtime_state(GeminiRealtimeState.RECONNECTING, reason="generate_reply_timeout")
                logger.warning(
                    "GEMINI_GENERATION_STILL_ACTIVE_AFTER_TIMEOUT action=await_existing_generation timeout_s=%.1f",
                    GEMINI_GENERATE_REPLY_TIMEOUT_SECONDS,
                )
                try:
                    generation_ev = await asyncio.wait_for(asyncio.shield(generation_fut), timeout=1.0)
                except llm.RealtimeError as exc:
                    if "timed out" not in str(exc).strip().lower():
                        request.result_fut.set_exception(exc)
                        return
                    request.result_fut.set_exception(exc)
                    return
                except asyncio.TimeoutError:
                    request.result_fut.set_exception(
                        llm.RealtimeError("generate_reply timed out waiting for generation_created event")
                    )
                    return
            except llm.RealtimeError as exc:
                request.result_fut.set_exception(exc)
                return
            request.result_fut.set_result(generation_ev)
            self._last_activity_at = time.monotonic()
            return
        except Exception as exc:
            if not request.result_fut.done():
                request.result_fut.set_exception(exc)
        finally:
            self._generation_in_progress = False

    def send_keepalive_audio(self) -> bool:
        if not self.is_live_connected:
            return False
        self._last_activity_at = time.monotonic()

        samples_per_channel = max(
            int(
                google_realtime_api.INPUT_AUDIO_SAMPLE_RATE
                * (GEMINI_HEARTBEAT_FRAME_MS / 1000.0)
            ),
            1,
        )
        silence_frame = rtc.AudioFrame.create(
            google_realtime_api.INPUT_AUDIO_SAMPLE_RATE,
            google_realtime_api.INPUT_AUDIO_CHANNELS,
            samples_per_channel,
        )
        self.push_audio(silence_frame)
        return True

    async def warmup_session(self) -> None:
        try:
            if not self.is_live_connected:
                return
            logger.info("GEMINI_WARMUP_START")
            session = self._active_session
            if session is None:
                return
            await session.send_client_content(
                turns=[
                    genai_types.Content(
                        role="user",
                        parts=[genai_types.Part(text=".")],
                    )
                ],
                turn_complete=False,
            )
            logger.info("GEMINI_WARMUP_SENT")
            await asyncio.sleep(0.5)
            self._last_activity_at = time.monotonic()
            logger.info("GEMINI_WARMUP_DONE")
        except Exception as e:
            logger.warning("GEMINI_WARMUP_FAILED error=%s — continuando sem warmup", e)

    async def _keepalive_loop(self) -> None:
        KEEPALIVE_INTERVAL_S = 60.0
        IDLE_THRESHOLD_S = 45.0
        while self._session_active and not self._msg_ch.closed:
            await asyncio.sleep(KEEPALIVE_INTERVAL_S)
            idle_s = time.monotonic() - self._last_activity_at
            if idle_s < IDLE_THRESHOLD_S:
                logger.debug("GEMINI_KEEPALIVE_SKIP idle=%.1fs threshold=%.1fs", idle_s, IDLE_THRESHOLD_S)
                continue
            logger.info("GEMINI_KEEPALIVE_WARMUP idle=%.1fs", idle_s)
            await self.warmup_session()

    def _handle_go_away(self, go_away: genai_types.LiveServerGoAway) -> None:
        parsed_time_left = parse_duration_seconds(getattr(go_away, "time_left", None))
        time_left_seconds = int(parsed_time_left) if parsed_time_left is not None else 0
        self._last_go_away_at = time.monotonic()
        self._set_kira_realtime_state(GeminiRealtimeState.RECONNECTING, reason="server_go_away")
        logger.warning(
            "Gemini server indicates disconnection soon. Time left: %ss",
            time_left_seconds,
        )
        self.emit(
            "gemini_disconnect_warning",
            GeminiDisconnectWarningEvent(time_left_seconds=time_left_seconds),
        )
        if time_left_seconds <= GEMINI_DISCONNECT_WARNING_SECONDS:
            logger.warning(
                "[GEMINI] Desconexao em %ss - iniciando reconexao preventiva",
                time_left_seconds,
            )
            self._schedule_proactive_reconnect(time_left_seconds)
            return
        self._session_should_close.set()

    async def _recv_task(self, session) -> None:
        try:
            while True:
                async with self._session_lock:
                    if self._session_should_close.is_set() or (
                        not self._active_session or self._active_session != session
                    ):
                        logger.debug("receive task: Session changed or closed, stopping receive.")
                        break

                async for response in session.receive():
                    if google_realtime_api.lk_google_debug:
                        resp_copy = response.model_dump(exclude_defaults=True)
                        if (
                            (server_content := resp_copy.get("server_content"))
                            and (model_turn := server_content.get("model_turn"))
                            and (parts := model_turn.get("parts"))
                        ):
                            for part in parts:
                                if part and part.get("inline_data"):
                                    part["inline_data"] = "<audio>"
                        logger.debug("<<< received response", extra={"response": resp_copy})

                    if (server_content := response.server_content) and server_content.interrupted:
                        since_ms = (time.monotonic() - self._last_client_content_at) * 1000 if self._last_client_content_at else -1
                        trace_logger.info(
                            "GEMINI_SERVER_INTERRUPTED generating=%s pending_user_request=%s ms_since_client_content=%.0f",
                            str(bool(self._current_generation and not self._current_generation._done)).lower(),
                            str(bool(self._pending_generation_fut)).lower(),
                            since_ms,
                        )
                    if not self._current_generation or self._current_generation._done:
                        if (server_content := response.server_content) and server_content.interrupted:
                            if not self._pending_generation_fut:
                                self._handle_input_speech_started()
                            server_content.interrupted = None
                            server_content_copy = server_content.model_dump(exclude_none=True)
                            if not server_content_copy:
                                response.server_content = None
                                if google_realtime_api.lk_google_debug:
                                    logger.debug("ignoring empty server content")

                        if self._is_new_generation(response):
                            self._start_new_generation()
                            if google_realtime_api.lk_google_debug:
                                logger.debug("new generation started: %s", self._current_generation)

                    if response.session_resumption_update:
                        resumption_update = response.session_resumption_update
                        logger.info(
                            "GEMINI_SESSION_RESUMPTION_UPDATE resumable=%s handle_received=%s",
                            str(bool(resumption_update.resumable)).lower(),
                            str(bool(resumption_update.new_handle)).lower(),
                        )
                        if resumption_update.resumable and resumption_update.new_handle:
                            self._session_resumption_handle = resumption_update.new_handle

                    if response.server_content:
                        self._handle_server_content(response.server_content)
                    if response.tool_call:
                        self._handle_tool_calls(response.tool_call)
                    if response.tool_call_cancellation:
                        self._handle_tool_call_cancellation(response.tool_call_cancellation)
                    if response.usage_metadata:
                        self._handle_usage_metadata(response.usage_metadata)
                    if response.go_away:
                        self._handle_go_away(response.go_away)

                    if self._num_retries > 0:
                        self._num_retries = 0
        except asyncio.CancelledError:
            raise
        except (ValueError, TypeError) as exc:
            logger.warning("REALTIME_RECV_RECOVERED_PARSE_ERROR error=%s", exc)
            await asyncio.sleep(0)
        except Exception as exc:
            if not self._session_should_close.is_set():
                logger.error("error in receive task: %s", exc, exc_info=exc)
                if "1007" in str(exc):
                    self._drop_history_on_connect = True
                    self._session_resumption_handle = None  # retomar a sessão recusada repetiria o mesmo erro
                    logger.warning("GEMINI_1007_HISTORY_WILL_BE_DROPPED")
                self._mark_restart_needed(on_error=True)
        finally:
            self._mark_current_generation_done()

    def _schedule_proactive_reconnect(self, time_left_seconds: float = 0.0) -> None:
        task = self._proactive_reconnect_task
        if task is not None and not task.done():
            return
        self._proactive_reconnect_task = asyncio.create_task(
            self._reconnect_gemini_session(time_left_seconds),
            name="kira-gemini-proactive-reconnect",
        )

    async def _reconnect_gemini_session(self, time_left_seconds: float = 0.0) -> None:
        """Reconecta antes de o servidor fechar. Com ferramenta em andamento ou a Kira falando, espera terminar (dentro do
        prazo do aviso): reconectando na hora, a resposta pronta do Olho de Deus ficou 14 s parada e a Kira não falou
        os números (sessão de 2026-10-24 12:25)."""
        deadline = time.monotonic() + max(0.0, float(time_left_seconds) - GO_AWAY_RECONNECT_MARGIN_SECONDS)
        waited = time.monotonic()
        while time.monotonic() < deadline and (tool_wait_state()[0] or self.speech_playing(margin=0.5)):
            await asyncio.sleep(0.25)
        if time.monotonic() - waited >= 0.25:
            trace_logger.info("GEMINI_PROACTIVE_RECONNECT_DEFERRED waited_s=%.1f", time.monotonic() - waited)
        await asyncio.sleep(0)
        self._session_should_close.set()

    def _is_retryable_initial_connect_error(self, error: Exception, *, hint: str | None) -> bool:
        if is_gemini_live_session_not_found_error(error):
            return True
        if hint:
            return False

        message = str(error).strip().lower()
        unrecoverable_fragments = (
            "1008",
            "policy violation",
            "api key",
            "unauthorized",
            "forbidden",
            "permission denied",
            "invalid argument",
            "unsupported model",
        )
        if any(fragment in message for fragment in unrecoverable_fragments):
            return False

        if isinstance(error, (TimeoutError, OSError, ConnectionError)):
            return True

        retryable_fragments = (
            "timed out during opening handshake",
            "failed to resolve",
            "temporary failure in name resolution",
            "name or service not known",
            "connection refused",
            "network is unreachable",
            "server disconnected",
            "timed out",
        )
        return any(fragment in message for fragment in retryable_fragments)

    async def _wait_before_reconnect(self) -> None:
        retry_interval = self._opts.conn_options._interval_for_retry(self._num_retries)
        attempt_number = self._num_retries + 1
        logger.warning(
            "Retrying Gemini... tentativa=%s espera=%.1fs",
            attempt_number,
            retry_interval,
        )
        await asyncio.sleep(retry_interval)
        self._num_retries += 1

    @utils.log_exceptions(logger=logger)
    async def _main_task(self) -> None:
        max_retries = self._opts.conn_options.max_retry
        exhausted_key_indexes: set[int] = set()
        logger.info(
            "GEMINI_REALTIME_MAIN_TASK_START model=%s backend=%s native_audio=%s key_slot=%s",
            self._opts.model,
            resolve_gemini_backend(),
            should_use_gemini_native_audio(),
            _current_gemini_key_slot_for_log(),
        )

        try:
            while not self._msg_ch.closed:
                await self._close_active_session()
                while self._economy_standby and not self._msg_ch.closed:
                    logger.info("GEMINI_ECONOMY_STANDBY_WAITING")
                    self._mark_live_ready(False)
                    self._economy_standby_changed.clear()
                    await self._economy_standby_changed.wait()

                self._session_should_close.clear()
                self._set_live_ready_failure(None)
                self._mark_live_ready(False)
                config = self._build_connect_config()
                session = None
                attempt_number = self._num_retries + 1
                try:
                    logger.info(
                        "GEMINI_REALTIME_MAIN_TASK_CONNECTING tentativa=%s model=%s backend=%s key_slot=%s",
                        attempt_number,
                        self._opts.model,
                        resolve_gemini_backend(),
                        _current_gemini_key_slot_for_log(),
                    )
                    logger.info(
                        "GEMINI_MODEL_PRIMARY_START model=%s backend=%s native_audio=%s key_slot=%s",
                        self._opts.model,
                        resolve_gemini_backend(),
                        should_use_gemini_native_audio(),
                        _current_gemini_key_slot_for_log(),
                    )
                    logger.info(
                        "GEMINI_CONNECT_START tentativa=%s model=%s timeout_s=%.1f",
                        attempt_number,
                        self._opts.model,
                        GEMINI_LIVE_CONNECT_TIMEOUT_SECONDS,
                    )
                    logger.info(
                        "Gemini Live connecting... tentativa=%s model=%s",
                        attempt_number,
                        self._opts.model,
                    )
                    async with self._client.aio.live.connect(
                        model=self._opts.model, config=config
                    ) as session:
                        logger.info(
                            "GEMINI_REALTIME_MAIN_TASK_CONNECTED tentativa=%s model=%s",
                            attempt_number,
                            self._opts.model,
                        )
                        logger.info(
                            "GEMINI_SESSION_CONNECT_RESUME handle_present=%s",
                            str(bool(self._session_resumption_handle)).lower(),
                        )
                        if self._last_go_away_at and time.monotonic() - self._last_go_away_at <= 120.0:
                            logger.info(
                                "GEMINI_SESSION_RESUME_RESULT status=unknown "
                                "reason=sdk_does_not_expose_acceptance"
                            )
                            self._last_go_away_at = 0.0
                        async with self._session_lock:
                            self._active_session = session
                            self._live_session_epoch += 1
                            logger.info(
                                "GEMINI_SESSION_EPOCH_READY epoch=%s",
                                self._live_session_epoch,
                            )

                            system_msg_count = sum(
                                1
                                for msg in self._chat_ctx.messages()
                                if msg.role in ("system", "developer")
                            )
                            if system_msg_count > 0:
                                logger.warning(
                                    "Gemini Realtime model '%s' does not support 'system' or "
                                    "'developer' roles in chat history. Dropping %s system "
                                    "message(s) from initial chat context during session initialization. "
                                    "Gemini Realtime only supports 'user' and 'model' roles. Use "
                                    "update_instructions() to set system-level context instead.",
                                    self._opts.model,
                                    system_msg_count,
                                )

                            turns_dict, _ = self._chat_ctx.copy(
                                exclude_function_call=True,
                                exclude_handoff=True,
                                exclude_instructions=True,
                                exclude_empty_message=True,
                                exclude_config_update=True,
                            ).to_provider_format(
                                format="google",
                                inject_dummy_user_message=False,
                            )
                            if self._drop_history_on_connect:
                                logger.warning("GEMINI_HISTORY_DROPPED_AFTER_1007 turns=%d", len(turns_dict or []))
                                turns_dict = []
                                self._drop_history_on_connect = False
                            if turns_dict:
                                turns = [
                                    genai_types.Content.model_validate(turn)
                                    for turn in turns_dict
                                ]
                                await session.send_client_content(
                                    turns=turns,  # type: ignore[arg-type]
                                    turn_complete=False,
                                )
                        self._mark_live_ready(True)
                        if self._voice_audio_degraded:
                            logger.info("VOICE_HEALTH_AFTER_WAKE_OK reason=gemini_reconnected")
                        self._voice_audio_degraded = False
                        self._voice_audio_degraded_reason = ""
                        self._live_session_dead = False
                        self._live_session_dead_reason = ""
                        self._kira_consecutive_connect_failures = 0
                        self._set_kira_realtime_state(GeminiRealtimeState.READY, reason="connect_ok")
                        self._num_retries = 0
                        exhausted_key_indexes.clear()
                        mark_current_gemini_key_working(logger_override=logger, key_type="text")
                        logger.info("GEMINI_CONNECT_OK tentativa=%s model=%s", attempt_number, self._opts.model)
                        logger.info("Gemini Live ready")
                        if self._gemini_1008_recovery_in_progress:
                            logger.info("GEMINI_LIVE_RECOVERY_READY")
                            self._gemini_1008_recovery_in_progress = False
                        if self._keepalive_task is None or self._keepalive_task.done():
                            self._keepalive_task = asyncio.create_task(
                                self._keepalive_loop(),
                                name="kira-gemini-keepalive",
                            )
                        logger.info("GEMINI_WARMUP_SCHEDULING")
                        self._warmup_task = asyncio.create_task(
                            self.warmup_session(),
                            name="kira-gemini-warmup",
                        )
                        logger.info("GEMINI_WARMUP_SCHEDULED — sessao vai aquecer em background")
                        if attempt_number > 1:
                            self.emit(
                                "session_reconnected",
                                llm.RealtimeSessionReconnectedEvent(),
                            )

                        send_task = asyncio.create_task(
                            self._send_task(session), name="gemini-realtime-send"
                        )
                        recv_task = asyncio.create_task(
                            self._recv_task(session), name="gemini-realtime-recv"
                        )
                        restart_wait_task = asyncio.create_task(
                            self._session_should_close.wait(), name="gemini-restart-wait"
                        )

                        done, pending = await asyncio.wait(
                            [send_task, recv_task, restart_wait_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )

                        for task in done:
                            if task is not restart_wait_task and task.exception():
                                logger.error(
                                    "error in task %s: %s",
                                    task.get_name(),
                                    task.exception(),
                                )
                                raise task.exception() or Exception(f"{task.get_name()} failed")

                        if restart_wait_task not in done and self._msg_ch.closed:
                            break

                        for task in pending:
                            await utils.aio.cancel_and_wait(task)

                except asyncio.CancelledError:
                    error = asyncio.CancelledError("Gemini realtime main task cancelada antes do ready")
                    self.fail_live_ready(error)
                    logger.error(
                        "GEMINI_REALTIME_MAIN_TASK_CANCELLED tentativa=%s model=%s",
                        attempt_number,
                        self._opts.model,
                    )
                    break
                except Exception as error:
                    logger.exception(
                        "GEMINI_REALTIME_MAIN_TASK_FAILED tentativa=%s model=%s key_slot=%s type=%s reason=%s",
                        attempt_number,
                        self._opts.model,
                        _current_gemini_key_slot_for_log(),
                        error.__class__.__name__,
                        error,
                    )
                    gemini_session_not_found = is_gemini_live_session_not_found_error(error)
                    session_not_found_retry_exhausted = False
                    if gemini_session_not_found:
                        logger.error("GEMINI_LIVE_1008_DETECTED")
                        logger.error("GEMINI_LIVE_SESSION_NOT_FOUND_DETECTED")
                        logger.error("GEMINI_LIVE_SESSION_NOT_FOUND_CLASSIFIED reason=session_not_found")
                        if self._num_retries < max_retries:
                            logger.info("GEMINI_LIVE_RECOVERY_BEGIN")
                            logger.info("GEMINI_LIVE_RECOVERY_CLOSE_OLD_SESSION")
                            self._gemini_1008_recovery_in_progress = True
                            self._session_should_close.set()
                            self._set_live_ready_failure(None)
                            self._mark_live_ready(False)
                            self._set_kira_realtime_state(GeminiRealtimeState.RECONNECTING, reason="gemini_live_1008_session_not_found")
                            self._emit_error(error, recoverable=True)
                            logger.info("GEMINI_LIVE_RECOVERY_RECREATE_BEGIN")
                            await self._close_active_session()
                            await self._wait_before_reconnect()
                            continue
                        logger.error("GEMINI_LIVE_RECOVERY_FAILED reason=retry_budget_exhausted")
                        logger.error("GEMINI_LIVE_RECOVERY_ESCALATE_AGENT_RESTART")
                        session_not_found_retry_exhausted = True
                        self.mark_gemini_session_dead("retry_budget_exhausted")
                        logger.warning("GEMINI_RECOVERY_DEFERRED_OR_STARTED reason=retry_budget_exhausted")
                        self._gemini_1008_recovery_in_progress = False
                    error_text = str(error or "").lower()
                    if "timed out during opening handshake" in error_text:
                        logger.warning("GEMINI_REALTIME_RECONNECT_FAILED reason=opening_handshake_timeout")
                    if "disconnection soon" in error_text:
                        logger.warning("GEMINI_DISCONNECT_SOON_DETECTED")
                    self._kira_consecutive_connect_failures += 1
                    if not session_not_found_retry_exhausted:
                        self._set_kira_realtime_state(
                            GeminiRealtimeState.RECONNECTING
                            if self._kira_consecutive_connect_failures <= self._kira_fast_reconnect_limit
                            else GeminiRealtimeState.COOLDOWN,
                            reason=error.__class__.__name__,
                        )
                    if is_rate_limit_error(error) and not self._opts.vertexai:
                        try:
                            exhausted_key_indexes.add(get_key_manager().current_text_index)
                        except Exception:
                            pass
                        rotated = self._rotate_api_key_on_rate_limit(
                            exhausted_indexes=exhausted_key_indexes,
                            reason="gemini_live_connect_rate_limit",
                        )
                        if rotated:
                            self._num_retries = 0
                            await asyncio.sleep(0.1)
                            continue

                    hint = google_realtime_api._get_1008_error_hint(str(error))
                    session_started = session is not None
                    retryable_initial_connect_error = (
                        not session_started
                        and self._is_retryable_initial_connect_error(error, hint=hint)
                    )
                    if retryable_initial_connect_error:
                        logger.warning(
                            "Gemini handshake timeout tentativa=%s erro=%s",
                            attempt_number,
                            error,
                        )
                        logger.error("GEMINI_CONNECT_FAILED tentativa=%s retryable=true reason=%s", attempt_number, error)
                    elif hint:
                        logger.error("Gemini Realtime API error: %s%s", error, hint)
                        logger.error("GEMINI_CONNECT_FAILED tentativa=%s retryable=false reason=%s", attempt_number, error)
                    else:
                        logger.error("Gemini Realtime API error: %s", error, exc_info=error)
                        logger.error("GEMINI_CONNECT_FAILED tentativa=%s retryable=false reason=%s", attempt_number, error)

                    if self._msg_ch.closed:
                        continue

                    should_retry = False
                    if session_started:
                        should_retry = self._num_retries < max_retries
                    else:
                        should_retry = retryable_initial_connect_error and (
                            GEMINI_LIVE_RETRY_INITIAL_CONNECTION_FOREVER
                            or self._num_retries < max_retries
                        )
                    if session_not_found_retry_exhausted:
                        should_retry = False

                    if should_retry:
                        logger.info("GEMINI_REALTIME_RECONNECT_BEGIN attempt=%s", self._kira_consecutive_connect_failures)
                        await self._wait_before_reconnect()
                        continue

                    self._set_live_ready_failure(error)
                    self._set_kira_realtime_state(GeminiRealtimeState.FAILED, reason="retry_exhausted")
                    logger.error("GEMINI_RECOVERY_FAILED failures=%s", self._kira_consecutive_connect_failures)
                    self._emit_error(error, recoverable=False)
                    attempts = self._num_retries + 1
                    error_msg = "Failed to connect to Gemini Live"
                    if attempts > 1:
                        error_msg = f"{error_msg} after {attempts} attempts"
                    if hint:
                        error_msg += hint
                    api_error = APIConnectionError(message=error_msg)
                    self.fail_live_ready(api_error)
                    raise api_error from error
                finally:
                    self._mark_live_ready(False)
                    await self._close_active_session()
        finally:
            logger.info("GEMINI_REALTIME_MAIN_TASK_EXITED model=%s", self._opts.model)

    async def aclose(self) -> None:
        while not self._generation_request_queue.empty():
            request = self._generation_request_queue.get_nowait()
            if not request.result_fut.done():
                request.result_fut.set_exception(
                    llm.RealtimeError("Session closed before queued reply was processed")
                )
        if self._generation_worker_task is not None:
            self._generation_worker_task.cancel()
            await asyncio.gather(self._generation_worker_task, return_exceptions=True)
            self._generation_worker_task = None
        if self._proactive_reconnect_task is not None:
            self._proactive_reconnect_task.cancel()
            await asyncio.gather(self._proactive_reconnect_task, return_exceptions=True)
            self._proactive_reconnect_task = None
        await super().aclose()

    def _build_connect_config(self) -> genai_types.LiveConnectConfig:
        tools_config = google_realtime_api.create_tools_config(
            self._tools,
            tool_behavior=self._opts.tool_behavior,
        )
        if UI_TOOLS_NON_BLOCKING:
            for tool in tools_config:
                for declaration in tool.function_declarations or []:
                    if declaration.name in UI_SILENT_TOOLS:
                        declaration.behavior = genai_types.Behavior.NON_BLOCKING
        conf = genai_types.LiveConnectConfig(
            response_modalities=self._opts.response_modalities,
            temperature=(
                self._opts.temperature
                if google_realtime_api.is_given(self._opts.temperature)
                else None
            ),
            max_output_tokens=(
                self._opts.max_output_tokens
                if google_realtime_api.is_given(self._opts.max_output_tokens)
                else None
            ),
            top_p=self._opts.top_p if google_realtime_api.is_given(self._opts.top_p) else None,
            top_k=self._opts.top_k if google_realtime_api.is_given(self._opts.top_k) else None,
            thinking_config=(
                self._opts.thinking_config
                if google_realtime_api.is_given(self._opts.thinking_config)
                else None
            ),
            system_instruction=(
                genai_types.Content(parts=[genai_types.Part(text=self._opts.instructions)])
                if google_realtime_api.is_given(self._opts.instructions)
                else None
            ),
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                        voice_name=self._opts.voice
                    )
                ),
                language_code=(
                    self._opts.language
                    if google_realtime_api.is_given(self._opts.language)
                    else None
                ),
            ),
            tools=tools_config,
            input_audio_transcription=self._opts.input_audio_transcription,
            output_audio_transcription=self._opts.output_audio_transcription,
            session_resumption=genai_types.SessionResumptionConfig(
                handle=self._session_resumption_handle
            ),
        )

        if bool(self._opts.proactivity):
            conf.proactivity = genai_types.ProactivityConfig(
                proactive_audio=self._opts.proactivity
            )
        if bool(self._opts.enable_affective_dialog):
            conf.enable_affective_dialog = self._opts.enable_affective_dialog
        if google_realtime_api.is_given(self._opts.realtime_input_config):
            conf.realtime_input_config = self._opts.realtime_input_config
        if google_realtime_api.is_given(self._opts.context_window_compression):
            conf.context_window_compression = self._opts.context_window_compression
        _lang = str(self._opts.language or "").strip()
        if _lang:
            logger.info("GEMINI_LANGUAGE_CONFIG_APPLIED language=%s method=speech_config.language_code", _lang)
        else:
            logger.warning("GEMINI_LANGUAGE_CONFIG_SKIPPED reason=no_supported_field")
        if google_realtime_api.is_given(self._opts.instructions):
            logger.info("GEMINI_INSTRUCTIONS_PTBR_ENFORCED")

        return conf


async def wait_for_gemini_live_ready(
    *,
    agent,
    timeout_seconds: float = GEMINI_LIVE_READY_TIMEOUT_SECONDS,
    poll_seconds: float = GEMINI_LIVE_READY_POLL_SECONDS,
) -> bool:
    timeout = max(timeout_seconds, 0.1)
    poll = max(poll_seconds, 0.01)
    deadline = asyncio.get_running_loop().time() + timeout
    no_session_logged = False
    logger.info(
        "GEMINI_LIVE_READY_WAIT_START timeout_s=%.1f poll_s=%.2f",
        timeout,
        poll,
    )

    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            logger.error("GEMINI_LIVE_READY_WAIT_TIMEOUT")
            raise TimeoutError("Gemini Live nao ficou pronto a tempo")
        try:
            realtime_session = agent.realtime_llm_session
        except RuntimeError:
            if not no_session_logged:
                no_session_logged = True
                logger.info("GEMINI_LIVE_READY_WAIT_NO_SESSION")
            await asyncio.sleep(min(poll, remaining))
            continue

        wait_until_ready = getattr(realtime_session, "wait_until_live_ready", None)
        if wait_until_ready is None:
            logger.info(
                "GEMINI_LIVE_READY_WAIT_SESSION_FOUND type=%s has_wait_until=false",
                type(realtime_session).__name__,
            )
            return True

        logger.info(
            "GEMINI_LIVE_READY_WAIT_SESSION_FOUND type=%s has_wait_until=true",
            type(realtime_session).__name__,
        )
        logger.info("GEMINI_LIVE_READY_WAIT_AWAIT_START remaining_s=%.1f", remaining)
        ready_task = asyncio.create_task(
            wait_until_ready(timeout=remaining),
            name="kira-gemini-live-ready-event",
        )
        main_task = getattr(realtime_session, "_main_atask", None)
        watched_tasks: set[asyncio.Task] = {ready_task}
        if isinstance(main_task, asyncio.Task):
            watched_tasks.add(main_task)
            logger.info(
                "GEMINI_LIVE_READY_WAIT_MAIN_TASK_OBSERVED done=%s cancelled=%s",
                main_task.done(),
                main_task.cancelled(),
            )
        try:
            done, pending = await asyncio.wait(
                watched_tasks,
                timeout=max(remaining, 0.1),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                error = TimeoutError("Gemini Live nao ficou pronto a tempo")
                fail_live_ready = getattr(realtime_session, "fail_live_ready", None)
                if callable(fail_live_ready):
                    fail_live_ready(error)
                if isinstance(main_task, asyncio.Task) and not main_task.done():
                    logger.error("GEMINI_CONNECT_TASK_CANCEL_REQUESTED source=ready_wait_timeout")
                    main_task.cancel()
                logger.error(
                    "GEMINI_LIVE_READY_WAIT_TIMEOUT model=%s backend=%s native_audio=%s key_slot=%s",
                    getattr(getattr(realtime_session, "_opts", None), "model", "unknown"),
                    resolve_gemini_backend(),
                    should_use_gemini_native_audio(),
                    _current_gemini_key_slot_for_log(),
                )
                raise error
            if ready_task in done:
                await ready_task
                logger.info("GEMINI_LIVE_READY_WAIT_AWAIT_DONE")
                return True
            if isinstance(main_task, asyncio.Task) and main_task in done:
                if main_task.cancelled():
                    raise RuntimeError("Gemini realtime main task foi cancelada antes do ready")
                exc = main_task.exception()
                if exc is not None:
                    raise RuntimeError("Gemini realtime main task falhou antes do ready") from exc
                raise RuntimeError("Gemini realtime main task saiu antes do ready")
        except asyncio.CancelledError:
            logger.error("GEMINI_LIVE_READY_WAIT_CANCELLED")
            ready_task.cancel()
            raise
        except Exception as exc:
            logger.exception(
                "GEMINI_BOOT_EXCEPTION type=%s model=%s backend=%s native_audio=%s key_slot=%s reason=%s",
                exc.__class__.__name__,
                getattr(getattr(realtime_session, "_opts", None), "model", "unknown"),
                resolve_gemini_backend(),
                should_use_gemini_native_audio(),
                _current_gemini_key_slot_for_log(),
                exc,
            )
            logger.error(
                "GEMINI_CONNECT_FAILED type=%s model=%s key_slot=%s reason=%s",
                exc.__class__.__name__,
                getattr(getattr(realtime_session, "_opts", None), "model", "unknown"),
                _current_gemini_key_slot_for_log(),
                exc,
            )
            raise
        finally:
            if not ready_task.done():
                ready_task.cancel()
        return True


class KiraRealtimeModel(google.beta.realtime.RealtimeModel):
    def update_api_key(self, api_key: str, *, reconnect: bool = True) -> None:
        self._opts.api_key = api_key
        for sess in list(self._sessions):
            if hasattr(sess, "update_api_key"):
                sess.update_api_key(api_key, reconnect=reconnect)

    def session(self) -> KiraRealtimeSession:
        sess = KiraRealtimeSession(self)
        self._sessions.add(sess)
        return sess
