"""Notícias por RSS: feeds por tema e busca por assunto (Google News RSS, sem chave).

Tudo que vem dos feeds é dado não confiável: texto limitado e sem HTML, links só https públicos, imagens só pelo proxy.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from services.image_proxy import ProxyError, _system_resolver, assert_public_https, proxied_url
from services.interests import iso_to_epoch
from services.ui_actions_hub import _public_https_host

TIMEOUT = httpx.Timeout(6.0, connect=3.0)
MAX_BYTES = 1_500_000
CACHE_SECONDS = 600.0
CACHE_MAX = 60
MAX_FEEDS = 6
USER_AGENT = "KiraDesktopAssistant/1.0 (local desktop app; https://commons.wikimedia.org/wiki/Commons:Contact)"
SEARCH_URL = "https://news.google.com/rss/search"
PAGE_IMAGE_BYTES = 64_000  # a og:image fica nos primeiros ~10 KB (g1, ge medidos)
PAGE_IMAGE_TIMEOUT = httpx.Timeout(3.0, connect=2.0)
META_IMAGE_RE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](?:og:image(?::secure_url)?|twitter:image)[\"'][^>]*>", re.IGNORECASE
)
GOOGLE_NEWS_HOST = "news.google.com"
GOOGLE_DECODE_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
SIGNATURE_RE = re.compile(r'data-n-a-sg="([^"]+)"')
TIMESTAMP_RE = re.compile(r'data-n-a-ts="([0-9]+)"')
CONTENT_RE = re.compile(r"content=[\"']([^\"']+)[\"']", re.IGNORECASE)
TOPIC_FEEDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "geral": ("Geral", ("https://g1.globo.com/rss/g1/", "https://feeds.bbci.co.uk/portuguese/rss.xml")),
    "tecnologia": ("Tecnologia", ("https://tecnoblog.net/feed/", "https://canaltech.com.br/rss/", "https://olhardigital.com.br/feed/")),
    "games": ("Games", ("https://pt.ign.com/feed.xml", "https://www.gamespot.com/feeds/news/")),
    "esportes": ("Esportes", ("https://ge.globo.com/rss/ge/", "https://ge.globo.com/rss/ge/futebol/times/fortaleza/")),
    "ciencia": ("Ciência", ("https://g1.globo.com/rss/g1/ciencia-e-saude/",)),
    "economia": ("Economia", ("https://g1.globo.com/rss/g1/economia/",)),
}
DEFAULT_TOPICS = tuple(TOPIC_FEEDS)
IMG_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _epoch(text: str) -> float | None:
    if not text:
        return None
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError):
        return iso_to_epoch(text.strip())


def plain_text(value: object, limit: int = 300) -> str:
    """HTML -> texto curto, sem marcação nem caracteres de controle."""
    text = CONTROL_RE.sub(" ", html.unescape(TAG_RE.sub(" ", str(value or ""))))
    return " ".join(text.split())[:limit]


def parse_feed(data: bytes, base: str = "", *, limit: int = 15) -> dict[str, Any]:
    """RSS/Atom -> fonte e itens com título, resumo, veículo (por item quando o feed informa), imagem e horário."""
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError("xml_inseguro")
    root = ET.fromstring(data)
    feed_title = next((c.text for c in root.iter() if _local(c.tag) == "title" and c.text), "")
    source = str(feed_title).strip()[:60] or urlsplit(base).hostname or "Notícias"
    items: list[dict[str, Any]] = []
    for node in root.iter():
        if _local(node.tag) not in {"item", "entry"}:
            continue
        title = link = published = image = summary = publisher = ""
        for child in node:
            name = _local(child.tag)
            if name == "title":
                title = (child.text or "").strip()
            elif name == "link":
                link = (child.text or child.attrib.get("href") or "").strip() or link
            elif name in {"pubDate", "published", "updated", "date"} and not published:
                published = (child.text or "").strip()
            elif name == "source" and child.text:
                publisher = child.text.strip()
            elif name in {"content", "thumbnail"} and child.attrib.get("url") and not image:
                image = child.attrib["url"]
            elif name == "enclosure" and str(child.attrib.get("type", "")).startswith("image") and not image:
                image = child.attrib.get("url", "")
            elif name in {"description", "summary", "encoded"}:
                if not image:
                    match = IMG_RE.search(child.text or "")
                    image = match.group(1) if match else ""
                if not summary:
                    summary = plain_text(child.text)
        if not title or _public_https_host(urlsplit(link)) is None:
            continue
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3]
        safe_image = image if _public_https_host(urlsplit(image)) else ""
        items.append({
            "title": plain_text(title, 200), "link": link, "image": proxied_url(safe_image) if safe_image else "",
            "source": plain_text(publisher, 60) or source, "summary": summary if summary != plain_text(title, 300) else "", "ts": _epoch(published),
        })
        if len(items) >= limit:
            break
    return {"source": source, "items": items}


def article_id(link: str, title: str) -> str:
    return "n" + hashlib.sha1(f"{link}|{title}".encode("utf-8")).hexdigest()[:12]


class NewsProvider:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, clock=time.monotonic) -> None:
        self._transport = transport
        self._clock = clock
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._limit = asyncio.Semaphore(4)
        self._http: httpx.AsyncClient | None = None
        self._http_loop: asyncio.AbstractEventLoop | None = None
        self._resolved: dict[str, str] = {}
        self._page_images: dict[str, str] = {}

    def _client(self) -> httpx.AsyncClient:
        # Conexão reaproveitada entre feeds: cliente novo por pedido custava DNS + TLS a cada fonte.
        loop = asyncio.get_running_loop()
        if self._http is None or self._http.is_closed or self._http_loop is not loop:
            self._http = httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT, "Accept": "application/xml;q=0.9, */*;q=0.5"}, transport=self._transport)
            self._http_loop = loop
        return self._http

    async def _fetch(self, url: str, *, limit: int = 15, **params: object) -> dict[str, Any]:
        key = f"{url}?{sorted(params.items())}"
        hit = self._cache.get(key)
        if hit and self._clock() - hit[0] < CACHE_SECONDS:
            return hit[1]
        async with self._limit:
            response = await self._client().get(url, params=params or None)
        response.raise_for_status()
        if len(response.content) > MAX_BYTES:
            raise ValueError("resposta_grande")
        parsed = parse_feed(response.content, url, limit=limit)
        self._cache[key] = (self._clock(), parsed)
        while len(self._cache) > CACHE_MAX:
            self._cache.pop(next(iter(self._cache)))
        return parsed

    async def topic_items(self, topics: list[str] | None = None, feeds: list[str] | None = None) -> list[dict[str, Any]]:
        """Itens dos feeds por tema (ou das fontes próprias do usuário, como tema "personalizado")."""
        if feeds:
            plan = [("personalizado", u) for u in dict.fromkeys(feeds) if _public_https_host(urlsplit(u))][:MAX_FEEDS]
        else:
            wanted = [t for t in dict.fromkeys(topics or DEFAULT_TOPICS) if t in TOPIC_FEEDS] or list(DEFAULT_TOPICS)
            plan = [(t, u) for t in wanted for u in TOPIC_FEEDS[t][1]]
        if not plan:
            raise ValueError("feed_invalido")
        results = await asyncio.gather(*(self._fetch(u) for _, u in plan), return_exceptions=True)
        now = time.time()
        return [
            {**item, "topic": topic, "ts": item["ts"] or now}
            for (topic, _), result in zip(plan, results)
            if isinstance(result, dict)
            for item in result["items"]
        ]

    async def resolve_link(self, link: str) -> str:
        """Link do Google News -> endereço real da matéria (o RSS só traz o redirecionamento codificado)."""
        parts = urlsplit(link)
        if parts.hostname != GOOGLE_NEWS_HOST or "/articles/" not in parts.path:
            return link
        if link in self._resolved:
            return self._resolved[link]
        code = parts.path.rsplit("/articles/", 1)[1]
        try:
            page = await self._client().get(f"https://{GOOGLE_NEWS_HOST}/rss/articles/{code}", timeout=PAGE_IMAGE_TIMEOUT, follow_redirects=True)
            signature, stamp = SIGNATURE_RE.search(page.text), TIMESTAMP_RE.search(page.text)
            if not (signature and stamp):
                return ""
            request = json.dumps([
                "garturlreq",
                [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                code, int(stamp.group(1)), signature.group(1),
            ])
            answer = await self._client().post(
                GOOGLE_DECODE_URL, content="f.req=" + quote(json.dumps([[["Fbv4je", request, None, "generic"]]])),
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}, timeout=PAGE_IMAGE_TIMEOUT,
            )
            real = str(json.loads(json.loads(answer.text.split("\n\n", 1)[1])[0][2])[1])
        except (httpx.HTTPError, ValueError, IndexError, TypeError, KeyError):
            return ""
        real = real if _public_https_host(urlsplit(real)) else ""
        self._resolved[link] = real
        while len(self._resolved) > 200:
            self._resolved.pop(next(iter(self._resolved)))
        return real

    async def page_image(self, url: str) -> str:
        """Imagem de capa que o próprio veículo declara (og:image) na página da matéria; "" se não houver."""
        if url in self._page_images:
            return self._page_images[url]
        image = await self._read_page_image(url)
        self._page_images[url] = image
        while len(self._page_images) > 300:
            self._page_images.pop(next(iter(self._page_images)))
        return image

    async def _read_page_image(self, url: str) -> str:
        url = await self.resolve_link(url)
        chunks: list[bytes] = []
        try:
            for _ in range(4):  # redirecionamentos seguidos à mão: cada salto passa pela guarda de SSRF
                if not _public_https_host(urlsplit(url)):
                    return ""
                await assert_public_https(url, _system_resolver)
                async with self._client().stream("GET", url, timeout=PAGE_IMAGE_TIMEOUT, follow_redirects=False) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    if response.status_code != 200 or "html" not in response.headers.get("content-type", ""):
                        return ""
                    size = 0
                    async for chunk in response.aiter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= PAGE_IMAGE_BYTES or b"</head>" in chunk:
                            break
                    break
        except (ProxyError, httpx.HTTPError, asyncio.TimeoutError):
            return ""
        head = b"".join(chunks).decode("utf-8", "replace")
        for tag in META_IMAGE_RE.findall(head):
            match = CONTENT_RE.search(tag)
            image = html.unescape(match.group(1)).strip() if match else ""
            if _public_https_host(urlsplit(image)):
                return proxied_url(image)
        return ""

    async def search(self, query: str, *, days: int = 7, limit: int = 40) -> list[dict[str, Any]]:
        """Busca por assunto no Google News (pt-BR); o link leva à matéria original."""
        text = " ".join(str(query or "").split())[:120]
        if len(text) < 2:
            raise ValueError("assunto_vazio")
        parsed = await self._fetch(SEARCH_URL, limit=limit, q=f"{text} when:{max(1, min(int(days), 30))}d", hl="pt-BR", gl="BR", ceid="BR:pt-419")
        return [{**item, "topic": "busca"} for item in parsed["items"]]


__all__ = ["DEFAULT_TOPICS", "NewsProvider", "TOPIC_FEEDS", "article_id", "parse_feed", "plain_text"]
