from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from time_utils import ensure_brasilia, now_brasilia


@dataclass(frozen=True)
class StartupGreeting:
    period_label: str
    message: str


WEEKDAYS_PT = ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")
LAST_CONTACT_FILE = Path(__file__).resolve().parents[2] / "configs" / "last_contact.json"


def day_period(hour: int) -> str:
    if hour < 5:
        return "madrugada"
    if hour < 12:
        return "manhã"
    if hour < 18:
        return "tarde"
    return "noite"


def _elapsed_text(delta_seconds: float) -> str:
    minutes = int(delta_seconds // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} minuto(s)"
    if minutes < 48 * 60:
        return f"{minutes // 60} hora(s)"
    return f"{minutes // (24 * 60)} dia(s)"


def build_greeting_context(*, now: datetime | None = None, last_contact: datetime | None = None) -> str:
    """Fatos de tempo para a saudacao: o Gemini nao sabe a hora local, entao informamos."""
    current = ensure_brasilia(now) or now_brasilia()
    period = day_period(current.hour)
    lines = [
        f"CONTEXTO DE TEMPO (America/Fortaleza): agora são {current:%H:%M} de {WEEKDAYS_PT[current.weekday()]}, período: {period}.",
        f"Cumprimente pelo período ({period}). Nunca diga 'bom dia' fora da manhã; de madrugada prefira 'oi' ou 'boa madrugada'.",
    ]
    previous = ensure_brasilia(last_contact) if last_contact else None
    if previous and (current - previous) >= timedelta(minutes=5):
        lines.append(f"O último contato foi há cerca de {_elapsed_text((current - previous).total_seconds())}.")
        if (current - previous) >= timedelta(days=2):  # sumiu por dias: ela puxa assunto do jeito dela (2026-11-07)
            lines.append(
                "Faz DIAS que ninguém fala com você. Reaja a isso com a SUA personalidade atual (carinhosa: saudade; "
                "sarcástica/bruta: provocação), como 'olha quem apareceu', 'lembrou de mim, foi?'. Se houver ÚLTIMA "
                "CONVERSA no contexto, cite o assunto numa frase e pergunte como ficou. Até duas frases."
            )
    lines.append("Uma frase de saudação e, no máximo, uma pergunta útil. Curta.")
    return " ".join(lines)


def load_last_contact(path: Path = LAST_CONTACT_FILE) -> datetime | None:
    try:
        return ensure_brasilia(datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_last_contact(now: datetime | None = None, path: Path = LAST_CONTACT_FILE) -> None:
    current = ensure_brasilia(now) or now_brasilia()
    try:
        path.write_text(json.dumps({"at": current.isoformat()}), encoding="utf-8")
    except OSError:
        return


def build_startup_greeting(
    *,
    user_name: str,
    now: datetime | None = None,
) -> StartupGreeting:
    current_time = ensure_brasilia(now) or now_brasilia()
    hour = current_time.hour
    clean_name = str(user_name or "").strip() or "Allan"

    if hour < 5:
        period_label = "Oi"
        options = (
            f"Oi, {clean_name}. Acordado a esta hora?",
            f"Oi, {clean_name}. Madrugada produtiva?",
            "Oi, chefe.",
        )
    elif hour < 12:
        period_label = "Bom dia"
        options = (
            f"Bom dia, {clean_name}.",
            f"Oi, {clean_name}. Tudo certo por ai?",
            "Bom dia, chefe.",
            "Oi, mestre.",
        )
    elif hour < 18:
        period_label = "Boa tarde"
        options = (
            f"Boa tarde, {clean_name}.",
            "Boa tarde, chefe.",
            "Oi, mestre.",
            f"Oi, {clean_name}. Tudo certo por ai?",
        )
    else:
        period_label = "Boa noite"
        options = (
            f"Boa noite, {clean_name}.",
            "Boa noite, senhor.",
            "Oi, mestre.",
            f"Oi, {clean_name}. Tudo certo por ai?",
        )

    rng = random.Random(f"{current_time:%Y%m%d%H}:{clean_name.lower()}")
    return StartupGreeting(
        period_label=period_label,
        message=rng.choice(options),
    )
