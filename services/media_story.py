"""Narração visual: um único visualizador que passa uma imagem por momento da explicação da Kira."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from typing import Awaitable, Callable
from urllib.parse import unquote

from services.media_search import MediaSearchService, _slug
from services.media_topics import clean_topic, fold

logger = logging.getLogger("kira.app")
# Objetos que dividem nome com o momento mas não o mostram (avião "Pero Vaz de Caminha" da TAP, moeda com Cabral).
OFF_TOPIC_RE = re.compile(
    r"\b(moeda|moedas|coin|coins|centavo|centavos|cent|cedula|banknote|nota de|selo|stamp|postage|airbus|boeing|"
    r"embraer|aircraft|airplane|airlines|airways|a3[0-9]0|b7[0-9]7|tap air|logo|logotipo|icon|emblem|brasao|"
    r"coat of arms|bandeira de|flag of|mapa rodoviario|qr code|screenshot)\b"
)
RECALL_IGNORE = frozenset("imagem imagens foto fotos mostra mostrar mostre novo nova outra vez aquela aquele essa esse sobre teve tinha".split())
VIEWER_ID = "viewer-kira"
SHOWN_TTL_SECONDS = 1800.0
SHOWN_MAX = 60
POOL_SIZE = 8
MIN_BEATS = 1
MAX_BEATS = 6
CAPTION_MAX = 90
SEARCH_CONCURRENCY = 6
# O plano chega com a Kira já falando: a imagem do 1º momento vai para a tela assim que sai (não espera as outras), e
# o plano inteiro tem um limite; momento mais lento que isso fica com uma imagem do assunto.
PLAN_FIRST_SECONDS = 4.0
PLAN_TOTAL_SECONDS = 8.0
GATE = Callable[[], Awaitable[object]]
NO_TOPIC = "Não identifiquei um assunto para pesquisar; nada foi aberto."
PREFETCH = Callable[[str], Awaitable[object]]
STOPWORDS = frozenset(
    "a o as os de da do das dos e em no na nos nas um uma uns umas que com por para pra sobre como foi era sao ser the of and in on to "
    "with from for at by its his her their this that isso esse essa este esta ele ela eles elas seu sua seus suas mais muito".split()
)


def _image_key(item: dict[str, object]) -> str:
    return str(item.get("imageUrl") or "")


def _short(text: object, limit: int = CAPTION_MAX) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    cut = value[: limit - 1].rsplit(" ", 1)[0]
    return f"{cut}…"


def keywords(*texts: object) -> list[str]:
    """Palavras que identificam um momento na fala (sem acento, sem palavras vazias)."""
    seen: list[str] = []
    for word in fold(" ".join(str(t or "") for t in texts)).split():
        if len(word) > 3 and word not in STOPWORDS and word not in seen:
            seen.append(word)
    return seen[:12]


def _mentions(item: dict[str, object], words: list[str], need: int) -> bool:
    """A imagem cita pelo menos `need` dessas palavras no título/descrição/endereço (sem palavras: aceita)."""
    if not words or need <= 0:
        return True
    text = fold(f"{item.get('title', '')} {item.get('description', '')} {unquote(str(item.get('imageUrl', '')))}")
    return sum(1 for w in set(words) if w[:6] in text) >= need


def _off_topic(item: dict[str, object]) -> bool:
    return bool(OFF_TOPIC_RE.search(fold(f"{item.get('title', '')} {item.get('description', '')}")))


def _rank_for_beat(found: list[dict[str, object]], label: str, topic_words: set[str]) -> list[dict[str, object]]:
    """Sem objetos fora do assunto; primeiro as imagens cujo título tem as palavras do próprio momento."""
    own = [w for w in keywords(label) if w not in topic_words] or keywords(label)
    usable = [item for item in found if not _off_topic(item)]
    return sorted(usable, key=lambda item: -sum(1 for w in own if w in fold(str(item.get("title", "")))))


def _raw_url(item: dict[str, object]) -> str:
    return unquote(_image_key(item).split("u=", 1)[-1])


class MediaStory:
    def __init__(
        self,
        media: MediaSearchService,
        registry,
        *,
        prefetch: PREFETCH | None = None,
        gate: GATE | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._media = media
        self._registry = registry
        self._prefetch = prefetch
        self._gate = gate
        self._clock = clock
        self._topic = ""
        self._beats: list[dict[str, object]] = []
        self._plan_id = 0
        self._shown: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._articles_opened: set[str] = set()
        self._tasks: set[asyncio.Task[object]] = set()

    # ---------- memoria ----------
    def _prune(self) -> None:
        now = self._clock()
        for key in [k for k, v in self._shown.items() if now - float(v["at"]) > SHOWN_TTL_SECONDS]:  # type: ignore[arg-type]
            self._shown.pop(key, None)
        while len(self._shown) > SHOWN_MAX:
            self._shown.popitem(last=False)

    def _remember(self, beat: dict[str, object]) -> None:
        item = beat["item"]  # type: ignore[assignment]
        self._shown[_image_key(item)] = {"item": dict(item), "caption": beat["caption"], "query": self._topic, "at": self._clock()}  # type: ignore[arg-type]
        self._shown.move_to_end(_image_key(item))  # type: ignore[arg-type]
        self._prune()

    # ---------- exibicao ----------
    def _viewer_open(self) -> bool:
        return any(p["id"] == VIEWER_ID for p in self._registry.active())

    async def _publish(self) -> None:
        beat = self._beats[0]
        item = beat["item"]  # type: ignore[assignment]
        props = {
            "query": self._topic, "title": self._topic, "activeBeat": beat["id"],
            "beats": [
                {"id": b["id"], "imageUrl": b["item"]["imageUrl"], "caption": b["caption"], "keywords": b["keywords"],  # type: ignore[index]
                 "source": b["item"].get("source", ""), "license": b["item"].get("license", "")}  # type: ignore[union-attr]
                for b in self._beats
            ],
            "imageUrl": item["imageUrl"], "caption": beat["caption"],  # type: ignore[index]
            "source": item.get("source", ""), "license": item.get("license", ""),  # type: ignore[union-attr]
        }
        if self._viewer_open():
            await self._registry.publish({"type": "update_panel", "id": VIEWER_ID, "props": props})
        else:
            await self._registry.publish({"type": "open_panel", "kind": "viewer", "id": VIEWER_ID, "title": "Imagens", "props": props, "focus": True})
        # Todas as imagens do plano: as seguintes trocam só na tela e o "mostra de novo a da missa" precisa achá-las.
        for planned in self._beats:
            self._remember(planned)

    def _warm(self) -> None:
        if self._prefetch is None:
            return
        warmed = self.__dict__.setdefault("_warmed", set())
        if len(warmed) > 200:
            warmed.clear()
        for beat in self._beats:
            url = _raw_url(beat["item"])  # type: ignore[arg-type]
            if url in warmed:
                continue  # o 1º momento já foi aquecido na entrega antecipada
            warmed.add(url)
            task = asyncio.ensure_future(self._safe_prefetch(url))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _safe_prefetch(self, url: str) -> None:
        try:
            await self._prefetch(url)  # type: ignore[misc]
        except Exception:
            return

    async def _open(self, plan_id: int) -> None:
        asked_at = time.monotonic()
        if self._gate is not None:
            await self._gate()
        if time.monotonic() - asked_at > 40.0:
            # esperou a narração tempo demais: abrir agora seria imagem "do nada" fora do assunto (2026-11-05 12:02)
            logger.info("MEDIA_STORY_OPEN_SKIPPED reason=stale waited_s=%.0f", time.monotonic() - asked_at)
            return
        if plan_id == self._plan_id and self._beats:
            await self._publish()
        else:
            logger.info("MEDIA_STORY_OPEN_SKIPPED reason=%s", "superseded" if plan_id != self._plan_id else "no_beats")

    # ---------- API ----------
    async def start(self, topic: str, *, with_article: bool = False) -> dict[str, object]:
        """Sem plano da Kira: imagens do tema, navegação manual; artigo só se for pedido."""
        cleaned = clean_topic(topic)
        if not cleaned:
            return {"ok": False, "summary": NO_TOPIC}
        if cleaned == self._topic and self._viewer_open():
            return {"ok": True, "summary": f"As imagens de {cleaned} já estão na tela."}
        kinds = ["images", "articles"] if with_article else ["images"]
        ticket = self._plan_id
        found = await self._media.search_topic(cleaned, kinds=kinds, language="pt", reserve_slot=True)
        if ticket != self._plan_id:
            # A Kira declarou os momentos (plan_visuals) enquanto esta busca rodava: o plano dela manda.
            logger.info("MEDIA_STORY_START_SKIPPED reason=plan_requested topic=%r", cleaned[:80])
            return {"ok": True, "summary": "As imagens vão acompanhar a narração."}
        if found.get("rate_limited"):
            return {"ok": False, "summary": "Muitas pesquisas seguidas; aguarde um instante."}
        article = (found.get("articles") or [None])[0]
        if article and with_article and cleaned not in self._articles_opened:
            self._articles_opened.add(cleaned)
            await self._registry.publish(
                {"type": "open_panel", "kind": "article", "id": f"article-{_slug(cleaned)}", "title": "Leitura relacionada", "props": article, "focus": False}
            )
        pool = [i for i in found.get("images") or [] if isinstance(i, dict) and _image_key(i)][:POOL_SIZE]
        if not pool:
            return {"ok": bool(article), "summary": f"Não encontrei imagens de {cleaned}." + (" Abri um artigo." if article else "")}
        self._plan_id += 1
        self._topic = cleaned
        self._beats = [{"id": f"b{i + 1}", "item": item, "caption": _short(topic), "keywords": []} for i, item in enumerate(pool)]
        self._warm()
        await self._open(self._plan_id)
        return {"ok": True, "summary": f"Mostrando imagens de {cleaned}. Para narrar, use plan_visuals com os momentos."}

    async def plan(self, topic: str, beats: list[dict[str, object]]) -> dict[str, object]:
        """Uma imagem boa por momento declarado, na ordem da narração; abre depois da primeira frase."""
        cleaned = clean_topic(topic)
        wanted = [b for b in beats if isinstance(b, dict) and clean_topic(str(b.get("query") or b.get("beat") or ""))][:MAX_BEATS]
        if not cleaned or len(wanted) < MIN_BEATS:
            logger.info("MEDIA_STORY_PLAN_REJECTED topic=%r beats=%s", topic[:80], len(beats))
            return {"ok": False, "summary": NO_TOPIC}
        self._plan_id += 1
        plan_id = self._plan_id
        limit = asyncio.Semaphore(SEARCH_CONCURRENCY)

        async def search(query: str) -> list[dict[str, object]]:
            async with limit:
                try:
                    return await self._media.specific_images(query[:160])
                except Exception:
                    return []

        async def candidates(beat: dict[str, object]) -> list[dict[str, object]]:
            """Imagem do próprio momento ("Primeira missa no Brasil"); só sem resultado tenta junto com o assunto.
            O Commons exige todos os termos: "descoberta do Brasil primeira missa" voltava vazio e todo momento caía
            nas imagens genéricas do assunto."""
            label = str(beat.get("beat") or "")
            query = str(beat.get("query") or label)
            found = await search(query)
            if found:
                return found
            # O Commons busca quase só em inglês: o artigo da Wikipédia em português do momento acha as fotos.
            async with limit:
                try:
                    article = await self._media.search_topic(query[:160], kinds=["images"], language="pt", reserve_slot=False)
                except Exception:
                    article = {}
            images = [i for i in (article.get("images") or []) if isinstance(i, dict)]
            return images or await search(f"{label} {topic}")

        tasks = [asyncio.ensure_future(candidates(b)) for b in wanted]
        pool_task = asyncio.ensure_future(search(str(topic)))
        topic_words = set(keywords(topic))
        started = asyncio.get_running_loop().time()

        def build(results: list[list[dict[str, object]]], topic_pool: list[dict[str, object]]) -> list[dict[str, object]]:
            used: set[str] = set()
            planned: list[dict[str, object]] = []
            for beat, found in zip(wanted, results):
                label = str(beat.get("beat") or "")
                item = next((c for c in _rank_for_beat(found, label, topic_words) if _image_key(c) and _image_key(c) not in used), None)
                # Sem imagem do momento: uma imagem do assunto, com legenda do assunto (nunca afirmar que mostra o momento).
                caption = _short(beat.get("caption") or label or cleaned) if item is not None else _short(topic)
                if item is None:
                    item = next((c for c in topic_pool if _image_key(c) and _image_key(c) not in used and not _off_topic(c)
                                 and _mentions(c, sorted(topic_words), min(2, len(topic_words)))), None)
                if item is None:
                    continue
                used.add(_image_key(item))
                # Palavras do assunto ("brasil") aparecem em todos os momentos e adiantariam a troca de imagem.
                own = [w for w in keywords(label, beat.get("caption")) if w not in topic_words] or keywords(label, beat.get("caption"))
                planned.append({"id": f"b{len(planned) + 1}", "item": item, "caption": caption, "keywords": own})
            return planned

        # 1º momento na tela assim que sair.
        await asyncio.wait({tasks[0]}, timeout=PLAN_FIRST_SECONDS)
        if plan_id != self._plan_id:
            logger.info("MEDIA_STORY_PLAN_SUPERSEDED topic=%r", cleaned[:80])
            return {"ok": False, "summary": "Plano substituído por um mais novo."}
        if tasks[0].done() and (first := build([tasks[0].result()], [])):
            self._topic = cleaned
            self._beats = first
            self._warm()
            await self._open(plan_id)
            logger.info("MEDIA_STORY_FIRST_BEAT_SHOWN seconds=%.1f", asyncio.get_running_loop().time() - started)
        remaining = max(0.5, PLAN_TOTAL_SECONDS - (asyncio.get_running_loop().time() - started))
        await asyncio.wait(set(tasks) | {pool_task}, timeout=remaining)
        if plan_id != self._plan_id:
            logger.info("MEDIA_STORY_PLAN_SUPERSEDED topic=%r", cleaned[:80])
            return {"ok": False, "summary": "Plano substituído por um mais novo."}
        for task in [*tasks, pool_task]:
            if not task.done():
                task.cancel()
        results = [task.result() if task.done() and not task.cancelled() else [] for task in tasks]
        topic_pool = pool_task.result() if pool_task.done() and not pool_task.cancelled() else []
        planned = build(results, topic_pool)
        logger.info(
            "MEDIA_STORY_PLAN topic=%r beats=%s planned=%s own_images=%s",
            cleaned[:80], len(wanted), len(planned), sum(1 for found in results if found),
        )
        if not planned:
            return {"ok": False, "summary": f"Não encontrei imagens boas para {cleaned}."}
        self._topic = cleaned
        self._beats = planned
        self._warm()
        await self._open(plan_id)
        return {"ok": True, "summary": "ok", "beats": len(planned)}

    async def recall(self, description: str) -> dict[str, object]:
        """Reabre uma imagem ja mostrada, sem nova busca."""
        self._prune()
        wanted = {w for w in keywords(description) if w not in RECALL_IGNORE}
        topic_words = set(keywords(self._topic))
        specific = (wanted - topic_words) or wanted
        best, score = None, 0
        for entry in reversed(self._shown.values()):
            haystack = set(keywords(entry["caption"], entry["item"].get("title", "")))  # type: ignore[union-attr]
            overlap = len(specific & haystack) * 2 + len(wanted & set(keywords(entry["query"])))
            if overlap > score:
                best, score = entry, overlap
        if best is None:
            ordered = list(self._shown.values())
            best = ordered[0] if ordered and not wanted else None
        if best is None:
            return {"ok": False, "summary": "Não encontrei essa imagem entre as que já mostrei."}
        self._plan_id += 1
        self._topic = str(best["query"])
        self._beats = [{"id": "b1", "item": best["item"], "caption": str(best["caption"]), "keywords": []}]
        await self._publish()
        return {"ok": True, "summary": f"Reabri: {best['caption'] or 'imagem anterior'}."}

    @property
    def shown_count(self) -> int:
        return len(self._shown)


__all__ = ["MediaStory", "VIEWER_ID", "keywords"]
