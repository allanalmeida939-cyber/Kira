from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from time_utils import ensure_brasilia, now_brasilia


PendingType = Literal["comunicacao", "tarefa", "lembrete"]
PendingStatus = Literal["pendente", "adiado", "feito", "cancelado", "falhou"]
PendingPriority = Literal["alta", "media", "baixa"]
RecurrenceType = Literal["none", "daily", "weekly", "weekdays"]
PendingMode = Literal["scheduled", "open"]

_NON_PROGRESS_ACTIONS = {
    "footer_sugerido",
    "idle_resume_digest",
    "lembrete_emitido",
    "scheduler_disparado",
    "status_consultado",
}


def coerce_pending_now(now: datetime | None) -> datetime:
    if now is None:
        return now_brasilia()
    current = ensure_brasilia(now)
    if current is None:
        return now_brasilia()
    return current


@dataclass(slots=True)
class PendingItem:
    id: str
    tipo: PendingType
    descricao: str
    texto_original: str = ""
    texto_normalizado: str = ""
    pessoa: str | None = None
    app_origem: str | None = None
    categoria: str = "pessoal"
    prioridade: PendingPriority = "media"
    modo_item: PendingMode = "scheduled"
    recorrencia_tipo: RecurrenceType = "none"
    recorrencia_dia_semana: int | None = None
    horario_criacao: datetime = field(default_factory=now_brasilia)
    horario_lembrete: datetime | None = None
    updated_at: datetime = field(default_factory=now_brasilia)
    status: PendingStatus = "pendente"
    tempo_parado: timedelta = field(default_factory=timedelta)
    quantidade_adiamentos: int = 0
    adiamentos_count: int = 0
    anunciado_count: int = 0
    ultimo_anuncio_em: datetime | None = None
    ultimo_adiamento_em: datetime | None = None
    concluido_count: int = 0
    ignorado_count: int = 0
    tempo_medio_para_concluir_segundos: float = 0.0
    ultima_conclusao_em: datetime | None = None
    ultimo_lembrete: datetime | None = None
    delivered: bool = False
    attempts: int = 0
    expired: bool = False
    historico: list[dict[str, str]] = field(default_factory=list)

    def is_open(self) -> bool:
        return self.status in {"pendente", "adiado"} and not self.expired and not self.delivered

    @property
    def criado_em(self) -> datetime:
        return self.horario_criacao

    @property
    def agendado_para(self) -> datetime | None:
        return self.horario_lembrete

    @property
    def adiado_vezes(self) -> int:
        return max(self.quantidade_adiamentos, self.adiamentos_count)


class PendingStore:
    def __init__(self) -> None:
        self._items: list[PendingItem] = []
        self._source_index: dict[str, str] = {}

    def all_items(self) -> list[PendingItem]:
        return list(self._items)

    def iter_reverse(self):
        return reversed(self._items)

    def add(self, item: PendingItem, *, source_id: str | None = None) -> PendingItem:
        existing = self.get(item.id)
        if existing is None:
            self._items.append(item)
        elif existing is not item:
            index = self._items.index(existing)
            self._items[index] = item
        if source_id:
            self._source_index[str(source_id)] = item.id
        return item

    def create(
        self,
        *,
        descricao: str,
        tipo: PendingType,
        texto_original: str = "",
        texto_normalizado: str = "",
        prioridade: PendingPriority = "media",
        modo_item: PendingMode = "scheduled",
        recorrencia_tipo: RecurrenceType = "none",
        recorrencia_dia_semana: int | None = None,
        horario_lembrete: datetime | None = None,
        pessoa: str | None = None,
        app_origem: str | None = None,
        categoria: str = "pessoal",
        item_id: str | None = None,
        source_id: str | None = None,
        now: datetime | None = None,
    ) -> PendingItem:
        current_now = coerce_pending_now(now)
        item = PendingItem(
            id=item_id or f"pend_{uuid4().hex[:12]}",
            tipo=tipo,
            descricao=str(descricao or "").strip(),
            texto_original=str(texto_original or "").strip(),
            texto_normalizado=str(texto_normalizado or "").strip(),
            pessoa=pessoa,
            app_origem=app_origem,
            categoria=categoria,
            prioridade=prioridade,
            modo_item=modo_item,
            recorrencia_tipo=recorrencia_tipo,
            recorrencia_dia_semana=recorrencia_dia_semana,
            horario_criacao=current_now,
            horario_lembrete=ensure_brasilia(horario_lembrete),
            updated_at=current_now,
        )
        self.record_history(
            item,
            "criada",
            detail="Pendencia registrada no store.",
            now=current_now,
        )
        return self.add(item, source_id=source_id)

    def get(self, item_id: str) -> PendingItem | None:
        target = str(item_id or "").strip()
        if not target:
            return None
        for item in self._items:
            if item.id == target:
                return item
        return None

    def find_by_source(self, source_id: str) -> PendingItem | None:
        target = str(source_id or "").strip()
        if not target:
            return None
        item_id = self._source_index.get(target)
        if not item_id:
            return None
        return self.get(item_id)

    def open_items(self, *, now: datetime | None = None) -> list[PendingItem]:
        self.refresh_dynamic_fields(now=now)
        return [item for item in self._items if item.is_open()]

    def latest_open_item(self, *, now: datetime | None = None) -> PendingItem | None:
        self.refresh_dynamic_fields(now=now)
        for item in reversed(self._items):
            if item.is_open():
                return item
        return None

    def refresh_dynamic_fields(self, *, now: datetime | None = None) -> None:
        current_now = coerce_pending_now(now)
        for item in self._items:
            if not item.is_open():
                item.tempo_parado = timedelta(0)
                continue
            item.tempo_parado = max(
                current_now - self.last_progress_at(item, now=current_now),
                timedelta(0),
            )

    def record_history(
        self,
        item: PendingItem,
        action: str,
        *,
        detail: str,
        now: datetime | None = None,
    ) -> None:
        current_now = coerce_pending_now(now)
        item.updated_at = current_now
        item.historico.append(
            {
                "horario": current_now.isoformat(),
                "acao": str(action or "").strip(),
                "detalhe": str(detail or "").strip(),
            }
        )

    def last_history_at(self, item: PendingItem, action: str) -> datetime | None:
        target_action = str(action or "").strip()
        for entry in reversed(item.historico):
            if str(entry.get("acao") or "").strip() != target_action:
                continue
            try:
                return ensure_brasilia(datetime.fromisoformat(str(entry.get("horario"))))
            except Exception:
                return None
        return None

    def last_progress_at(
        self,
        item: PendingItem,
        *,
        now: datetime | None = None,
    ) -> datetime:
        del now
        for entry in reversed(item.historico):
            action = str(entry.get("acao") or "").strip()
            if action in _NON_PROGRESS_ACTIONS:
                continue
            try:
                historical = ensure_brasilia(
                    datetime.fromisoformat(str(entry.get("horario")))
                )
                if historical is not None:
                    return historical
            except Exception:
                break
        return item.horario_criacao


__all__ = [
    "PendingItem",
    "PendingPriority",
    "PendingMode",
    "RecurrenceType",
    "PendingStatus",
    "PendingStore",
    "PendingType",
    "coerce_pending_now",
]
