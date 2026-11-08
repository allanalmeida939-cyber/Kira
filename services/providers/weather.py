"""Previsão do tempo pelo Open-Meteo (sem chave): geocodificação + previsão, validadas e normalizadas.

Nada aqui é inventado: campo ausente na resposta fica ausente (None) e resposta incoerente vira erro tipado.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
SOURCE = {"name": "Open-Meteo", "url": "https://open-meteo.com/"}
TIMEOUT = httpx.Timeout(6.0, connect=3.0)
CACHE_SECONDS = 600.0
CACHE_MAX = 32
USER_AGENT = "KiraDesktopAssistant/1.0 (local desktop app)"
CONDITIONS = {
    0: "céu limpo", 1: "predomínio de sol", 2: "parcialmente nublado", 3: "nublado", 45: "neblina", 48: "neblina com geada",
    51: "garoa fraca", 53: "garoa", 55: "garoa forte", 61: "chuva fraca", 63: "chuva", 65: "chuva forte", 66: "chuva congelante",
    67: "chuva congelante forte", 71: "neve fraca", 73: "neve", 75: "neve forte", 80: "pancadas de chuva fracas", 81: "pancadas de chuva",
    82: "pancadas de chuva fortes", 95: "trovoadas", 96: "trovoadas com granizo", 99: "trovoadas fortes com granizo",
}
PERIODS = (("madrugada", 0, 6), ("manhã", 6, 12), ("tarde", 12, 18), ("noite", 18, 24))


class WeatherError(ValueError):
    """Erro tipado; a mensagem é segura para a Kira repetir ao usuário."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Location:
    name: str
    region: str
    country: str
    latitude: float
    longitude: float
    timezone: str


@dataclass
class DayForecast:
    date: str
    condition: str | None
    code: int | None
    min: float | None
    max: float | None
    precipitationProbability: int | None
    periods: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Weather:
    location: Location
    observedAt: str | None
    updatedAt: str
    current: dict[str, Any]
    forecast: list[DayForecast]
    source: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def condition_of(code: object) -> str | None:
    return CONDITIONS.get(code) if isinstance(code, int) else None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _hour(stamp: str) -> int:
    digits = stamp[11:13]
    return int(digits) if digits.isdigit() else -1


def _series(block: dict[str, Any], key: str, size: int) -> list[Any]:
    values = block.get(key)
    if not isinstance(values, list) or len(values) != size:
        raise WeatherError("resposta_invalida", "a previsão veio incompleta")
    return values


def parse_forecast(location: Location, payload: dict[str, Any], *, now: float | None = None) -> Weather:
    """Valida e normaliza a resposta do Open-Meteo."""
    if not isinstance(payload, dict):
        raise WeatherError("resposta_invalida", "a previsão veio em formato inesperado")
    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    daily = payload.get("daily") if isinstance(payload.get("daily"), dict) else {}
    hourly = payload.get("hourly") if isinstance(payload.get("hourly"), dict) else {}
    days = daily.get("time")
    if not isinstance(days, list) or not days:
        raise WeatherError("resposta_invalida", "a previsão veio sem os próximos dias")
    size = len(days)
    codes = _series(daily, "weather_code", size)
    highs = _series(daily, "temperature_2m_max", size)
    lows = _series(daily, "temperature_2m_min", size)
    rain = daily.get("precipitation_probability_max") if isinstance(daily.get("precipitation_probability_max"), list) and len(daily["precipitation_probability_max"]) == size else [None] * size
    hours = hourly.get("time") if isinstance(hourly.get("time"), list) else []
    hour_rain = hourly.get("precipitation_probability") if isinstance(hourly.get("precipitation_probability"), list) and len(hourly.get("precipitation_probability")) == len(hours) else []
    hour_temp = hourly.get("temperature_2m") if isinstance(hourly.get("temperature_2m"), list) and len(hourly.get("temperature_2m")) == len(hours) else []
    hour_code = hourly.get("weather_code") if isinstance(hourly.get("weather_code"), list) and len(hourly.get("weather_code")) == len(hours) else []
    forecast: list[DayForecast] = []
    for index, day in enumerate(days):
        periods = []
        for label, start, end in PERIODS:
            picks = [i for i, stamp in enumerate(hours) if isinstance(stamp, str) and stamp.startswith(str(day)) and start <= _hour(stamp) < end]
            if not picks or not hour_temp:
                continue
            temps = [_number(hour_temp[i]) for i in picks if _number(hour_temp[i]) is not None]
            chances = [int(hour_rain[i]) for i in picks if hour_rain and isinstance(hour_rain[i], (int, float))]
            middle = picks[len(picks) // 2]
            code = hour_code[middle] if hour_code and isinstance(hour_code[middle], int) else None
            periods.append({
                "period": label,
                "temperature": round(sum(temps) / len(temps), 1) if temps else None,
                "precipitationProbability": max(chances) if chances else None,
                "condition": condition_of(code),
                "code": code,
            })
        code = codes[index] if isinstance(codes[index], int) else None
        forecast.append(DayForecast(
            date=str(day), condition=condition_of(code), code=code, min=_number(lows[index]), max=_number(highs[index]),
            precipitationProbability=int(rain[index]) if isinstance(rain[index], (int, float)) else None, periods=periods,
        ))
    current_code = current.get("weather_code") if isinstance(current.get("weather_code"), int) else None
    normalized_current = {
        "temperature": _number(current.get("temperature_2m")),
        "apparentTemperature": _number(current.get("apparent_temperature")),
        "humidity": _number(current.get("relative_humidity_2m")),
        "windSpeed": _number(current.get("wind_speed_10m")),
        "condition": condition_of(current_code),
        "code": current_code,
    }
    stamp = now if now is not None else time.time()
    return Weather(
        location=location,
        observedAt=str(current.get("time")) if current.get("time") else None,
        updatedAt=datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds"),
        current=normalized_current,
        forecast=forecast,
        source=dict(SOURCE),
    )


class WeatherProvider:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, clock=time.monotonic) -> None:
        self._transport = transport
        self._clock = clock
        self._cache: dict[str, tuple[float, Weather]] = {}
        self._places: dict[str, Location] = {}
        self._lock = asyncio.Semaphore(3)
        self._http: httpx.AsyncClient | None = None
        self._http_loop: asyncio.AbstractEventLoop | None = None

    def _client(self) -> httpx.AsyncClient:
        # Conexão reaproveitada: abrir cliente novo a cada chamada custava DNS + TLS (~1,4 s por pedido).
        loop = asyncio.get_running_loop()
        if self._http is None or self._http.is_closed or self._http_loop is not loop:
            self._http = httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, transport=self._transport)
            self._http_loop = loop
        return self._http

    async def _get(self, url: str, **params: object) -> dict[str, Any]:
        async with self._lock:
            try:
                response = await self._client().get(url, params=params)
            except httpx.HTTPError as exc:
                raise WeatherError("indisponivel", "o serviço de previsão não respondeu") from exc
        if response.status_code != 200:
            raise WeatherError("indisponivel", "o serviço de previsão não respondeu")
        try:
            return response.json()
        except ValueError as exc:
            raise WeatherError("resposta_invalida", "a previsão veio em formato inesperado") from exc

    async def locate(self, city: str) -> Location:
        name = " ".join(str(city or "").split())[:80]
        if len(name) < 2:
            raise WeatherError("sem_cidade", "não sei de qual cidade")
        known = self._places.get(name.lower())
        if known:
            return known
        data = await self._get(GEOCODE_URL, name=name, count=1, language="pt", format="json")
        place = (data.get("results") or [None])[0] if isinstance(data, dict) else None
        if not isinstance(place, dict) or _number(place.get("latitude")) is None or _number(place.get("longitude")) is None:
            raise WeatherError("cidade_nao_encontrada", f"não encontrei a cidade {name}")
        location = Location(
            name=str(place.get("name") or name), region=str(place.get("admin1") or ""), country=str(place.get("country") or ""),
            latitude=float(place["latitude"]), longitude=float(place["longitude"]), timezone=str(place.get("timezone") or "auto"),
        )
        self._places[name.lower()] = location
        while len(self._places) > CACHE_MAX:
            self._places.pop(next(iter(self._places)))
        return location

    async def forecast(self, city: str) -> Weather:
        key = " ".join(str(city or "").lower().split())
        hit = self._cache.get(key)
        if hit and self._clock() - hit[0] < CACHE_SECONDS:
            return hit[1]
        location = await self.locate(city)
        payload = await self._get(
            FORECAST_URL, latitude=location.latitude, longitude=location.longitude, timezone=location.timezone, forecast_days=6,
            current="temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
            hourly="temperature_2m,precipitation_probability,weather_code",
            daily="weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        )
        weather = parse_forecast(location, payload)
        self._cache[key] = (self._clock(), weather)
        while len(self._cache) > CACHE_MAX:
            self._cache.pop(next(iter(self._cache)))
        return weather


__all__ = ["DayForecast", "Location", "Weather", "WeatherError", "WeatherProvider", "condition_of", "parse_forecast"]
