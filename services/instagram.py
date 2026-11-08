"""Instagram do Chefe pela sessão do navegador da Kira (o login que ele fez uma vez na aba Internet).

Lê as mesmas rotas que o site instagram.com usa (perfil, seguidores, seguindo, Direct, atividade), sem senha e sem
login automático (o Instagram recusa). Guarda uma foto dos seguidores em memory/local/instagram.json para dizer quem
entrou e quem saiu desde a última olhada. Só leitura: não segue, não curte, não manda mensagem.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger("kira.app.instagram")

BASE = "https://www.instagram.com"
APP_HEADERS = {"X-IG-App-ID": "936619743392459", "X-ASBD-ID": "129477"}
LIST_LIMIT = 3000          # seguidores/seguindo lidos por vez (contas maiores: a comparação fica parcial)
PAGE_PAUSE_S = 0.9         # entre páginas: devagar, como uma pessoa rolando a lista
RELATIONS_TTL_S = 15 * 60  # listas guardadas em memória por 15 min (perguntas seguidas não relêem tudo)

Getter = Callable[[str, dict[str, str]], Awaitable[tuple[int, Any]]]


class NotLoggedIn(Exception):
    """Sem login do Instagram no navegador da Kira."""


class Instagram:
    def __init__(self, get: Getter, user_id: Callable[[], Awaitable[str]], state_path: Path, *,
                 clock: Callable[[], float] = time.time, pause: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
        self._get = get
        self._user_id = user_id
        self._state_path = Path(state_path)
        self._clock = clock
        self._pause = pause
        self._relations: tuple[float, dict[str, list[dict[str, Any]]]] | None = None
        self._lock = asyncio.Lock()
        self._cooldown_until = 0.0  # 429: espera progressiva (não insistir piora o bloqueio)
        self._cooldown_s = 0.0

    # ---------- leitura ----------
    async def _json(self, path: str) -> dict[str, Any]:
        wait = self._cooldown_until - self._clock()
        if wait > 0:
            raise RuntimeError(f"o Instagram limitou as consultas; dá para tentar de novo em uns {max(1, round(wait / 60))} min")
        status, body = await self._get(BASE + path, dict(APP_HEADERS))
        if status in (0, 301, 302, 401, 403) or (isinstance(body, dict) and (body.get("require_login") or body.get("message") == "login_required")):
            raise NotLoggedIn()
        if status == 429:
            self._cooldown_s = min(3600.0, max(300.0, self._cooldown_s * 2))
            self._cooldown_until = self._clock() + self._cooldown_s
            raise RuntimeError(f"o Instagram limitou as consultas (muitas seguidas); tento de novo em uns {round(self._cooldown_s / 60)} min")
        if status == 200:
            self._cooldown_s = 0.0
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError(f"resposta {status} do Instagram")
        return body

    async def _uid(self) -> str:
        uid = str(await self._user_id() or "").strip()
        if not uid.isdigit():
            raise NotLoggedIn()
        return uid

    async def me(self) -> dict[str, Any]:
        uid = await self._uid()
        user = (await self._json(f"/api/v1/users/{uid}/info/")).get("user") or {}
        return {
            "id": uid, "username": str(user.get("username") or ""), "name": str(user.get("full_name") or ""),
            "followers": int(user.get("follower_count") or 0), "following": int(user.get("following_count") or 0),
            "posts": int(user.get("media_count") or 0), "verified": bool(user.get("is_verified")),
            "pic": str(user.get("profile_pic_url") or ""),
        }

    async def _list(self, uid: str, kind: str) -> list[dict[str, Any]]:
        users: list[dict[str, Any]] = []
        max_id = ""
        while len(users) < LIST_LIMIT:
            query = f"?count=100{'&max_id=' + max_id if max_id else ''}"
            page = await self._json(f"/api/v1/friendships/{uid}/{kind}/{query}")
            users.extend(_user(item) for item in page.get("users") or [] if isinstance(item, dict))
            max_id = str(page.get("next_max_id") or "")
            if not max_id:
                break
            await self._pause(PAGE_PAUSE_S)
        return users

    async def relations(self, *, fresh: bool = False) -> dict[str, list[dict[str, Any]]]:
        async with self._lock:
            if not fresh and self._relations and self._clock() - self._relations[0] < RELATIONS_TTL_S:
                return self._relations[1]
            uid = await self._uid()
            data = {"followers": await self._list(uid, "followers"), "following": await self._list(uid, "following")}
            self._relations = (self._clock(), data)
            return data

    async def inbox(self) -> list[dict[str, Any]]:
        uid = await self._uid()
        body = await self._json("/api/v1/direct_v2/inbox/?persistentBadging=true&limit=20&thread_message_limit=1")
        threads = []
        for thread in (body.get("inbox") or {}).get("threads") or []:
            item = thread.get("last_permanent_item") or {}
            when = _ts(item.get("timestamp"))
            seen = _ts(((thread.get("last_seen_at") or {}).get(uid) or {}).get("timestamp"))
            mine = str(item.get("user_id") or "") == uid
            people = [str(user.get("full_name") or user.get("username") or "") for user in thread.get("users") or []]
            threads.append({
                "name": str(thread.get("thread_title") or ", ".join(p for p in people if p) or "conversa"),
                "text": _item_text(item, mine), "when": when, "unread": bool(not mine and when and when > seen),
            })
        return threads

    async def activity(self) -> list[dict[str, Any]]:
        body = await self._json("/api/v1/news/inbox/")
        events = []
        for story in [*(body.get("new_stories") or []), *(body.get("old_stories") or [])]:
            args = story.get("args") or {}
            text = str(args.get("text") or args.get("rich_text") or "").strip()
            if text:
                events.append({"text": text[:200], "when": _ts(args.get("timestamp")), "kind": _activity_kind(text)})
        return events

    async def search(self, name: str) -> list[dict[str, Any]]:
        from urllib.parse import quote

        body = await self._json(f"/web/search/topsearch/?context=user&query={quote(name)}")
        return [_user(item.get("user") or {}) for item in body.get("users") or [] if isinstance(item, dict)]

    # ---------- respostas prontas ----------
    async def novidades(self) -> dict[str, Any]:
        """O que mudou: seguidores que entraram/saíram desde a última olhada, mensagens novas e atividade de hoje."""
        me = await self.me()
        # lista recente (TTL) basta: refazer seguidores+seguindo a cada pergunta gerava o 429
        relations = await self.relations()
        followers = {user["id"]: user for user in relations["followers"]}
        state = self._load()
        before = state.get("followers") or {}
        first = not before
        new = [] if first else [user for uid, user in followers.items() if uid not in before]
        lost = [] if first else [{"id": uid, "username": name, "name": "", "verified": False, "pic": ""} for uid, name in before.items() if uid not in followers]
        self._save({"at": self._clock(), "followers": {uid: user["username"] for uid, user in followers.items()}, "count": me["followers"]})
        today = _today_start(self._clock())
        threads, events = [], []
        try:
            threads = await self.inbox()
        except RuntimeError as exc:
            logger.info("INSTAGRAM_INBOX_FAILED reason=%s", exc)
        try:
            events = [event for event in await self.activity() if event["when"] >= today]
        except RuntimeError as exc:
            logger.info("INSTAGRAM_ACTIVITY_FAILED reason=%s", exc)
        counts = {kind: sum(1 for event in events if event["kind"] == kind) for kind in ("curtida", "comentario", "seguiu", "reel")}
        return {
            "me": me, "primeira_vez": first, "desde": state.get("at"), "novos": new, "perdidos": lost,
            "parcial": me["followers"] > LIST_LIMIT,
            "mensagens_hoje": [thread for thread in threads if thread["when"] >= today],
            "nao_lidas": [thread for thread in threads if thread["unread"]],
            "atividade_hoje": counts, "eventos_hoje": events[:12],
        }

    async def nao_seguem_de_volta(self) -> list[dict[str, Any]]:
        relations = await self.relations()
        followers = {user["id"] for user in relations["followers"]}
        return [user for user in relations["following"] if user["id"] not in followers]

    async def verificados(self) -> dict[str, list[dict[str, Any]]]:
        relations = await self.relations()
        return {"seguindo": [u for u in relations["following"] if u["verified"]], "seguidores": [u for u in relations["followers"] if u["verified"]]}

    async def buscar(self, name: str) -> dict[str, Any]:
        """Quantas pessoas têm esse nome: na busca do Instagram e entre quem você segue / quem te segue."""
        wanted = _norm(name)
        if not wanted:
            raise ValueError("diga o nome")
        found = await self.search(name)
        relations = await self.relations()
        match = lambda user: wanted in _norm(user["name"]) or wanted in _norm(user["username"])  # noqa: E731
        return {"busca": found[:20], "seguindo": [u for u in relations["following"] if match(u)], "seguidores": [u for u in relations["followers"] if match(u)]}

    # ---------- foto dos seguidores ----------
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._state_path)


def _user(raw: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(raw.get("pk") or raw.get("pk_id") or raw.get("id") or ""), "username": str(raw.get("username") or ""),
            "name": str(raw.get("full_name") or ""), "verified": bool(raw.get("is_verified")), "pic": str(raw.get("profile_pic_url") or "")}


def _ts(value: object) -> float:
    """Instagram manda segundos (atividade) ou microssegundos (Direct)."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return number / 1_000_000 if number > 1e12 else number


def _item_text(item: dict[str, Any], mine: bool) -> str:
    kind = str(item.get("item_type") or "")
    text = str(item.get("text") or "").strip() or {
        "media_share": "mandou um post", "clip": "mandou um reel", "reel_share": "respondeu um story", "story_share": "mandou um story",
        "voice_media": "mandou um áudio", "media": "mandou uma foto", "like": "curtiu", "link": "mandou um link", "raven_media": "mandou uma foto",
    }.get(kind, "mandou algo")
    return f"Você: {text}"[:160] if mine else text[:160]


def _activity_kind(text: str) -> str:
    low = text.lower()
    if "reel" in low:
        return "reel"
    if "curtiu" in low or "liked" in low:
        return "curtida"
    if "coment" in low or "comment" in low or "mencion" in low:
        return "comentario"
    if "seguir" in low or "following" in low or "follow" in low:
        return "seguiu"
    return "outro"


def _today_start(now: float) -> float:
    day = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return day.timestamp()


def _norm(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", str(text or "").lower()) if unicodedata.category(ch) != "Mn").strip()


__all__ = ["Instagram", "NotLoggedIn", "LIST_LIMIT"]
