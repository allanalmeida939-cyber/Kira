from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import time
from typing import Any

from PIL import Image
from livekit import rtc

from resilience import log_exception_safely

from .models import VisualRuntimeSnapshot, VisualUiContext, VisualUiSnapshot, normalize_text


logger = logging.getLogger(__name__)

FRAME_SIGNATURE_SIZE = (96, 54)
FRAME_STABLE_SCORE_THRESHOLD = 4.0
FRAME_READY_MAX_AGE_SECONDS = 8.0
FRAME_FRESH_MAX_AGE_SECONDS = max(
    min(float(os.getenv("KIRA_SCREEN_FRAME_FRESH_MAX_AGE_SECONDS", "3.0")), FRAME_READY_MAX_AGE_SECONDS),
    0.8,
)
UI_CACHE_SECONDS = 1.5
UI_TEXT_LIMIT = 80
UI_BUTTON_LIMIT = 40
UI_PROGRESS_LIMIT = 8

_LOADING_KEYWORDS = (
    "carregando",
    "loading",
    "aguarde",
    "abrindo",
    "processando",
    "sincronizando",
    "baixando",
    "download",
    "instalando",
    "instalacao",
    "updating",
    "atualizando",
)
_ERROR_KEYWORDS = ("erro", "error", "falha", "failed", "nao foi possivel", "problema")
_SUCCESS_KEYWORDS = ("concluido", "concluida", "feito", "sucesso", "success", "pronto", "done")
_LOGIN_KEYWORDS = ("login", "entrar", "sign in", "senha", "email")
_FORM_KEYWORDS = ("nome", "email", "telefone", "cep", "endereco", "enviar", "submit")
_CHAT_KEYWORDS = ("mensagem", "chat", "digite", "responder", "enviar mensagem")
_PANEL_KEYWORDS = ("dashboard", "painel", "overview", "configuracoes", "settings", "menu")
_STORE_KEYWORDS = ("comprar", "carrinho", "checkout", "preco", "wishlist", "store")
_VIDEO_KEYWORDS = ("play", "pause", "inscrever", "curtir", "playlist", "assistindo")
_DIALOG_BUTTON_HINTS = ("ok", "cancelar", "confirmar", "sim", "nao", "close", "fechar")
_IMPORTANT_BUTTON_HINTS = (
    "jogar",
    "play",
    "continuar",
    "instalar",
    "baixar",
    "download",
    "enviar",
    "entrar",
    "confirmar",
    "avancar",
    "next",
)
_GAME_INVENTORY_KEYWORDS = ("inventario", "inventory")
_GAME_MAP_KEYWORDS = ("mapa", "map")
_GAME_CRAFTING_KEYWORDS = ("craft", "crafting")
_GAME_BOSS_KEYWORDS = ("boss", "elite")
_GAME_DANGER_KEYWORDS = ("danger", "perigo", "warning", "alerta", "low hp", "vida baixa")
_GAME_DEFEAT_KEYWORDS = ("game over", "voce morreu", "you died", "derrota", "defeat", "morreu")
_GAME_VICTORY_KEYWORDS = ("vitoria", "victory", "venceu", "mission complete", "fase concluida")
_GAME_REWARD_KEYWORDS = ("reward", "recompensa", "novo item", "unlocked", "desbloqueado", "loot")
_SITE_HINTS = {
    "gmail": "Gmail",
    "youtube": "YouTube",
    "github": "GitHub",
    "chatgpt": "ChatGPT",
    "openai": "OpenAI",
    "notion": "Notion",
    "figma": "Figma",
    "discord": "Discord",
    "whatsapp": "WhatsApp",
    "steam": "Steam",
    "twitch": "Twitch",
    "netflix": "Netflix",
}


def _build_frame_signature(frame: rtc.VideoFrame) -> bytes:
    fmt = getattr(frame, "type", None)
    logger.debug("VISION_FRAME_FORMAT_DETECTED format=%s width=%s height=%s", fmt, frame.width, frame.height)
    image: Image.Image | None = None
    try:
        rgb_frame = frame.convert(rtc.VideoBufferType.RGB24)
        image = Image.frombytes("RGB", (rgb_frame.width, rgb_frame.height), bytes(rgb_frame.data))
    except Exception:
        # RGB24 conversion failed (common for RGBA frames) — convert via PIL
        try:
            if fmt == rtc.VideoBufferType.RGBA:
                rgba_frame = frame
            else:
                rgba_frame = frame.convert(rtc.VideoBufferType.RGBA)
            rgba_image = Image.frombytes("RGBA", (rgba_frame.width, rgba_frame.height), bytes(rgba_frame.data))
            image = rgba_image.convert("RGB")
            logger.debug("VISION_FRAME_RGBA_TO_RGB_OK width=%s height=%s", frame.width, frame.height)
        except Exception as inner_exc:
            logger.warning("VISION_FRAME_SIGNATURE_FAILED error=%s", inner_exc)
            raise
    grayscale = image.convert("L").resize(FRAME_SIGNATURE_SIZE)
    sig = grayscale.tobytes()
    logger.debug("VISION_FRAME_SIGNATURE_OK size=%s", len(sig))
    return sig


def _signature_diff_score(current: bytes, previous: bytes) -> float:
    if not current or not previous or len(current) != len(previous):
        return 0.0
    total = 0
    for left, right in zip(current, previous):
        total += abs(left - right)
    return total / max(len(current), 1)


def _foreground_window_handle() -> int:
    try:
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def _limited_descendants(window: Any, *, control_type: str, limit: int) -> list[Any]:
    try:
        descendants = window.descendants(control_type=control_type)
    except Exception:
        return []
    return list(descendants)[: max(limit, 1)]


def _safe_control_text(control: Any) -> str:
    for getter_name in ("window_text", "get_value"):
        getter = getattr(control, getter_name, None)
        if callable(getter):
            try:
                value = str(getter() or "").strip()
            except Exception:
                continue
            normalized = normalize_text(value)
            if normalized:
                return normalized
    element_info = getattr(control, "element_info", None)
    name = getattr(element_info, "name", "") if element_info is not None else ""
    return normalize_text(name)


def _safe_is_enabled(control: Any) -> bool:
    flag = getattr(control, "is_enabled", None)
    if callable(flag):
        try:
            return bool(flag())
        except Exception:
            return False
    return False


def _remember_value(target: list[str], value: str, *, limit: int) -> None:
    normalized = normalize_text(value)
    if not normalized or normalized in target:
        return
    if len(target) >= limit:
        return
    target.append(normalized)


def _remember_progress(target: list[int], value: str) -> None:
    text = normalize_text(value)
    if not text:
        return
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return
    try:
        percent = int(digits)
    except ValueError:
        return
    if 0 <= percent <= 100 and percent not in target:
        target.append(percent)


def _contains_keyword(values: tuple[str, ...], keywords: tuple[str, ...]) -> bool:
    return any(any(keyword in value for keyword in keywords) for value in values)


def _pick_first_matching(values: tuple[str, ...], keywords: tuple[str, ...]) -> str:
    for value in values:
        if any(keyword in value for keyword in keywords):
            return value
    return ""


def _first_non_generic(values: tuple[str, ...]) -> str:
    generic = {"menu", "home", "back", "next", "ok", "cancelar", "sim", "nao"}
    for value in values:
        if value and value not in generic:
            return value
    return ""


def _capture_window_ui_snapshot(expected_title: str = "") -> VisualUiSnapshot | None:
    pythoncom = None
    try:
        import pythoncom as _pythoncom

        pythoncom = _pythoncom
        pythoncom.CoInitialize()
    except Exception:
        pythoncom = None

    try:
        from pywinauto import Desktop
    except Exception:
        if pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass
        return None

    try:
        hwnd = _foreground_window_handle()
        if not hwnd:
            return None

        window = Desktop(backend="uia").window(handle=hwnd).wrapper_object()
        title = _safe_control_text(window) or normalize_text(expected_title)
        texts: list[str] = []
        menus: list[str] = []
        tabs: list[str] = []
        fields: list[str] = []
        list_items: list[str] = []
        buttons: list[str] = []
        enabled_buttons: list[str] = []
        progress_values: list[int] = []

        _remember_value(texts, title, limit=UI_TEXT_LIMIT)

        for control_type in ("Text", "Document", "Hyperlink"):
            for control in _limited_descendants(window, control_type=control_type, limit=UI_TEXT_LIMIT):
                _remember_value(texts, _safe_control_text(control), limit=UI_TEXT_LIMIT)

        for control in _limited_descendants(window, control_type="MenuItem", limit=UI_TEXT_LIMIT):
            _remember_value(menus, _safe_control_text(control), limit=UI_TEXT_LIMIT)

        for control in _limited_descendants(window, control_type="TabItem", limit=UI_TEXT_LIMIT):
            _remember_value(tabs, _safe_control_text(control), limit=UI_TEXT_LIMIT)

        for control_type in ("Edit", "ComboBox", "CheckBox"):
            for control in _limited_descendants(window, control_type=control_type, limit=UI_TEXT_LIMIT):
                _remember_value(fields, _safe_control_text(control), limit=UI_TEXT_LIMIT)

        for control_type in ("ListItem", "TreeItem"):
            for control in _limited_descendants(window, control_type=control_type, limit=UI_TEXT_LIMIT):
                _remember_value(list_items, _safe_control_text(control), limit=UI_TEXT_LIMIT)

        for control in _limited_descendants(window, control_type="Button", limit=UI_BUTTON_LIMIT):
            label = _safe_control_text(control)
            _remember_value(buttons, label, limit=UI_BUTTON_LIMIT)
            if _safe_is_enabled(control):
                _remember_value(enabled_buttons, label, limit=UI_BUTTON_LIMIT)

        for control in _limited_descendants(window, control_type="ProgressBar", limit=UI_PROGRESS_LIMIT):
            _remember_progress(progress_values, _safe_control_text(control))

        for value in (*texts, *menus, *tabs, *fields, *list_items, *buttons, *enabled_buttons):
            _remember_progress(progress_values, value)

        merged = tuple(dict.fromkeys((*texts, *menus, *tabs, *fields, *list_items, *buttons, *enabled_buttons)))
        loading_visible = any(any(keyword in value for keyword in _LOADING_KEYWORDS) for value in merged)
        if progress_values and max(progress_values) < 100:
            loading_visible = True
        error_visible = any(any(keyword in value for keyword in _ERROR_KEYWORDS) for value in merged)
        success_visible = any(any(keyword in value for keyword in _SUCCESS_KEYWORDS) for value in merged)

        return VisualUiSnapshot(
            captured_at=time.monotonic(),
            window_title=title,
            texts=tuple(texts),
            menus=tuple(menus),
            tabs=tuple(tabs),
            fields=tuple(fields),
            list_items=tuple(list_items),
            buttons=tuple(buttons),
            enabled_buttons=tuple(enabled_buttons),
            progress_values=tuple(sorted(progress_values)),
            loading_visible=loading_visible,
            error_visible=error_visible,
            success_visible=success_visible,
        )
    except Exception as exc:
        log_exception_safely(
            logger,
            "Falha ao capturar snapshot da UI ativa",
            exc,
            level=logging.WARNING,
        )
        return None
    finally:
        if pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


class VisualRuntime:
    def __init__(self, *, observer) -> None:
        self._observer = observer
        self._visual_state = "no_visual_context"
        self._share_active = False
        self._track_sid = ""
        self._track_source = "visual_context"
        self._captured_at = 0.0
        self._frame_token = 0
        self._width = 0
        self._height = 0
        self._change_score = 0.0
        self._stable_since = 0.0
        self._signature = b""
        self._last_invalid_reason = ""
        self._ui_cache: VisualUiSnapshot | None = None
        self._ui_cache_window_title = ""
        self._ui_cache_at = 0.0
        self._ui_lock = asyncio.Lock()

    @property
    def share_active(self) -> bool:
        return self._share_active

    def on_visual_context_active(self, active: bool) -> None:
        enabled = bool(active)
        self._share_active = enabled
        if not enabled:
            self._invalidate_context(reason="visual_context_disabled", clear_track=False)
            self._visual_state = "visual_context_ended"
            logger.info(
                "SCREEN_CONTEXT_INVALIDATED track_sid=%s source=%s width=%s height=%s frame_ts=%.3f age_ms=%.1f reason=%s",
                self._track_sid or "none",
                self._track_source,
                self._width,
                self._height,
                self._captured_at,
                self.frame_age_ms(),
                "visual_context_disabled",
            )

    def clear(self) -> None:
        self._share_active = False
        self._track_sid = ""
        self._track_source = "visual_context"
        self._visual_state = "no_visual_context"
        self._captured_at = 0.0
        self._frame_token = 0
        self._width = 0
        self._height = 0
        self._change_score = 0.0
        self._stable_since = 0.0
        self._signature = b""
        self._ui_cache = None
        self._ui_cache_window_title = ""
        self._ui_cache_at = 0.0
        self._last_invalid_reason = ""

    def on_track_subscribed(
        self,
        *,
        track_sid: str,
        source: str,
        width: int = 0,
        height: int = 0,
        now: float | None = None,
    ) -> None:
        now = now or time.monotonic()
        sid = str(track_sid or "").strip()
        raw_src = str(source or "").strip().lower() or "visual_context"
        src = "screen_share" if "screen" in raw_src else ("webcam" if "camera" in raw_src or "webcam" in raw_src else raw_src)
        changed = bool(self._track_sid and sid and self._track_sid != sid)
        if changed:
            logger.info(
                "SCREEN_TRACK_CHANGED track_sid=%s source=%s width=%s height=%s frame_ts=%.3f age_ms=%.1f reason=%s new_track_sid=%s",
                self._track_sid,
                self._track_source,
                self._width,
                self._height,
                self._captured_at,
                self.frame_age_ms(),
                "track_sid_changed",
                sid,
            )
            self._invalidate_context(reason="track_sid_changed", clear_track=False)
        self._share_active = True
        self._track_sid = sid or self._track_sid
        self._track_source = src
        if width > 0:
            self._width = int(width)
        if height > 0:
            self._height = int(height)
        self._visual_state = "visual_context_connecting"
        logger.info(
            "SCREEN_TRACK_SUBSCRIBED track_sid=%s source=%s width=%s height=%s frame_ts=%.3f age_ms=%.1f",
            self._track_sid or "none",
            self._track_source,
            self._width,
            self._height,
            self._captured_at,
            self.frame_age_ms(now=now),
        )
        logger.info(
            "VISION_TRACK_SUBSCRIBED source=%s participant=unknown",
            "screen_share" if src == "screen_share" else src,
        )

    def on_track_ended(
        self,
        *,
        track_sid: str = "",
        source: str = "visual_context",
        reason: str = "track_ended",
    ) -> None:
        sid = str(track_sid or "").strip()
        if sid and self._track_sid and sid != self._track_sid:
            return
        self._track_source = str(source or self._track_source or "visual_context").strip().lower() or "visual_context"
        logger.info(
            "SCREEN_TRACK_ENDED track_sid=%s source=%s width=%s height=%s frame_ts=%.3f age_ms=%.1f reason=%s",
            self._track_sid or "none",
            self._track_source,
            self._width,
            self._height,
            self._captured_at,
            self.frame_age_ms(),
            reason,
        )
        self._share_active = False
        self._visual_state = "visual_context_ended"
        self._invalidate_context(reason=reason, clear_track=True)

    def observe_frame(
        self,
        *,
        frame: rtc.VideoFrame,
        source: str,
        track_sid: str = "",
        now: float | None = None,
    ) -> float:
        raw_source = str(source or "").strip().lower()
        if raw_source not in {"visual_context", "livekit_screen_share", "livekit_camera", "screen_share", "webcam", "camera"}:
            return 0.0

        now = now or time.monotonic()
        sid = str(track_sid or "").strip()
        if sid and self._track_sid and sid != self._track_sid:
            frame_age_s = (now - self._captured_at) if self._captured_at > 0 else float("inf")
            if self._signature and frame_age_s < 5.0:
                # Active track still has fresh frames — ignore residual frames from old/different SID
                logger.info(
                    "VISION_TRACK_OLD_FRAME_IGNORED old_sid=%s active_sid=%s active_frame_age_s=%.2f",
                    sid,
                    self._track_sid,
                    frame_age_s,
                )
                return 0.0
            # Active track is stale or has no frames yet — accept the switch
            logger.info(
                "SCREEN_TRACK_CHANGED track_sid=%s source=%s width=%s height=%s frame_ts=%.3f age_ms=%.1f reason=%s new_track_sid=%s",
                self._track_sid,
                self._track_source,
                self._width,
                self._height,
                self._captured_at,
                self.frame_age_ms(now=now),
                "frame_from_new_track",
                sid,
            )
            self._invalidate_context(reason="frame_from_new_track", clear_track=False)
        if sid:
            self._track_sid = sid
        self._track_source = "screen_share" if "screen" in raw_source else ("webcam" if "camera" in raw_source or "webcam" in raw_source else "visual_context")
        try:
            signature = _build_frame_signature(frame)
        except Exception as exc:
            log_exception_safely(
                logger,
                "Falha ao gerar assinatura do frame compartilhado",
                exc,
                level=logging.WARNING,
            )
            return 0.0

        prev_width, prev_height = self._width, self._height
        previous_signature = self._signature
        change_score = _signature_diff_score(signature, previous_signature)
        self._share_active = True
        self._captured_at = now
        self._frame_token += 1
        self._width = int(frame.width)
        self._height = int(frame.height)
        self._change_score = change_score
        self._signature = signature
        self._visual_state = "frame_fresh"
        if prev_width > 0 and prev_height > 0 and (prev_width != self._width or prev_height != self._height):
            prev_area = max(prev_width * prev_height, 1)
            next_area = max(self._width * self._height, 1)
            area_ratio = max(prev_area, next_area) / max(min(prev_area, next_area), 1)
            if area_ratio >= 1.35:
                logger.info(
                    "SCREEN_TRACK_CHANGED track_sid=%s source=%s width=%s height=%s frame_ts=%.3f age_ms=%.1f reason=%s",
                    self._track_sid or "none",
                    self._track_source,
                    self._width,
                    self._height,
                    self._captured_at,
                    self.frame_age_ms(now=now),
                    "resolution_changed",
                )
                self._ui_cache = None
                self._ui_cache_window_title = ""
                self._ui_cache_at = 0.0
        logger.info(
            "SCREEN_FRAME_RECEIVED track_sid=%s source=%s width=%s height=%s frame_ts=%.3f age_ms=%.1f frame_token=%s",
            self._track_sid or "none",
            self._track_source,
            self._width,
            self._height,
            self._captured_at,
            self.frame_age_ms(now=now),
            self._frame_token,
        )
        logger.info(
            "VISION_FRAME_RECEIVED source=%s age_ms=%.1f",
            "screen_share" if self._track_source == "screen_share" else self._track_source,
            self.frame_age_ms(now=now),
        )
        logger.info(
            "VISION_EFFECTIVE_STATE active=true reason=%s_frame_recent",
            "screen_share" if self._track_source == "screen_share" else self._track_source,
        )

        if not previous_signature:
            self._stable_since = 0.0
            self._visual_state = "frame_pending"
            return change_score

        if change_score < FRAME_STABLE_SCORE_THRESHOLD:
            if self._stable_since <= 0:
                self._stable_since = now
            self._visual_state = "frame_fresh"
            return change_score

        self._stable_since = 0.0
        self._visual_state = "frame_pending"
        return change_score

    def has_recent_frame(self) -> bool:
        if not self._share_active or self._frame_token <= 0:
            return False
        return (time.monotonic() - self._captured_at) <= FRAME_READY_MAX_AGE_SECONDS

    def has_fresh_frame(self, *, max_age_seconds: float = FRAME_FRESH_MAX_AGE_SECONDS) -> bool:
        if not self._share_active or self._frame_token <= 0:
            if self._share_active:
                self._visual_state = "frame_pending"
            return False
        age_seconds = time.monotonic() - self._captured_at
        if age_seconds <= max(max_age_seconds, 0.1):
            self._visual_state = "frame_fresh"
            return True
        self._visual_state = "frame_stale"
        logger.info(
            "SCREEN_CONTEXT_EXPIRED track_sid=%s source=%s width=%s height=%s frame_ts=%.3f age_ms=%.1f reason=%s",
            self._track_sid or "none",
            self._track_source,
            self._width,
            self._height,
            self._captured_at,
            self.frame_age_ms(),
            "frame_stale",
        )
        return False

    def frame_age_ms(self, *, now: float | None = None) -> float:
        if self._captured_at <= 0:
            return float("inf")
        current_now = now or time.monotonic()
        return max((current_now - self._captured_at) * 1000.0, 0.0)

    async def wait_until_ready(self, *, timeout_seconds: float = 4.0, poll_seconds: float = 0.2) -> bool:
        deadline = time.monotonic() + max(timeout_seconds, 0.5)
        while time.monotonic() < deadline:
            if self.has_recent_frame():
                return True
            await asyncio.sleep(max(poll_seconds, 0.05))
        return self.has_recent_frame()

    async def wait_for_fresh_frame(
        self,
        *,
        timeout_seconds: float = 2.0,
        poll_seconds: float = 0.08,
        max_age_seconds: float = FRAME_FRESH_MAX_AGE_SECONDS,
    ) -> bool:
        deadline = time.monotonic() + max(timeout_seconds, 0.2)
        while time.monotonic() < deadline:
            if self.has_fresh_frame(max_age_seconds=max_age_seconds):
                return True
            await asyncio.sleep(max(poll_seconds, 0.04))
        return self.has_fresh_frame(max_age_seconds=max_age_seconds)

    async def get_snapshot(self, *, require_ui: bool = False) -> VisualRuntimeSnapshot | None:
        if not self._share_active:
            return None

        now = time.monotonic()
        stable_for_seconds = 0.0
        if self._stable_since > 0:
            stable_for_seconds = max(now - self._stable_since, 0.0)

        ui_snapshot = None
        if require_ui:
            ui_snapshot = await self._get_ui_snapshot()

        analysis = self._build_ui_context(
            window_title=str(getattr(self._observer.state, "active_window_title", "") or "").strip(),
            screen_summary=str(getattr(self._observer.state, "screen_summary", "") or "").strip(),
            change_score=self._change_score,
            stable_for_seconds=stable_for_seconds,
            ui_snapshot=ui_snapshot,
        )

        return VisualRuntimeSnapshot(
            share_active=self._share_active,
            captured_at=self._captured_at,
            frame_token=self._frame_token,
            width=self._width,
            height=self._height,
            change_score=self._change_score,
            stable_for_seconds=stable_for_seconds,
            window_title=str(getattr(self._observer.state, "active_window_title", "") or "").strip(),
            screen_summary=str(getattr(self._observer.state, "screen_summary", "") or "").strip(),
            signature=bytes(self._signature),
            ui=ui_snapshot,
            analysis=analysis,
        )

    def get_context_state(self) -> dict[str, object]:
        return {
            "state": self._visual_state,
            "share_active": self._share_active,
            "track_sid": self._track_sid,
            "source": self._track_source,
            "width": self._width,
            "height": self._height,
            "frame_timestamp": self._captured_at,
            "age_ms": self.frame_age_ms(),
            "frame_token": self._frame_token,
            "last_invalid_reason": self._last_invalid_reason,
        }

    async def refresh_observer_state(self, *, require_ui: bool = False) -> VisualRuntimeSnapshot | None:
        snapshot = await self.get_snapshot(require_ui=require_ui)
        if snapshot is None:
            return None
        self._apply_snapshot_to_observer(snapshot)
        return snapshot

    async def _get_ui_snapshot(self) -> VisualUiSnapshot | None:
        current_title = normalize_text(getattr(self._observer.state, "active_window_title", ""))
        now = time.monotonic()
        if (
            self._ui_cache is not None
            and self._ui_cache_window_title == current_title
            and (now - self._ui_cache_at) <= UI_CACHE_SECONDS
        ):
            return self._ui_cache

        async with self._ui_lock:
            now = time.monotonic()
            if (
                self._ui_cache is not None
                and self._ui_cache_window_title == current_title
                and (now - self._ui_cache_at) <= UI_CACHE_SECONDS
            ):
                return self._ui_cache

            snapshot = await asyncio.to_thread(
                _capture_window_ui_snapshot,
                getattr(self._observer.state, "active_window_title", ""),
            )
            self._ui_cache = snapshot
            self._ui_cache_window_title = normalize_text(snapshot.window_title) if snapshot else current_title
            self._ui_cache_at = now
            return snapshot

    def _invalidate_context(self, *, reason: str, clear_track: bool) -> None:
        self._captured_at = 0.0
        self._frame_token = 0
        self._change_score = 0.0
        self._stable_since = 0.0
        self._signature = b""
        self._ui_cache = None
        self._ui_cache_window_title = ""
        self._ui_cache_at = 0.0
        self._last_invalid_reason = str(reason or "unknown")
        self._visual_state = "frame_pending" if self._share_active else "no_visual_context"
        if clear_track:
            self._track_sid = ""
            self._track_source = "visual_context"
            self._width = 0
            self._height = 0

    def _apply_snapshot_to_observer(self, snapshot: VisualRuntimeSnapshot) -> None:
        state = self._observer.state
        analysis = snapshot.analysis or VisualUiContext()
        state.visual_change_score = snapshot.change_score
        state.ui_summary = analysis.summary
        state.ui_mode = analysis.mode
        state.ui_primary_action = analysis.primary_action
        state.ui_prominent_text = analysis.prominent_text
        state.ui_flags = list(analysis.flags)
        state.confidence = max(state.confidence, analysis.confidence)

        if analysis.site_name:
            entities = [analysis.site_name, *list(state.entities)]
            deduped: list[str] = []
            seen: set[str] = set()
            for entity in entities:
                key = normalize_text(entity)
                if not key or key in seen:
                    continue
                seen.add(key)
                deduped.append(entity)
            state.entities = deduped[:6]

        action_override = {
            "loading": "carregando",
            "erro": "erro_visivel",
            "login": "login",
            "menu": "menu",
            "inventario": "inventario",
            "chat": "chat",
            "formulario": "formulario",
            "combate": "combate",
            "vitoria": "vitoria",
            "derrota": "derrota",
            "perigo": "perigo",
        }.get(analysis.mode, "")
        if action_override and state.action_guess in {"", "navegando", "jogando", "assistindo_video"}:
            state.action_guess = action_override

        browser_apps = {"Google Chrome", "Microsoft Edge", "Firefox"}
        if analysis.site_name and (state.domain == "desktop" or state.app_name in browser_apps):
            state.domain = "web"

    def _build_ui_context(
        self,
        *,
        window_title: str,
        screen_summary: str,
        change_score: float,
        stable_for_seconds: float,
        ui_snapshot: VisualUiSnapshot | None,
    ) -> VisualUiContext:
        state = self._observer.state
        values = [normalize_text(window_title), normalize_text(screen_summary)]
        if ui_snapshot is not None:
            values.extend(ui_snapshot.texts)
            values.extend(ui_snapshot.menus)
            values.extend(ui_snapshot.tabs)
            values.extend(ui_snapshot.fields)
            values.extend(ui_snapshot.list_items)
            values.extend(ui_snapshot.buttons)
            values.extend(ui_snapshot.enabled_buttons)
        strings = tuple(value for value in values if value)

        flags: list[str] = []
        if ui_snapshot is not None and ui_snapshot.loading_visible:
            flags.append("loading")
        if ui_snapshot is not None and ui_snapshot.error_visible:
            flags.append("erro")
        if ui_snapshot is not None and ui_snapshot.success_visible:
            flags.append("sucesso")

        site_name = ""
        for keyword, label in _SITE_HINTS.items():
            if any(keyword in value for value in strings):
                site_name = label
                break

        mode = ""
        if ui_snapshot is not None and ui_snapshot.error_visible:
            mode = "erro"
        elif ui_snapshot is not None and ui_snapshot.success_visible:
            mode = "sucesso"
        elif ui_snapshot is not None and ui_snapshot.loading_visible:
            mode = "loading"
        elif _contains_keyword(strings, _LOGIN_KEYWORDS):
            mode = "login"
        elif _contains_keyword(strings, _FORM_KEYWORDS):
            mode = "formulario"
        elif _contains_keyword(strings, _CHAT_KEYWORDS):
            mode = "chat"
        elif _contains_keyword(strings, _STORE_KEYWORDS):
            mode = "loja"
        elif _contains_keyword(strings, _VIDEO_KEYWORDS):
            mode = "video"
        elif _contains_keyword(strings, _PANEL_KEYWORDS):
            mode = "painel"
        elif ui_snapshot is not None and (
            _contains_keyword(ui_snapshot.menus, _DIALOG_BUTTON_HINTS)
            or _contains_keyword(ui_snapshot.enabled_buttons, _DIALOG_BUTTON_HINTS)
        ):
            mode = "dialogo"

        if state.domain == "game":
            if _contains_keyword(strings, _GAME_DEFEAT_KEYWORDS):
                mode = "derrota"
            elif _contains_keyword(strings, _GAME_VICTORY_KEYWORDS):
                mode = "vitoria"
            elif _contains_keyword(strings, _GAME_REWARD_KEYWORDS):
                mode = "recompensa"
            elif _contains_keyword(strings, _GAME_BOSS_KEYWORDS):
                mode = "boss"
            elif _contains_keyword(strings, _GAME_DANGER_KEYWORDS):
                mode = "perigo"
            elif _contains_keyword(strings, _GAME_INVENTORY_KEYWORDS):
                mode = "inventario"
            elif _contains_keyword(strings, _GAME_MAP_KEYWORDS):
                mode = "mapa"
            elif _contains_keyword(strings, _GAME_CRAFTING_KEYWORDS):
                mode = "crafting"
            elif mode == "menu" or _contains_keyword(strings, ("pause", "options", "configuracoes", "settings", "menu")):
                mode = "menu"
            elif change_score >= 12.0 and stable_for_seconds < 1.5:
                mode = "combate"
            elif not mode and change_score >= 8.0:
                mode = "jogo"

        if not mode and _contains_keyword(strings, ("menu", "configuracoes", "settings", "options")):
            mode = "menu"
        if not mode and ui_snapshot is not None and ui_snapshot.list_items:
            mode = "lista"

        if mode and mode not in flags:
            flags.append(mode)

        primary_action = ""
        if ui_snapshot is not None:
            primary_action = _pick_first_matching(ui_snapshot.enabled_buttons, _IMPORTANT_BUTTON_HINTS)
            if not primary_action:
                primary_action = _first_non_generic(ui_snapshot.enabled_buttons) or _first_non_generic(ui_snapshot.buttons)

        prominent_text = ""
        if ui_snapshot is not None:
            candidates = (
                ui_snapshot.texts
                + ui_snapshot.menus
                + ui_snapshot.tabs
                + ui_snapshot.fields
                + ui_snapshot.list_items
            )
            prominent_text = _first_non_generic(candidates)

        summary_parts: list[str] = []
        if site_name:
            summary_parts.append(site_name)
        if mode:
            summary_parts.append(f"modo {mode}")
        if primary_action:
            summary_parts.append(f"botao {primary_action}")
        if prominent_text:
            summary_parts.append(prominent_text[:60])
        summary = "; ".join(summary_parts)[:160]

        confidence = 0.18
        if ui_snapshot is not None:
            confidence = 0.42
        if mode:
            confidence = max(confidence, 0.7 if state.domain == "game" else 0.62)
        if site_name or primary_action or prominent_text:
            confidence = max(confidence, 0.58)

        return VisualUiContext(
            mode=mode,
            summary=summary,
            primary_action=primary_action,
            prominent_text=prominent_text,
            site_name=site_name,
            flags=tuple(flags),
            confidence=confidence,
        )
