"""Interesses do usuário para ranquear notícias: lista local, editável e que pode ser limpa."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

MAX_TERMS = 60
DECAY = 0.985
STOPWORDS = frozenset(
    "para como sobre mais isso essa esse esta este uma uns umas que quem onde quando porque pelo pela pelos pelas com sem por dos das nos nas "
    "mas muito muita tudo todo toda fala falar conta contar mostra mostrar explica explicar kira voce voces pode quer queria gostaria preciso "
    "fazer faz hoje agora ainda tambem entao assim sempre nunca aquele aquela algum alguma outro outra depois antes "
    "qual quais quanto quanta quantos quantas minha minhas meus seus suas dele dela tela telas vendo veio aconteceu acontece "
    "certo legal quero diga sabe acho ficar fica "
    "coisa coisas vezes favor obrigado obrigada tava estava esta estou sobre "
    "gente tinha visto ultima ultimo pessoa pessoas fato traducao traduz traduzir significa significado".split()
)
# Fala em inglês (áudio de teste, música, legenda de vídeo) não ensina interesse: a lista enchia de "grandmother",
# "potatoes", "ejaculated" (2026-10-24). Palavras de ligação do inglês denunciam a frase.
ENGLISH_WORDS = frozenset(
    "the and that with from have this they were what when your about would there their which been could should into "
    "very said will just like know make made good always because before after over only then them these those".split()
)
TOKEN_RE = re.compile(r"[a-z0-9]{4,}")
# Só pedidos de assunto ensinam interesse ("notícias sobre a Nvidia", "história de Chernobyl", "pesquisa RTX 5090").
# Conversa comum ("tá vendo minha tela?") enchia a lista de palavras soltas.
TOPIC_RE = re.compile(
    r"\b(?:noticias?|novidades?|historia|pesquisa|pesquisar|procura|procurar|busca|buscar|"
    r"fala(?:r)? (?:sobre|de|da|do)|conta(?:r)? (?:sobre|de|da|do)|quem (?:e|foi)|o que e)\b"
    r"(?:\s+(?:sobre|de|da|do|das|dos|a|o|as|os|um|uma))*\s+(?P<topic>[^,.;!?]+)"
)
MAX_TOPIC_TERMS = 4
FILE_VERSION = 2


def fold(value: object) -> str:
    return unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()


def terms_of(text: object) -> list[str]:
    return [t for t in TOKEN_RE.findall(fold(text)) if t not in STOPWORDS]


def looks_english(text: object) -> bool:
    """Frase em inglês ("the cured pork from the cellar"): duas palavras de ligação ou um quarto da frase."""
    words = re.findall(r"[a-z]+", fold(text))
    hits = sum(1 for word in words if word in ENGLISH_WORDS)
    return len(words) >= 3 and (hits >= 2 or hits * 4 >= len(words))


def topic_terms(text: object) -> list[str]:
    """Assunto de um pedido ("me conta a história de Fortaleza no futebol" -> fortaleza, futebol); nada se não for pedido."""
    match = TOPIC_RE.search(fold(text))
    return terms_of(match.group("topic"))[:MAX_TOPIC_TERMS] if match else []


class Interests:
    def __init__(self, path: Path | None, *, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._scores: dict[str, float] = self._load()

    def _load(self) -> dict[str, float]:
        if self._path is None:
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if int(data.get("version") or 1) < FILE_VERSION:
                return {}  # versão 1 aprendia toda palavra de toda fala ("quanto", "minha", "tela"): começa limpa
            # Palavras que viraram stopword saem da lista salva.
            return {str(k): float(v) for k, v in (data.get("topics") or {}).items() if isinstance(v, (int, float)) and k not in STOPWORDS}
        except Exception:
            return {}

    def _save(self) -> None:
        if self._path is None:
            return
        temporary: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(self._path.parent), suffix=".tmp") as handle:
                temporary = handle.name
                json.dump({"version": FILE_VERSION, "topics": self._scores}, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except Exception:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

    def note(self, text: object, weight: float = 1.0) -> None:
        """Aprende assuntos de uma fala, pesquisa ou pedido; os mais antigos vão perdendo peso."""
        found = terms_of(text)
        if not found:
            return
        with self._lock:
            for term in list(self._scores):
                self._scores[term] *= DECAY
            for term in dict.fromkeys(found):
                self._scores[term] = self._scores.get(term, 0.0) + weight
            for term in sorted(self._scores, key=self._scores.get)[: max(0, len(self._scores) - MAX_TERMS)]:  # type: ignore[arg-type]
                del self._scores[term]
            self._save()

    def top(self, limit: int = 20) -> list[dict[str, object]]:
        with self._lock:
            ordered = sorted(self._scores.items(), key=lambda kv: -kv[1])[:limit]
        return [{"term": term, "score": round(score, 2)} for term, score in ordered]

    def replace(self, terms: list[str]) -> None:
        """Edição manual: a lista informada vira o conjunto de interesses (peso alto)."""
        with self._lock:
            self._scores = {term: 5.0 for term in list(dict.fromkeys(terms_of(" ".join(str(t) for t in terms))))[:MAX_TERMS]}
            self._save()

    def clear(self) -> None:
        with self._lock:
            self._scores = {}
            self._save()

    def rank(self, items: list[dict[str, object]], *, limit: int = 40) -> list[dict[str, object]]:
        """Interesse + recência, sem títulos repetidos."""
        now = self._clock()
        with self._lock:
            scores = dict(self._scores)
        seen: set[str] = set()
        scored: list[tuple[float, dict[str, object]]] = []
        for item in items:
            key = " ".join(terms_of(item.get("title")))[:80]
            if not key or key in seen:
                continue
            seen.add(key)
            interest = sum(scores.get(t, 0.0) for t in set(terms_of(item.get("title"))))
            age_hours = max(0.0, (now - float(item.get("ts") or now)) / 3600.0)
            scored.append((interest * 2.0 + 3.0 / (1.0 + age_hours / 6.0), {**item, "match": True} if interest > 0 else item))
        scored.sort(key=lambda pair: -pair[0])
        return [item for _, item in scored[:limit]]


_SHARED: Interests | None = None


def get_interests() -> Interests:
    global _SHARED
    if _SHARED is None:
        _SHARED = Interests(Path(__file__).resolve().parents[1] / "configs" / "interests.json")
    return _SHARED


def note_user_text(text: object) -> None:
    """Gancho best-effort das falas do usuário: só pedidos de assunto contam; nunca afeta a conversa."""
    try:
        if looks_english(text):
            return
        topic = topic_terms(text)
        if topic:
            get_interests().note(" ".join(topic))
    except Exception:
        return


def note_search(query: object) -> None:
    """Pesquisa feita pela Kira: a consulta inteira é o assunto."""
    try:
        if looks_english(query):
            return
        get_interests().note(query)
    except Exception:
        return


def iso_to_epoch(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


__all__ = ["Interests", "get_interests", "iso_to_epoch", "looks_english", "note_search", "note_user_text", "terms_of", "topic_terms"]
