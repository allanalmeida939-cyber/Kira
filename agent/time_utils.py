from __future__ import annotations

import logging
import re
from datetime import date, datetime, time as dt_time, timedelta, timezone, tzinfo

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python sem zoneinfo nativo
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment]


logger = logging.getLogger(__name__)

BRASILIA_TIMEZONE_NAME = "America/Fortaleza"
_BRASILIA_FALLBACK = timezone(timedelta(hours=-3), BRASILIA_TIMEZONE_NAME)
_TIME_REGEX = re.compile(
    r"\b(?:as|a|às)\s*(\d{1,2})(?::(\d{2})|h(\d{2})?|h)?\b",
    flags=re.IGNORECASE,
)
_BRASILIA_TZ: tzinfo | None = None
def get_brasilia_tz() -> tzinfo:
    global _BRASILIA_TZ
    if _BRASILIA_TZ is None:
        try:
            if ZoneInfo is None:
                raise RuntimeError("zoneinfo indisponivel")
            _BRASILIA_TZ = ZoneInfo(BRASILIA_TIMEZONE_NAME)
        except (ZoneInfoNotFoundError, RuntimeError):
            _BRASILIA_TZ = _BRASILIA_FALLBACK
    return _BRASILIA_TZ


def _current_brasilia() -> datetime:
    return datetime.now(get_brasilia_tz())


def now_brasilia() -> datetime:
    return _current_brasilia()


def today_brasilia() -> date:
    return _current_brasilia().date()


def ensure_brasilia(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    tz = get_brasilia_tz()
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)


def combine_date_time_brasilia(target_date: date, target_time: dt_time) -> datetime:
    return datetime.combine(target_date, target_time, tzinfo=get_brasilia_tz())


def to_utc_from_brasilia(value: datetime) -> datetime:
    ensured = ensure_brasilia(value)
    if ensured is None:
        raise ValueError("Datetime invalido para conversao UTC.")
    return ensured.astimezone(timezone.utc)


def format_brasilia(
    value: datetime | None,
    *,
    include_date: bool | None = None,
    include_seconds: bool = False,
) -> str:
    if value is None:
        return "sem horario"
    current = ensure_brasilia(value)
    if current is None:
        return "sem horario"
    if include_date is None:
        include_date = current.date() != today_brasilia()
    time_fmt = "%H:%M:%S" if include_seconds else "%H:%M"
    if include_date:
        return current.strftime(f"%d/%m/%Y {time_fmt}")
    return current.strftime(time_fmt)


def parse_relative_time_from_brasilia(
    text: str,
    *,
    now: datetime | None = None,
) -> datetime | None:
    _, remind_at = extract_time_reference_from_brasilia(text, now=now)
    return remind_at


def extract_time_reference_from_brasilia(
    text: str,
    *,
    now: datetime | None = None,
) -> tuple[str, datetime | None]:
    original = str(text or "")
    normalized = " ".join(original.strip().split())
    if not normalized:
        return "", None

    current_now = ensure_brasilia(now) or now_brasilia()

    relative_match = re.search(
        r"\b(?:em|daqui(?:\s+a)?)\s+(\d+)\s*(min|mins|minuto|minutos|h|hora|horas)\b",
        normalized,
        flags=re.IGNORECASE,
    )
    if relative_match is not None:
        amount = int(relative_match.group(1))
        unit = str(relative_match.group(2) or "").lower()
        delta = (
            timedelta(hours=amount)
            if unit.startswith("h") or unit.startswith("hora")
            else timedelta(minutes=amount)
        )
        remind_at = current_now + delta
        cleaned = _remove_text_span(normalized, relative_match.span())
        return cleaned, remind_at.replace(second=0, microsecond=0)

    explicit_day_match = re.search(
        r"\b(hoje|amanh(?:a|ã))\s+(?:as|a|às)\s*(\d{1,2})(?::(\d{2})|h(\d{2})?|h)?\b",
        normalized,
        flags=re.IGNORECASE,
    )
    if explicit_day_match is not None:
        day_token = _fold_basic(explicit_day_match.group(1))
        hour = int(explicit_day_match.group(2))
        minute = int(explicit_day_match.group(3) or explicit_day_match.group(4) or 0)
        remind_at = _build_clock_from_base(
            current_now + (timedelta(days=1) if day_token == "amanha" else timedelta(0)),
            hour=hour,
            minute=minute,
        )
        cleaned = _remove_text_span(normalized, explicit_day_match.span())
        return cleaned, remind_at

    same_day_match = _TIME_REGEX.search(normalized)
    if same_day_match is None:
        return normalized, None

    hour = int(same_day_match.group(1))
    minute = int(same_day_match.group(2) or same_day_match.group(3) or 0)
    remind_at = _build_clock_from_base(current_now, hour=hour, minute=minute)
    if remind_at <= current_now:
        remind_at = remind_at + timedelta(days=1)
    cleaned = _remove_text_span(normalized, same_day_match.span())
    return cleaned, remind_at


def _build_clock_from_base(base: datetime, *, hour: int, minute: int) -> datetime:
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("Horario fora do intervalo valido.")
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _fold_basic(value: str | None) -> str:
    text = str(value or "").strip().lower()
    replacements = (
        ("á", "a"),
        ("à", "a"),
        ("ã", "a"),
        ("â", "a"),
        ("é", "e"),
        ("ê", "e"),
        ("í", "i"),
        ("ó", "o"),
        ("ô", "o"),
        ("õ", "o"),
        ("ú", "u"),
        ("ç", "c"),
    )
    for original, replacement in replacements:
        text = text.replace(original, replacement)
    return text


def _remove_text_span(text: str, span: tuple[int, int]) -> str:
    start, end = span
    return " ".join(f"{text[:start]} {text[end:]}".split())


__all__ = [
    "BRASILIA_TIMEZONE_NAME",
    "combine_date_time_brasilia",
    "ensure_brasilia",
    "extract_time_reference_from_brasilia",
    "format_brasilia",
    "get_brasilia_tz",
    "now_brasilia",
    "parse_relative_time_from_brasilia",
    "to_utc_from_brasilia",
    "today_brasilia",
]
