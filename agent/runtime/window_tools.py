"""Tools das janelas satélite da Kira: falam somente com o backend local."""

from __future__ import annotations

import os
import re
import unicodedata
from urllib.parse import urlsplit

import httpx
from livekit.agents import function_tool

_WINDOW_NUMBER_RE = re.compile(r"(\d+)\s*$")
_LIMIT_MESSAGE = "Já existem 3 janelas extras abertas; feche uma antes."
_UNAVAILABLE = "Não consegui falar com as janelas agora."
WIDGET_ALIASES = {
    "relogio": "clock", "hora": "clock", "clock": "clock", "relogio mundial": "worldclock", "calendario": "calendar", "data": "bigdate",
    "timer": "timer", "temporizador": "timer", "contagem regressiva": "countdown", "pomodoro": "pomodoro",
    "noticias": "news", "ultimas noticias": "news", "news": "news", "clima": "weather", "tempo": "weather", "previsao": "weather",
    "cotacoes": "quotes", "cotacao": "quotes", "dolar": "quotes", "placar": "scores", "jogos": "scores", "lua": "moon", "frase": "dayquote",
    "frase do dia": "dayquote", "hoje na historia": "history", "sistema vital": "vitals", "sistema": "vitals", "vitals": "vitals",
    "cpu": "cpucores", "nucleos": "cpucores", "memoria": "ram", "ram": "ram", "disco": "disk", "rede": "network", "gpu": "gpu", "placa de video": "gpu", "bateria": "battery", "temperatura": "battery", "tempo ligado": "uptime", "uptime": "uptime", "top aplicativos": "topapps", "aplicativos": "topapps", "aplicativos abertos": "openapps", "transcricao": "transcript", "lembretes": "reminders", "musica": "music", "apis": "apis", "apis ativas": "apis",
    "imagens": "viewer", "visualizador": "viewer", "notas": "notes", "tarefas": "todo", "lista de tarefas": "todo", "atalhos": "shortcuts",
    "imagem": "image", "texto": "text", "ofertas": "games", "ofertas de jogos": "games", "steam": "games", "estreias": "games", "lancamentos": "games",
    "jogos gratis": "freegames", "jogos gratuitos": "freegames", "epic": "freegames", "em alta": "trending", "tendencias": "trending", "mais lidos": "trending",
}


def _fold(value: object) -> str:
    return unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower().strip()


def widget_kind(value: object) -> str | None:
    """Nome falado ou interno de um widget -> tipo aceito pelo backend."""
    text = _fold(value)
    return WIDGET_ALIASES.get(text) or (text if text in set(WIDGET_ALIASES.values()) else None)


def window_id_from_reference(reference: object) -> str | None:
    """'2', 'w2' e 'janela 2' viram 'w2'; a janela 1 é a principal e não é endereçável aqui."""
    match = _WINDOW_NUMBER_RE.search(str(reference or "").strip().lower())
    if not match or match.group(1) == "1":
        return None
    return f"w{int(match.group(1))}"


def _backend_url(path: str) -> str:
    configured = str(os.getenv("KIRA_UI_BASE_URL") or "http://127.0.0.1:3000").strip().rstrip("/")
    try:
        parsed = urlsplit(configured)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            configured = "http://127.0.0.1:3000"
    except Exception:
        configured = "http://127.0.0.1:3000"
    return f"{configured}{path}"


async def _call(method: str, path: str, payload: dict[str, object] | None = None) -> httpx.Response:
    headers = {"X-Kira-Internal": str(os.getenv("KIRA_INTERNAL_TOKEN") or "").strip()}
    timeout = httpx.Timeout(connect=0.35, read=4.0, write=0.35, pool=0.35)
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        return await client.request(method, _backend_url(path), json=payload)


async def _resolve_window(reference: object, *, open_saved: bool = True) -> str | None:
    """Acha a janela aberta pelo número ou nome; se for uma janela fixa fechada, abre."""
    data = (await _call("GET", "/api/ui/windows")).json()
    text = _fold(reference)
    for window in data.get("windows", []):
        if text in {window["windowId"], _fold(window.get("name")), _fold(window.get("title"))}:
            return window["windowId"]
    by_number = window_id_from_reference(reference)
    if by_number and any(w["windowId"] == by_number for w in data.get("windows", [])):
        return by_number
    if open_saved:
        for profile in data.get("profiles", []):
            if text == _fold(profile.get("name")):
                opened = (await _call("POST", "/api/ui/windows", {"profileId": profile["id"]})).json()
                return opened.get("window", {}).get("windowId")
    return None


class WindowToolsMixin:
    @function_tool(
        name="list_windows",
        description=(
            "Lista as janelas extras da Kira (a principal não entra): quantas há, se cada uma está à mostra, minimizada ou "
            "coberta por outra janela, e os widgets de cada uma. Conteúdo que a Kira mostra (imagens, criação, notícias) "
            "abre na principal; vai para uma janela extra à mostra quando o Chefe pede (move_panel) ou quando ele já passou "
            "esse tipo de painel para lá."
        ),
    )
    async def list_windows(self) -> str:
        try:
            body = (await _call("GET", "/api/ui/windows")).json()
        except Exception:
            return _UNAVAILABLE
        return " ".join(str(body.get("summary") or _UNAVAILABLE).split())[:500]

    @function_tool(
        name="open_window",
        description="Abre uma janela satélite vazia, só quando o usuário pedir. name opcional: abre a janela fixa salva com esse nome. Máximo 3 janelas extras.",
    )
    async def open_window(self, name: str | None = None) -> str:
        try:
            if name and await _resolve_window(name):
                return "ok"
            response = await _call("POST", "/api/ui/windows", {})
        except Exception:
            return _UNAVAILABLE
        if response.status_code == 409:
            return _LIMIT_MESSAGE
        return "ok" if response.status_code < 400 else "Não consegui abrir a janela agora."

    @function_tool(
        name="add_widget",
        description=(
            "Coloca um widget numa janela satélite. window: número ('2') ou nome ('Painel TV'). kind: relógio, clima, notícias, cotações, "
            "placar, sistema vital, transcrição, lembretes, notas, tarefas, calendário, timer, cronômetro, pomodoro, lua, frase do dia, "
            "hoje na história, música, apis, imagens, atalhos, texto. Use para 'coloca o relógio na janela Painel TV'."
        ),
    )
    async def add_widget(self, window: str, kind: str) -> str:
        normalized = widget_kind(kind)
        if normalized is None:
            return "Não conheço esse widget."
        try:
            target = await _resolve_window(window)
            if target is None:
                return "Essa janela não está aberta."
            response = await _call("POST", f"/api/ui/windows/{target}/widgets", {"kind": normalized})
        except Exception:
            return _UNAVAILABLE
        return "ok" if response.status_code < 400 else "Não consegui adicionar o widget."

    @function_tool(
        name="remove_widget",
        description="Tira um widget de uma janela satélite. window: número ou nome. kind: o widget (relógio, clima...). Use para 'tira o clima'.",
    )
    async def remove_widget(self, window: str, kind: str) -> str:
        normalized = widget_kind(kind)
        if normalized is None:
            return "Não conheço esse widget."
        try:
            target = await _resolve_window(window, open_saved=False)
            if target is None:
                return "Essa janela não está aberta."
            response = await _call("DELETE", f"/api/ui/windows/{target}/widgets/{normalized}")
        except Exception:
            return _UNAVAILABLE
        if response.status_code == 422:
            return "Esse widget não está nessa janela."
        return "ok" if response.status_code < 400 else "Não consegui remover o widget."

    @function_tool(
        name="show_in_window",
        description="Atalho de add_widget: mostra um conteúdo (relógio, transcrição, sistema vital...) numa janela satélite. window: número ou nome.",
    )
    async def show_in_window(self, window_id: str, content: str) -> str:
        return await self.add_widget(window_id, content)

    @function_tool(
        name="move_panel",
        description=(
            "Passa painéis da Kira (imagens, vídeo, artigo, notícias, placar, criação...) entre a janela principal e as "
            "janelas extras. panel: 'isso' (o mais recente), 'tudo', um tipo ('imagens', 'vídeo', 'placar', 'criação') ou "
            "parte do assunto ('as fotos do gato'). window: para onde: 'principal', '2', 'a outra', 'a janela nova' ou o "
            "nome da janela fixa, ou 'monitor 1'/'monitor 2' (painel solto sobre aquele monitor, sem janela). from_window: opcional, de onde ('2' em 'traz tudo da janela 2'). Ex.: 'move esse painel "
            "pra janela 2', 'traz da janela 2 pra principal', 'joga as imagens na outra janela', 'passa o clima pro monitor 2'. Se o painel ainda não "
            "abriu ('mostra o placar do Flamengo na janela 2'), chame esta tool antes da que mostra: o próximo painel "
            "desse tipo abre lá. Sem pedido, tudo abre na principal. Diga o resultado com suas palavras."
        ),
    )
    async def move_panel(self, panel: str = "isso", window: str = "principal", from_window: str | None = None) -> str:
        try:
            response = await _call("POST", "/api/ui/panels/move", {"panel": panel, "window": window, "from_window": from_window or ""})
            body = response.json()
        except Exception:
            return _UNAVAILABLE
        return " ".join(str(body.get("summary") or "Não consegui mover agora.").split())[:300]

    @function_tool(
        name="close_window",
        description="Fecha uma janela satélite (a janela fixa continua salva). window_id: número ou nome. A janela 1 não pode ser fechada por aqui.",
    )
    async def close_window(self, window_id: str) -> str:
        if _fold(window_id) in {"1", "janela 1", "w1", "principal"}:
            return "Só consigo fechar as janelas extras (2 em diante)."
        try:
            target = await _resolve_window(window_id, open_saved=False)
            if target is None:
                return "Essa janela não está aberta."
            response = await _call("DELETE", f"/api/ui/windows/{target}")
        except Exception:
            return _UNAVAILABLE
        return "ok" if response.status_code < 400 else "Não consegui fechar a janela agora."


__all__ = ["WindowToolsMixin", "widget_kind", "window_id_from_reference"]
