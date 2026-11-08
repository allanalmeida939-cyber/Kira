"""Hub local e validado para ações enviadas pelo backend à interface da Kira."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import time
from collections import deque
from urllib.parse import urlsplit


MAX_PAYLOAD_BYTES = 256 * 1024
MAX_BATCH_ACTIONS = 12
MAX_CLIENTS = 8
MAX_ACTIONS_PER_SECOND = 20
ACTION_TYPES = frozenset({"status", "open_panel", "update_panel", "close_panel", "media_cue", "layout", "window", "present", "present_patch", "screen_locate_result", "ui_control", "ui_call"})
# Música e vídeo no celular (pedido feito por lá toca lá): os mesmos comandos do player da tela do PC
UI_CONTROL_COMMANDS = frozenset({"open_tab", "play_item", "play_pause", "play", "pause", "next", "previous", "stop", "set_volume", "volume_up",
                                 "volume_down", "fullscreen", "exit_fullscreen", "close", "minimize", "restore"})
TRACK_KEYS = frozenset({"videoId", "title", "channel", "channelTitle", "durationSeconds", "duration", "thumbnail", "url", "source"})
SCREEN_LOCATE_STATES = frozenset({"looking", "found", "not_found", "ambiguous", "error", "cleared"})
WINDOW_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
KIRA_STATES = frozenset({"idle", "listening", "thinking", "searching", "speaking"})
PANEL_KINDS = frozenset(
    {
        "transcript",
        "apis",
        "gallery",
        "video",
        "article",
        "search-log",
        "scoreboard",
        "viewer",
        "election",
        "info",
        "download",
        "notifications",
        "whatsapp",
        "music",
        "memory",
        "logs",
        "reminders",
        "tutor",  # Estudos (aula, simulado, correção): faltava aqui e o simulado sumia em silêncio (2026-11-05)
        "quadro",  # painéis que a Kira cria na hora (services/quadro_panels.py)
        "smart_home",
        "creation",  # imagem criada pela Kira (services/image_creation.py); sem ela o painel era descartado em silêncio
    }
)
VIEW_IDS = frozenset({"main", "chat"})
LAYOUT_PRESETS = frozenset({"default", "main", "focus", "grid", "split", "research"})
ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$", re.IGNORECASE)
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
URL_KEY_RE = re.compile(r"(?:url|href|src|link|image|thumbnail|poster)$", re.IGNORECASE)
SAFE_PROP_KEYS = frozenset(
    {
        "items",
        "query",
        "content",
        "body",
        "description",
        "text",
        "url",
        "imageUrl",
        "image",
        "thumbnail",
        "poster",
        "link",
        "title",
        "source",
        "sourceUrl",
        "sources",
        "steps",
        "logs",
        "status",
        "value",
        "index",
        "duration",
        "media",
        "videos",
        "articles",
        "images",
        "results",
        "label",
        "done",
        "kind",
        "id",
        "name",
        "date",
        "author",
        "category",
        "href",
        "src",
        "license",
        "licenses",
        "videoId",
        "types",
        "time",
        "timestamp",
        "ok",
        "append",
        "alt",
        "level",
        "home",
        "away",
        "homeScore",
        "awayScore",
        "minute",
        "league",
        "state",
        "team",
        "venue",
        "updatedAt",
        "homeLogoUrl",
        "awayLogoUrl",
        "stale",
        "caption",
        "excerpt",
        "party",
        "number",
        "votes",
        "pct",
        "photoUrl",
        "apurado",
        "elected",
        "summary",
        "facts",
        "beats",
        "activeBeat",
        "keywords",
    }
)

# Os resultados de mídia são aceitos somente de origens que o backend conhece.
ALLOWED_URL_HOSTS = frozenset(
    {
        "upload.wikimedia.org",
        "thumb.wikimedia.org",
        "commons.wikimedia.org",
        "i.ytimg.com",
        "api.openverse.org",
        "pt.wikipedia.org",
        "en.wikipedia.org",
        "www.wikipedia.org",
        "www.youtube.com",
        "youtube.com",
        "www.youtube-nocookie.com",
        "youtube-nocookie.com",
    }
)


def _is_plain_object(value: object) -> bool:
    return isinstance(value, dict)


def _safe_string(value: object, limit: int = 2000) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if len(text) <= limit else None


IMAGE_KEY_RE = re.compile(r"(?:image|imageurl|thumbnail|poster|src|logourl|photourl)$", re.IGNORECASE)
PROXY_PATH = "/api/ui/img?u="
# Imagem criada pela Kira, servida pelo backend (mesma regra do actions.js da interface).
CREATION_PATH_RE = re.compile(r"^/api/ui/creation/file/[a-z0-9-]{1,60}-\d{10}\.(?:jpg|png|webp)$")
# Radar montado pelo backend (services/sky_watch.py), servido localmente.
LOCAL_IMAGE_RE = re.compile(r"^/api/(sky/image/radar-[0-9a-f]{10}\.png|vision/image/cam-[0-9a-f]{10}\.jpg)$")


def _public_https_host(parsed: object) -> str | None:
    host = str(getattr(parsed, "hostname", "") or "").lower().rstrip(".")
    if getattr(parsed, "scheme", "").lower() != "https" or not host or parsed.username or parsed.password:
        return None
    try:
        if not ipaddress.ip_address(host).is_global:
            return None
    except ValueError:
        if host == "localhost" or "." not in host:
            return None
    return host


def _safe_url(value: object, key: str = "") -> str | None:
    """Imagens: so via proxy local (ou hosts conhecidos). Links: qualquer https publico, abertos so por clique."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if IMAGE_KEY_RE.search(key) and text.startswith(PROXY_PATH):
        return text if len(text) <= 2000 and not any(ch.isspace() or ord(ch) < 32 for ch in text) else None
    if IMAGE_KEY_RE.search(key) and (CREATION_PATH_RE.fullmatch(text) or LOCAL_IMAGE_RE.fullmatch(text)):
        return text
    try:
        parsed = urlsplit(text)
    except Exception:
        return None
    host = _public_https_host(parsed)
    if host is None:
        return None
    if IMAGE_KEY_RE.search(key) and host not in ALLOWED_URL_HOSTS:
        return None
    return text


def _clean_value(value: object, key: str = "", depth: int = 0) -> object:
    if depth > 5:
        raise ValueError("payload_depth")
    if isinstance(value, str):
        if URL_KEY_RE.search(key):
            safe_url = _safe_url(value, key)
            if safe_url is None:
                raise ValueError("unsafe_url")
            return safe_url
        safe_text = _safe_string(value)
        if safe_text is None:
            raise ValueError("string_too_long")
        return safe_text
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
            raise ValueError("non_finite_number")
        return value
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, list):
        if len(value) > 24:
            raise ValueError("array_too_long")
        return [_clean_value(item, key, depth + 1) for item in value]
    if not _is_plain_object(value):
        raise ValueError("invalid_object")
    result: dict[str, object] = {}
    for child_key, child_value in value.items():
        if not isinstance(child_key, str) or len(child_key) > 64:
            raise ValueError("invalid_property_name")
        if child_key not in SAFE_PROP_KEYS and child_key.lower() not in {item.lower() for item in SAFE_PROP_KEYS}:
            continue
        result[child_key] = _clean_value(child_value, child_key, depth + 1)
    return result


def _validate_id(value: object, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    return value if isinstance(value, str) and ID_RE.fullmatch(value) else ""


def validate_action(raw: object) -> dict[str, object] | None:
    """Valida a ação e preserva o windowId (padrão: janela principal) quando informado."""
    result = _validate_action_body(raw)
    if result is None or not isinstance(raw, dict):
        return result
    window_id = raw.get("windowId")
    if window_id is not None:
        if not isinstance(window_id, str) or not WINDOW_ID_RE.fullmatch(window_id):
            return None
        result["windowId"] = window_id
    return result


def _validate_action_body(raw: object) -> dict[str, object] | None:
    """Valida e normaliza uma ação antes que ela alcance qualquer WebSocket."""

    if not _is_plain_object(raw) or not isinstance(raw.get("type"), str):
        return None
    action_type = raw["type"]
    if action_type not in ACTION_TYPES:
        return None

    if action_type in {"present", "present_patch"}:
        from services.presentation import validate_present, validate_present_patch

        return validate_present(raw) if action_type == "present" else validate_present_patch(raw)

    if action_type == "screen_locate_result":
        return _validate_screen_locate(raw)

    if action_type == "ui_call":  # ida e volta com a tela do celular (services/phone_rpc.py)
        from services.phone_rpc import MAX_ARGS, TARGETS

        call_id, target, method, args = raw.get("id"), raw.get("target"), raw.get("method"), raw.get("args")
        if not (isinstance(call_id, str) and re.fullmatch(r"[0-9a-f]{8,40}", call_id)) or method not in TARGETS.get(target, ()):
            return None
        try:
            if len(json.dumps(args, ensure_ascii=False)) > MAX_ARGS:
                return None
        except (TypeError, ValueError):
            return None
        return {"type": "ui_call", "id": call_id, "target": target, "method": method, "args": args}

    if action_type == "ui_control":
        command = raw.get("command")
        if command not in UI_CONTROL_COMMANDS:
            return None
        value = raw.get("value")
        if isinstance(value, dict):  # faixa do YouTube: só campos simples conhecidos
            value = {key: item for key, item in value.items() if key in TRACK_KEYS and isinstance(item, (str, int, float)) and len(str(item)) <= 300}
            if command == "play_item" and not re.fullmatch(r"[\w-]{6,20}", str(value.get("videoId") or "")):
                return None
        elif command == "open_tab":
            if value not in ("kira", "whatsapp", "config", "browser", "music", "estudos", "casa"):
                return None
        elif value is not None and not isinstance(value, (int, float)):
            return None
        return {"type": "ui_control", "command": command, "value": value}

    if action_type == "status":
        state = raw.get("state")
        if state not in KIRA_STATES:
            return None
        result: dict[str, object] = {"type": "status", "state": state}
        if raw.get("text") is not None:
            text = _safe_string(raw.get("text"))
            if text is None:
                return None
            result["text"] = text
        if raw.get("scope") is not None:
            if raw.get("scope") not in {"media", "voice"}:
                return None
            result["scope"] = raw["scope"]
        steps = raw.get("steps")
        if steps is not None:
            if not isinstance(steps, list) or len(steps) > 24:
                return None
            normalized_steps = []
            for step in steps:
                if not _is_plain_object(step):
                    return None
                label = _safe_string(step.get("label"), 120)
                if label is None or not isinstance(step.get("done"), bool):
                    return None
                normalized_steps.append({"label": label, "done": step["done"]})
            result["steps"] = normalized_steps
        return result

    if action_type == "open_panel":
        kind = raw.get("kind")
        if kind not in PANEL_KINDS:
            return None
        result = {"type": "open_panel", "kind": kind}
        panel_id = _validate_id(raw.get("id"), required=False)
        if panel_id == "":
            return None
        if panel_id is not None:
            result["id"] = panel_id
        if raw.get("title") is not None:
            title = _safe_string(raw.get("title"), 120)
            if title is None:
                return None
            result["title"] = title
        if raw.get("props") is not None:
            try:
                # Estudos e quadros têm campos próprios e aninhados (simulado, questões); as views só usam textContent e
                # imagem por proxy, então passam inteiros como no actions.js (o tamanho é limitado em MAX_PAYLOAD_BYTES).
                props = json.loads(json.dumps(raw["props"], ensure_ascii=False)) if kind in {"tutor", "quadro"} else _clean_value(raw["props"])
            except (ValueError, TypeError):
                return None
            if not isinstance(props, dict):
                return None
            items = props.get("items")
            if isinstance(items, list) and len(items) > (6 if kind == "video" else 24):
                return None
            if kind == "video":
                for item in items or []:
                    if isinstance(item, dict) and item.get("videoId") is not None and not (
                        isinstance(item.get("videoId"), str) and VIDEO_ID_RE.fullmatch(item["videoId"].strip())
                    ):
                        return None
                if props.get("videoId") is not None and not (
                    isinstance(props.get("videoId"), str) and VIDEO_ID_RE.fullmatch(props["videoId"].strip())
                ):
                    return None
            result["props"] = props
        if raw.get("focus") is not None:
            if not isinstance(raw["focus"], bool):
                return None
            result["focus"] = raw["focus"]
        if raw.get("autoCloseMs") is not None:
            value = raw["autoCloseMs"]
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 600000:
                return None
            result["autoCloseMs"] = value
        if raw.get("view") is not None:
            if raw["view"] not in VIEW_IDS:
                return None
            result["view"] = raw["view"]
        return result

    if action_type == "window":
        window_id = raw.get("windowId")
        if not isinstance(window_id, str) or not WINDOW_ID_RE.fullmatch(window_id):
            return None
        if raw.get("closed") is True:
            return {"type": "window", "windowId": window_id, "closed": True}
        if raw.get("reload") is True:
            return {"type": "window", "windowId": window_id, "reload": True}
        return None

    if action_type == "update_panel":
        panel_id = _validate_id(raw.get("id"))
        if not panel_id:
            return None
        if not _is_plain_object(raw.get("props")):
            return None
        try:
            props = _clean_value(raw["props"])
        except ValueError:
            return None
        return {"type": "update_panel", "id": panel_id, "props": props}

    if action_type == "close_panel":
        panel_id = _validate_id(raw.get("id"))
        if not panel_id:
            return None
        result = {"type": "close_panel", "id": panel_id}
        if raw.get("preserveUserTouched") is not None:
            if not isinstance(raw["preserveUserTouched"], bool):
                return None
            result["preserveUserTouched"] = raw["preserveUserTouched"]
        return result

    if action_type == "media_cue":
        panel_id = _validate_id(raw.get("panelId"))
        index = raw.get("index")
        if not panel_id or not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 10000:
            return None
        return {"type": "media_cue", "panelId": panel_id, "index": index}

    preset = raw.get("preset")
    return {"type": "layout", "preset": preset} if preset in LAYOUT_PRESETS else None


def _validate_screen_locate(raw: dict) -> dict[str, object] | None:
    """Resultado do 'onde está isso?': estado, alvo e caixas em pixels físicos (texto vindo da tela é só rótulo)."""
    request_id, state = raw.get("requestId"), raw.get("state")
    if not isinstance(request_id, str) or not ID_RE.fullmatch(request_id) or state not in SCREEN_LOCATE_STATES:
        return None
    result: dict[str, object] = {"type": "screen_locate_result", "requestId": request_id, "state": state}
    if raw.get("target") is not None:
        target = _safe_string(raw.get("target"), 120)
        if target is None:
            return None
        result["target"] = target
    monitor = raw.get("monitor")
    if monitor is not None:
        if not isinstance(monitor, int) or isinstance(monitor, bool) or not 1 <= monitor <= 16:
            return None
        result["monitor"] = monitor
    matches = raw.get("matches")
    if matches is not None:
        if not isinstance(matches, list) or len(matches) > 10:
            return None
        clean = []
        for match in matches:
            rect = match.get("rect") if isinstance(match, dict) else None
            label = _safe_string(match.get("label"), 80) if isinstance(match, dict) else None
            confidence = match.get("confidence", 0) if isinstance(match, dict) else None
            if label is None or not isinstance(rect, dict) or not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
                return None
            numbers = [rect.get(key) for key in ("x", "y", "width", "height")]
            if not all(isinstance(n, int) and not isinstance(n, bool) and -100_000 <= n <= 100_000 for n in numbers):
                return None
            clean.append({"label": label, "rect": dict(zip(("x", "y", "width", "height"), numbers)), "confidence": min(1.0, max(0.0, float(confidence)))})
        result["matches"] = clean
    return result


def validate_actions(raw: object) -> list[dict[str, object]] | None:
    if isinstance(raw, list):
        if not raw or len(raw) > MAX_BATCH_ACTIONS:
            return None
        values = raw
    else:
        values = [raw]
    actions = [validate_action(item) for item in values]
    if any(action is None for action in actions):
        return None
    try:
        serialized = json.dumps(actions, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    if len(serialized.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        return None
    return [action for action in actions if action is not None]


class UiActionsHub:
    """Distribui ações somente a clientes locais conectados à UI."""

    def __init__(self, *, max_clients: int = MAX_CLIENTS, rate_limit: int = MAX_ACTIONS_PER_SECOND) -> None:
        self.max_clients = max(1, int(max_clients))
        self.rate_limit = max(1, int(rate_limit))
        self._clients: set[object] = set()
        self._timestamps: deque[float] = deque()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def phone_connected(self) -> bool:
        """Alguma tela aberta pelo celular (o portão marca a conexão com X-Kira-Remote)."""
        return any(getattr(client, "headers", {}).get("x-kira-remote") for client in self._clients)

    def has_window(self, window_id: str) -> bool:
        """A página da janela extra já conectou (satellite.js manda ?window=<id>)."""
        return any(getattr(client, "query_params", {}).get("window") == window_id for client in self._clients)

    def add_client(self, websocket: object) -> bool:
        if len(self._clients) >= self.max_clients:
            return False
        self._clients.add(websocket)
        return True

    def remove_client(self, websocket: object) -> None:
        self._clients.discard(websocket)

    def _consume_rate(self, count: int) -> bool:
        now = time.monotonic()
        while self._timestamps and now - self._timestamps[0] >= 1.0:
            self._timestamps.popleft()
        if len(self._timestamps) + count > self.rate_limit:
            return False
        self._timestamps.extend([now] * count)
        return True

    async def publish(self, raw: object) -> int:
        actions = validate_actions(raw)
        if not actions or not self._consume_rate(len(actions)):
            return 0
        payloads = [json.dumps(action, ensure_ascii=False, separators=(",", ":")) for action in actions]
        clients = tuple(self._clients)
        if not clients:
            return 0

        async def send(websocket: object, payload: str) -> bool:
            try:
                await asyncio.wait_for(websocket.send_text(payload), timeout=2.0)
                return True
            except Exception:
                self.remove_client(websocket)
                return False

        delivered = 0
        for payload in payloads:
            results = await asyncio.gather(*(send(client, payload) for client in clients), return_exceptions=True)
            delivered += sum(result is True for result in results)
        return delivered


__all__ = [
    "ACTION_TYPES",
    "ALLOWED_URL_HOSTS",
    "KIRA_STATES",
    "MAX_ACTIONS_PER_SECOND",
    "MAX_BATCH_ACTIONS",
    "MAX_CLIENTS",
    "MAX_PAYLOAD_BYTES",
    "PANEL_KINDS",
    "UiActionsHub",
    "validate_action",
    "validate_actions",
]
