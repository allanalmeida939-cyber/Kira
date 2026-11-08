"""Pesquisa assíncrona e normalização segura de mídia para os painéis da Kira."""

from __future__ import annotations

import asyncio
import logging
import copy
import os
import re
import time
import unicodedata
from collections import OrderedDict, deque
from typing import Awaitable, Callable, Mapping
from urllib.parse import quote

import httpx

from services.image_proxy import proxied_url
from services.providers.video import VideoProvider, VideoUnavailable
from services.shared_http import ensure_tls
from services.ui_actions_hub import ALLOWED_URL_HOSTS
from services.ui_media_settings import UiMediaSettings


HTTP_TIMEOUT_SECONDS = 6.0
CACHE_MAX_ITEMS = 200
CACHE_TTL_SECONDS = 600.0
MAX_SEARCHES_PER_MINUTE = 6
MAX_IMAGES = 12
MAX_VIDEOS = 4
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
MIN_IMAGE_WIDTH = 800
UNWANTED_TITLE_RE = re.compile(r"(map|mapa|diagram|diagrama|collage|colagem|montage|chart|graph|locator|flag|bandeira|coat of arms|brasao|logo|icon|svg)", re.IGNORECASE)


def _wanted_image(title: object, width: object) -> bool:
    """Descarta colagens, mapas, diagramas e imagens pequenas demais para o visualizador."""
    if UNWANTED_TITLE_RE.search(str(title or "")):
        return False
    return not isinstance(width, (int, float)) or width >= MIN_IMAGE_WIDTH
HTML_TAG_RE = re.compile(r"<[^>]+>")
KIND_LABELS = {"images": "imagens", "articles": "artigos", "videos": "vídeos"}
VALID_KINDS = frozenset(KIND_LABELS)
PUBLISHER = Callable[[dict[str, object]], Awaitable[object]]


def _flag(value: object, default: bool = False) -> bool:
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off", "nao", "não"}


def _text(value: object, limit: int = 500) -> str:
    cleaned = HTML_TAG_RE.sub(" ", str(value or ""))
    return " ".join(cleaned.split())[:limit].strip()


def _safe_language(value: object) -> str:
    candidate = str(value or "pt").strip().lower().replace("_", "-")
    return "en" if candidate.startswith("en") else "pt"


def _safe_query(value: object, limit: int = 180) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def _host_allowed(url: object) -> bool:
    try:
        parsed = httpx.URL(str(url))
        return parsed.scheme == "https" and str(parsed.host or "").lower() in ALLOWED_URL_HOSTS
    except Exception:
        return False


def _safe_url(value: object) -> str:
    candidate = str(value or "").strip()
    return candidate if _host_allowed(candidate) else ""


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    result = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return (result or "midia")[:48]


def media_panel_ids(topic: str) -> tuple[str, str, str]:
    slug = _slug(topic)
    return (f"gallery-{slug}", f"video-{slug}", f"article-{slug}")


logger = logging.getLogger("kira.app")


def _log_background_failure(task: asyncio.Task[object]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.warning("MEDIA_BACKGROUND_FAILED error=%s: %s", type(task.exception()).__name__, str(task.exception())[:200])


class MediaSearchService:
    """Orquestra fontes de pesquisa sem bloquear o loop do backend."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        environment: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        settings: UiMediaSettings | None = None,
    ) -> None:
        self._transport = transport
        self._environment = dict(environment) if environment is not None else None
        self._clock = clock
        self._settings = settings
        self._request_semaphore = asyncio.Semaphore(3)
        self._cache: OrderedDict[tuple[object, ...], tuple[float, dict[str, object]]] = OrderedDict()
        self._search_starts: deque[float] = deque()
        self._rate_lock = asyncio.Lock()
        self._active_task: asyncio.Task[object] | None = None
        self._background: set[asyncio.Future[object]] = set()

    def _env_value(self, key: str) -> str:
        source = self._environment if self._environment is not None else os.environ
        return str(source.get(key) or "")

    @property
    def user_agent(self) -> str:
        contact = _text(self._env_value("KIRA_HTTP_CONTACT"), 180) or "https://github.com/kira-local/kira"
        return f"KiraAssistant/8.0 ({contact}) python-httpx"

    @property
    def safe_mode(self) -> bool:
        return _flag(self._env_value("KIRA_MEDIA_SAFE_MODE"), True)

    @property
    def enabled(self) -> bool:
        if self._settings is not None:
            return self._settings.enabled
        return _flag(self._env_value("KIRA_MEDIA_ENABLED"), True)

    @property
    def youtube_key_available(self) -> bool:
        return bool(self._env_value("YOUTUBE_API_KEY").strip())

    @property
    def openverse_enabled(self) -> bool:
        return _flag(self._env_value("KIRA_MEDIA_OPENVERSE"), False)

    def _cache_get(self, key: tuple[object, ...]) -> dict[str, object] | None:
        item = self._cache.get(key)
        if item is None:
            return None
        created_at, value = item
        if self._clock() - created_at >= CACHE_TTL_SECONDS:
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return copy.deepcopy(value)

    def _cache_put(self, key: tuple[object, ...], value: dict[str, object]) -> None:
        self._cache[key] = (self._clock(), copy.deepcopy(value))
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_MAX_ITEMS:
            self._cache.popitem(last=False)

    async def _reserve_search(self) -> bool:
        async with self._rate_lock:
            now = self._clock()
            while self._search_starts and now - self._search_starts[0] >= 60.0:
                self._search_starts.popleft()
            if len(self._search_starts) >= MAX_SEARCHES_PER_MINUTE:
                return False
            self._search_starts.append(now)
            return True

    async def _json_get(self, client: httpx.AsyncClient, url: str, params: dict[str, object]) -> dict[str, object]:
        async with self._request_semaphore:
            response = await client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("resposta_json_invalida")
        return payload

    async def _commons_pages(self, client: httpx.AsyncClient, params: dict[str, object]) -> list[dict[str, object]]:
        payload = await self._json_get(
            client,
            "https://commons.wikimedia.org/w/api.php",
            {
                "action": "query",
                "format": "json",
                "prop": "imageinfo",
                "iiprop": "url|extmetadata|mime|size",
                "iiurlwidth": 1280,
                **params,
            },
        )
        pages = ((payload.get("query") or {}).get("pages") or {})
        if not isinstance(pages, dict):
            return []
        result: list[dict[str, object]] = []
        for page in sorted((p for p in pages.values() if isinstance(p, dict)), key=lambda p: int(p.get("index") or 0)):
            info = (page.get("imageinfo") or [{}])[0]
            if not isinstance(info, dict) or str(info.get("mime") or "") not in IMAGE_MIMES:
                continue
            if not _wanted_image(page.get("title"), info.get("width")):
                continue
            image_url = _safe_url(info.get("thumburl") or info.get("url"))
            metadata = info.get("extmetadata") or {}
            if not image_url or not isinstance(metadata, dict):
                continue
            license_name = _text(
                ((metadata.get("LicenseShortName") or {}).get("value") if isinstance(metadata.get("LicenseShortName"), dict) else "")
                or ((metadata.get("UsageTerms") or {}).get("value") if isinstance(metadata.get("UsageTerms"), dict) else "")
            )
            source_url = f"https://commons.wikimedia.org/wiki/{quote(str(page.get('title') or '').replace(' ', '_'))}"
            if not license_name or not _host_allowed(source_url):
                continue
            description = metadata.get("ImageDescription")
            caption = _text(description.get("value") if isinstance(description, dict) else "", 140)
            result.append(
                {
                    "title": _text(str(page.get("title") or "").removeprefix("File:"), 160) or "Imagem do Commons",
                    "caption": caption,
                    "imageUrl": proxied_url(image_url),
                    "source": "Wikimedia Commons",
                    "sourceUrl": source_url,
                    "license": license_name,
                }
            )
        return result

    async def _wikipedia_title(self, client: httpx.AsyncClient, query: str, language: str) -> tuple[str, str]:
        """Resolve a consulta livre para (título, idioma) do artigo mais relevante."""
        for lang in dict.fromkeys((language, "en" if language == "pt" else "pt")):
            payload = await self._json_get(
                client,
                f"https://{lang}.wikipedia.org/w/api.php",
                {"action": "query", "format": "json", "list": "search", "srsearch": query, "srlimit": 1, "srprop": ""},
            )
            hits = (payload.get("query") or {}).get("search")
            if isinstance(hits, list) and hits and isinstance(hits[0], dict) and hits[0].get("title"):
                return str(hits[0]["title"]), lang
        return "", language

    async def _article_image_titles(self, client: httpx.AsyncClient, title: str, language: str) -> list[str]:
        payload = await self._json_get(
            client,
            f"https://{language}.wikipedia.org/api/rest_v1/page/media-list/{quote(title.replace(' ', '_'), safe='')}",
            {},
        )
        items = payload.get("items")
        names: list[str] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or item.get("type") != "image" or item.get("showInGallery") is False:
                continue
            name = str(item.get("title") or "")
            if name.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                names.append(f"File:{name.rpartition(':')[2]}")
        return names[:MAX_IMAGES * 2]

    async def _commons_images(self, client: httpx.AsyncClient, query: str, language: str = "pt") -> list[dict[str, object]]:
        title, wiki_lang = await self._wikipedia_title(client, query, language)
        jobs = [
            self._commons_pages(
                client,
                {"generator": "search", "gsrsearch": title or query, "gsrnamespace": 6, "gsrlimit": MAX_IMAGES},
            )
        ]
        if title:
            names = await self._article_image_titles(client, title, wiki_lang)
            if names:
                jobs.insert(0, self._commons_pages(client, {"titles": "|".join(names)}))
        batches = await asyncio.gather(*jobs, return_exceptions=True)
        if all(isinstance(batch, Exception) for batch in batches):
            raise batches[0]  # type: ignore[misc]
        merged: list[dict[str, object]] = []
        seen: set[object] = set()
        for batch in batches:
            for image in batch if isinstance(batch, list) else []:
                if image["imageUrl"] not in seen:
                    seen.add(image["imageUrl"])
                    merged.append(image)
        return merged[:MAX_IMAGES]

    async def specific_images(self, query: str) -> list[dict[str, object]]:
        """Busca direta no Commons pela consulta exata (ex.: 'Pripyat before 1986'), sem resolver artigo."""
        verify = await ensure_tls()
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS), headers=headers, transport=self._transport, verify=verify) as client:
            return await self._commons_pages(client, {"generator": "search", "gsrsearch": query, "gsrnamespace": 6, "gsrlimit": MAX_IMAGES})

    async def _openverse_images(self, client: httpx.AsyncClient, query: str) -> list[dict[str, object]]:
        payload = await self._json_get(
            client,
            "https://api.openverse.org/v1/images/",
            {"q": query, "page_size": MAX_IMAGES, "mature": "false"},
        )
        results = payload.get("results")
        if not isinstance(results, list):
            return []
        normalized: list[dict[str, object]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            if item.get("filetype") == "svg" or not _wanted_image(item.get("title"), item.get("width")):
                continue
            image_url = _safe_url(item.get("url") or item.get("thumbnail"))
            source_url = _safe_url(item.get("foreign_landing_url") or item.get("url"))
            license_name = _text(item.get("license") or item.get("license_version"), 120)
            if not image_url or not source_url or not license_name:
                continue
            normalized.append(
                {
                    "title": _text(item.get("title"), 160) or "Imagem Openverse",
                    "imageUrl": proxied_url(image_url),
                    "source": "Openverse",
                    "sourceUrl": source_url,
                    "license": license_name,
                }
            )
            if len(normalized) >= MAX_IMAGES:
                break
        return normalized

    async def _wikipedia_article(self, client: httpx.AsyncClient, query: str, language: str) -> list[dict[str, object]]:
        found_title, language = await self._wikipedia_title(client, query, language)
        if not found_title:
            return []
        article_url = f"https://{language}.wikipedia.org/api/rest_v1/page/summary/{quote(found_title.replace(' ', '_'), safe='')}"
        payload = await self._json_get(client, article_url, {})
        content_urls = payload.get("content_urls") or {}
        desktop = content_urls.get("desktop") if isinstance(content_urls, dict) else {}
        source_url = _safe_url(desktop.get("page") if isinstance(desktop, dict) else "")
        if not source_url:
            source_url = f"https://{language}.wikipedia.org/wiki/{quote(found_title.replace(' ', '_'), safe='')}"
        thumbnail = payload.get("thumbnail") or {}
        thumbnail_url = _safe_url(thumbnail.get("source") if isinstance(thumbnail, dict) else "")
        title = _text(payload.get("title"), 160)
        excerpt = _text(payload.get("extract"), 1400)
        if not title or not excerpt or not source_url:
            return []
        article: dict[str, object] = {
            "title": title,
            "excerpt": excerpt,
            "source": "Wikipedia",
            "sourceUrl": source_url,
            "url": source_url,
            "license": "CC BY-SA 4.0",
        }
        if thumbnail_url:
            article["thumbnail"] = proxied_url(thumbnail_url)
        return [article]

    async def _youtube_videos(self, client: httpx.AsyncClient, query: str) -> list[dict[str, object]]:
        """Mesma busca do VideoProvider (uma implementação só do YouTube), no formato dos painéis de mídia."""
        del client
        provider = VideoProvider(transport=self._transport, key_provider=lambda: self._env_value("YOUTUBE_API_KEY"))
        try:
            videos = await provider.search(query, limit=MAX_VIDEOS)
        except VideoUnavailable:
            return []
        return [
            {
                "videoId": v.videoId, "title": v.title or "Vídeo", "description": v.description, "source": "YouTube", "license": "YouTube",
                "thumbnail": v.thumbnailUrl, "sourceUrl": v.url, "url": v.url, "embeddable": v.embeddable,
            }
            for v in videos
        ]

    async def _search_kind(self, query: str, kind: str, language: str) -> tuple[list[dict[str, object]], list[str]]:
        failures: list[str] = []
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        timeout = httpx.Timeout(HTTP_TIMEOUT_SECONDS)
        verify = await ensure_tls()
        async with httpx.AsyncClient(timeout=timeout, headers=headers, transport=self._transport, verify=verify) as client:
            if kind == "images":
                jobs = [self._commons_images(client, query, language)]
                if self.openverse_enabled:
                    jobs.append(self._openverse_images(client, query))
            elif kind == "articles":
                jobs = [self._wikipedia_article(client, query, language)]
            else:
                if not self.youtube_key_available:
                    return [], []
                jobs = [self._youtube_videos(client, query)]
            responses = await asyncio.gather(*jobs, return_exceptions=True)
        normalized: list[dict[str, object]] = []
        for response in responses:
            if isinstance(response, Exception):
                failures.append(kind)
                continue
            normalized.extend(response)
        if kind == "images":
            return normalized[:MAX_IMAGES], failures
        if kind == "videos":
            return normalized[:MAX_VIDEOS], failures
        return normalized[:1], failures

    async def search_topic(
        self,
        topic: str,
        *,
        kinds: list[str] | tuple[str, ...] | None = None,
        image_query: str | None = None,
        language: str = "pt",
        reserve_slot: bool = True,
    ) -> dict[str, object]:
        query = _safe_query(image_query or topic)
        normalized_kinds = tuple(kind for kind in (kinds or ["images", "articles", "videos"]) if kind in VALID_KINDS)
        result: dict[str, object] = {"topic": _safe_query(topic), "images": [], "articles": [], "videos": [], "failures": []}
        if not query or not normalized_kinds:
            return result
        cache_key = (query, _safe_language(language), normalized_kinds, self.safe_mode, self.openverse_enabled, self.youtube_key_available)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        if reserve_slot and not await self._reserve_search():
            result["rate_limited"] = True
            return result
        for kind in normalized_kinds:
            values, failures = await self._search_kind(query, kind, _safe_language(language))
            result[kind] = values
            if failures:
                result["failures"] = [*result["failures"], *failures]
        if "videos" in normalized_kinds and not self.youtube_key_available:
            result["video_unavailable"] = True
        self._cache_put(cache_key, result)
        return copy.deepcopy(result)

    async def _publish(self, publish: PUBLISHER, action: dict[str, object]) -> None:
        try:
            await publish(action)
        except Exception:
            return

    async def run_media_job(
        self,
        topic: str,
        *,
        kinds: list[str] | tuple[str, ...] | None = None,
        image_query: str | None = None,
        language: str = "pt",
        publish: PUBLISHER,
    ) -> dict[str, object]:
        normalized_topic = _safe_query(topic)
        requested_kinds = [kind for kind in (kinds or ["images", "articles", "videos"]) if kind in VALID_KINDS]
        if not normalized_topic or not requested_kinds:
            return {"ok": False, "reason": "consulta_invalida"}
        if not self.enabled:
            await self._publish(
                publish,
                {"type": "status", "scope": "media", "state": "idle", "text": "Mídia desativada pelo usuário."},
            )
            return {"ok": False, "disabled": True}

        if not await self._reserve_search():
            await self._publish(
                publish,
                {"type": "status", "scope": "media", "state": "idle", "text": "A pesquisa de mídia atingiu o limite temporário."},
            )
            return {"ok": False, "rate_limited": True}

        steps = [{"label": KIND_LABELS[kind].capitalize(), "done": False} for kind in requested_kinds]
        collected: dict[str, list[dict[str, object]]] = {kind: [] for kind in requested_kinds}
        failures: list[str] = []
        video_unavailable = False
        for index, kind in enumerate(requested_kinds):
            label = KIND_LABELS[kind]
            await self._publish(
                publish,
                {"type": "status", "scope": "media", "state": "searching", "text": f"{('Procurando' if kind == 'images' else 'Lendo' if kind == 'articles' else 'Buscando')} {label}…", "steps": copy.deepcopy(steps)},
            )
            result = await self.search_topic(
                normalized_topic,
                kinds=[kind],
                image_query=image_query if kind == "images" else None,
                language=language,
                reserve_slot=False,
            )
            collected[kind] = list(result.get(kind) or [])
            failures.extend(list(result.get("failures") or []))
            video_unavailable = video_unavailable or bool(result.get("video_unavailable"))
            steps[index]["done"] = True

        gallery_id, video_id, article_id = media_panel_ids(normalized_topic)
        if collected.get("images"):
            await self._publish(
                publish,
                {"type": "open_panel", "kind": "gallery", "id": gallery_id, "title": "Imagens encontradas", "props": {"query": normalized_topic, "items": collected["images"]}, "focus": True},
            )
        if collected.get("videos"):
            await self._publish(
                publish,
                {"type": "open_panel", "kind": "video", "id": video_id, "title": "Vídeos encontrados", "props": {"query": normalized_topic, "items": collected["videos"]}, "focus": False},
            )
        if collected.get("articles"):
            article = collected["articles"][0]
            await self._publish(
                publish,
                {"type": "open_panel", "kind": "article", "id": article_id, "title": "Leitura relacionada", "props": article, "focus": False},
            )

        found = sum(len(items) for items in collected.values())
        message = f"Encontrei {found} resultado(s) de mídia para {normalized_topic}." if found else f"Não encontrei mídias seguras para {normalized_topic}."
        if not found and video_unavailable:
            message = "Não encontrei mídias seguras; vídeos ficam indisponíveis sem a chave do YouTube."
        await self._publish(
            publish,
            {"type": "status", "scope": "media", "state": "idle", "text": message, "steps": steps},
        )
        return {"ok": bool(found), "topic": normalized_topic, "results": collected, "failures": failures, "video_unavailable": video_unavailable}

    def start_background(self, coroutine: Awaitable[object]) -> None:
        task = asyncio.ensure_future(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(_log_background_failure)

    def start_media_job(self, topic: str, *, publish: PUBLISHER, **kwargs: object) -> asyncio.Task[object]:
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()
        task = asyncio.create_task(self.run_media_job(topic, publish=publish, **kwargs), name="kira-media-search")
        self._active_task = task
        return task

    async def shutdown(self) -> None:
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()
            await asyncio.gather(self._active_task, return_exceptions=True)
        self._active_task = None


_SERVICE: MediaSearchService | None = None


def get_media_search_service(settings: UiMediaSettings | None = None) -> MediaSearchService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = MediaSearchService(settings=settings)
    return _SERVICE


__all__ = [
    "CACHE_MAX_ITEMS",
    "CACHE_TTL_SECONDS",
    "HTTP_TIMEOUT_SECONDS",
    "MAX_IMAGES",
    "MAX_SEARCHES_PER_MINUTE",
    "MAX_VIDEOS",
    "MediaSearchService",
    "get_media_search_service",
    "media_panel_ids",
]
