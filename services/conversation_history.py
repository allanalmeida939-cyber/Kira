"""Histórico de conversas com a Kira, um arquivo por dia (memory/local/conversas/AAAA-MM-DD.jsonl).

O registro da última conversa (session_recap) guarda só 40 falas; aqui fica tudo, para "o que a gente falou ontem?"
e "o que eu te disse sobre o carro?". Só no computador do Chefe. Busca sem acento e sem maiúsculas.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("kira.conversation_history")

ROOT = Path(__file__).resolve().parents[1]
HISTORY_DIR = ROOT / "memory" / "local" / "conversas"
LEGACY_FILE = ROOT / "agent" / "memoria_diaria.json"
KEEP_DAYS = 365
DEFAULT_SEARCH_DAYS = 90
_lock = threading.Lock()
_STOPWORDS = {"a", "o", "as", "os", "de", "da", "do", "das", "dos", "e", "que", "sobre", "um", "uma", "eu", "voce", "te",
              "me", "gente", "falou", "falamos", "falei", "disse", "conversamos", "em", "no", "na", "com", "pra", "para"}


def _fold(text: object) -> str:
    value = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.findall(r"[a-z0-9]+", value))


def _day_file(day: date, folder: Path) -> Path:
    return folder / f"{day.isoformat()}.jsonl"


def append(lines: list[dict], folder: Path = HISTORY_DIR) -> None:
    """Falas {t, who, text} (t em segundos) no arquivo do dia de cada uma."""
    if not lines:
        return
    by_day: dict[date, list[str]] = {}
    for line in lines:
        day = datetime.fromtimestamp(float(line["t"])).date()
        by_day.setdefault(day, []).append(json.dumps({"t": line["t"], "who": line["who"], "text": line["text"]}, ensure_ascii=False))
    with _lock:
        folder.mkdir(parents=True, exist_ok=True)
        for day, rows in by_day.items():
            with _day_file(day, folder).open("a", encoding="utf-8") as handle:
                handle.write("\n".join(rows) + "\n")


def _read_day(day: date, folder: Path) -> list[dict]:
    path = _day_file(day, folder)
    if not path.is_file():
        return []
    rows = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(raw))
        except ValueError:
            continue
    return rows


def resolve_period(period: str, today: date) -> tuple[date, date] | None:
    """'hoje', 'ontem', 'anteontem', 'semana', 'mes', 'AAAA-MM-DD', 'DD/MM' ou 'DD/MM/AAAA'. None se não entendeu."""
    text = _fold(period)
    if not text:
        return None
    named = {"hoje": 0, "ontem": 1, "anteontem": 2}
    if text in named:
        day = today - timedelta(days=named[text])
        return day, day
    if "semana" in text:
        return today - timedelta(days=7), today
    if "mes" in text:
        return today - timedelta(days=31), today
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m"):
        try:
            parsed = datetime.strptime(period.strip(), fmt).date()
        except ValueError:
            continue
        if fmt == "%d/%m":
            parsed = parsed.replace(year=today.year)
        return parsed, parsed
    return None


def search(period: str = "", query: str = "", *, limit: int = 25, today: date | None = None, folder: Path = HISTORY_DIR) -> list[dict]:
    """Falas do período (padrão: últimos 90 dias) que têm todas as palavras de `query`; as mais recentes."""
    today = today or date.today()
    span = resolve_period(period, today) or (today - timedelta(days=DEFAULT_SEARCH_DAYS), today)
    words = [w for w in _fold(query).split() if w not in _STOPWORDS and len(w) > 1]
    found: list[dict] = []
    day = span[1]
    while day >= span[0] and len(found) < limit:
        rows = _read_day(day, folder)
        if words:
            # o assunto pode estar na pergunta do Chefe e a resposta logo depois: leva a fala seguinte junto
            hits = [i for i, row in enumerate(rows) if all(w in _fold(row.get("text")) for w in words)]
            keep = sorted({j for i in hits for j in (i, i + 1) if j < len(rows)})
            rows = [rows[j] for j in keep]
        found = rows[-(limit - len(found)):] + found if rows else found
        day -= timedelta(days=1)
    return found[-limit:]


def format_lines(rows: list[dict]) -> str:
    out = []
    for row in rows:
        when = datetime.fromtimestamp(float(row["t"])).strftime("%d/%m %H:%M") if row.get("t") else "?"
        out.append(f"{when} {row.get('who')}: {row.get('text')}")
    return "\n".join(out)


def import_legacy(path: Path = LEGACY_FILE, folder: Path = HISTORY_DIR) -> int:
    """Uma vez: as conversas do memoria_diaria.json antigo viram dias do histórico (meio-dia, sem hora exata)."""
    marker = folder / ".legacy_imported"
    if marker.exists() or not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    lines = []
    for day, entries in (data or {}).items():
        try:
            noon = datetime.fromisoformat(day).replace(hour=12).timestamp()
        except ValueError:
            continue
        for item in entries or []:
            who = "Chefe" if str(item.get("role")) == "user" else "Kira"
            text = " ".join(str(item.get("content") or "").split())[:280]
            if text:
                lines.append({"t": noon, "who": who, "text": text})
    append(lines, folder)
    folder.mkdir(parents=True, exist_ok=True)
    marker.write_text("ok", encoding="utf-8")
    logger.info("CONVERSATION_HISTORY_LEGACY_IMPORTED lines=%d", len(lines))
    return len(lines)


def prune(folder: Path = HISTORY_DIR, *, today: date | None = None) -> int:
    """Apaga dias com mais de um ano."""
    cutoff = (today or date.today()) - timedelta(days=KEEP_DAYS)
    removed = 0
    for path in folder.glob("*.jsonl") if folder.is_dir() else []:
        try:
            if date.fromisoformat(path.stem) < cutoff:
                path.unlink()
                removed += 1
        except ValueError:
            continue
    return removed


__all__ = ["append", "format_lines", "import_legacy", "prune", "resolve_period", "search"]
