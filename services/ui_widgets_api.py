"""Dados externos dos widgets: a UI nunca chama a internet; tudo passa por aqui, com timeout e limite de tamanho."""

from __future__ import annotations

import hashlib
import json
import re
import time
import webbrowser
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from fastapi import APIRouter, Body, HTTPException, Query, Request, Response

from services.image_proxy import proxied_url
from services.interests import Interests, get_interests, iso_to_epoch
from services.live_score import LiveScoreService
from services.providers.news import TOPIC_FEEDS, NewsProvider, parse_feed
from services.local_request import is_local_request
from services.ui_actions_hub import _public_https_host

TIMEOUT = httpx.Timeout(6.0, connect=3.0)
MAX_BYTES = 1_500_000
MAX_UPLOAD_BYTES = 3_000_000
CACHE_SECONDS = {"weather": 600.0, "quotes": 60.0, "history": 3600.0, "feed": 300.0, "scores": 60.0, "games": 900.0, "trending": 1800.0}
GAME_SECTIONS = {"specials": "Ofertas", "coming_soon": "Em breve", "top_sellers": "Mais vendidos", "new_releases": "Novidades"}
STEAM_URL = "https://store.steampowered.com/api/featuredcategories"
EPIC_URL = "https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
TRENDING_URL = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/pt.wikipedia/all-access/{day:%Y/%m/%d}"
HEX_SLUG_RE = re.compile(r"[0-9a-f]{32}")
SKIP_ARTICLE_RE = re.compile(r"^(Wikip[ée]dia|Especial|Categoria|Ajuda|Portal|Ficheiro|Arquivo|Predefini|Discuss)[:_]|^(Página_principal|-)$", re.IGNORECASE)
USER_AGENT = "KiraDesktopAssistant/1.0 (local desktop app; https://commons.wikimedia.org/wiki/Commons:Contact)"
QUOTE_PAIRS = ("USD-BRL", "EUR-BRL", "BTC-BRL")
CITY_CLEAN_RE = re.compile(r"[^\w .'-]")
IMAGE_TYPES = {b"\xff\xd8\xff": ("image/jpeg", ".jpg"), b"\x89PNG": ("image/png", ".png"), b"GIF8": ("image/gif", ".gif"), b"RIFF": ("image/webp", ".webp")}
FILE_NAME_RE = re.compile(r"^[a-f0-9]{16}\.(jpg|png|gif|webp)$")


VOICEBOOK_PATH = Path(__file__).resolve().parents[1] / "configs" / "voice_profiles" / "voicebook.json"


def voice_profiles(path: Path = VOICEBOOK_PATH) -> list[dict[str, Any]]:
    """Quem a Kira reconhece pela voz e quantas amostras tem (só nome e contagem; a voz em si nunca sai do arquivo)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    people = data.get("profiles") if isinstance(data, dict) else None
    return [
        {"name": str(item.get("name") or "")[:40], "samples": len(item.get("embeddings") or [])}
        for item in (people if isinstance(people, list) else [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ][:10]


def sniff_image(data: bytes) -> tuple[str, str] | None:
    for magic, kind in IMAGE_TYPES.items():
        if data.startswith(magic) and (magic != b"RIFF" or data[8:12] == b"WEBP"):
            return kind
    return None


class WidgetData:
    def __init__(
        self,
        *,
        scores: LiveScoreService | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock=time.monotonic,
        interests: Interests | None = None,
        files_dir: Path | None = None,
    ) -> None:
        self._scores = scores or LiveScoreService()
        self._transport = transport
        self._news = NewsProvider(transport=transport)
        self._clock = clock
        self._interests = interests
        self._files_dir = files_dir or Path(__file__).resolve().parents[1] / "configs" / "widget_files"
        self._cache: dict[str, tuple[float, Any]] = {}

    @property
    def interests(self) -> Interests:
        return self._interests or get_interests()

    def _cached(self, key: str, kind: str) -> Any | None:
        hit = self._cache.get(key)
        return hit[1] if hit and self._clock() - hit[0] < CACHE_SECONDS[kind] else None

    def _store(self, key: str, value: Any) -> Any:
        self._cache[key] = (self._clock(), value)
        if len(self._cache) > 80:
            self._cache.pop(next(iter(self._cache)))
        return value

    async def _get(self, url: str, **params: object) -> httpx.Response:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/xml;q=0.9, */*;q=0.5"}, transport=self._transport) as client:
            response = await client.get(url, params=params or None)
        response.raise_for_status()
        if len(response.content) > MAX_BYTES:
            raise ValueError("resposta_grande")
        return response

    async def news(self, feeds: list[str], limit: int = 30, topics: list[str] | None = None) -> dict[str, Any]:
        """Fontes próprias (feeds) valem como um único tópico "personalizado"; senão, um conjunto de feeds por tópico."""
        items = await self._news.topic_items(topics, feeds)
        if not items:
            raise ValueError("sem_noticias")
        per_topic = max(4, -(-limit // len({item["topic"] for item in items})) + 2)
        taken: dict[str, int] = {}
        ranked = []
        for item in self.interests.rank(items, limit=limit * 4):
            if taken.get(item["topic"], 0) < per_topic:
                taken[item["topic"]] = taken.get(item["topic"], 0) + 1
                ranked.append(item)
        ranked = ranked[:limit]
        present = {item["topic"] for item in ranked}
        labels = [{"key": key, "label": TOPIC_FEEDS[key][0]} for key in TOPIC_FEEDS if key in present]
        return {"items": ranked, "topics": labels, "interests": self.interests.top(8)}

    async def games(self, section: str) -> dict[str, Any]:
        """Vitrine da Steam (ofertas, em breve, mais vendidos) sem chave; imagens passam pelo proxy local."""
        if section not in GAME_SECTIONS:
            raise ValueError("secao_invalida")
        showcase = self._cached("games:steam", "games")
        if showcase is None:
            showcase = self._store("games:steam", (await self._get(STEAM_URL, cc="br", l="portuguese")).json())
        rows = []
        for entry in (showcase.get(section) or {}).get("items") or []:
            image = str(entry.get("large_capsule_image") or "")
            price = entry.get("final_price")
            rows.append(
                {
                    "title": str(entry.get("name") or "")[:90],
                    "link": f"https://store.steampowered.com/app/{int(entry['id'])}/",
                    "image": proxied_url(image) if _public_https_host(urlsplit(image)) else "",
                    "discount": int(entry.get("discount_percent") or 0),
                    "price": None if price is None else round(price / 100, 2),
                    "original": round(entry["original_price"] / 100, 2) if entry.get("original_price") else None,
                }
            )
        return {"section": section, "sections": [{"key": k, "label": v} for k, v in GAME_SECTIONS.items()], "items": [r for r in rows if r["title"]][:12]}

    async def free_games(self) -> dict[str, Any]:
        cached = self._cached("games:epic", "games")
        if cached is not None:
            return cached
        data = (await self._get(EPIC_URL, locale="pt-BR", country="BR", allowCountries="BR")).json()
        rows = []
        for entry in (((data.get("data") or {}).get("Catalog") or {}).get("searchStore") or {}).get("elements") or []:
            promotions = entry.get("promotions") or {}
            now_offers = promotions.get("promotionalOffers") or []
            next_offers = promotions.get("upcomingPromotionalOffers") or []
            window = next(iter((now_offers[0] if now_offers else next_offers[0] if next_offers else {}).get("promotionalOffers") or []), None)
            if not window:
                continue
            images = {img.get("type"): img.get("url") for img in entry.get("keyImages") or []}
            image = str(images.get("OfferImageWide") or images.get("Thumbnail") or "")
            slug = str(entry.get("productSlug") or entry.get("urlSlug") or "").split("/")[0]
            rows.append(
                {
                    "title": str(entry.get("title") or "")[:90],
                    "link": f"https://store.epicgames.com/pt-BR/p/{quote(slug)}" if slug and not HEX_SLUG_RE.fullmatch(slug) else "https://store.epicgames.com/pt-BR/free-games",
                    "image": proxied_url(image) if _public_https_host(urlsplit(image)) else "",
                    "now": bool(now_offers),
                    "start": iso_to_epoch(str(window.get("startDate") or "")),
                    "end": iso_to_epoch(str(window.get("endDate") or "")),
                }
            )
        if not rows:
            raise ValueError("sem_jogos")
        rows.sort(key=lambda r: (not r["now"], r["start"] or 0))
        return self._store("games:epic", {"items": rows})

    async def trending(self) -> dict[str, Any]:
        """Artigos mais lidos da Wikipédia em português (ontem), sem páginas internas."""
        cached = self._cached("trending", "trending")
        if cached is not None:
            return cached
        rows: list[dict[str, Any]] = []
        for back in (1, 2):
            day = date.fromordinal(date.today().toordinal() - back)
            try:
                data = (await self._get(TRENDING_URL.format(day=day))).json()
            except Exception:
                continue
            articles = ((data.get("items") or [{}])[0]).get("articles") or []
            rows = [
                {"title": a["article"].replace("_", " "), "views": int(a.get("views") or 0), "link": "https://pt.wikipedia.org/wiki/" + quote(a["article"], safe="_():,'")}
                for a in articles
                if isinstance(a.get("article"), str) and not SKIP_ARTICLE_RE.search(a["article"])
            ][:10]
            if rows:
                break
        if not rows:
            raise ValueError("sem_tendencias")
        return self._store("trending", {"items": rows})

    async def weather(self, city: str) -> dict[str, Any]:
        key = f"weather:{city.lower()}"
        cached = self._cached(key, "weather")
        if cached is not None:
            return cached
        geo = (await self._get("https://geocoding-api.open-meteo.com/v1/search", name=city, count=1, language="pt")).json()
        place = (geo.get("results") or [None])[0]
        if not place:
            raise ValueError("cidade_nao_encontrada")
        data = (
            await self._get(
                "https://api.open-meteo.com/v1/forecast", latitude=place["latitude"], longitude=place["longitude"], timezone="auto",
                current="temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,apparent_temperature",
                daily="temperature_2m_max,temperature_2m_min,weather_code,sunrise,sunset", forecast_days=6,
            )
        ).json()
        return self._store(key, {"city": place.get("name", city), "current": data.get("current"), "daily": data.get("daily")})

    async def quotes(self) -> dict[str, Any]:
        cached = self._cached("quotes", "quotes")
        if cached is not None:
            return cached
        data = (await self._get("https://economia.awesomeapi.com.br/last/" + ",".join(QUOTE_PAIRS))).json()
        rows = [{"pair": v["code"], "bid": v["bid"], "pct": v.get("pctChange")} for v in data.values() if isinstance(v, dict) and "bid" in v]
        return self._store("quotes", {"items": rows})

    async def history(self) -> dict[str, Any]:
        today = date.today()
        cached = self._cached(f"history:{today}", "history")
        if cached is not None:
            return cached
        events: list[dict[str, Any]] = []
        for lang in ("pt", "en"):
            try:
                data = (await self._get(f"https://{lang}.wikipedia.org/api/rest_v1/feed/onthisday/events/{today.month:02d}/{today.day:02d}")).json()
            except Exception:
                continue
            events = [{"year": e.get("year"), "text": str(e.get("text") or "")[:240]} for e in (data.get("events") or [])[:10]]
            if events:
                break
        if not events:
            raise ValueError("sem_eventos")
        return self._store(f"history:{today}", {"items": events})

    async def scores(self) -> dict[str, Any]:
        cached = self._cached("scores", "scores")
        if cached is not None:
            return cached
        matches = await self._scores.today_matches()
        return self._store("scores", {"items": [m for m in matches if isinstance(m, dict)][:12]})

    # ---------- arquivos do widget de imagem ----------
    def save_file(self, data: bytes) -> str:
        kind = sniff_image(data)
        if kind is None or len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("imagem_invalida")
        name = f"{hashlib.sha256(data).hexdigest()[:16]}{kind[1]}"
        self._files_dir.mkdir(parents=True, exist_ok=True)
        (self._files_dir / name).write_bytes(data)
        return name

    def read_file(self, name: str) -> tuple[bytes, str]:
        if not FILE_NAME_RE.fullmatch(name):
            raise ValueError("nome_invalido")
        data = (self._files_dir / name).read_bytes()
        kind = sniff_image(data)
        if kind is None:
            raise ValueError("imagem_invalida")
        return data, kind[0]


def create_ui_widgets_router(data: WidgetData | None = None) -> APIRouter:
    router = APIRouter()
    source = data or WidgetData()

    def local_only(request: Request) -> None:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")

    async def serve(request: Request, call) -> dict[str, Any]:
        local_only(request)
        try:
            return {"ok": True, **await call()}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception:
            raise HTTPException(status_code=502, detail="indisponivel")

    @router.get("/api/ui/widgets/news")
    async def news(
        request: Request,
        feeds: str = Query(default="", max_length=1500),
        topics: str = Query(default="", max_length=200),
        limit: int = Query(default=30, ge=1, le=60),
    ) -> dict[str, Any]:
        urls = [u.strip() for u in feeds.split(",") if u.strip()]
        chosen = [t.strip().lower() for t in topics.split(",") if t.strip()]
        return await serve(request, lambda: source.news(urls, limit, chosen))

    @router.get("/api/ui/widgets/games")
    async def games(request: Request, section: str = Query(default="specials", max_length=20)) -> dict[str, Any]:
        return await serve(request, lambda: source.games(section))

    @router.get("/api/ui/widgets/free-games")
    async def free_games(request: Request) -> dict[str, Any]:
        return await serve(request, source.free_games)

    @router.get("/api/ui/widgets/trending")
    async def trending(request: Request) -> dict[str, Any]:
        return await serve(request, source.trending)

    @router.get("/api/ui/widgets/weather")
    async def weather(request: Request, city: str = Query(default="São Paulo", max_length=80)) -> dict[str, Any]:
        return await serve(request, lambda: source.weather(CITY_CLEAN_RE.sub("", city).strip() or "São Paulo"))

    @router.get("/api/ui/widgets/quotes")
    async def quotes(request: Request) -> dict[str, Any]:
        return await serve(request, source.quotes)

    @router.get("/api/ui/widgets/history")
    async def history(request: Request) -> dict[str, Any]:
        return await serve(request, source.history)

    @router.get("/api/ui/widgets/scores")
    async def scores(request: Request) -> dict[str, Any]:
        return await serve(request, source.scores)

    @router.get("/api/ui/voice-profile")
    async def voice_profile(request: Request) -> dict[str, Any]:
        local_only(request)
        return {"ok": True, "profiles": voice_profiles()}

    @router.get("/api/ui/interests")
    async def get_topics(request: Request) -> dict[str, Any]:
        local_only(request)
        return {"ok": True, "topics": source.interests.top(30)}

    @router.put("/api/ui/interests")
    async def put_topics(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        local_only(request)
        terms = payload.get("terms") if isinstance(payload, dict) else None
        if not isinstance(terms, list) or len(terms) > 60:
            raise HTTPException(status_code=422, detail="termos invalidos")
        source.interests.replace([str(t)[:40] for t in terms])
        return {"ok": True, "topics": source.interests.top(30)}

    @router.delete("/api/ui/interests")
    async def clear_topics(request: Request) -> dict[str, Any]:
        local_only(request)
        source.interests.clear()
        return {"ok": True, "topics": []}

    @router.post("/api/ui/widgets/upload")
    async def upload(request: Request) -> dict[str, Any]:
        local_only(request)
        body = await request.body()
        try:
            return {"ok": True, "file": source.save_file(body)}
        except ValueError:
            raise HTTPException(status_code=422, detail="imagem invalida ou maior que 3 MB")

    @router.get("/api/ui/widgets/file/{name}")
    async def file(name: str, request: Request) -> Response:
        local_only(request)
        try:
            content, media_type = source.read_file(name)
        except (ValueError, OSError):
            raise HTTPException(status_code=404, detail="arquivo nao encontrado")
        return Response(content=content, media_type=media_type, headers={"X-Content-Type-Options": "nosniff"})

    @router.post("/api/ui/open-external")
    async def open_external(request: Request, payload: Any = Body(default=None)) -> dict[str, Any]:
        local_only(request)
        url = str(payload.get("url") or "") if isinstance(payload, dict) else ""
        if len(url) > 2000 or _public_https_host(urlsplit(url)) is None:
            raise HTTPException(status_code=422, detail="url invalida")
        return {"ok": bool(webbrowser.open(url, new=2))}

    return router


__all__ = ["WidgetData", "create_ui_widgets_router", "parse_feed", "sniff_image"]
