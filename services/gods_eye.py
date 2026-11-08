"""Olho de Deus: globo com terremotos (USGS), aviões (adsb.lol) e satélites (CelesTrak), todos sem chave.

O backend busca e normaliza os dados (só números e textos curtos chegam à tela) com cache curto, e repassa os
comandos de voz da Kira para o globo da interface (window.KiraGodsEye).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import os
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Callable

import httpx
from fastapi import APIRouter, Body, Header, HTTPException, Request

from services.local_request import is_local_request

logger = logging.getLogger("kira.app.gods_eye")

USGS_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"
PLANES_URL = "https://api.adsb.lol/v2/point/{lat:.3f}/{lon:.3f}/{nm}"
SATELLITES_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
# Bairros, ruas e pontos ("Benfica, Fortaleza"): OpenStreetMap. Uso leve e com cache, como a política deles pede.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
# endereço com rua/número: o OpenStreetMap quase não tem número de casa no Brasil ("Rua Pomar Carioca 757" não vinha)
# Busca aproximada do OpenStreetMap: aceita nome falado de qualquer jeito e prefere o que está perto do ponto.
PHOTON_URL = "https://photon.komoot.io/api/"
ARCGIS_GEOCODE_URL = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
ARCGIS_MIN_SCORE = 80
# "Mostra a casa / o comércio": fotos públicas perto do ponto (Wikimedia Commons, sem chave) e, com a chave grátis do
# Mapillary (MAPILLARY_TOKEN no .env), fotos de rua. Tudo num cartão dentro do Olho de Deus.
COMMONS_URL = "https://commons.wikimedia.org/w/api.php"
MAPILLARY_URL = "https://graph.mapillary.com/images"
MAPILLARY_KEY_ENV = "MAPILLARY_TOKEN"
SATELLITE_PHOTO_URL = ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export"
                       "?bbox={west:.6f},{south:.6f},{east:.6f},{north:.6f}&bboxSR=4326&imageSR=3857&size=640,400&format=jpg&f=image")
PHOTO_HOSTS = ("https://upload.wikimedia.org/", "https://server.arcgisonline.com/")
PLACE_PHOTO_RADIUS_M = 150
STREET_WORDS_RE = re.compile(r"^\s*(?:rua|r\.|avenida|av\.?|travessa|tv\.|alameda|rodovia|estrada|pra[çc]a|largo|beco|viela|ladeira)\b", re.IGNORECASE)
# Câmeras públicas (turismo, trânsito, praias): Windy Webcams, chave grátis em api.windy.com/keys.
WEBCAMS_URL = "https://api.windy.com/webcams/api/v3/webcams"
WEBCAMS_KEY_ENV = "WINDY_WEBCAMS_KEY"
# "lixo": os quatro grandes campos de detritos rastreados (colisão de 2009, testes antissatélite da China e da Rússia)
SAT_GROUPS = {"estacoes": "stations", "visiveis": "visual", "starlink": "starlink", "gps": "gps-ops", "clima": "weather",
              "lixo": "cosmos-2251-debris,iridium-33-debris,fengyun-1c-debris,cosmos-1408-debris"}
# grupo do globo -> grupo do serviço do céu
ORBIT_KEYS = {"estacoes": "estacoes", "visiveis": "brilhantes", "starlink": "starlink", "lixo": "lixo"}
# Tempo e espaço, todos grátis: radar de chuva (RainViewer), eventos naturais e asteroides/foto do dia (NASA; chave
# NASA_API_KEY no .env opcional, sem ela usa a DEMO_KEY de poucos pedidos por hora) e clima espacial (NOAA SWPC).
RAIN_URL = "https://api.rainviewer.com/public/weather-maps.json"
EONET_URL = "https://eonet.gsfc.nasa.gov/api/v3/events"
KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
AURORA_URL = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"
APOD_URL = "https://api.nasa.gov/planetary/apod"
# asteroides: API de aproximações do JPL (NASA), sem chave. A foto do dia usa a chave: a DEMO_KEY só aceita 10 pedidos por dia
NEO_URL = "https://ssd-api.jpl.nasa.gov/cad.api"
APOD_PAGE_URL = "https://science.nasa.gov/apod/"
APOD_IMAGE_HOSTS = ("https://apod.nasa.gov/", "https://assets.science.nasa.gov/")
AU_KM = 149_597_870.7
MOON_KM = 384_400
NASA_KEY_ENV = "NASA_API_KEY"
EONET_KINDS = {"wildfires": "queimada", "severeStorms": "tempestade", "volcanoes": "vulcão", "floods": "enchente", "seaLakeIce": "gelo",
               "earthquakes": "terremoto", "landslides": "deslizamento", "drought": "seca", "dustHaze": "poeira", "snow": "neve",
               "tempExtremes": "calor/frio extremo", "manmade": "acidente", "waterColor": "água colorida"}
CACHE_SECONDS = {"quakes": 120.0, "planes": 15.0, "sats": 6 * 3600.0, "webcams": 600.0, "rain": 300.0, "eonet": 1800.0,
                 "space": 600.0, "apod": 6 * 3600.0, "neo": 3 * 3600.0}
LAYERS = ("terremotos", "avioes", "satelites", "cameras", "starlink", "lixo", "navios", "chuva", "raios", "aurora", "eventos",
          "asteroides", "foto", "vento", "mare", "bicicletas", "radios")
ACTIONS = ("abrir", "fechar", "focar", "camadas", "globo", "3d", "lugar")
# "cidade", "bairro", "ruas": zoom do mapa de satélite (o globo vira mapa a partir de 5)
DETAIL_ZOOM = {"pais": 6, "estado": 7, "cidade": 11, "bairro": 14, "ruas": 16, "casas": 18}
MAX_PLANES = 400
MAX_SATS = 250


def _text(value: object, limit: int = 60) -> str:
    return " ".join(str(value or "").split())[:limit]


def _num(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def zoom_for_rank(rank: object) -> int | None:
    """Zoom do mapa de satélite pelo tipo de lugar achado (place_rank do OpenStreetMap): bairro, rua, casa. Cidade e
    maiores ficam no globo de perto ("foca em Fortaleza"); antes "foca no Bom Sucesso" também ficava só no globo."""
    value = _num(rank)
    if value is None:
        return None
    if value >= 27:
        return 18
    if value >= 23:
        return 16
    if value >= 19:
        return 14
    return None


def joined_variant(name: str) -> str | None:
    """'Bom Sucesso, Fortaleza' -> 'Bonsucesso, Fortaleza': bairros com nome composto costumam estar escritos juntos
    no mapa (e 'm' antes de consoante vira 'n')."""
    head, sep, tail = str(name or "").partition(",")
    words = head.split()
    if len(words) < 2 or not words[1][:1].isalpha():
        return None
    first, second = words[0], words[1].lower()
    if first.lower().endswith("m") and second[:1] not in "pbm":
        first = first[:-1] + "n"
    return " ".join([first + second, *words[2:]]) + (sep + tail if sep else "")


def _place_label(display_name: str, fallback: str) -> str:
    parts = [part.strip() for part in str(display_name or fallback).split(",") if part.strip()]
    label = ", ".join(parts[:2] + parts[-1:]) if len(parts) > 3 else ", ".join(parts)
    return _text(label, 80)


def parse_place(reverse: dict, photos: list[dict[str, str]], lat: float, lon: float) -> dict[str, Any]:
    """Cartão do lugar: só textos curtos e fotos de servidores conhecidos (nada de HTML de fora)."""
    tags = reverse.get("extratags") if isinstance(reverse.get("extratags"), dict) else {}
    name = _text(reverse.get("name"), 60)
    kind = _text(str(reverse.get("type") or "").replace("_", " "), 30) if reverse.get("category") not in {"highway", "place", "boundary"} else ""
    details = []
    for key, label in (("opening_hours", "Horário"), ("phone", "Telefone"), ("contact:phone", "Telefone"), ("website", "Site"),
                       ("contact:website", "Site"), ("cuisine", "Cozinha")):
        value = _text(tags.get(key), 70)
        if value and not any(line.startswith(label) for line in details):
            details.append(f"{label}: {value}")
    delta = 0.0012
    satellite = SATELLITE_PHOTO_URL.format(west=lon - delta, south=lat - delta * 0.62, east=lon + delta, north=lat + delta * 0.62)
    clean_photos = [
        {"url": photo["url"], "credit": _text(photo.get("credit"), 40)}
        for photo in photos if str(photo.get("url") or "").startswith(PHOTO_HOSTS + ("https://scontent",))
    ][:8]
    return {"lat": lat, "lon": lon, "title": name or _place_label(reverse.get("display_name"), ""), "kind": kind,
            "address": _place_label(reverse.get("display_name"), ""), "details": details[:4],
            "satellite": satellite, "photos": clean_photos}


def describe_place(card: dict[str, Any]) -> str:
    """Frase para a Kira falar sobre o lugar (dados do mapa aberto; ela não deve inventar o que não está aqui)."""
    bits = [f"Mostrando {card.get('title') or 'o lugar'} de perto num cartão no Olho de Deus"]
    if card.get("kind"):
        bits.append(f"tipo: {card['kind']}")
    if card.get("address") and card.get("address") != card.get("title"):
        bits.append(f"endereço: {card['address']}")
    bits.extend(card.get("details") or [])
    photos = len(card.get("photos") or [])
    bits.append(f"{photos} foto(s) públicas por perto" if photos else "sem fotos públicas por perto, só a foto de satélite")
    return "; ".join(bits) + ". Fale só o que está aqui."


def parse_quakes(data: dict) -> list[dict[str, Any]]:
    quakes = []
    for feature in (data.get("features") or [])[:500]:
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        props = feature.get("properties") or {}
        lon, lat = (_num(coords[0]), _num(coords[1])) if len(coords) >= 2 else (None, None)
        mag = _num(props.get("mag"))
        if lat is None or lon is None or mag is None:
            continue
        quakes.append({"lat": lat, "lon": lon, "mag": round(mag, 1), "place": _text(props.get("place"), 80), "time": int(_num(props.get("time")) or 0),
                       "depth": round(_num(coords[2]) or 0) if len(coords) > 2 else None})
    return quakes


def parse_planes(data: dict) -> list[dict[str, Any]]:
    planes = []
    for plane in (data.get("ac") or [])[:MAX_PLANES]:
        lat, lon = _num(plane.get("lat")), _num(plane.get("lon"))
        if lat is None or lon is None:
            continue
        altitude = plane.get("alt_baro")
        planes.append({
            "lat": lat, "lon": lon, "callsign": _text(plane.get("flight"), 10) or _text(plane.get("r"), 10),
            "type": _text(plane.get("t"), 8), "alt_ft": int(altitude) if isinstance(altitude, (int, float)) else 0,
            "speed_kt": int(_num(plane.get("gs")) or 0), "track": int(_num(plane.get("track")) or 0),
        })
    return planes


def parse_webcams(data: dict) -> list[dict[str, Any]]:
    cams = []
    for cam in (data.get("webcams") or [])[:40]:
        location = cam.get("location") or {}
        lat, lon = _num(location.get("latitude")), _num(location.get("longitude"))
        if lat is None or lon is None:
            continue
        preview = str((((cam.get("images") or {}).get("current") or {}).get("preview")) or "")
        cam_id = str(cam.get("webcamId") or "")
        cams.append({
            "lat": lat, "lon": lon, "title": _text(cam.get("title"), 60), "city": _text(location.get("city"), 40),
            "image": preview if preview.startswith("https://") else "",
            "url": f"https://www.windy.com/webcams/{cam_id}" if cam_id.isdigit() else "",
        })
    return cams


def parse_rain(data: dict) -> dict[str, Any] | None:
    frames = ((data.get("radar") or {}).get("past") or [])
    host = str(data.get("host") or "")
    if not frames or not host.startswith("https://"):
        return None
    last = frames[-1]
    return {"host": host, "path": str(last.get("path") or ""), "time": int(_num(last.get("time")) or 0)}


def parse_eonet(data: dict) -> list[dict[str, Any]]:
    events = []
    for event in (data.get("events") or [])[:400]:
        geometry = [g for g in event.get("geometry") or [] if g.get("type") == "Point"]
        if not geometry:
            continue
        coords = geometry[-1].get("coordinates") or []
        lon, lat = (_num(coords[0]), _num(coords[1])) if len(coords) >= 2 else (None, None)
        if lat is None or lon is None:
            continue
        kind = ((event.get("categories") or [{}])[0] or {}).get("id") or ""
        events.append({"lat": lat, "lon": lon, "title": _text(event.get("title"), 80), "kind": EONET_KINDS.get(kind, "evento"),
                       "cat": _text(kind, 20), "date": _text(geometry[-1].get("date"), 10)})
    return events


def kp_label(kp: float) -> str:
    if kp >= 7:
        return "tempestade solar forte"
    if kp >= 5:
        return "tempestade solar"
    if kp >= 4:
        return "campo magnético agitado"
    return "calmo"


def parse_space(kp_rows: list, aurora: dict) -> dict[str, Any]:
    rows = [row for row in kp_rows if isinstance(row, dict) and _num(row.get("Kp")) is not None]
    kp = round(_num(rows[-1]["Kp"]) or 0.0, 1) if rows else None
    points = []
    for item in aurora.get("coordinates") or []:
        if len(item) >= 3 and (_num(item[2]) or 0) >= 6:
            lon = _num(item[0]) or 0.0
            points.append([_num(item[1]), lon - 360 if lon > 180 else lon, int(item[2])])
    return {"kp": kp, "label": kp_label(kp or 0), "time": _text(rows[-1].get("time_tag"), 19) if rows else "", "aurora": points[:6000]}


def parse_apod(data: dict) -> dict[str, Any]:
    url = str(data.get("url") or "")
    image = url if data.get("media_type") == "image" and url.startswith("https://") else ""
    return {"title": _text(data.get("title"), 90), "date": _text(data.get("date"), 10), "image": image,
            "text": _text(data.get("explanation"), 700), "credit": _text(data.get("copyright"), 60)}


def parse_apod_page(html: str) -> dict[str, str]:
    """Página science.nasa.gov/apod: título do dia (o h2 grande) e a foto do mês atual (vídeo .mp4 não serve)."""
    title = re.search(r'<h2 class="display-48[^"]*">([^<]{3,120})<', html)
    month = datetime.now().strftime("%B").lower()
    image = re.search(rf'https://assets\.science\.nasa\.gov/content/dam/science/cds/apod/apod/\d{{4}}/{month}/[^"?\s]+\.(?:jpe?g|png)', html)
    return {"title": _text(title.group(1), 90) if title else "", "image": image.group(0) if image else ""}


def parse_neo(data: dict) -> list[dict[str, Any]]:
    """Aproximações das próximas 48 h (JPL). Tamanho estimado pelo brilho (magnitude H, albedo típico de 14%)."""
    fields = data.get("fields") or []
    rocks = []
    for row in data.get("data") or []:
        item = dict(zip(fields, row))
        au, h = _num(item.get("dist")), _num(item.get("h"))
        if au is None:
            continue
        km = au * AU_KM
        rocks.append({"name": _text(item.get("des"), 30), "when": _text(item.get("cd"), 17), "km": int(km), "lunar": round(km / MOON_KM, 1),
                      "size_m": int(3552_000 * 10 ** (-h / 5)) if h is not None else 0,
                      "speed_kmh": int((_num(item.get("v_rel")) or 0) * 3600)})
    return sorted(rocks, key=lambda rock: rock["km"])


def parse_tle(text: str, limit: int | None = MAX_SATS) -> list[dict[str, str]]:
    lines = [line.rstrip() for line in str(text or "").splitlines() if line.strip()]
    sats = []
    for index in range(0, len(lines) - 2, 3):
        name, one, two = lines[index], lines[index + 1], lines[index + 2]
        if one.startswith("1 ") and two.startswith("2 ") and len(one) >= 69 and len(two) >= 69:
            sats.append({"name": _text(name, 40), "line1": one[:69], "line2": two[:69]})
    return sats if limit is None else sats[:limit]


def spread_sample(items: list, limit: int = MAX_SATS) -> list:
    """Amostra espalhada (grupos enormes como Starlink e lixo espacial): o globo desenha no máximo `limit`."""
    if len(items) <= limit:
        return items
    step = len(items) / limit
    return [items[int(index * step)] for index in range(limit)]


async def _get(url: str, *, headers: dict[str, str] | None = None, **params: Any) -> httpx.Response:
    # identificação no padrão que o Wikimedia/OpenStreetMap pedem ("Nome/versão (URL)"); fora dele o Commons responde 403
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "KiraBot/8.0 (https://commons.wikimedia.org/wiki/Commons:API; personal use)", **(headers or {})}) as client:
        response = await client.get(url, params=params or None)
    response.raise_for_status()
    return response


_ORDINAL_WORDS = {
    "primeira": "1ª", "primeiro": "1º", "segunda": "2ª", "segundo": "2º", "terceira": "3ª", "terceiro": "3º", "quarta": "4ª", "quarto": "4º",
    "quinta": "5ª", "quinto": "5º", "sexta": "6ª", "sexto": "6º", "sétima": "7ª", "setima": "7ª", "sétimo": "7º", "setimo": "7º",
    "oitava": "8ª", "oitavo": "8º", "nona": "9ª", "nono": "9º", "décima": "10ª", "decima": "10ª", "décimo": "10º", "decimo": "10º",
}


def _ordinals(name: str) -> str:
    """'décima região militar' -> '10ª região militar' (o mapa só acha unidades e batalhões pelo número)."""
    return " ".join(_ORDINAL_WORDS.get(word.lower(), word) for word in str(name or "").split())


class GodsEyeData:
    def __init__(self, fetch: Callable[..., Awaitable[httpx.Response]] = _get, clock: Callable[[], float] = time.monotonic,
                 orbits: Callable[[str], Awaitable[list[tuple[str, str, str]]]] | None = None) -> None:
        self._fetch = fetch
        # órbitas do serviço do céu (services/sky_watch.py), com o mesmo cache: o CelesTrak recusa o mesmo grupo duas
        # vezes em 2 horas, então os dois não podem baixar cada um o seu
        self._orbits = orbits
        self._clock = clock
        self._cache: dict[str, tuple[float, Any]] = {}
        self._loading: dict[str, asyncio.Future] = {}
        self._sat_totals: dict[str, int] = {}

    async def _cached(self, key: str, ttl: float, load: Callable[[], Awaitable[Any]]) -> Any:
        hit = self._cache.get(key)
        if hit and self._clock() - hit[0] < ttl:
            return hit[1]
        # O globo e a contagem da Kira pedem a mesma fonte ao mesmo tempo: um download só, dividido entre os dois.
        pending = self._loading.get(key)
        if pending is None:
            pending = asyncio.ensure_future(load())
            self._loading[key] = pending
            pending.add_done_callback(lambda done: self._finish_loading(key, done))
        value = await asyncio.shield(pending)
        self._cache[key] = (self._clock(), value)
        if len(self._cache) > 64:
            self._cache.pop(next(iter(self._cache)))
        return value

    def _finish_loading(self, key: str, done: asyncio.Future) -> None:
        self._loading.pop(key, None)
        if not done.cancelled():
            done.exception()  # quem desistiu de esperar (timeout da contagem) não deixa aviso de erro esquecido no log

    async def quakes(self) -> list[dict[str, Any]]:
        async def load():
            return parse_quakes((await self._fetch(USGS_URL)).json())
        return await self._cached("quakes", CACHE_SECONDS["quakes"], load)

    async def planes(self, lat: float, lon: float, radius_km: float) -> list[dict[str, Any]]:
        nm = int(max(10, min(250, radius_km / 1.852)))
        url = PLANES_URL.format(lat=lat, lon=lon, nm=nm)

        async def load():
            return parse_planes((await self._fetch(url)).json())
        return await self._cached(f"planes:{lat:.1f}:{lon:.1f}:{nm}", CACHE_SECONDS["planes"], load)

    async def satellites(self, group: str) -> list[dict[str, str]]:
        celestrak = SAT_GROUPS.get(group, "stations")

        async def load():
            if self._orbits is not None and group in ORBIT_KEYS:
                everything = [{"name": _text(name, 40), "line1": one[:69], "line2": two[:69]} for name, one, two in await self._orbits(ORBIT_KEYS[group])]
            else:
                texts = [(await self._fetch(SATELLITES_URL.format(group=name))).text for name in celestrak.split(",")]
                everything = [sat for text in texts for sat in parse_tle(text, None)]
            self._sat_totals[group] = len(everything)
            return spread_sample(everything)
        return await self._cached(f"sats:{celestrak}", CACHE_SECONDS["sats"], load)

    def sat_total(self, group: str) -> int | None:
        """Quantos o grupo tem de verdade (o globo recebe só uma amostra dos grandes)."""
        return self._sat_totals.get(group)

    async def webcams(self, lat: float, lon: float, radius_km: float) -> list[dict[str, Any]] | None:
        """Câmeras públicas perto do ponto; None quando falta a chave (o globo mostra como ativar)."""
        key = str(os.getenv(WEBCAMS_KEY_ENV) or "").strip()
        if not key:
            return None
        radius = int(max(5, min(250, radius_km)))

        async def load():
            response = await self._fetch(WEBCAMS_URL, headers={"x-windy-api-key": key}, nearby=f"{lat:.3f},{lon:.3f},{radius}",
                                         limit=25, include="images,location")
            return parse_webcams(response.json())
        return await self._cached(f"cams:{lat:.2f}:{lon:.2f}:{radius}", CACHE_SECONDS["webcams"], load)

    async def rain(self) -> dict[str, Any] | None:
        async def load():
            return parse_rain((await self._fetch(RAIN_URL)).json())
        return await self._cached("rain", CACHE_SECONDS["rain"], load)

    async def eonet(self) -> list[dict[str, Any]]:
        async def load():
            return parse_eonet((await self._fetch(EONET_URL, status="open", days=30, limit=400)).json())
        return await self._cached("eonet", CACHE_SECONDS["eonet"], load)

    async def space(self) -> dict[str, Any]:
        async def load():
            kp, aurora = await asyncio.gather(self._fetch(KP_URL), self._fetch(AURORA_URL))
            return parse_space(kp.json(), aurora.json())
        return await self._cached("space", CACHE_SECONDS["space"], load)

    @staticmethod
    def _nasa_key() -> str:
        return str(os.getenv(NASA_KEY_ENV) or "").strip() or "DEMO_KEY"

    async def apod(self) -> dict[str, Any] | None:
        async def load():
            # a foto de hoje sai no meio do dia nos EUA; antes disso a API dá erro: usa a de ontem
            for back in range(3):
                day = (date.today() - timedelta(days=back)).isoformat()
                try:
                    apod = parse_apod((await self._fetch(APOD_URL, api_key=self._nasa_key(), date=day)).json())
                except (httpx.HTTPStatusError, ValueError):
                    continue
                if apod["image"].startswith(APOD_IMAGE_HOSTS) and apod["title"] != "NASA Science":
                    return apod
                # desde a mudança para science.nasa.gov a API manda o texto certo, mas título "NASA Science" e o
                # logotipo no lugar da foto: título e foto vêm da página nova (hoje pode ser vídeo: fica só o texto)
                if back == 0 and apod["text"]:
                    try:
                        page = parse_apod_page((await self._fetch(APOD_PAGE_URL)).text)
                    except (httpx.HTTPError, ValueError):
                        page = {"title": "", "image": ""}
                    return {**apod, "title": page["title"] or "Foto do dia da NASA", "image": page["image"]}
            return None
        return await self._cached("apod", CACHE_SECONDS["apod"], load)

    async def neo(self) -> list[dict[str, Any]]:
        async def load():
            return parse_neo((await self._fetch(NEO_URL, **{"date-min": "now", "date-max": "+2", "dist-max": "0.2", "sort": "dist"})).json())
        return await self._cached("neo", CACHE_SECONDS["neo"], load)

    # ---- barra lateral (2026-11-03): busca com sugestões/CEP e dados do lugar focado; tudo de fontes grátis e reais ----
    async def suggest(self, query: str, lat: float, lon: float) -> list[dict[str, Any]]:
        q = _text(query, 80)
        if len(q) < 2:
            return []
        digits = re.sub(r"\D", "", q)

        async def load():
            if len(digits) == 8:  # CEP: ViaCEP dá o endereço, o geocoder dá o ponto
                cep = (await self._fetch(f"https://viacep.com.br/ws/{digits}/json/")).json()
                if cep.get("erro"):
                    return []
                label = ", ".join(x for x in (cep.get("logradouro"), cep.get("bairro"), cep.get("localidade"), cep.get("uf")) if x)
                spot = await self.geocode(label) or await self.geocode(f"{cep.get('localidade')}, {cep.get('uf')}")
                return [{**spot, "label": f"CEP {digits[:5]}-{digits[5:]} · {label}"}] if spot else []
            feats = (await self._fetch(PHOTON_URL, q=q, lat=f"{lat:.3f}", lon=f"{lon:.3f}", limit=6)).json().get("features") or []
            out = []
            for f in feats:
                pr = f.get("properties") or {}
                c = (f.get("geometry") or {}).get("coordinates") or []
                if len(c) == 2:
                    label = ", ".join(dict.fromkeys(x for x in (pr.get("name"), pr.get("city"), pr.get("state"), pr.get("country")) if x))
                    out.append({"lat": c[1], "lon": c[0], "label": _text(label, 90)})
            return out
        return await self._cached(f"sug:{q.lower()}:{round(lat)}:{round(lon)}", 3600.0, load)

    async def local(self, lat: float, lon: float) -> dict[str, Any]:
        """Vento, nuvens e temperatura agora (Open-Meteo) e a maré das próximas horas (Open-Meteo Marine), se houver mar."""
        async def load():
            now = (await self._fetch("https://api.open-meteo.com/v1/forecast", latitude=lat, longitude=lon, timezone="auto",
                                     current="temperature_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,cloud_cover")).json()
            out: dict[str, Any] = {"weather": now.get("current"), "tide": None}
            try:
                sea = (await self._fetch("https://marine-api.open-meteo.com/v1/marine", latitude=lat, longitude=lon, timezone="auto",
                                         hourly="sea_level_height_msl", forecast_days=2)).json().get("hourly") or {}
                levels = [(t, v) for t, v in zip(sea.get("time") or [], sea.get("sea_level_height_msl") or []) if v is not None]
                if levels:
                    out["tide"] = levels[:30]
            except Exception:
                pass
            return out
        return await self._cached(f"local:{lat:.2f}:{lon:.2f}", 900.0, load)

    async def bikes(self, lat: float, lon: float) -> dict[str, Any] | None:
        """Estações de bicicleta compartilhada da rede mais perto (CityBikes), com bicicletas livres agora."""
        async def nets():
            return (await self._fetch("https://api.citybik.es/v2/networks?fields=id,name,location")).json().get("networks") or []
        networks = await self._cached("bike-nets", 24 * 3600.0, nets)
        best = min(networks, key=lambda n: _distance_km(lat, lon, n["location"]["latitude"], n["location"]["longitude"]), default=None)
        if not best or _distance_km(lat, lon, best["location"]["latitude"], best["location"]["longitude"]) > 60:
            return None

        async def load():
            st = (await self._fetch(f"https://api.citybik.es/v2/networks/{best['id']}")).json().get("network", {}).get("stations") or []
            return {"name": best["name"], "items": [{"lat": x["latitude"], "lon": x["longitude"], "name": _text(x.get("name"), 60),
                                                      "free": x.get("free_bikes"), "slots": x.get("empty_slots")} for x in st[:400]]}
        return await self._cached(f"bikes:{best['id']}", 120.0, load)

    async def radios(self, lat: float, lon: float) -> list[dict[str, Any]]:
        """Rádios com localização perto do ponto (Radio Browser, aberto)."""
        async def load():
            # Busca já pela posição (geo_distance em metros): antes vinham as 300 mais ouvidas do Brasil e quase nenhuma
            # ficava a menos de 150 km de Fortaleza.
            rows = (await self._fetch("https://de1.api.radio-browser.info/json/stations/search", geo_lat=f"{lat:.4f}", geo_long=f"{lon:.4f}",
                                      geo_distance="150000", has_geo_info="true", hidebroken="true", limit=120,
                                      order="clickcount", reverse="true")).json()
            near = [r for r in rows if r.get("geo_lat") is not None and _distance_km(lat, lon, r["geo_lat"], r["geo_long"]) < 150]
            if not near and -35 < lat < 6 and -75 < lon < -32:  # sem coordenada: as do estado mais próximo pelo nome da cidade
                rows = (await self._fetch("https://de1.api.radio-browser.info/json/stations/search", countrycode="BR", has_geo_info="true",
                                          limit=500, order="clickcount", reverse="true")).json()
                near = [r for r in rows if r.get("geo_lat") is not None and _distance_km(lat, lon, r["geo_lat"], r["geo_long"]) < 400]
            return [{"lat": r["geo_lat"], "lon": r["geo_long"], "name": _text(r.get("name"), 50), "url": r.get("url_resolved") or ""} for r in near[:40]]
        return await self._cached(f"radios:{lat:.1f}:{lon:.1f}", 3600.0, load)

    async def _nominatim(self, query: str) -> list[dict[str, Any]]:
        try:
            found = (await self._fetch(NOMINATIM_URL, q=query, format="jsonv2", limit=5, **{"accept-language": "pt-BR"})).json()
        except (httpx.HTTPError, ValueError):
            return []
        return [item for item in found if isinstance(item, dict)] if isinstance(found, list) else []

    async def _arcgis(self, query: str) -> dict[str, Any] | None:
        try:
            found = (await self._fetch(ARCGIS_GEOCODE_URL, SingleLine=query, f="json", maxLocations=1, countryCode="BRA",
                                       outFields="Addr_type")).json().get("candidates") or []
        except (httpx.HTTPError, ValueError, AttributeError):
            return None
        best = found[0] if found and isinstance(found[0], dict) else None
        if best is None or _num(best.get("score")) < ARCGIS_MIN_SCORE:
            return None
        where = best.get("location") or {}
        return {"lat": _num(where.get("y")), "lon": _num(where.get("x")), "label": _text(best.get("address"), 80), "rank": 30.0}

    async def geocode_near(self, place: str, lat: float, lon: float, radius_km: float = 60.0) -> dict[str, Any] | None:
        """Lugar perto de um ponto ("hospital da Unimed" perto de casa, não o de outro estado).

        ArcGIS com o ponto de referência (aceita nome de estabelecimento e erro de fala) e, sem nada perto, o OpenStreetMap
        só dentro de uma caixa em volta do ponto. None = não achou nada perto.
        """
        name = _ordinals(_text(place, 80))
        if not name:
            return None

        async def load():
            def near(la: float | None, lo: float | None) -> bool:
                return la is not None and lo is not None and _distance_km(lat, lon, la, lo) <= radius_km
            try:
                found = (await self._fetch(ARCGIS_GEOCODE_URL, SingleLine=name, f="json", maxLocations=6, countryCode="BRA",
                                           location=f"{lon:.5f},{lat:.5f}", outFields="Addr_type,PlaceName")).json().get("candidates") or []
            except (httpx.HTTPError, ValueError, AttributeError):
                found = []
            close = [c for c in found if isinstance(c, dict) and _num(c.get("score")) >= 70
                     and near(_num((c.get("location") or {}).get("y")), _num((c.get("location") or {}).get("x")))]
            # Só lugar de verdade (estabelecimento ou endereço com número) vence direto. Nome de rua/bairro parecido
            # passava na frente: "10ª Região Militar" virava a rua "Vila Militar" (2026-10-31); aí o OSM decide antes.
            exact = [c for c in close if str((c.get("attributes") or {}).get("Addr_type") or "") in {"POI", "PointAddress", "Subaddress"}]
            if exact:
                best = max(exact, key=lambda c: _num(c.get("score")) or 0)
                where = best.get("location") or {}
                return {"lat": _num(where.get("y")), "lon": _num(where.get("x")), "label": _text(best.get("address"), 80), "rank": 30.0}
            try:
                features = (await self._fetch(PHOTON_URL, q=name, lat=f"{lat:.4f}", lon=f"{lon:.4f}", limit=5)).json().get("features") or []
            except (httpx.HTTPError, ValueError, AttributeError):
                features = []
            for feature in features:
                lo, la = ((feature.get("geometry") or {}).get("coordinates") or [None, None])[:2]
                info = feature.get("properties") or {}
                if info.get("osm_key") != "highway" and near(_num(la), _num(lo)):  # rua com nome parecido não vale
                    label = ", ".join(str(info[k]) for k in ("name", "district", "city") if info.get(k))
                    return {"lat": _num(la), "lon": _num(lo), "label": _text(label, 80), "rank": 30.0}
            box = f"{lon - 0.6:.4f},{lat + 0.6:.4f},{lon + 0.6:.4f},{lat - 0.6:.4f}"
            try:
                items = (await self._fetch(NOMINATIM_URL, q=name, format="jsonv2", limit=5, viewbox=box, bounded=1,
                                           **{"accept-language": "pt-BR"})).json()
            except (httpx.HTTPError, ValueError):
                items = []
            items = [i for i in items if isinstance(i, dict) and near(_num(i.get("lat")), _num(i.get("lon")))] if isinstance(items, list) else []
            if items:
                return {"lat": _num(items[0].get("lat")), "lon": _num(items[0].get("lon")),
                        "label": _place_label(items[0].get("display_name"), name), "rank": _num(items[0].get("place_rank"))}
            if close:  # nada melhor por perto: a rua/bairro parecido do ArcGIS ainda é melhor que nada
                best = max(close, key=lambda c: _num(c.get("score")) or 0)
                where = best.get("location") or {}
                return {"lat": _num(where.get("y")), "lon": _num(where.get("x")), "label": _text(best.get("address"), 80), "rank": 30.0}
            return None
        return await self._cached(f"geonear:{name.lower()}:{lat:.2f}:{lon:.2f}", 24 * 3600.0, load)

    async def geocode(self, place: str) -> dict[str, Any] | None:
        name = _text(place, 80)
        if not name:
            return None

        async def load():
            if STREET_WORDS_RE.match(name) or re.search(r"\d", name):
                address = await self._arcgis(name)
                if address is not None:
                    return address
            found = await self._nominatim(name)
            chosen = found[0] if found else None
            if chosen is not None and chosen.get("category") == "highway" and not STREET_WORDS_RE.match(name):
                # Pediram um bairro/lugar e veio uma rua ("Bom Sucesso, Fortaleza" -> Rua Francisco Uchôa): prefere uma
                # área entre os resultados ou a grafia junta ("Bonsucesso"), que é como o bairro está no mapa.
                area = next((item for item in found if item.get("category") in {"boundary", "place"}), None)
                variant = joined_variant(name)
                if area is None and variant:
                    area = next((item for item in await self._nominatim(variant) if item.get("category") in {"boundary", "place"}), None)
                chosen = area or chosen
            if chosen is not None:
                return {"lat": _num(chosen.get("lat")), "lon": _num(chosen.get("lon")),
                        "label": _place_label(chosen.get("display_name"), name), "rank": _num(chosen.get("place_rank"))}
            results = (await self._fetch(GEOCODE_URL, name=name, count=1, language="pt")).json().get("results") or []
            if not results:
                return None
            first = results[0]
            return {"lat": _num(first.get("latitude")), "lon": _num(first.get("longitude")), "label": _text(f"{first.get('name')}, {first.get('country') or ''}".strip(", "), 80)}
        return await self._cached(f"geo:{name.lower()}", 24 * 3600.0, load)

    async def place_card(self, lat: float, lon: float) -> dict[str, Any]:
        """Endereço, dados do estabelecimento (se o mapa aberto tiver) e fotos públicas perto do ponto."""
        async def load():
            try:
                reverse = (await self._fetch(NOMINATIM_REVERSE_URL, lat=f"{lat:.6f}", lon=f"{lon:.6f}", format="jsonv2", zoom=18,
                                             extratags=1, **{"accept-language": "pt-BR"})).json()
            except (httpx.HTTPError, ValueError):
                reverse = {}
            photos: list[dict[str, str]] = []
            try:
                pages = ((await self._fetch(COMMONS_URL, action="query", generator="geosearch", ggscoord=f"{lat:.6f}|{lon:.6f}",
                                            ggsradius=PLACE_PHOTO_RADIUS_M, ggsnamespace=6, ggslimit=6, prop="imageinfo",
                                            iiprop="url", iiurlwidth=640, format="json")).json().get("query") or {}).get("pages") or {}
                for page in pages.values():
                    info = (page.get("imageinfo") or [{}])[0]
                    photos.append({"url": str(info.get("thumburl") or ""), "credit": "Wikimedia Commons"})
            except (httpx.HTTPError, ValueError, AttributeError):
                pass
            token = str(os.getenv(MAPILLARY_KEY_ENV) or "").strip()
            if token:
                delta = 0.0008
                try:
                    images = (await self._fetch(MAPILLARY_URL, access_token=token, fields="id,thumb_1024_url", limit=4,
                                                bbox=f"{lon - delta:.6f},{lat - delta:.6f},{lon + delta:.6f},{lat + delta:.6f}")).json()
                    photos.extend({"url": str(item.get("thumb_1024_url") or ""), "credit": "Mapillary"} for item in images.get("data") or [])
                except (httpx.HTTPError, ValueError, AttributeError):
                    pass
            return parse_place(reverse if isinstance(reverse, dict) else {}, photos, lat, lon)
        return await self._cached(f"place:{lat:.5f}:{lon:.5f}", 3600.0, load)


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    a = math.sin(math.radians(lat2 - lat1) / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(min(1.0, math.sqrt(a)))


def build_command(payload: dict, place: dict[str, Any] | None) -> dict[str, Any]:
    action = str(payload.get("action") or "abrir").strip().lower()
    if action not in ACTIONS:
        action = "abrir"
    layers = [layer for layer in (payload.get("layers") or []) if layer in LAYERS] if isinstance(payload.get("layers"), list) else []
    command: dict[str, Any] = {"action": action, "layers": layers}
    if place and place.get("lat") is not None and place.get("lon") is not None:
        command["focus"] = {"lat": place["lat"], "lon": place["lon"], "label": place.get("label", "")}
    radius = _num(payload.get("radius_km"))
    if radius:
        command["radius_km"] = max(20.0, min(460.0, radius))
    zoom = (DETAIL_ZOOM.get(str(payload.get("detail") or "").strip().lower()) or _num(payload.get("zoom"))
            or (18 if action == "lugar" else zoom_for_rank((place or {}).get("rank"))))
    if zoom:
        command["zoom"] = int(max(5, min(18, zoom)))
    return command


COUNT_TIMEOUT_S = 12.0


async def count_layers(data: GodsEyeData, layers: list[str], focus: dict[str, Any], live: dict[str, Any] | None = None) -> dict[str, int | str | None]:
    """Quantos itens cada camada tem agora, direto das fontes (mesmo cache da tela). None = fonte não respondeu.

    O globo carrega as camadas sem esperar; contar o que ele já desenhou dava 0 e a Kira dizia "nenhum terremoto".
    """
    lat, lon = _num(focus.get("lat")), _num(focus.get("lon"))
    radius = _num(focus.get("radius")) or 250.0

    async def one(key: str) -> int | str | None:
        if key == "terremotos":
            return len(await data.quakes())
        if key == "satelites":
            groups = await asyncio.gather(data.satellites("estacoes"), data.satellites("visiveis"))
            return sum(len(group) for group in groups)
        if key in {"starlink", "lixo"}:
            sample = await data.satellites(key)
            return data.sat_total(key) or len(sample)
        if key == "avioes" and lat is not None and lon is not None:
            return len(await data.planes(lat, lon, radius))
        if key == "cameras" and lat is not None and lon is not None:
            cams = await data.webcams(lat, lon, 50.0)
            return None if cams is None else len(cams)
        if key == "eventos":
            events = await data.eonet()
            kinds: dict[str, int] = {}
            for event in events:
                kinds[event["kind"]] = kinds.get(event["kind"], 0) + 1
            top = ", ".join(f"{n} {kind}" for kind, n in sorted(kinds.items(), key=lambda kv: -kv[1])[:4])
            return f"{len(events)} eventos naturais ativos no mundo segundo a NASA ({top})"
        if key == "aurora":
            space = await data.space()
            if space["kp"] is None:
                return None
            return (f"clima espacial: índice Kp {str(space['kp']).replace('.', ',')}, {space['label']}; a faixa verde no globo é onde "
                    "pode haver aurora agora (perto dos polos, não aparece no Brasil)")
        if key == "chuva":
            rain = await data.rain()
            if not rain:
                return None
            return f"radar de chuva das {datetime.fromtimestamp(rain['time']).strftime('%H:%M')} ligado (manchas coloridas = chuva agora)"
        if key == "asteroides":
            rocks = await data.neo()
            if not rocks:
                return "nenhum asteroide passando perto hoje"
            near = rocks[0]
            km = f"{near['km']:,}".replace(",", ".")
            return (f"{len(rocks)} asteroides passam perto da Terra nas próximas 48 horas; o mais perto é o {near['name']}, a {km} km ({str(near['lunar']).replace('.', ',')} vezes a distância da Lua), uns {near['size_m']} metros, sem risco de batida")
        if key == "foto":
            apod = await data.apod()
            return f"foto do dia da NASA: '{apod['title']}' (já na tela; diga o título e explique em uma frase)" if apod else None
        if key == "raios" and live is not None:
            strikes = live["raios"].recent()
            near = [s for s in strikes if lat is not None and lon is not None and abs(s["lat"] - lat) < 3 and abs(s["lon"] - lon) < 3]
            return (f"{len(strikes)} raios no mundo nos últimos 10 minutos" + (f", {len(near)} perto daqui" if lat is not None else "")
                    + " (a rede acabou de ligar e vai juntando: nos primeiros segundos o número ainda é baixo)")
        if key == "navios" and live is not None and lat is not None and lon is not None:
            ships = live["navios"].near(lat, lon, radius)
            if ships is None:
                return "navios precisam da chave grátis do aisstream.io (AISSTREAM_KEY no .env)"
            return f"{len(ships)} navios na área (vão aparecendo conforme mandam posição, alguns por minuto)"
        return None

    keys = [key for key in layers if key in LAYERS]
    try:
        found = await asyncio.wait_for(asyncio.gather(*(one(key) for key in keys), return_exceptions=True), COUNT_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {key: None for key in keys}
    return {key: value if isinstance(value, (int, str)) else None for key, value in zip(keys, found)}


def describe_counts(counts: dict[str, int | str | None], focus: dict[str, Any]) -> str:
    """Os números reais, curtos, para a Kira falar como estão.

    Numa frase longa ("magnitude 2,5 ou mais...; estações espaciais e...") ela resumia e pulava os números.
    """
    where = str(focus.get("label") or "").strip().split(",")[0]
    radius = int(_num(focus.get("radius")) or 250)
    texts = {
        "terremotos": lambda n: f"{n} terremotos no mundo nas últimas 24 horas",
        "satelites": lambda n: f"{n} satélites rastreados",
        "avioes": lambda n: f"{n} aviões no ar" + (f" perto de {where}" if where else f" num raio de {radius} km")
        + ("" if n else " (a rede aberta de radares quase não cobre essa região: não diga que o céu está vazio)"),
        "cameras": lambda n: f"{n} câmeras públicas perto" + (f" de {where}" if where else ""),
        "starlink": lambda n: f"{n} satélites Starlink em órbita (o globo mostra uma amostra)",
        "lixo": lambda n: f"{n} pedaços de lixo espacial rastreados nos quatro grandes campos de detritos (o globo mostra uma amostra)",
    }
    parts = []
    for key, value in counts.items():
        if isinstance(value, str):
            parts.append(value)
            continue
        if key not in texts and value is not None:
            continue
        parts.append(texts[key](value) if value is not None else f"{key}: a fonte não respondeu agora (não diga que não há)")
    return ("Números para falar agora: " + ", ".join(parts) + ".") if parts else ""


def create_gods_eye_router(data: GodsEyeData, *, token_provider: Callable[[], str], run_js: Callable[[str], Any],
                           call_globe: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None, live: dict[str, Any] | None = None) -> APIRouter:
    """call_globe(método, argumento): leva o comando ao globo da tela em uso (celular ou PC); sem ele, run_js na do PC."""
    router = APIRouter()

    async def globe(method: str, arg: dict[str, Any]) -> Any:
        if call_globe is not None:
            return await call_globe(method, arg)
        js = (f"(function(){{ if (!window.KiraGodsEye) return JSON.stringify({{ok:false,error:'globo_indisponivel'}}); "
              f"return JSON.stringify(window.KiraGodsEye.{method}({json.dumps(arg)})); }})();")
        return await asyncio.to_thread(run_js, js)

    def require_local(request: Request) -> None:
        if not is_local_request(request):
            raise HTTPException(status_code=403, detail="origem nao autorizada")

    async def guarded(load: Callable[[], Awaitable[Any]]) -> dict[str, Any]:
        try:
            return {"ok": True, "items": await load()}
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("GODS_EYE_SOURCE_FAILED reason=%s", type(exc).__name__)
            return {"ok": False, "items": []}

    @router.get("/api/godseye/earthquakes")
    async def earthquakes(request: Request) -> dict[str, Any]:
        require_local(request)
        return await guarded(data.quakes)

    @router.get("/api/godseye/planes")
    async def planes(request: Request, lat: float, lon: float, radius_km: float = 250.0) -> dict[str, Any]:
        require_local(request)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise HTTPException(status_code=422, detail="coordenada inválida")
        return await guarded(lambda: data.planes(lat, lon, radius_km))

    @router.get("/api/godseye/webcams")
    async def webcams(request: Request, lat: float, lon: float, radius_km: float = 50.0) -> dict[str, Any]:
        require_local(request)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise HTTPException(status_code=422, detail="coordenada inválida")
        try:
            items = await data.webcams(lat, lon, radius_km)
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("GODS_EYE_SOURCE_FAILED source=webcams reason=%s", type(exc).__name__)
            return {"ok": False, "items": []}
        return {"ok": True, "items": [], "needs_key": True} if items is None else {"ok": True, "items": items}

    @router.get("/api/godseye/rain")
    async def rain(request: Request) -> dict[str, Any]:
        require_local(request)
        return await guarded(data.rain)

    @router.get("/api/godseye/eonet")
    async def eonet(request: Request) -> dict[str, Any]:
        require_local(request)
        return await guarded(data.eonet)

    @router.get("/api/godseye/space")
    async def space(request: Request) -> dict[str, Any]:
        require_local(request)
        return await guarded(data.space)

    @router.get("/api/godseye/apod")
    async def apod(request: Request) -> dict[str, Any]:
        require_local(request)
        return await guarded(data.apod)

    @router.get("/api/godseye/neo")
    async def neo(request: Request) -> dict[str, Any]:
        require_local(request)
        return await guarded(data.neo)

    @router.get("/api/godseye/lightning")
    async def lightning(request: Request) -> dict[str, Any]:
        require_local(request)
        return {"ok": True, "items": live["raios"].recent()} if live is not None else {"ok": False, "items": []}

    @router.get("/api/godseye/keys")
    async def missing_keys(request: Request) -> dict[str, Any]:
        """Camadas sem chave no .env: o menu do Olho de Deus nem mostra (2026-11-05)."""
        require_local(request)
        need = {"cameras": "WINDY_WEBCAMS_KEY", "navios": "AISSTREAM_KEY"}
        return {"ok": True, "missing": [k for k, env in need.items() if not os.getenv(env, "").strip()]}

    @router.get("/api/godseye/ships")
    async def ships(request: Request, lat: float, lon: float, radius_km: float = 250.0) -> dict[str, Any]:
        require_local(request)
        if live is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return {"ok": False, "items": []}
        items = live["navios"].near(lat, lon, radius_km)
        return {"ok": True, "items": [], "needs_key": True} if items is None else {"ok": True, "items": items}

    @router.get("/api/godseye/place")
    async def place(request: Request, lat: float, lon: float) -> dict[str, Any]:
        require_local(request)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise HTTPException(status_code=422, detail="coordenada inválida")
        return {"ok": True, "place": await data.place_card(lat, lon)}

    @router.get("/api/godseye/suggest")
    async def suggest(request: Request, q: str, lat: float = -3.72, lon: float = -38.54) -> dict[str, Any]:
        require_local(request)
        return await guarded(lambda: data.suggest(q, lat, lon))

    @router.get("/api/godseye/local")
    async def local(request: Request, lat: float, lon: float) -> dict[str, Any]:
        require_local(request)
        return await guarded(lambda: data.local(lat, lon))

    @router.get("/api/godseye/bikes")
    async def bikes(request: Request, lat: float, lon: float) -> dict[str, Any]:
        require_local(request)
        return await guarded(lambda: data.bikes(lat, lon))

    @router.get("/api/godseye/radios")
    async def radios(request: Request, lat: float, lon: float) -> dict[str, Any]:
        require_local(request)
        return await guarded(lambda: data.radios(lat, lon))

    @router.get("/api/godseye/satellites")
    async def satellites(request: Request, group: str = "estacoes") -> dict[str, Any]:
        require_local(request)
        return await guarded(lambda: data.satellites(group))

    @router.post("/api/godseye/command")
    async def command(payload: Any = Body(default=None), x_kira_internal: str | None = Header(default=None, alias="X-Kira-Internal")) -> dict[str, Any]:
        expected = str(token_provider() or "")
        if not expected or not x_kira_internal or not hmac.compare_digest(str(x_kira_internal), expected):
            raise HTTPException(status_code=403, detail="acao interna nao autorizada")
        body = payload if isinstance(payload, dict) else {}
        place = None
        if body.get("place"):
            try:
                place = await data.geocode(str(body["place"]))
            except (httpx.HTTPError, ValueError):
                place = None
            if place is None:
                return {"ok": False, "summary": f"Não achei o lugar '{_text(body['place'], 60)}' no mapa."}
        cmd = build_command(body, place)
        try:
            result = await globe("command", cmd)
        except RuntimeError as exc:  # janela ainda não pronta ou fechada
            logger.info("GODS_EYE_UI_UNAVAILABLE reason=%s", exc)
            result = None
        ok = isinstance(result, dict) and bool(result.get("ok"))
        where = f" em {cmd['focus']['label']}" if "focus" in cmd else ""
        if not ok:
            return {"ok": False, "summary": "O globo não respondeu (a janela da Kira está aberta?)."}
        if cmd["action"] == "fechar":
            return {"ok": True, "summary": "Olho de Deus fechado."}
        focus = result.get("focus") if isinstance(result.get("focus"), dict) else {}
        lat, lon = _num(focus.get("lat")), _num(focus.get("lon"))
        if result.get("mode") == "lugar" and lat is not None and lon is not None:
            card = await data.place_card(lat, lon)
            try:
                await globe("showPlace", card)
            except RuntimeError as exc:
                logger.info("GODS_EYE_PLACE_UI_UNAVAILABLE reason=%s", exc)
            return {"ok": True, "summary": describe_place(card)}
        if result.get("mode") == "3d":
            return {"ok": True, "summary": f"Visão 3D{where}: prédios em volume sobre a foto de satélite, a câmera gira em volta do "
                                           "ponto (o pino roxo). Esc ou 'volta pro mapa' sai do 3D."}
        layers = [str(key) for key in (result.get("layers") or []) if isinstance(key, str)]
        counts = await count_layers(data, layers, focus, live)
        if result.get("map"):
            summary = f"Mapa de satélite{where} aberto (dá para aproximar até as ruas)."
            if where:
                summary += " O lugar está marcado com um pino roxo, com o nome, no centro do mapa."
        else:
            summary = f"Olho de Deus: {', '.join(layers) or 'globo'}{where}."
        return {"ok": True, "summary": " ".join(filter(None, [summary, describe_counts(counts, focus)])), "counts": counts}

    return router


__all__ = ["GodsEyeData", "build_command", "count_layers", "spread_sample", "create_gods_eye_router", "describe_counts", "describe_place", "joined_variant",
           "parse_place", "parse_planes", "parse_quakes", "parse_tle", "zoom_for_rank"]
