"""Registro dos painéis que a Kira abriu: o que está na tela, tempo limite e fechamento por alvo."""

from __future__ import annotations

import asyncio
import random
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Awaitable, Callable

PUBLISH = Callable[[dict[str, object]], Awaitable[object]]
# Painéis abertos pela Kira por conta própria. Um assunto novo não apaga mais o anterior: ele encolhe para o lado
# (miniatura) e só o mais antigo sai quando passa de três na mesma janela.
AUTO_KINDS = frozenset({"viewer", "gallery", "article", "video", "presentation", "election", "info"})
MAX_AUTO_PANELS = 3
MAIN_WINDOW = "w1"
# o celular é uma "janela" a mais: com o Chefe usando o celular, os painéis abrem lá (services/remote_devices.py)
PHONE_WINDOW = "celular"
TV_WINDOW = "tv"  # navegador da TV LG (services/tv_screen.py): recebe pelo SSE, não pelo hub
# O que pode ir para uma janela extra à mostra. Abre na principal; vai para a extra quando o Chefe pede, quando ele já passou
# esse tipo de painel para lá ou quando continua um assunto que está lá. Dados pessoais ficam na principal.
ROUTABLE_KINDS = frozenset({"viewer", "gallery", "article", "video", "presentation", "election", "info", "scoreboard", "creation", "quadro",
                            "tutor", "smart_home"})  # Estudos e Casa também vão para a TV (2026-11-06)
MAIN_ONLY_IDS = frozenset({"briefing-kira", "agenda-kira", "email-kira", "health-kira"})
# Nunca vão para a TV (nem no modo "tudo na TV"): conversa, lembretes, WhatsApp, memória, logs, e-mail, agenda, saúde.
TV_BLOCKED_KINDS = frozenset({"transcript", "reminders", "apis", "notifications", "whatsapp", "memory", "logs"})
TV_BLOCKED_IDS = MAIN_ONLY_IDS - {"briefing-kira"}  # o briefing vai só com pedido explícito ("mostra na TV")

LIVE_STATES = frozenset({"live", "paused"})
TTL_SECONDS = {
    "viewer": 45.0, "gallery": 45.0, "article": 60.0, "video": 90.0, "info": 45.0, "presentation": 15.0,
    "election": 90.0, "scoreboard": 120.0,
}
DOWNLOAD_DONE_TTL_SECONDS = 15.0
BUSY_RECHECK_SECONDS = 5.0
# Depois de mover um painel de janela, o "fechei" que a janela antiga manda não apaga o registro.
MOVE_CLOSE_GRACE_SECONDS = 3.0
# "Mostra X na janela 2" antes de X abrir: o próximo painel (desse tipo) vai para lá, se abrir dentro deste tempo.
NEXT_WINDOW_SECONDS = 90.0
# Atualizações de apresentação guardadas para reabrir igual em outra janela.
MAX_KEPT_PATCHES = 40
# Palavras do pedido de mover que não dizem qual painel ("a pesquisa SOBRE Marte", "as imagens DESSA janela").
MOVE_FILLERS = frozenset({
    "sobre", "para", "pela", "pelo", "pelas", "pelos", "desse", "dessa", "deste", "desta", "daquele", "daquela", "aquele",
    "aquela", "esse", "essa", "este", "esta", "isso", "isto", "aqui", "agora", "janela", "painel", "paineis", "tela",
    "principal", "outra", "outro", "mais", "mesmo", "mesma", "dele", "dela", "deles", "delas", "todos", "todas", "tudo",
})
# Depois que a Kira para de falar, o painel ainda fica este tempo antes de sair sozinho.
QUIET_AFTER_SPEECH_SECONDS = 20.0
# Briefing, notícias e clima: a Kira terminou de falar, os cartões saem logo (o Chefe reclamou que ficavam na tela). O
# tempo vem da aba Aparência ("8 s", "30 s" ou "até eu fechar" = 0).
PRESENTATION_WAITS = (8, 30, 0)
# "Tá bom, obrigado": o assunto acabou e os painéis dele saem. Vídeo, placar ao vivo, download e imagem criada ficam.
TOPIC_KINDS = frozenset({"presentation", "viewer", "gallery", "article", "info", "election"})
_CLOSING_WORDS = frozenset({
    "obrigado", "obrigada", "brigado", "brigada", "valeu", "beleza", "blz", "ok", "okay", "show", "perfeito", "entendi",
    "certo", "massa", "top", "otimo", "maravilha", "tranquilo", "combinado", "legal",
})
_CLOSING_FILLERS = frozenset({"ta", "bom", "tudo", "bem", "muito", "pode", "fechar", "ja", "vi", "kira", "chefe", "viu", "entao",
                              "e", "isso", "ai", "sim", "demais", "mesmo", "agora", "por", "enquanto", "era", "so"})
SPEECH_POLL_SECONDS = 2.0
VITALS_ID = "vitals"
KIND_ALIASES = {
    "imagem": "viewer", "imagens": "viewer", "foto": "viewer", "fotos": "viewer", "visualizador": "viewer",
    "galeria": "gallery", "artigo": "article", "artigos": "article", "video": "video", "videos": "video",
    "placar": "scoreboard", "jogo": "scoreboard", "jogos": "scoreboard", "eleicao": "election", "eleicoes": "election",
    "sistema": "vitals", "sistema vital": "vitals", "vital": "vitals", "vitais": "vitals", "vitals": "vitals",
    "pesquisa": "info", "info": "info", "download": "download", "estudos": "tutor", "simulado": "tutor", "downloads": "download",
    "criacao": "creation", "criacoes": "creation", "imagem criada": "creation",
}


# "a imagem", "essa foto": vale para as fotos da internet e para a imagem criada pela Kira (o Chefe não separa as duas)
IMAGE_KINDS = frozenset({"viewer", "gallery", "creation"})
# "pode fechar isso / esse painel": o painel mais recente
GENERIC_TARGETS = frozenset({"isso", "isto", "aquilo", "essa", "esse", "esta", "este", "ela", "ele", "painel", "paineis", "janela",
                             "card", "cards", "cartao", "cartoes", "tela", "quadro"})
LEADING_WORDS = ("a ", "o ", "as ", "os ", "essa ", "esse ", "esta ", "este ", "aquela ", "aquele ", "do ", "da ")


PANEL_NAMES = {
    "viewer": "imagens", "gallery": "galeria", "presentation": "painel", "article": "artigo",
    "election": "eleição", "scoreboard": "placar", "vitals": "sistema vital", "creation": "criação",
    "video": "vídeo", "info": "pesquisa", "download": "download", "tutor": "estudos",
}


def _beats_detail(props: dict[str, object]) -> str:
    beats = props.get("beats") if isinstance(props.get("beats"), list) else []
    captions = [str(b.get("caption") or "") for b in beats if isinstance(b, dict) and b.get("caption")]
    return ("imagens: " + "; ".join(captions))[:200] if captions else ""


def fold(value: object) -> str:
    return unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower().strip()


def presentation_wait() -> int:
    """Segundos que os cartões do assunto ficam depois da fala (0 = até o Chefe fechar). Falha de leitura = 8 s."""
    try:
        from services.kira_settings import load

        wait = load().get("panels_after_speech")
    except Exception:
        return 8
    return wait if wait in PRESENTATION_WAITS and not isinstance(wait, bool) else 8


def is_topic_closing(text: object) -> bool:
    """'Tá bom, obrigado', 'valeu, Kira', 'beleza, pode fechar': a fala inteira só encerra o assunto (nada de pedido novo)."""
    words = "".join(ch if ch.isalnum() else " " for ch in fold(text)).split()
    if not words or len(words) > 8 or any(w not in _CLOSING_WORDS and w not in _CLOSING_FILLERS for w in words):
        return False
    joined = " ".join(words)
    return any(w in _CLOSING_WORDS for w in words) or any(pair in joined for pair in ("ta bom", "tudo bem", "pode fechar", "ja vi"))


@dataclass
class PanelRecord:
    id: str
    kind: str
    topic: str = ""
    opened_at: float = 0.0
    updated_at: float = 0.0
    touched: bool = False
    pinned: bool = False
    state: str = ""
    detail: str = ""
    window_id: str = MAIN_WINDOW
    timer: asyncio.Task[object] | None = field(default=None, repr=False)
    # a ação de abrir (com as atualizações somadas): reabre o painel em outra janela quando o Chefe o arrasta para lá
    action: dict[str, object] | None = field(default=None, repr=False)
    # apresentação: as atualizações vão em ordem depois do "present" (cada uma muda só uma parte)
    patches: list[dict[str, object]] = field(default_factory=list, repr=False)
    moved_at: float = 0.0

    def as_dict(self, now: float) -> dict[str, object]:
        return {"id": self.id, "kind": self.kind, "topic": self.topic, "detail": self.detail, "age_s": int(now - self.opened_at), "touched": self.touched,
                "pinned": self.pinned, "window": self.window_id}


class PanelRegistry:
    def __init__(
        self,
        publish: PUBLISH,
        *,
        clock: Callable[[], float] = time.monotonic,
        is_busy: Callable[[], bool] | None = None,
        visible_windows: Callable[[], list[str]] | None = None,
        wait: Callable[[], int] = presentation_wait,
        phone_active: Callable[[], bool] | None = None,
        floating_window: Callable[[], Awaitable[str | None]] | None = None,
        tv_alive: Callable[[], bool] | None = None,
        tv_send: Callable[[dict[str, object]], object] | None = None,
    ) -> None:
        self._publish = publish
        # TV como tela extra (2026-11-06): viva = página da TV dando sinal; envio = SSE do servidor da TV
        self._tv_alive = tv_alive
        self._tv_send = tv_send
        self._tv_sticky_until = 0.0  # "mostra os painéis na TV": os próximos vão todos pra TV até soltar/expirar
        self._recent_closed: PanelRecord | None = None  # clima/briefing somem sozinhos; "joga pra TV" ainda acha
        # Kira minimizada/na bandeja: devolve a janela flutuante (sempre por cima), criando se precisar; None = mostrar na principal
        self._floating_window = floating_window
        self._phone_active = phone_active
        self._wait = wait
        self._is_busy = is_busy
        self._clock = clock
        # Janelas extras que estão à mostra agora (abertas, não minimizadas nem cobertas).
        self._visible_windows = visible_windows
        self._panels: dict[str, PanelRecord] = {}
        # tipo de painel -> janela extra para onde o Chefe o passou nesta sessão (arrastando ou por voz)
        self._kind_window: dict[str, str] = {}
        # pedido para o próximo painel: (janela, tipo ou "" para qualquer um, prazo)
        self._next_window: tuple[str, str, float] | None = None
        self._vitals_visible = False
        self._last_busy_at = 0.0
        self._speech_watch: asyncio.Task[object] | None = None

    # ---------- em qual janela ----------
    def _visible(self) -> list[str]:
        try:
            return [str(w) for w in (self._visible_windows() if self._visible_windows else []) if str(w) != MAIN_WINDOW]
        except Exception:
            return []

    def tv_on(self) -> bool:
        try:
            return bool(self._tv_alive and self._tv_send and self._tv_alive())
        except Exception:
            return False

    def tv_stick(self, seconds: float = 600.0) -> None:
        self._tv_sticky_until = self._clock() + seconds

    def tv_unstick(self) -> None:
        self._tv_sticky_until = 0.0

    def tv_sticky(self) -> bool:
        return self._clock() < self._tv_sticky_until

    def _choose_window(self, panel_id: str, panel_kind: str, requested: object, topic: str = "") -> str:
        """Principal por padrão. Uma janela extra à mostra só quando: o pedido veio com ela; o Chefe pediu "mostra na
        janela 2" (próximo painel); ele já passou esse tipo de painel para lá; ou o assunto continua o de um painel que
        está lá. Antes revezava entre as janelas e quase tudo caía na extra (o Chefe reclamou em 2026-10-25).
        Minimizada, coberta ou fechada não entra; dado pessoal fica na principal."""
        if self._phone_active is not None and requested in (None, "", PHONE_WINDOW) and self._phone_active():
            return PHONE_WINDOW
        visible = self._visible()
        if requested in visible or requested == MAIN_WINDOW:
            return str(requested)
        if requested == TV_WINDOW and self.tv_on():
            return TV_WINDOW
        # pedido explícito do Chefe ("mostra os painéis na TV"): vale até para o briefing (que normalmente fica na principal)
        if self.tv_sticky() and self.tv_on() and panel_kind not in TV_BLOCKED_KINDS and panel_id not in TV_BLOCKED_IDS:
            return TV_WINDOW
        if self.tv_on():
            visible = [*visible, TV_WINDOW]
        if not visible or panel_kind not in ROUTABLE_KINDS or panel_id in MAIN_ONLY_IDS:
            return MAIN_WINDOW
        pending = self._next_window
        if pending is not None:
            window, kind, deadline = pending
            if self._clock() > deadline or window not in visible:
                self._next_window = None
            elif not kind or kind == panel_kind or (kind == "viewer" and panel_kind in IMAGE_KINDS):
                self._next_window = None
                return window
        preferred = self._kind_window.get(panel_kind)
        if preferred in visible:
            return str(preferred)
        folded = fold(topic)
        if folded:
            same = [r.window_id for r in self._panels.values() if r.window_id in visible and fold(r.topic) == folded]
            if same:
                return same[-1]
        # Pedido do Chefe (2026-11-06): com mais de uma tela à mostra (principal, janela extra, TV), sorteia entre elas.
        return random.choice([MAIN_WINDOW, *visible])

    @staticmethod
    def _topic_of(action: dict[str, object]) -> str:
        if action.get("type") == "present":
            return str(action.get("title") or "")
        props = action.get("props") if isinstance(action.get("props"), dict) else {}
        return str(props.get("query") or props.get("topic") or props.get("title") or "")

    @staticmethod
    def _address(action: dict[str, object], window_id: str) -> dict[str, object]:
        addressed = {key: value for key, value in action.items() if key != "windowId"}
        if window_id != MAIN_WINDOW:
            addressed["windowId"] = window_id
        return addressed

    # ---------- publicacao observada ----------
    async def publish(self, action: dict[str, object]) -> object:
        """Escolhe a janela, publica no hub e registra abertura, atualizacao ou fechamento."""
        kind = action.get("type")
        panel_id = str((action.get("presentationId") if kind in {"present", "present_patch"} else action.get("id")) or "")
        record = self._panels.get(panel_id)
        window = record.window_id if record else MAIN_WINDOW
        if kind in {"open_panel", "present"}:
            panel_kind = "presentation" if kind == "present" else str(action.get("kind") or "")
            if record is None:
                window = self._choose_window(panel_id, panel_kind, action.get("windowId"), self._topic_of(action))
                if window == MAIN_WINDOW and not self.tv_on() and self._floating_window is not None and panel_kind in ROUTABLE_KINDS | {"presentation"}:
                    try:
                        floating = await self._floating_window()
                    except Exception:
                        floating = None
                    if floating:
                        window = floating
            if window != TV_WINDOW:
                await self._make_room(panel_id, panel_kind, window)
        if window == TV_WINDOW:
            result = self._send_tv(action)
        else:
            result = await self._emit_to(action, window)
        if kind == "open_panel":
            self._observe_open(action, window)
        elif kind == "present":
            self._observe_present(action, window)
        elif kind in {"update_panel", "present_patch"}:
            self._observe_update(action)
        elif kind == "close_panel":
            self._forget(panel_id)
        return result

    def _observe_present(self, action: dict[str, object], window: str = MAIN_WINDOW) -> None:
        """Apresentações (notícias, clima, briefing...) também ocupam a tela e entram no prazo e na descrição."""
        panel_id = str(action.get("presentationId") or "")
        if not panel_id:
            return
        now = self._clock()
        record = self._panels.get(panel_id) or PanelRecord(panel_id, "presentation", opened_at=now, window_id=window)
        record.updated_at = now
        record.action = {key: value for key, value in action.items() if key != "windowId"}
        record.patches = []
        record.topic = str(action.get("title") or record.topic)[:80]
        sections = action.get("sections") if isinstance(action.get("sections"), list) else []
        headings = [str(section.get("heading") or "") for section in sections if isinstance(section, dict) and section.get("heading")]
        record.detail = ", ".join(headings)[:160]
        self._panels[panel_id] = record
        self._arm(record)

    def _observe_open(self, action: dict[str, object], window: str = MAIN_WINDOW) -> None:
        panel_id, panel_kind = str(action.get("id") or ""), str(action.get("kind") or "")
        if not panel_id or panel_kind in {"reminders", "transcript", "apis"}:
            return
        props = action.get("props") if isinstance(action.get("props"), dict) else {}
        now = self._clock()
        record = self._panels.get(panel_id) or PanelRecord(panel_id, panel_kind, opened_at=now, window_id=window)
        record.kind, record.updated_at = panel_kind, now
        record.topic = str(props.get("query") or props.get("topic") or props.get("title") or record.topic)[:80]
        record.detail = _beats_detail(props) or record.detail
        record.state = str(props.get("state") or "")
        record.action = {key: value for key, value in action.items() if key != "windowId"}
        self._panels[panel_id] = record
        self._arm(record)

    def _observe_update(self, action: dict[str, object]) -> None:
        record = self._panels.get(str(action.get("id") or action.get("presentationId") or ""))
        if record is None:
            return
        props = action.get("props") if isinstance(action.get("props"), dict) else {}
        if action.get("type") == "present_patch":
            record.patches = [*record.patches, {key: value for key, value in action.items() if key != "windowId"}][-MAX_KEPT_PATCHES:]
        elif record.action is not None and props:
            record.action = {**record.action, "props": {**(record.action.get("props") or {}), **props}}  # type: ignore[dict-item]
        record.updated_at = self._clock()
        record.state = str(props.get("state") or record.state)
        record.detail = _beats_detail(props) or record.detail
        self._arm(record)

    def _forget(self, panel_id: str) -> None:
        record = self._panels.pop(panel_id, None)
        if record is not None and record.action and record.kind not in {"transcript", "reminders", "apis"}:
            self._recent_closed = record
        if record and record.timer and not record.timer.done() and record.timer is not asyncio.current_task():
            record.timer.cancel()

    # ---------- tempo limite ----------
    def ttl_for(self, record: PanelRecord) -> float | None:
        if record.kind == "download":
            return DOWNLOAD_DONE_TTL_SECONDS if record.state in {"done", "failed", "cancelled"} else None
        if record.kind == "scoreboard" and record.state in LIVE_STATES:
            return None
        if record.kind == "presentation" and self._wait() == 0:
            return None
        return TTL_SECONDS.get(record.kind)

    async def _watch_speech(self) -> None:
        """Enquanto há painel, anota quando a Kira falou por último (o prazo conta a partir do fim da fala)."""
        while self._panels and self._is_busy is not None:
            if self._is_busy():
                self._last_busy_at = self._clock()
            await asyncio.sleep(SPEECH_POLL_SECONDS)

    def _arm(self, record: PanelRecord) -> None:
        if self._is_busy is not None and (self._speech_watch is None or self._speech_watch.done()):
            self._speech_watch = asyncio.ensure_future(self._watch_speech())
        if record.timer and not record.timer.done():
            record.timer.cancel()
        ttl = self.ttl_for(record)
        if ttl is not None:
            record.timer = asyncio.ensure_future(self._expire(record.id, ttl))

    async def _expire(self, panel_id: str, ttl: float) -> None:
        await asyncio.sleep(ttl)
        record = self._panels.get(panel_id)
        if record is None or record.touched or record.pinned or record.window_id == TV_WINDOW:  # na TV fica até mandarem tirar
            return
        if self._is_busy is not None and self._is_busy():
            self._last_busy_at = self._clock()
            record.timer = asyncio.ensure_future(self._expire(panel_id, BUSY_RECHECK_SECONDS))
            return
        quiet = self._clock() - self._last_busy_at
        needed = float(self._wait()) if record.kind == "presentation" else QUIET_AFTER_SPEECH_SECONDS
        if self._last_busy_at and quiet < needed:
            record.timer = asyncio.ensure_future(self._expire(panel_id, needed - quiet))
            return
        await self.close_ids([panel_id])

    async def end_topic(self) -> int:
        """O Chefe encerrou o assunto: saem os painéis dele que ninguém tocou nem fixou (com "até eu fechar", ficam)."""
        if self._wait() == 0:
            return 0
        ids = [r.id for r in self._panels.values() if r.kind in TOPIC_KINDS and not (r.touched or r.pinned)]
        return await self.close_ids(ids)

    # ---------- capacidade ----------
    async def _emit_to(self, action: dict[str, object], window: str) -> object:
        """Manda para a janela certa: a TV pelo SSE dela, as outras pelo hub."""
        if window == TV_WINDOW:
            return self._send_tv(action)
        return await self._publish(self._address(action, window))

    def _send_tv(self, action: dict[str, object]) -> object:
        clean = {key: value for key, value in action.items() if key != "windowId"}
        try:
            return self._tv_send({"t": "ui", "action": clean}) if self._tv_send else 0
        except Exception:
            return 0

    def last_panel(self) -> PanelRecord | None:
        """Último painel aberto (o "esse painel" de "joga esse painel pra TV")."""
        records = [r for r in self._panels.values() if r.action and r.kind not in TV_BLOCKED_KINDS and r.id not in TV_BLOCKED_IDS]
        recent = self._recent_closed
        return max(records, key=lambda r: r.updated_at, default=None) or (
            recent if recent and recent.kind not in TV_BLOCKED_KINDS and recent.id not in TV_BLOCKED_IDS else None)

    async def move_to_tv(self, panel_id: str = "") -> bool:
        """Leva um painel (o último, sem id) para a TV: fecha onde estava e reabre lá."""
        record = self._panels.get(panel_id) if panel_id else self.last_panel()
        if record is None or not record.action or not self.tv_on():
            return False
        if record.kind in TV_BLOCKED_KINDS or record.id in TV_BLOCKED_IDS:  # dado pessoal nunca vai para a TV
            return False
        if record.window_id != TV_WINDOW and record.id in self._panels:
            try:
                await self._emit_to({"type": "close_panel", "id": record.id}, record.window_id)
            except Exception:
                pass
        self._send_tv(record.action)
        for patch in getattr(record, "patches", []) or []:
            self._send_tv(patch)
        record.window_id = TV_WINDOW
        self._panels.setdefault(record.id, record)
        return True

    async def _make_room(self, panel_id: str, kind: str, window: str = MAIN_WINDOW) -> None:
        """No máximo três na mesma janela (um em destaque e dois em miniatura): sai o mais antigo não tocado."""
        if kind not in AUTO_KINDS or panel_id in self._panels:
            return
        same_window = [r for r in self._panels.values() if r.kind in AUTO_KINDS and r.window_id == window]
        auto = sorted((r for r in same_window if not (r.touched or r.pinned)), key=lambda r: r.opened_at)
        overflow = len(same_window) - (MAX_AUTO_PANELS - 1)
        await self.close_ids([r.id for r in auto[: max(overflow, 0)]])

    # ---------- consulta e fechamento ----------
    def set_vitals_visible(self, visible: bool) -> None:
        self._vitals_visible = bool(visible)

    def window_of(self, panel_id: str) -> str | None:
        record = self._panels.get(panel_id)
        return record.window_id if record else None

    def active(self) -> list[dict[str, object]]:
        now = self._clock()
        items = [r.as_dict(now) for r in sorted(self._panels.values(), key=lambda r: r.opened_at)]
        if self._vitals_visible:
            items.append({"id": VITALS_ID, "kind": "vitals", "topic": "Sistema Vital", "age_s": 0, "touched": False, "pinned": False})
        return items

    def describe(self) -> str:
        items = self.active()
        if not items:
            return "Nenhum painel aberto."
        groups: dict[str, list[str]] = {}
        for item in items:
            text = PANEL_NAMES.get(str(item["kind"]), str(item["kind"]))
            if item["topic"]:
                text += f" «{item['topic']}»"
            if item.get("detail"):
                text += f" ({item['detail']})"
            groups.setdefault(str(item.get("window") or MAIN_WINDOW), []).append(text)
        if list(groups) == [MAIN_WINDOW]:
            return "; ".join(groups[MAIN_WINDOW])
        # o mais recente de cada janela fica em destaque; os anteriores, em miniatura ao lado
        # a Kira precisa saber onde cada painel está: mexer num painel da TV atualiza o da TV (não cria outro no PC)
        name = lambda w: "janela principal (PC)" if w == MAIN_WINDOW else "TV da sala" if w == TV_WINDOW else "janela " + w.lstrip("w")  # noqa: E731
        return " | ".join(f"{name(w)}: {'; '.join(texts)}" for w, texts in groups.items())

    def resolve(self, target: str) -> list[str]:
        # o Chefe diz "clima", o painel se chama "Previsão do tempo"
        wanted = " ".join({"clima": "tempo", "noticia": "noticias"}.get(w, w) for w in fold(target).replace(",", " ").split())
        while wanted.startswith(LEADING_WORDS):
            wanted = wanted.split(" ", 1)[1]
        if wanted in {"", "tudo", "todos", "todas", "all"}:
            return list(self._panels) + ([VITALS_ID] if self._vitals_visible else [])
        if wanted in GENERIC_TARGETS:
            latest = max(self._panels.values(), key=lambda r: max(r.opened_at, r.updated_at), default=None)
            return [latest.id] if latest else []
        ids = self._by_kind(KIND_ALIASES.get(wanted))
        if ids is not None:
            return ids
        by_topic = [r.id for r in self._panels.values() if wanted in fold(r.topic) or wanted == fold(r.id)]
        if by_topic:
            return by_topic
        # "imagem do gato": a palavra que é um tipo decide (e o resto do pedido escolhe pelo assunto); senão, uma palavra do assunto
        words = wanted.split()
        for word in words:
            ids = self._by_kind(KIND_ALIASES.get(word))
            if ids:
                rest = [w for w in words if w != word and len(w) >= 4]
                narrowed = [i for i in ids if i in self._panels and any(w in fold(self._panels[i].topic) for w in rest)]
                return narrowed or ids
        return [r.id for r in self._panels.values() if any(len(word) >= 4 and word in fold(r.topic) for word in words)]

    def _by_kind(self, kind: str | None) -> list[str] | None:
        if not kind:
            return None
        if kind == "vitals":
            return [VITALS_ID] if self._vitals_visible else []
        kinds = IMAGE_KINDS if kind == "viewer" else {kind}
        return [r.id for r in self._panels.values() if r.kind in kinds]

    async def close_ids(self, ids: list[str]) -> int:
        count = 0
        for panel_id in ids:
            if panel_id == VITALS_ID and self._vitals_visible:
                self._vitals_visible = False
                count += 1
                try:
                    await self._publish({"type": "close_panel", "id": VITALS_ID})
                except Exception:
                    continue
            elif panel_id in self._panels:
                window = self._panels[panel_id].window_id
                self._forget(panel_id)
                count += 1
                try:
                    await self._emit_to({"type": "close_panel", "id": panel_id}, window)
                except Exception:
                    continue
        return count

    async def close(self, target: str = "all") -> int:
        ids = self.resolve(target)
        # Kira na TV: "fecha todos os painéis" fecha os da TV; os do PC só se ele disser "do computador/PC" (2026-11-07)
        words = fold(target)
        if self.tv_sticky() and not any(w in words for w in ("computador", "pc", "notebook", "monitor")):
            on_tv = [i for i in ids if (rec := self._panels.get(i)) is not None and rec.window_id == TV_WINDOW]
            ids = on_tv or ids
        return await self.close_ids(ids)

    async def move(self, panel_id: str, window: str) -> bool:
        """O Chefe passou o painel para outra janela da Kira (arrastando ou por voz): fecha onde estava e abre igual na
        outra. Os próximos painéis desse tipo vão para a mesma janela (voltar para a principal desfaz)."""
        record = self._panels.get(str(panel_id))
        if record is None or record.action is None or record.window_id == window or record.id in MAIN_ONLY_IDS:
            return False
        if window == TV_WINDOW and record.kind in TV_BLOCKED_KINDS:
            return False
        old = record.window_id
        record.window_id = window
        record.moved_at = self._clock()
        record.touched = True  # o Chefe mexeu: não sai sozinho pelo prazo
        if window == MAIN_WINDOW:
            self._kind_window.pop(record.kind, None)
        else:
            self._kind_window[record.kind] = window
        await self._emit_to({"type": "close_panel", "id": record.id}, old)
        await self._emit_to(dict(record.action), window)
        for patch in record.patches:
            await self._emit_to(dict(patch), window)
        return True

    def _pick_for_move(self, target: str, window: str, source: str | None) -> tuple[list[str], str]:
        """Painéis que o pedido falado aponta (fora da janela de destino; com "de", só os dessa janela) e o tipo pedido."""
        wanted = " ".join(fold(target).replace(",", " ").split())
        while wanted.startswith(LEADING_WORDS):
            wanted = wanted.split(" ", 1)[1]
        pool = [r for r in self._panels.values() if r.window_id != window and (source is None or r.window_id == source)]
        latest = sorted(pool, key=lambda r: max(r.opened_at, r.updated_at))
        if wanted in {"", "tudo", "todos", "todas"}:
            return [r.id for r in latest], ""
        if wanted in GENERIC_TARGETS or wanted.split(" ")[0] in GENERIC_TARGETS and len(wanted.split()) <= 2:
            return ([latest[-1].id] if latest else []), ""
        words = wanted.split()
        kind = KIND_ALIASES.get(wanted) or next((KIND_ALIASES[w] for w in words if w in KIND_ALIASES), "")
        kinds = IMAGE_KINDS if kind == "viewer" else {kind}
        rest = [w for w in words if KIND_ALIASES.get(w) != kind and len(w) >= 4 and w not in MOVE_FILLERS]
        by_topic = [r.id for r in latest if (not kind or r.kind in kinds) and (wanted in fold(r.topic) or wanted == fold(r.id)
                    or (rest and all(w in fold(r.topic) for w in rest)))]
        if by_topic or rest:
            return by_topic, kind
        return [r.id for r in latest if kind and r.kind in kinds], kind

    async def move_target(self, target: str, window: str, *, source: str | None = None) -> dict[str, object]:
        """'Move esse painel pra janela 2', 'traz da janela 2 pra principal', 'joga as imagens na janela TV'. Sem painel
        aberto que combine, o próximo painel desse tipo abre lá ("mostra o placar na janela 2", antes de o placar abrir)."""
        ids, kind = self._pick_for_move(target, window, source)
        if kind == "vitals":
            return {"moved": [], "vitals": True}  # o Sistema Vital é da principal; nas extras ele é um widget
        personal = [i for i in ids if i in MAIN_ONLY_IDS]
        moved = [i for i in ids if i not in MAIN_ONLY_IDS and await self.move(i, window)]
        if moved:
            names = sorted({PANEL_NAMES.get(self._panels[i].kind, self._panels[i].kind) for i in moved if i in self._panels})
            return {"moved": moved, "names": names}
        if personal and window != MAIN_WINDOW:
            return {"moved": [], "personal": True}
        if window != MAIN_WINDOW and (kind or not ids):
            self._next_window = (window, kind, self._clock() + NEXT_WINDOW_SECONDS)
            return {"moved": [], "next": PANEL_NAMES.get(kind, kind) if kind else "qualquer"}
        return {"moved": []}

    # ---------- estado reportado pela UI ----------
    def report(self, panel_id: str, state: str) -> bool:
        record = self._panels.get(panel_id)
        if state == "closed":
            if record is not None and record.moved_at and self._clock() - record.moved_at < MOVE_CLOSE_GRACE_SECONDS:
                return True  # é o fechar da janela antiga, logo depois de mover: o painel continua (na janela nova)
            self._forget(panel_id)
            return True
        if record is None:
            return False
        if state == "touched":
            record.touched = True
        elif state == "pinned":
            record.pinned = True
        elif state == "unpinned":
            record.pinned = False
        return True

    async def shutdown(self) -> None:
        for record in list(self._panels.values()):
            self._forget(record.id)
        if self._speech_watch is not None and not self._speech_watch.done():
            self._speech_watch.cancel()


__all__ = ["AUTO_KINDS", "MAIN_ONLY_IDS", "MAIN_WINDOW", "MAX_AUTO_PANELS", "PanelRecord", "PanelRegistry", "ROUTABLE_KINDS", "is_topic_closing"]
