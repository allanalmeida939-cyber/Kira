"""Céu, chuva e aviões perto do Chefe (Olho de Deus ampliado). Fontes abertas, sem chave:

- ISS, satélites, Starlink e lixo espacial: TLEs do CelesTrak + Skyfield (posição agora e próximas passagens).
- Chuva nos próximos minutos: Open-Meteo de 15 em 15 minutos + radar do RainViewer sobre um mapa escuro.
- Avião passando perto: adsb.lol (a mesma fonte do globo), rota pelo adsbdb.com, OpenSky de reserva.
- Foto de satélite: NASA GIBS (a do dia, com as nuvens) e EOX Sentinel-2 sem nuvens (detalhe de 10 m).

Tudo vira números curtos para a Kira falar e um cartão na tela. O que vem de fora é dado, nunca instrução.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

logger = logging.getLogger("kira.app.sky")

TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
RAINVIEWER_URL = "https://api.rainviewer.com/public/weather-maps.json"
BASEMAP_URL = "https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
PLANES_URL = "https://api.adsb.lol/v2/point/{lat:.3f}/{lon:.3f}/{nm}"
ROUTE_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
OPENSKY_URL = "https://opensky-network.org/api/states/all"
GIBS_URL = ("https://wvs.earthdata.nasa.gov/api/v1/snapshot?REQUEST=GetSnapshot&TIME={day}&BBOX={south:.4f},{west:.4f},{north:.4f},{east:.4f}"
            "&CRS=EPSG:4326&LAYERS=VIIRS_SNPP_CorrectedReflectance_TrueColor,Coastlines_15m&WRAP=day,x&FORMAT=image/jpeg&WIDTH=640&HEIGHT=640")
EOX_URL = ("https://tiles.maps.eox.at/wms?service=WMS&request=GetMap&version=1.1.1&layers=s2cloudless-2024&styles=&srs=EPSG:4326"
           "&bbox={west:.5f},{south:.5f},{east:.5f},{north:.5f}&width=640&height=640&format=image/jpeg")
EPHEMERIS = "de421.bsp"  # Sol e Terra para saber se a ISS está iluminada e se já escureceu (17 MB, baixado uma vez)
USER_AGENT = "KiraAssistente/1.0 (uso pessoal)"
# CelesTrak pede no máximo um download por grupo a cada 2 horas
TLE_TTL_S = 6 * 3600.0
GROUPS = {
    "estacoes": ("stations",),
    "brilhantes": ("visual",),
    "ativos": ("active",),
    "starlink": ("starlink",),
    "lixo": ("cosmos-2251-debris", "iridium-33-debris", "fengyun-1c-debris", "cosmos-1408-debris"),
}
PROXY = "/api/ui/img?u="
IMAGE_DIR_NAME = "sky"
MAX_IMAGES = 30
COMPASS = ("norte", "nordeste", "leste", "sudeste", "sul", "sudoeste", "oeste", "noroeste")
ACTIONS = ("iss", "satelites", "starlink", "lixo", "chuva", "aviao", "foto")


def _text(value: object, limit: int = 80) -> str:
    return " ".join(str(value or "").split())[:limit]


def compass(azimuth_deg: float) -> str:
    return COMPASS[int(((azimuth_deg % 360) + 22.5) // 45) % 8]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def parse_tle(text: str) -> list[tuple[str, str, str]]:
    lines = [line.rstrip() for line in str(text or "").splitlines() if line.strip()]
    out = []
    for index in range(0, len(lines) - 2, 3):
        name, line1, line2 = lines[index], lines[index + 1], lines[index + 2]
        if line1.startswith("1 ") and line2.startswith("2 "):
            out.append((name.strip(), line1, line2))
    return out


def tile_of(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Posição (fracionária) no esquema de ladrilhos do OpenStreetMap."""
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(max(-85.0511, min(85.0511, lat)))
    y = (1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n
    return x, y


def rain_words(mm_per_15min: float) -> str:
    rate = mm_per_15min * 4  # mm por hora
    if rate <= 0.05:
        return "sem chuva"
    if rate < 2.5:
        return "chuva fraca"
    if rate < 10:
        return "chuva moderada"
    return "chuva forte"


def summarize_rain(minutely: dict[str, Any], hourly_probability: list[Any]) -> dict[str, Any]:
    """Open-Meteo de 15 em 15 minutos -> o que a Kira precisa falar."""
    values = [float(v or 0.0) for v in (minutely.get("precipitation") or [])][:8]
    times = [str(t) for t in (minutely.get("time") or [])][:8]
    if not values:
        return {"ok": False}
    next_rain = next((index for index, value in enumerate(values) if value > 0.05), None)
    facts = {
        "ok": True,
        "agora": rain_words(values[0]),
        "proximos_15_min_mm": round(values[1] if len(values) > 1 else values[0], 1),
        "proxima_hora_mm": round(sum(values[:4]), 1),
        "proximas_2_horas_mm": round(sum(values), 1),
        "chuva_comeca_em_min": None if next_rain is None else next_rain * 15,
        "probabilidade_nesta_hora_pct": hourly_probability[0] if hourly_probability else None,
        "faixas": [{"hora": t[-5:], "mm": round(v, 1), "como": rain_words(v)} for t, v in zip(times, values)],
    }
    return facts


@dataclass
class Place:
    lat: float
    lon: float
    label: str


class SkyWatch:
    def __init__(self, cache_dir: Path, *, location_city: Callable[[], str | None] = lambda: None,
                 fetch: Callable[..., Awaitable[httpx.Response]] | None = None, clock: Callable[[], float] = time.time) -> None:
        self._dir = Path(cache_dir)
        self._images = self._dir / "imagens"
        self._city = location_city
        self._fetch = fetch or self._http_get
        self._clock = clock
        self._tles: dict[str, tuple[float, list[tuple[str, str, str]]]] = {}
        self._sats: dict[str, tuple[int, list[Any]]] = {}
        self._eph: Any = None
        self._eph_failed_at = 0.0
        self._lock = asyncio.Lock()

    # ---------- fontes ----------
    @staticmethod
    async def _http_get(url: str, **params: Any) -> httpx.Response:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=6.0), headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
            response = await client.get(url, params=params or None)
            response.raise_for_status()
            return response

    async def locate(self, place: str = "") -> Place | None:
        name = _text(place) or (self._city() or "")
        if not name:
            return None
        query = name.split(",")[0].strip()
        data = (await self._fetch(GEOCODE_URL, name=query, count=1, language="pt", format="json")).json()
        results = data.get("results") if isinstance(data, dict) else None
        if not results:
            return None
        first = results[0]
        label = ", ".join(p for p in (_text(first.get("name"), 60), _text(first.get("admin1"), 40)) if p)
        return Place(float(first["latitude"]), float(first["longitude"]), label or name)

    async def tles(self, key: str) -> list[tuple[str, str, str]]:
        """TLEs do grupo, com cache no disco (CelesTrak pede pouco download)."""
        now = self._clock()
        cached = self._tles.get(key)
        if cached and now - cached[0] < TLE_TTL_S:
            return cached[1]
        path = self._dir / f"tle_{key}.txt"
        try:
            if path.exists() and now - path.stat().st_mtime < TLE_TTL_S:
                items = parse_tle(path.read_text(encoding="utf-8"))
                self._tles[key] = (path.stat().st_mtime, items)
                return items
        except OSError:
            pass
        texts = []
        try:
            for group in GROUPS[key]:
                texts.append((await self._fetch(TLE_URL.format(group=group))).text)
        except httpx.HTTPError:
            # CelesTrak recusa (403) baixar o mesmo grupo de novo em menos de 2 horas: vale o arquivo velho
            if path.exists():
                items = parse_tle(path.read_text(encoding="utf-8"))
                self._tles[key] = (now, items)
                return items
            raise
        items = [item for text in texts for item in parse_tle(text)]
        if items:
            self._dir.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join("\n".join(item) for item in items), encoding="utf-8")
        self._tles[key] = (now, items)
        return items

    def _satellites(self, key: str, items: list[tuple[str, str, str]]) -> list[Any]:
        from skyfield.api import EarthSatellite, load

        version = hash(tuple(item[1] for item in items[:50])) ^ len(items)
        cached = self._sats.get(key)
        if cached and cached[0] == version:
            return cached[1]
        ts = load.timescale(builtin=True)
        sats = []
        for name, line1, line2 in items:
            try:
                sats.append(EarthSatellite(line1, line2, name, ts))
            except ValueError:
                continue
        self._sats[key] = (version, sats)
        return sats

    def _ephemeris(self) -> Any:
        """Efemérides do Sol (baixadas uma vez). Sem elas, as passagens saem sem dizer se dá para ver."""
        if self._eph is not None:
            return self._eph
        if self._clock() - self._eph_failed_at < 3600:
            return None
        try:
            from skyfield.api import Loader

            self._dir.mkdir(parents=True, exist_ok=True)
            self._eph = Loader(str(self._dir), verbose=False)(EPHEMERIS)
        except Exception as exc:  # sem internet ou download interrompido
            logger.info("SKY_EPHEMERIS_UNAVAILABLE reason=%s", type(exc).__name__)
            self._eph_failed_at = self._clock()
            return None
        return self._eph

    # ---------- cálculos ----------
    def _passes(self, sat: Any, place: Place, hours: float = 48.0, limit: int = 5) -> list[dict[str, Any]]:
        from skyfield.api import load, wgs84

        ts = load.timescale(builtin=True)
        observer = wgs84.latlon(place.lat, place.lon)
        t0 = ts.now()
        t1 = ts.tt_jd(t0.tt + hours / 24.0)
        times, events = sat.find_events(observer, t0, t1, altitude_degrees=10.0)
        eph = self._ephemeris()
        passes: list[dict[str, Any]] = []
        current: dict[str, Any] = {}
        for t, event in zip(times, events):
            alt, az, _ = (sat - observer).at(t).altaz()
            if event == 0:
                current = {"inicio": t, "de": compass(az.degrees)}
            elif event == 1 and current:
                current.update({"pico": t, "altura_max_graus": int(round(float(alt.degrees))), "pico_az": float(az.degrees)})
            elif event == 2 and current.get("pico") is not None:
                current.update({"fim": t, "para": compass(az.degrees)})
                visible = None
                if eph is not None:
                    sun_alt = (eph["earth"] + observer).at(current["pico"]).observe(eph["sun"]).apparent().altaz()[0].degrees
                    visible = bool(sat.at(current["pico"]).is_sunlit(eph)) and bool(sun_alt < -6)
                start = current["inicio"].utc_datetime().astimezone()
                passes.append({
                    "quando": start.strftime("%d/%m %H:%M"),
                    "inicio_iso": start.isoformat(timespec="minutes"),
                    "minutos": max(1, round((current["fim"].tt - current["inicio"].tt) * 24 * 60)),
                    "altura_max_graus": current["altura_max_graus"],
                    "de": current["de"],
                    "para": current["para"],
                    "visivel_a_olho_nu": visible,
                })
                current = {}
                if len(passes) >= limit:
                    break
        return passes

    def _above(self, sats: list[Any], place: Place, min_alt: float = 0.0) -> tuple[int, list[tuple[str, float]]]:
        from skyfield.api import load, wgs84

        ts = load.timescale(builtin=True)
        observer = wgs84.latlon(place.lat, place.lon)
        now = ts.now()
        count, named = 0, []
        for sat in sats:
            try:
                alt = float((sat - observer).at(now).altaz()[0].degrees)
            except Exception:
                continue
            if alt > min_alt:
                count += 1
                named.append((sat.name, alt))
        named.sort(key=lambda item: -item[1])
        return count, named

    # ---------- perguntas ----------
    async def iss(self, place: Place) -> dict[str, Any]:
        items = [item for item in await self.tles("estacoes") if item[0].startswith("ISS (ZARYA)")]
        if not items:
            return {"ok": False, "erro": "sem dados da ISS agora"}
        sat = (await asyncio.to_thread(self._satellites, "iss", items))[0]
        passes = await asyncio.to_thread(self._passes, sat, place)
        above = await asyncio.to_thread(self._above, [sat], place)
        return {"ok": True, "lugar": place.label, "iss_acima_do_horizonte_agora": bool(above[0]), "proximas_passagens": passes}

    async def satellites(self, place: Place, key: str) -> dict[str, Any]:
        items = await self.tles(key)
        sats = await asyncio.to_thread(self._satellites, key, items)
        count, named = await asyncio.to_thread(self._above, sats, place)
        high = sum(1 for _, alt in named if alt >= 30)
        facts = {"ok": True, "lugar": place.label, "grupo": key, "rastreados_no_grupo": len(sats), "acima_do_horizonte_agora": count,
                 "acima_de_30_graus": high, "mais_altos": [{"nome": _text(name, 40), "altura_graus": round(alt)} for name, alt in named[:6]]}
        if key == "brilhantes":
            facts["observacao"] = "satélites mais brilhantes (os que dá para ver a olho nu, de madrugada ou ao anoitecer)"
        return facts

    async def rain(self, place: Place) -> dict[str, Any]:
        forecast = (await self._fetch(FORECAST_URL, latitude=f"{place.lat:.4f}", longitude=f"{place.lon:.4f}", minutely_15="precipitation,weather_code",
                                      forecast_minutely_15=8, hourly="precipitation_probability", forecast_hours=3, timezone="auto")).json()
        facts = summarize_rain(forecast.get("minutely_15") or {}, (forecast.get("hourly") or {}).get("precipitation_probability") or [])
        facts["lugar"] = place.label
        facts["radar"] = {"pendente": True}  # montado depois (18 imagens): a resposta falada não espera
        return facts

    async def radar_image(self, place: Place, zoom: int = 7) -> dict[str, Any]:
        """Último quadro do radar (RainViewer) sobre um mapa escuro, 3x3 ladrilhos com o lugar marcado."""
        maps = (await self._fetch(RAINVIEWER_URL)).json()
        frames = (maps.get("radar") or {}).get("past") or []
        if not frames:
            return {"ok": False}
        frame = frames[-1]
        host = str(maps.get("host") or "https://tilecache.rainviewer.com")
        fx, fy = tile_of(place.lat, place.lon, zoom)
        cx, cy = int(fx), int(fy)
        spots = [(dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
        urls = [BASEMAP_URL.format(z=zoom, x=cx + dx, y=cy + dy) for dx, dy in spots]
        urls += [f"{host}{frame['path']}/256/{zoom}/{cx + dx}/{cy + dy}/2/1_1.png" for dx, dy in spots]
        tiles = [response.content for response in await asyncio.gather(*(self._fetch(url) for url in urls))]
        name = f"radar-{uuid.uuid4().hex[:10]}.png"
        radar_pixels = await asyncio.to_thread(self._compose_radar, tiles, spots, (fx - cx + 1) * 256, (fy - cy + 1) * 256, name)
        captured = datetime.fromtimestamp(int(frame.get("time") or 0), tz=timezone.utc).astimezone()
        return {"ok": True, "url": f"/api/sky/image/{name}", "hora": captured.strftime("%H:%M"), "tem_chuva_no_mapa": radar_pixels > 50}

    def _compose_radar(self, tiles: list[bytes], spots: list[tuple[int, int]], px: float, py: float, name: str) -> int:
        from PIL import Image, ImageDraw

        canvas = Image.new("RGBA", (768, 768), (8, 6, 18, 255))
        radar_pixels = 0
        half = len(spots)
        for index, (dx, dy) in enumerate(spots):
            where = ((dx + 1) * 256, (dy + 1) * 256)
            canvas.alpha_composite(Image.open(io.BytesIO(tiles[index])).convert("RGBA"), where)
            layer = Image.open(io.BytesIO(tiles[half + index])).convert("RGBA")
            radar_pixels += sum(layer.getchannel("A").histogram()[41:])  # pontos com chuva desenhada
            canvas.alpha_composite(layer, where)
        draw = ImageDraw.Draw(canvas)
        draw.ellipse((px - 7, py - 7, px + 7, py + 7), outline=(240, 171, 252, 255), width=3)
        draw.ellipse((px - 2, py - 2, px + 2, py + 2), fill=(255, 255, 255, 255))
        self._save_image(canvas.convert("RGB"), name)
        return radar_pixels

    def _save_image(self, image: Any, name: str) -> None:
        self._images.mkdir(parents=True, exist_ok=True)
        image.save(self._images / name, "PNG", optimize=True)
        files = sorted(self._images.glob("*.png"), key=lambda p: p.stat().st_mtime)
        for old in files[:-MAX_IMAGES]:
            try:
                old.unlink()
            except OSError:
                pass

    def image_path(self, name: str) -> Path | None:
        if not re.fullmatch(r"radar-[0-9a-f]{10}\.png", name or ""):
            return None
        path = self._images / name
        return path if path.is_file() else None

    async def planes(self, place: Place, radius_km: float = 40.0) -> dict[str, Any]:
        nm = max(5, min(100, round(radius_km / 1.852)))
        source = "adsb.lol"
        planes: list[dict[str, Any]] = []
        try:
            data = (await self._fetch(PLANES_URL.format(lat=place.lat, lon=place.lon, nm=nm))).json()
            for ac in data.get("ac") or []:
                lat, lon = ac.get("lat"), ac.get("lon")
                if lat is None or lon is None or ac.get("alt_baro") == "ground":
                    continue
                planes.append({"voo": _text(ac.get("flight"), 10), "modelo": _text(ac.get("t"), 10), "matricula": _text(ac.get("r"), 10),
                               "lat": float(lat), "lon": float(lon), "alt_m": round(float(ac.get("alt_baro") or 0) * 0.3048),
                               "vel_kmh": round(float(ac.get("gs") or 0) * 1.852), "rumo": float(ac.get("track") or 0)})
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("SKY_ADSB_FAILED reason=%s", type(exc).__name__)
            source = "OpenSky"
            pad = radius_km / 111.0
            data = (await self._fetch(OPENSKY_URL, lamin=f"{place.lat - pad:.3f}", lomin=f"{place.lon - pad:.3f}",
                                      lamax=f"{place.lat + pad:.3f}", lomax=f"{place.lon + pad:.3f}")).json()
            for state in data.get("states") or []:
                if state[5] is None or state[6] is None or state[8]:
                    continue
                planes.append({"voo": _text(state[1], 10), "modelo": "", "matricula": "", "lat": float(state[6]), "lon": float(state[5]),
                               "alt_m": round(float(state[7] or 0)), "vel_kmh": round(float(state[9] or 0) * 3.6), "rumo": float(state[10] or 0)})
        for plane in planes:
            plane["dist_km"] = round(haversine_km(place.lat, place.lon, plane["lat"], plane["lon"]), 1)
            plane["direcao"] = compass(bearing_deg(place.lat, place.lon, plane["lat"], plane["lon"]))
            plane["indo_para"] = compass(plane["rumo"])
        planes.sort(key=lambda p: p["dist_km"])
        for plane in planes[:3]:
            plane.update(await self.route(plane["voo"]))
        nearest = planes[0] if planes else None
        return {"ok": True, "lugar": place.label, "fonte": source, "raio_km": radius_km, "quantos": len(planes),
                "mais_perto": nearest, "outros": planes[1:6]}

    async def route(self, callsign: str) -> dict[str, Any]:
        code = re.sub(r"[^A-Z0-9]", "", str(callsign or "").upper())
        if not code:
            return {}
        try:
            data = (await self._fetch(ROUTE_URL.format(callsign=code))).json()
        except (httpx.HTTPError, ValueError):
            return {}
        route = (data.get("response") or {}).get("flightroute") if isinstance(data.get("response"), dict) else None
        if not isinstance(route, dict):
            return {}

        def where(airport: object) -> str:
            if not isinstance(airport, dict):
                return ""
            return ", ".join(p for p in (_text(airport.get("municipality"), 40), _text(airport.get("iata_code"), 4)) if p)

        return {"companhia": _text((route.get("airline") or {}).get("name"), 40), "de": where(route.get("origin")), "para": where(route.get("destination"))}

    async def photo(self, place: Place) -> dict[str, Any]:
        """Foto do dia (nuvens de hoje) e detalhe sem nuvens; as imagens passam pelo proxy local da Kira."""
        now = datetime.now(timezone.utc)
        day = (now - timedelta(days=0 if now.hour >= 18 else 1)).strftime("%Y-%m-%d")
        wide = 0.9
        near_lat = 0.12
        near_lon = 0.12 / max(0.2, math.cos(math.radians(place.lat)))
        gibs = GIBS_URL.format(day=day, south=place.lat - wide, west=place.lon - wide, north=place.lat + wide, east=place.lon + wide)
        eox = EOX_URL.format(west=place.lon - near_lon, south=place.lat - near_lat, east=place.lon + near_lon, north=place.lat + near_lat)
        return {"ok": True, "lugar": place.label, "dia_da_foto_do_dia": day,
                "imagens": [{"imageUrl": PROXY + quote(gibs, safe=""), "caption": f"{place.label} visto do satélite VIIRS em {day} (nuvens do dia) · NASA GIBS"},
                            {"imageUrl": PROXY + quote(eox, safe=""), "caption": f"{place.label} sem nuvens, detalhe de 10 m · Sentinel-2 cloudless 2024 (EOX)"}]}

    async def ask(self, action: str, place_name: str = "") -> dict[str, Any]:
        action = action if action in ACTIONS else "satelites"
        try:
            place = await self.locate(place_name)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.info("SKY_GEOCODE_FAILED reason=%s", type(exc).__name__)
            place = None
        if place is None:
            return {"ok": False, "acao": action, "erro": "sem lugar: diga a cidade (ou salve a cidade na aba Kira das Configurações)"}
        try:
            if action == "iss":
                facts = await self.iss(place)
            elif action in {"satelites", "starlink", "lixo"}:
                facts = await self.satellites(place, {"satelites": "brilhantes"}.get(action, action))
            elif action == "chuva":
                facts = await self.rain(place)
            elif action == "aviao":
                facts = await self.planes(place)
            else:
                facts = await self.photo(place)
        except (httpx.HTTPError, ValueError, OSError) as exc:
            logger.info("SKY_SOURCE_FAILED action=%s reason=%s", action, type(exc).__name__)
            return {"ok": False, "acao": action, "lugar": place.label, "erro": "a fonte não respondeu agora"}
        facts["acao"] = action
        facts["lat"], facts["lon"] = round(place.lat, 4), round(place.lon, 4)
        return facts


# ---------- cartão e fala ----------
TITLES = {"iss": "Estação Espacial sobre {lugar}", "satelites": "Satélites sobre {lugar}", "starlink": "Starlink sobre {lugar}",
          "lixo": "Lixo espacial sobre {lugar}", "chuva": "Chuva em {lugar}", "aviao": "Aviões perto de {lugar}", "foto": "{lugar} visto do espaço"}


def build_presentation(facts: dict[str, Any], request_id: str) -> dict[str, Any] | None:
    action = facts.get("acao")
    if not facts.get("ok") or action not in TITLES:
        return None
    sections: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    if action == "iss":
        passes = facts.get("proximas_passagens") or []
        events = [{"date": p["quando"], "label": f"{p['minutos']} min, até {p['altura_max_graus']}° de altura, de {p['de']} para {p['para']}"
                   + {True: " · dá para ver", False: " · não dá para ver (dia ou sombra)", None: ""}[p.get("visivel_a_olho_nu")], "order": i}
                  for i, p in enumerate(passes)]
        sections.append({"id": "agora", "type": "text", "heading": "Agora", "status": "ready",
                         "data": {"body": "A ISS está acima do horizonte agora." if facts.get("iss_acima_do_horizonte_agora") else "A ISS não está sobre você agora."}})
        sections.append({"id": "passagens", "type": "timeline", "heading": "Próximas passagens", "status": "ready" if events else "unavailable",
                         "data": {"events": events}, **({} if events else {"reason": "Nenhuma passagem acima de 10° nas próximas 48 horas."})})
        sources.append({"id": "celestrak", "name": "CelesTrak (órbitas)", "url": "https://celestrak.org"})
    elif action in {"satelites", "starlink", "lixo"}:
        points = [f"{s['nome']}: {s['altura_graus']}° de altura" for s in facts.get("mais_altos") or []]
        body = (f"{facts['acima_do_horizonte_agora']} de {facts['rastreados_no_grupo']} estão acima do horizonte agora "
                f"({facts['acima_de_30_graus']} acima de 30°).")
        sections.append({"id": "agora", "type": "text", "heading": "Agora", "status": "ready", "data": {"body": body, "points": points}})
        sources.append({"id": "celestrak", "name": "CelesTrak (órbitas)", "url": "https://celestrak.org"})
    elif action == "chuva":
        bits = [f"Agora: {facts.get('agora')}.", f"Próxima hora: {facts.get('proxima_hora_mm')} mm."]
        if facts.get("chuva_comeca_em_min") is not None:
            bits.append(f"Chuva prevista em {facts['chuva_comeca_em_min']} min.")
        if facts.get("probabilidade_nesta_hora_pct") is not None:
            bits.append(f"Chance nesta hora: {facts['probabilidade_nesta_hora_pct']}%.")
        sections.append({"id": "agora", "type": "text", "heading": "Próximos minutos", "status": "ready",
                         "data": {"body": " ".join(bits), "points": [f"{f['hora']}: {f['como']} ({f['mm']} mm)" for f in facts.get("faixas") or []]}})
        sections.append(radar_section(facts.get("radar") or {}))
        sources += [{"id": "openmeteo", "name": "Open-Meteo", "url": "https://open-meteo.com"}, {"id": "rainviewer", "name": "RainViewer", "url": "https://www.rainviewer.com"}]
    elif action == "aviao":
        nearest = facts.get("mais_perto")
        if nearest:
            route = " → ".join(p for p in (nearest.get("de"), nearest.get("para")) if p)
            body = (f"{nearest.get('voo') or 'Sem identificação'}{' · ' + nearest['companhia'] if nearest.get('companhia') else ''}"
                    f"{' · ' + route if route else ''}. A {nearest['dist_km']} km ao {nearest['direcao']}, {nearest['alt_m']} m de altitude, "
                    f"{nearest['vel_kmh']} km/h indo para {nearest['indo_para']}.")
        else:
            body = "Nenhum avião com transmissor aberto perto agora (a cobertura dos radares abertos é falha em algumas regiões)."
        points = [f"{p.get('voo') or '?'}{' (' + p['de'] + ' → ' + p['para'] + ')' if p.get('de') and p.get('para') else ''}: {p['dist_km']} km, {p['alt_m']} m"
                  for p in facts.get("outros") or []]
        sections.append({"id": "perto", "type": "text", "heading": "Mais perto agora", "status": "ready", "data": {"body": body, "points": points}})
        sources += [{"id": "adsb", "name": "adsb.lol", "url": "https://adsb.lol"}, {"id": "adsbdb", "name": "adsbdb (rotas)", "url": "https://www.adsbdb.com"}]
    elif action == "foto":
        sections.append({"id": "fotos", "type": "image_gallery", "heading": "Do espaço", "status": "ready", "data": {"items": facts.get("imagens") or []}})
        sources += [{"id": "gibs", "name": "NASA GIBS", "url": "https://www.earthdata.nasa.gov/engage/open-data-services-software/earthdata-developer-portal/gibs-api"},
                    {"id": "eox", "name": "EOX Sentinel-2 cloudless", "url": "https://s2maps.eu"}]
    return {"type": "present", "presentationId": "sky-kira", "requestId": request_id, "title": TITLES[action].format(lugar=facts.get("lugar") or "você")[:120],
            "status": "ready", "sections": sections, "sources": sources}


def radar_section(radar: dict[str, Any]) -> dict[str, Any]:
    """Seção do radar: pendente enquanto monta, pronta com a imagem, ou indisponível."""
    if radar.get("pendente"):
        return {"id": "radar", "type": "image_gallery", "heading": "Radar", "status": "pending"}
    if radar.get("ok"):
        return {"id": "radar", "type": "image_gallery", "heading": "Radar", "status": "ready",
                "data": {"items": [{"imageUrl": radar["url"], "caption": f"Radar às {radar['hora']} · RainViewer · mapa CARTO"}]}}
    return {"id": "radar", "type": "image_gallery", "heading": "Radar", "status": "unavailable", "reason": "O radar não respondeu agora."}


def narration(facts: dict[str, Any]) -> str:
    """Só os números, para a Kira contar com as palavras dela."""
    if not facts.get("ok"):
        return f"Não deu: {facts.get('erro') or 'a fonte não respondeu agora'}. Diga isso com naturalidade."
    keep = {k: v for k, v in facts.items() if k not in {"ok", "lat", "lon", "imagens", "radar", "faixas"}}
    # valores do Skyfield/numpy (bool_, float64) viram tipos comuns
    text = json.dumps(keep, ensure_ascii=False, default=lambda value: value.item() if hasattr(value, "item") else str(value))
    return "Dados para falar agora (curto, com os números; o cartão já está na tela): " + text[:1800]


__all__ = ["ACTIONS", "Place", "SkyWatch", "bearing_deg", "build_presentation", "compass", "haversine_km", "narration", "parse_tle", "radar_section",
           "rain_words", "summarize_rain", "tile_of"]
