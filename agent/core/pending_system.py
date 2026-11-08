from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from time_utils import extract_time_reference_from_brasilia, format_brasilia

from .pending_store import (
    PendingItem,
    PendingMode,
    PendingPriority,
    RecurrenceType,
    PendingStatus,
    PendingStore,
    PendingType,
    coerce_pending_now,
)


_WHITESPACE_RE = re.compile(r"\s+")
_QUERY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bo que (?:esta|ta) pendente\b"),
    re.compile(r"\bo que falta\b"),
    re.compile(r"\bquais (?:sao|as)?\s*pendencias\b"),
    re.compile(r"\btem pendenc"),
    re.compile(r"\bstatus das pendenc"),
)
_PROGRESS_QUERY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bprogresso hoje\b"),
    re.compile(r"\bcomo foi meu progresso\b"),
    re.compile(r"\bresumo do dia\b"),
)
_PLAN_QUERY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\borganiza(?:r)? minhas pendenc"),
    re.compile(r"\bme sugere uma ordem\b"),
    re.compile(r"\bplano de pendenc"),
)
_ENHANCED_DAY_QUERY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcomo foi meu dia\b"),
    re.compile(r"\bfaz um resumo do dia\b"),
)
_MOST_POSTPONED_QUERY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bo que eu adiei mais hoje\b"),
    re.compile(r"\bqual area ficou mais pendente hoje\b"),
)
_COMPLETE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^ja fiz(?: isso)?$"),
    re.compile(r"^pronto$"),
    re.compile(r"^ok$"),
    re.compile(r"^feito$"),
    re.compile(r"^conclui(?: isso)?$"),
    re.compile(r"^resolvido$"),
)
_NOT_DONE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^nao fiz$"),
    re.compile(r"^ainda nao$"),
    re.compile(r"^nao deu$"),
)
_IGNORE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^ignora(?: isso)?$"),
    re.compile(r"^deixa pra la$"),
)
_POSTPONE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^adiar?\b"),
    re.compile(r"^adia\b"),
    re.compile(r"^depois$"),
    re.compile(r"^me lembra depois\b"),
    re.compile(r"^adiar isso\b"),
    re.compile(r"^me lembra daqui\b"),
)
_COMMUNICATION_VERBS = (
    "mandar mensagem",
    "manda mensagem",
    "enviar mensagem",
    "envia mensagem",
    "dar retorno",
    "da retorno",
    "falar com",
    "fala com",
    "responder",
    "responde",
    "retornar",
    "retorna",
    "ligar",
    "liga",
    "avisar",
    "avisa",
    "cobrar",
    "cobra",
)
_TASK_VERBS = {
    "comprar",
    "compra",
    "pegar",
    "pega",
    "buscar",
    "busca",
    "levar",
    "leva",
    "pagar",
    "paga",
    "estudar",
    "estuda",
    "revisar",
    "revisa",
    "entregar",
    "entrega",
    "separar",
    "separa",
    "organizar",
    "organiza",
    "resolver",
    "resolve",
    "verificar",
    "verifica",
    "conferir",
    "confere",
    "anotar",
    "anota",
    "fazer",
    "faz",
    "lembrar",
    "lembra",
}
_OPEN_PENDING_HINTS = (
    "tenho que",
    "preciso",
    "nao posso esquecer",
    "depois preciso",
    "mais tarde tenho que",
)
_THEME_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("estudo", ("estudar", "estudo", "revisar", "matematica", "matemática")),
    ("mensagens", ("responder", "mensagem", "joao", "joão", "enviar")),
    ("compras", ("comprar", "mercado", "pao", "pão")),
    ("rotina", ("rotina", "agua", "água", "remedio", "remédio")),
)
_QUESTION_PREFIXES = (
    "o que",
    "qual",
    "quais",
    "como",
    "por que",
    "porque",
    "me conta",
    "me explica",
    "pode",
    "voce consegue",
    "consegue",
)
_WORK_HINTS = (
    "cliente",
    "reuniao",
    "reunião",
    "trabalho",
    "projeto",
    "planilha",
    "relatorio",
    "relatório",
    "email",
    "e-mail",
    "discord",
    "slack",
    "jira",
    "deploy",
)
_HIGH_PRIORITY_HINTS = (
    "urgente",
    "importante",
    "agora",
    "nao esquecer",
    "hoje",
    "assim que",
    "quanto antes",
)
_LOW_PRIORITY_HINTS = (
    "depois",
    "mais tarde",
    "quando der",
    "sem pressa",
    "qualquer dia",
)
_COMMUNICATION_STOP_WORDS = frozenset(
    {
        "depois",
        "mais",
        "tarde",
        "em",
        "daqui",
        "hoje",
        "amanha",
        "as",
        "urgente",
        "agora",
        "quando",
        "se",
        "pelo",
        "pela",
        "no",
        "na",
    }
)
_PRIORITY_WINDOWS = {
    "alta": timedelta(minutes=30),
    "media": timedelta(hours=2),
    "baixa": timedelta(hours=6),
}
_DEFAULT_VAGUE_REMINDER_DELAY = timedelta(hours=3)
_ANNOUNCE_COOLDOWN = timedelta(seconds=10)
_ANNOUNCE_GROUP_WINDOW = timedelta(seconds=45)
_MAX_GROUP_ITEMS = 3
_MIN_USER_SILENCE_FOR_PROACTIVE = timedelta(seconds=60)
_MIN_KIRA_SILENCE_FOR_PROACTIVE = timedelta(seconds=45)
_OPEN_AGE_FOR_INSIST_LIGHT = timedelta(hours=6)
_OPEN_AGE_FOR_INSIST_FIRM = timedelta(hours=12)
_WEEKDAY_NAME_TO_INT: dict[str, int] = {
    "segunda": 0,
    "segunda-feira": 0,
    "terca": 1,
    "terca-feira": 1,
    "quarta": 2,
    "quarta-feira": 2,
    "quinta": 3,
    "quinta-feira": 3,
    "sexta": 4,
    "sexta-feira": 4,
    "sabado": 5,
    "domingo": 6,
}


def _normalize_text(value: object) -> str:
    return _WHITESPACE_RE.sub(" ", str(value or "").strip())


def _fold_text(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", _normalize_text(value).lower())
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


@dataclass(slots=True)
class PendingDraft:
    tipo: PendingType
    descricao: str
    texto_original: str = ""
    texto_normalizado: str = ""
    pessoa: str | None = None
    app_origem: str | None = None
    categoria: str = "pessoal"
    prioridade: PendingPriority = "media"
    modo_item: PendingMode = "scheduled"
    horario_lembrete: datetime | None = None
    recorrencia_tipo: RecurrenceType = "none"
    recorrencia_dia_semana: int | None = None


@dataclass(slots=True)
class PendingCommandResult:
    handled: bool = False
    action: str = ""
    message: str = ""
    item: PendingItem | None = None
    context_note: str = ""


@dataclass(slots=True)
class PendingAnnouncement:
    message: str
    item_ids: list[str]
    primary_item_id: str = ""


class PendingRegistry:
    def __init__(self, *, store: PendingStore | None = None) -> None:
        self._store = store or PendingStore()
        self.ultimo_lembrete_anunciado_id: str | None = None
        self.ultimo_lembrete_anunciado_em: datetime | None = None
        self.contexto_confirmacao_ativa: bool = False
        self._queued_due_reminder_ids: list[str] = []
        self._external_busy_until: datetime | None = None
        self.last_user_interaction_at: datetime | None = None
        self.last_kira_reply_at: datetime | None = None
        self._behavior_by_theme: dict[str, dict[str, float]] = {}
        self._pending_micro_plan: list[tuple[str, datetime]] = []
        self._pending_micro_plan_created_at: datetime | None = None
        self._last_announcement_item_ids: list[str] = []

    def register_from_text(
        self,
        text: str,
        *,
        app_origem: str | None = None,
        now: datetime | None = None,
    ) -> PendingItem | None:
        self.record_user_interaction(now=now)
        draft = interpret_pending_text(
            text,
            app_origem=app_origem,
            now=now,
        )
        if draft is None:
            return None
        item, _created = self._create_or_refresh(draft, now=now)
        return item

    def record_user_interaction(self, *, now: datetime | None = None) -> None:
        self.last_user_interaction_at = _coerce_now(now)

    def record_kira_reply(self, *, now: datetime | None = None) -> None:
        self.last_kira_reply_at = _coerce_now(now)

    def handle_quick_command(
        self,
        text: str,
        *,
        app_origem: str | None = None,
        now: datetime | None = None,
    ) -> PendingCommandResult:
        current_now = _coerce_now(now)
        self.record_user_interaction(now=current_now)
        normalized = _normalize_text(text)
        folded = _fold_text(normalized)
        if not normalized:
            return PendingCommandResult()

        confirmation_result = self._handle_confirmation_command(
            normalized,
            folded_text=folded,
            now=current_now,
        )
        if confirmation_result is not None:
            return confirmation_result

        microplan_result = self._handle_microplan_confirmation(
            normalized,
            folded_text=folded,
            now=current_now,
        )
        if microplan_result is not None:
            return microplan_result

        if self._looks_like_progress_query(folded):
            summary = self.get_daily_progress_summary(now=current_now)
            return PendingCommandResult(
                handled=True,
                action="progresso",
                message=summary,
            )

        if self._looks_like_plan_query(folded):
            plan = self.build_micro_plan(now=current_now)
            return PendingCommandResult(
                handled=True,
                action="planejar",
                message=plan,
            )

        if self._looks_like_enhanced_day_query(folded):
            summary = self.get_enhanced_daily_summary(now=current_now)
            return PendingCommandResult(
                handled=True,
                action="resumo_dia_avancado",
                message=summary,
            )

        if self._looks_like_most_postponed_query(folded):
            postponed = self.get_most_postponed_today(now=current_now)
            return PendingCommandResult(
                handled=True,
                action="adiado_mais_hoje",
                message=postponed,
            )

        if self._looks_like_query(folded):
            message = self.build_overview_message()
            return PendingCommandResult(
                handled=True,
                action="status",
                message=message,
                context_note=f"[PENDENCIAS] Resumo atual: {message}",
            )

        postpone_minutes = _extract_postpone_minutes(folded)
        if postpone_minutes is not None and self._looks_like_postpone_command(folded):
            item = self.postpone_latest(postpone_minutes, now=current_now)
            if item is None:
                return PendingCommandResult(
                    handled=True,
                    action="adiar",
                    message="Nao achei uma pendencia aberta para adiar agora.",
                )
            return PendingCommandResult(
                handled=True,
                action="adiar",
                item=item,
                message=(
                    f"Chefe, adiei esse lembrete por {postpone_minutes} minutos."
                ),
                context_note=(
                    f"[PENDENCIAS] Pendencia adiada: {item.descricao}. "
                    f"Novo lembrete: {_format_datetime(item.horario_lembrete)}."
                ),
            )

        postpone_dt = _extract_postpone_datetime(normalized, now=current_now)
        if postpone_dt is not None and self._looks_like_postpone_command(folded):
            item = self.postpone_latest_to_datetime(postpone_dt, now=current_now)
            if item is None:
                return PendingCommandResult(
                    handled=True,
                    action="adiar",
                    message="Nao achei uma pendencia aberta para adiar agora.",
                )
            postpone_minutes = max(1, int((postpone_dt - current_now).total_seconds() // 60))
            return PendingCommandResult(
                handled=True,
                action="adiar",
                item=item,
                message=(
                    f"Chefe, adiei esse lembrete por {postpone_minutes} minutos."
                ),
                context_note=(
                    f"[PENDENCIAS] Pendencia adiada: {item.descricao}. "
                    f"Novo lembrete: {_format_datetime(item.horario_lembrete)}."
                ),
            )
        if _looks_like_defer_without_time(folded):
            return PendingCommandResult(
                handled=True,
                action="solicitar_tempo",
                message="Daqui quanto tempo, chefe?",
            )

        if self._looks_like_complete_command(folded):
            item = self.complete_latest(now=current_now)
            if item is None:
                return PendingCommandResult(
                    handled=True,
                    action="concluir",
                    message="Nao achei uma pendencia aberta para marcar como feita.",
                )
            return PendingCommandResult(
                handled=True,
                action="concluir",
                item=item,
                message=f"Perfeito, marquei '{item.descricao}' como feito.",
                context_note=f"[PENDENCIAS] Pendencia concluida: {item.descricao}.",
            )

        item = self.register_from_text(
            normalized,
            app_origem=app_origem,
            now=current_now,
        )
        if item is None:
            return PendingCommandResult()

        return PendingCommandResult(
            handled=True,
            action="registrar",
            item=item,
            message=self._build_register_message(item),
            context_note=self._build_register_context_note(item),
        )

    def build_overview_message(self) -> str:
        open_items = self.open_items()
        if not open_items:
            return "Voce nao tem nada pendente agora."

        communication_count = sum(1 for item in open_items if item.tipo == "comunicacao")
        task_count = sum(1 for item in open_items if item.tipo == "tarefa")
        reminder_count = sum(1 for item in open_items if item.tipo == "lembrete")
        return (
            "Voce tem:\n"
            f"- {communication_count} resposta(s) pendente(s)\n"
            f"- {task_count} tarefa(s)\n"
            f"- {reminder_count} lembrete(s) com horario"
        )

    def build_simple_plan(self, *, now: datetime | None = None) -> str:
        current_now = _coerce_now(now)
        items = [item for item in self.open_items() if item.modo_item == "open"]
        if not items:
            return "Chefe, no momento nao vejo pendencias abertas para planejar."
        items.sort(
            key=lambda item: (
                _priority_order(self._effective_priority(item, now=current_now)),
                self._next_attention_at(item) or current_now,
            )
        )
        top = items[:3]
        names = [self._context_snippet(item) for item in top]
        if len(names) == 1:
            return f"Chefe, eu sugiro resolver primeiro: {names[0]}."
        if len(names) == 2:
            return f"Chefe, eu sugiro esta ordem: {names[0]} e depois {names[1]}."
        return (
            f"Chefe, eu sugiro esta ordem: {names[0]}, depois {names[1]}, "
            f"e por ultimo {names[2]}."
        )

    def build_micro_plan(self, *, now: datetime | None = None) -> str:
        current_now = _coerce_now(now)
        open_items = [item for item in self.open_items() if item.modo_item == "open"]
        if not open_items:
            return "Chefe, agora nao vejo pendencias abertas para montar microplano."
        open_items.sort(
            key=lambda item: (
                _priority_order(self._effective_priority(item, now=current_now)),
                item.horario_criacao,
            )
        )
        top = open_items[:3]
        plan_pairs = self._build_micro_plan_slots(top, now=current_now)
        self._pending_micro_plan = plan_pairs
        self._pending_micro_plan_created_at = current_now
        parts: list[str] = []
        for item_id, when in plan_pairs:
            item = self._store.get(item_id)
            if item is None:
                continue
            parts.append(f"{self._context_snippet(item)} em {_format_datetime(when)}")
        if not parts:
            return "Chefe, nao consegui montar um microplano agora."
        if len(parts) == 1:
            return f"Chefe, posso deixar assim: {parts[0]}. Se quiser, eu aplico agora."
        return (
            "Chefe, meu microplano sugerido e: "
            + "; ".join(parts)
            + ". Se voce quiser, eu aplico agora."
        )

    def apply_micro_plan_to_items(self, *, now: datetime | None = None) -> int:
        current_now = _coerce_now(now)
        applied = 0
        for item_id, run_at in list(self._pending_micro_plan):
            item = self._store.get(item_id)
            if item is None or not item.is_open():
                continue
            self.postpone_item_to_datetime(item, run_at, now=current_now)
            item.modo_item = "scheduled"
            item.tipo = "lembrete"
            self._append_history(
                item,
                "microplano_aplicado",
                detail=f"Microplano aplicou horario {_format_datetime(run_at)}.",
                now=current_now,
            )
            applied += 1
        self._pending_micro_plan = []
        self._pending_micro_plan_created_at = None
        return applied

    def _build_micro_plan_slots(
        self,
        items: list[PendingItem],
        *,
        now: datetime,
    ) -> list[tuple[str, datetime]]:
        base_now_slot = now + timedelta(minutes=30)
        today_evening = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if today_evening <= now:
            today_evening += timedelta(days=1)
        tomorrow_morning = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        slots = [base_now_slot, today_evening, tomorrow_morning]
        pairs: list[tuple[str, datetime]] = []
        for idx, item in enumerate(items):
            slot = slots[min(idx, len(slots) - 1)]
            pairs.append((item.id, slot))
        return pairs

    def suggest_schedule_for_open_items(self, *, now: datetime | None = None) -> list[tuple[str, datetime]]:
        current_now = _coerce_now(now)
        base = current_now.replace(hour=18, minute=0, second=0, microsecond=0)
        if base <= current_now:
            base += timedelta(days=1)
        suggestions: list[tuple[str, datetime]] = []
        ordered = [item for item in self.open_items() if item.modo_item == "open"]
        ordered.sort(key=lambda item: _priority_order(self._effective_priority(item, now=current_now)))
        for index, item in enumerate(ordered[:3]):
            suggestions.append((item.id, base + timedelta(hours=index)))
        return suggestions

    def get_user_load_state(self, *, now: datetime | None = None) -> str:
        current_now = _coerce_now(now)
        today = current_now.date()
        postponed_today = 0
        announcements_recent = 0
        for item in self._store.all_items():
            if item.ultimo_anuncio_em is not None and (current_now - item.ultimo_anuncio_em) <= timedelta(minutes=20):
                announcements_recent += 1
            for entry in item.historico:
                try:
                    entry_dt = _coerce_now(datetime.fromisoformat(str(entry.get("horario", ""))))
                except Exception:
                    continue
                if entry_dt.date() != today:
                    continue
                if str(entry.get("acao", "")) == "adiada":
                    postponed_today += 1
        open_count = len([item for item in self.open_items() if item.modo_item == "open"])
        if postponed_today >= 4 or open_count >= 6 or announcements_recent >= 3:
            return "high"
        if postponed_today >= 2 or open_count >= 3:
            return "medium"
        return "low"

    def get_daily_progress_summary(self, *, now: datetime | None = None) -> str:
        current_now = _coerce_now(now)
        today = current_now.date()
        concluded = 0
        postponed = 0
        ignored = 0
        for item in self._store.all_items():
            for entry in item.historico:
                try:
                    entry_dt = datetime.fromisoformat(str(entry.get("horario", "")))
                except Exception:
                    continue
                if _coerce_now(entry_dt).date() != today:
                    continue
                action = str(entry.get("acao", ""))
                if action == "concluida":
                    concluded += 1
                elif action == "adiada":
                    postponed += 1
                elif action in {"cancelada", "ignorada"}:
                    ignored += 1
        open_count = len(self.open_items())
        return (
            f"Chefe, hoje voce concluiu {concluded} pendencia(s), "
            f"adiou {postponed}, ignorou {ignored} e ainda tem {open_count} aberta(s)."
        )

    def get_enhanced_daily_summary(self, *, now: datetime | None = None) -> str:
        base = self.get_daily_progress_summary(now=now)
        load = self.get_user_load_state(now=now)
        most_postponed = self.get_most_postponed_today(now=now)
        if load == "high":
            suggestion = " Hoje parece puxado. Quer que eu reorganize amanha cedo?"
        elif load == "low":
            suggestion = " Seu ritmo hoje foi bom. Quer que eu puxe mais uma pendencia importante agora?"
        else:
            suggestion = " Se quiser, eu monto um microplano para fechar o resto. Quer que eu faca isso?"
        return f"{base} {most_postponed}{suggestion}"

    def get_most_postponed_today(self, *, now: datetime | None = None) -> str:
        current_now = _coerce_now(now)
        today = current_now.date()
        counters: dict[str, int] = {}
        for item in self._store.all_items():
            count = 0
            for entry in item.historico:
                try:
                    entry_dt = _coerce_now(datetime.fromisoformat(str(entry.get("horario", ""))))
                except Exception:
                    continue
                if entry_dt.date() != today:
                    continue
                if str(entry.get("acao", "")) == "adiada":
                    count += 1
            if count > 0:
                counters[item.id] = count
        if not counters:
            return "Hoje voce quase nao adiou pendencias."
        top_id = max(counters, key=counters.get)
        top_item = self._store.get(top_id)
        if top_item is None:
            return "Hoje voce adiou algumas pendencias, mas sem destaque claro."
        theme = self._theme_for_item(top_item)
        return (
            f"O que voce mais adiou hoje foi '{self._context_snippet(top_item)}' "
            f"({counters[top_id]}x), tema {theme}."
        )

    def open_items(self) -> list[PendingItem]:
        return self._store.open_items()

    def latest_open_item(self) -> PendingItem | None:
        return self._store.latest_open_item()

    def complete_latest(self, *, now: datetime | None = None) -> PendingItem | None:
        item = self.latest_open_item()
        if item is None:
            return None
        self._mark_item_completed(item, now=_coerce_now(now), detail="Marcada como feita.")
        return item

    def postpone_latest(
        self,
        minutes: int,
        *,
        now: datetime | None = None,
    ) -> PendingItem | None:
        current_now = _coerce_now(now)
        return self.postpone_latest_to_datetime(
            current_now + timedelta(minutes=max(minutes, 1)),
            now=current_now,
        )

    def postpone_latest_to_datetime(
        self,
        target_at: datetime,
        *,
        now: datetime | None = None,
    ) -> PendingItem | None:
        item = self.latest_open_item()
        if item is None:
            return None
        return self.postpone_item_to_datetime(item, target_at, now=now)

    def postpone_item_to_datetime(
        self,
        item: PendingItem,
        target_at: datetime,
        *,
        now: datetime | None = None,
    ) -> PendingItem:
        current_now = _coerce_now(now)
        item.status = "pendente"
        item.horario_lembrete = target_at
        item.quantidade_adiamentos += 1
        item.adiamentos_count += 1
        item.ultimo_adiamento_em = current_now
        item.tempo_parado = timedelta(0)
        postponed_minutes = max(1, int((target_at - current_now).total_seconds() // 60))
        self._append_history(
            item,
            "adiada",
            detail=f"Adiada por {postponed_minutes} minuto(s).",
            now=current_now,
        )
        self._record_theme_behavior(item, event="postponed", now=current_now)
        return item

    def collect_due_reminder_messages(self, *, now: datetime | None = None) -> list[str]:
        announcements = self.collect_due_reminder_announcements(now=now)
        return [announcement.message for announcement in announcements]

    def collect_due_reminder_announcements(
        self,
        *,
        now: datetime | None = None,
    ) -> list[PendingAnnouncement]:
        current_now = _coerce_now(now)
        self._collect_due_into_queue(now=current_now)
        if not self.can_announce_now(now=current_now):
            return []

        announce_items = self._take_announcable_from_queue(now=current_now)
        if not announce_items:
            return []

        message = self._build_grouped_reminder_message(announce_items, now=current_now)
        self._last_announcement_item_ids = [item.id for item in announce_items]
        for item in announce_items:
            item.ultimo_lembrete = current_now
            item.anunciado_count += 1
            item.ultimo_anuncio_em = current_now
            self._append_history(
                item,
                "lembrete_emitido",
                detail=message,
                now=current_now,
            )
            self._reschedule_recurring(item, now=current_now)
        self._activate_confirmation_context(
            item_id=announce_items[0].id,
            announced_at=current_now,
        )
        self.record_kira_reply(now=current_now)
        return [
            PendingAnnouncement(
                message=message,
                item_ids=[item.id for item in announce_items],
                primary_item_id=announce_items[0].id,
            )
        ]

    def set_announcement_busy(self, busy: bool, *, now: datetime | None = None) -> None:
        if busy:
            self._external_busy_until = datetime.max.replace(tzinfo=_coerce_now(now).tzinfo)
            return
        self._external_busy_until = None

    def can_announce_now(self, *, now: datetime | None = None) -> bool:
        current_now = _coerce_now(now)
        if self._external_busy_until is not None and current_now < self._external_busy_until:
            return False
        if self.ultimo_lembrete_anunciado_em is None:
            return self.is_good_moment_for_proactive_reminder(now=current_now)
        return (
            (current_now - self.ultimo_lembrete_anunciado_em) >= _ANNOUNCE_COOLDOWN
            and self.is_good_moment_for_proactive_reminder(now=current_now)
        )

    def next_announce_window(self, *, now: datetime | None = None) -> datetime:
        current_now = _coerce_now(now)
        if self._external_busy_until is not None and current_now < self._external_busy_until:
            return self._external_busy_until
        if self.ultimo_lembrete_anunciado_em is None:
            return current_now
        return max(current_now, self.ultimo_lembrete_anunciado_em + _ANNOUNCE_COOLDOWN)

    def queue_size(self) -> int:
        return len(self._queued_due_reminder_ids)

    def is_good_moment_for_proactive_reminder(self, *, now: datetime | None = None) -> bool:
        current_now = _coerce_now(now)
        if self.last_user_interaction_at is not None:
            if (current_now - self.last_user_interaction_at) < _MIN_USER_SILENCE_FOR_PROACTIVE:
                return False
        if self.last_kira_reply_at is not None:
            if (current_now - self.last_kira_reply_at) < _MIN_KIRA_SILENCE_FOR_PROACTIVE:
                return False
        return True

    def _collect_due_into_queue(self, *, now: datetime) -> None:
        for item in self.open_items():
            # Lembretes com timer proprio (ReminderScheduler) sao entregues pelo
            # proprio scheduler no horario exato; aqui eles existem apenas para
            # rastreamento de status. Sem este skip, o loop proativo os entrega
            # DE NOVO -> duas falas competindo pelo canal de voz (uma generica do
            # scheduler, outra "com personalidade" do pending), causando a
            # divergencia/corte que o usuario via ("Tudo certo, Chefe." em vez do
            # lembrete). O scheduler e o dono correto de lembretes explicitos
            # porque dispara sempre, sem os gates de "bom momento" do pending.
            if str(getattr(item, "app_origem", "") or "").strip().lower() == "reminder_scheduler":
                continue
            next_attention = self._next_attention_at(item)
            if next_attention is None or next_attention > now:
                continue
            self.enqueue_due_reminder(item.id)

    def enqueue_due_reminder(self, item_id: str) -> None:
        if item_id in self._queued_due_reminder_ids:
            return
        self._queued_due_reminder_ids.append(item_id)

    def _take_announcable_from_queue(self, *, now: datetime) -> list[PendingItem]:
        queued_items: list[PendingItem] = []
        remaining_ids: list[str] = []
        for item_id in self._queued_due_reminder_ids:
            item = self._store.get(item_id)
            if item is None or not item.is_open():
                continue
            next_attention = self._next_attention_at(item)
            if next_attention is None or next_attention > now:
                remaining_ids.append(item_id)
                continue
            queued_items.append(item)
        self._queued_due_reminder_ids = remaining_ids
        if not queued_items:
            return []

        queued_items.sort(
            key=lambda item: (
                _priority_order(self._effective_priority(item, now=now)),
                self._next_attention_at(item) or now,
                item.horario_criacao,
            )
        )

        first_due = self._next_attention_at(queued_items[0]) or now
        batch: list[PendingItem] = []
        for item in queued_items:
            item_due = self._next_attention_at(item) or now
            if not self._can_announce_item_now(item, now=now):
                self.enqueue_due_reminder(item.id)
                continue
            if len(batch) >= _MAX_GROUP_ITEMS:
                self.enqueue_due_reminder(item.id)
                continue
            if batch and (item_due - first_due) > _ANNOUNCE_GROUP_WINDOW:
                self.enqueue_due_reminder(item.id)
                continue
            batch.append(item)
        return batch

    def _can_announce_item_now(self, item: PendingItem, *, now: datetime) -> bool:
        if item.prioridade == "alta":
            return True
        if item.modo_item == "open":
            return self.is_good_moment_for_proactive_reminder(now=now)
        return True

    def _build_grouped_reminder_message(self, items: list[PendingItem], *, now: datetime) -> str:
        if not items:
            return ""
        if len(items) == 1:
            return self._build_single_priority_reminder_message(items[0], now=now)
        snippets = [self._context_snippet(item) for item in items[:_MAX_GROUP_ITEMS]]
        if len(items) == 2:
            joined = f"{snippets[0]} e {snippets[1]}"
            return f"Chefe, voce tem duas pendencias agora: {joined}."
        if len(items) <= _MAX_GROUP_ITEMS:
            joined = ", ".join(snippets[:-1]) + f" e {snippets[-1]}"
            return f"Chefe, voce tem {len(items)} coisas pendentes agora: {joined}."
        joined = ", ".join(snippets)
        return (
            f"Chefe, voce tem {len(items)} pendencias agora. "
            f"As principais sao: {joined}."
        )

    def _build_single_priority_reminder_message(self, item: PendingItem, *, now: datetime) -> str:
        contexto = self._context_snippet(item)
        effective_priority = self._effective_priority(item, now=now)
        suggestion = self._build_action_suggestion(item, now=now)
        insist_mode = self._evaluate_insistence_mode(item, now=now)
        repeated_delay_hint = ""
        if insist_mode == "insistir_firme":
            repeated_delay_hint = " Isso ja passou do ponto de so lembrar."
        elif insist_mode == "insistir_leve":
            repeated_delay_hint = " Isso ja foi adiado algumas vezes."
        if effective_priority == "alta":
            return f"Chefe, esse aqui e importante: {contexto}.{repeated_delay_hint}{suggestion}"
        if effective_priority == "baixa":
            return f"Chefe, so te lembrando: {contexto}.{suggestion}"
        return f"Chefe, passando pra te lembrar de {contexto}.{suggestion}"

    def _effective_priority(self, item: PendingItem, *, now: datetime) -> PendingPriority:
        if item.prioridade == "alta":
            return "alta"
        overdue_boost = 0
        next_attention = self._next_attention_at(item)
        if next_attention is not None and now - next_attention >= timedelta(minutes=30):
            overdue_boost += 1
        if item.adiado_vezes >= 2:
            overdue_boost += 1
        if item.anunciado_count >= 4:
            overdue_boost += 1
        if overdue_boost >= 2:
            return "alta"
        if overdue_boost >= 1 and item.prioridade == "baixa":
            return "media"
        return item.prioridade

    def _context_snippet(self, item: PendingItem) -> str:
        return item.texto_normalizado or item.descricao or item.texto_original or "uma pendencia sua"

    def _build_action_suggestion(self, item: PendingItem, *, now: datetime | None = None) -> str:
        reference_now = (
            now
            or item.ultimo_adiamento_em
            or item.ultimo_anuncio_em
            or item.horario_lembrete
            or item.horario_criacao
        )
        current_now = _coerce_now(reference_now)
        load_state = self.get_user_load_state(now=current_now)
        adaptive = self._build_adaptive_action_hint(item)
        if adaptive:
            return adaptive
        if load_state == "high":
            return " Quer que eu reorganize isso num plano curto?"
        if item.modo_item == "open":
            return " Quer que eu marque um horario pra isso?"
        return " Quer que eu te lembre disso de novo em 30 minutos?"

    def _evaluate_insistence_mode(self, item: PendingItem, *, now: datetime) -> str:
        load_state = self.get_user_load_state(now=now)
        age = now - item.horario_criacao
        theme = self._theme_for_item(item)
        theme_stats = self._behavior_by_theme.get(theme, {})
        theme_ignored = int(theme_stats.get("ignored", 0.0) or 0)

        firm = (
            self._effective_priority(item, now=now) == "alta"
            and item.adiado_vezes >= 3
        ) or (
            item.modo_item == "open"
            and age >= _OPEN_AGE_FOR_INSIST_FIRM
            and item.anunciado_count >= 4
        ) or (
            theme_ignored >= 3 and item.anunciado_count >= 3
        )
        if load_state == "high" and firm:
            return "insistir_leve"
        if firm:
            return "insistir_firme"

        light = (
            item.adiado_vezes >= 2
            or (item.modo_item == "open" and age >= _OPEN_AGE_FOR_INSIST_LIGHT)
            or item.anunciado_count >= 3
        )
        if light:
            return "insistir_leve"
        return "normal"

    def _build_adaptive_action_hint(self, item: PendingItem) -> str:
        if item.adiado_vezes >= 3:
            variants = (
                " Melhor marcar um horario de vez?",
                " Quer que eu replaneje isso em vez de so adiar?",
                " Quer que eu trave um horario fixo para isso?",
            )
            return variants[item.adiado_vezes % len(variants)]
        if item.concluido_count >= 2 and 0 < item.tempo_medio_para_concluir_segundos <= 20 * 60:
            variants = (
                " Isso voce costuma resolver rapidinho.",
                " Esse tipo voce normalmente fecha rapido.",
                " Esse costuma ser rapido para voce.",
            )
            return variants[item.concluido_count % len(variants)]
        if item.ignorado_count >= 2:
            variants = (
                " Quer que eu replaneje isso em vez de insistir agora?",
                " Quer que eu jogue isso para um horario melhor?",
                " Quer transformar isso num plano simples para nao acumular?",
            )
            return variants[item.ignorado_count % len(variants)]
        return ""

    def _activate_confirmation_context(self, *, item_id: str, announced_at: datetime) -> None:
        self.ultimo_lembrete_anunciado_id = item_id
        self.ultimo_lembrete_anunciado_em = announced_at
        self.contexto_confirmacao_ativa = True

    def _resolve_confirmation_item(self) -> PendingItem | None:
        if not self.contexto_confirmacao_ativa:
            return None
        if not self.ultimo_lembrete_anunciado_id:
            return None
        return self._store.get(self.ultimo_lembrete_anunciado_id)

    def _clear_confirmation_context(self) -> None:
        self.contexto_confirmacao_ativa = False

    def _handle_confirmation_command(
        self,
        text: str,
        *,
        folded_text: str,
        now: datetime,
    ) -> PendingCommandResult | None:
        item = self._resolve_confirmation_item()
        if item is None:
            return None

        schedule_from_reply = _extract_schedule_or_snooze_from_reply(text, now=now)
        if schedule_from_reply is not None:
            self.postpone_item_to_datetime(item, schedule_from_reply, now=now)
            was_open = item.modo_item == "open"
            item.modo_item = "scheduled"
            item.tipo = "lembrete"
            self._append_history(
                item,
                "convertida_para_agendada",
                detail=f"Convertida para horario {_format_datetime(schedule_from_reply)}.",
                now=now,
            )
            self._clear_confirmation_context()
            return PendingCommandResult(
                handled=True,
                action="converter_agendada" if was_open else "adiar",
                item=item,
                message=f"Chefe, perfeito. Deixei isso agendado para {_format_datetime(schedule_from_reply)}.",
            )

        if "marca como importante" in folded_text or "deixa importante" in folded_text:
            item.prioridade = "alta"
            self._append_history(item, "prioridade_alterada", detail="Prioridade elevada para alta.", now=now)
            return PendingCommandResult(
                handled=True,
                action="prioridade_alta",
                item=item,
                message="Chefe, marquei isso como importante.",
            )

        if any(pattern.search(folded_text) for pattern in _IGNORE_PATTERNS):
            self._mark_item_ignored(item, now=now, detail="Ignorada pelo usuario.")
            self._clear_confirmation_context()
            return PendingCommandResult(
                handled=True,
                action="ignorar",
                item=item,
                message="Chefe, beleza. Vou ignorar esse lembrete.",
            )

        if any(pattern.search(folded_text) for pattern in _COMPLETE_PATTERNS):
            self._mark_item_completed(item, now=now, detail="Concluida apos anuncio.")
            self._clear_confirmation_context()
            return PendingCommandResult(
                handled=True,
                action="concluir",
                item=item,
                message="Chefe, beleza, vou considerar isso concluido.",
            )

        if any(pattern.search(folded_text) for pattern in _NOT_DONE_PATTERNS):
            self._append_history(item, "nao_concluida", detail="Usuario informou que ainda nao fez.", now=now)
            return PendingCommandResult(
                handled=True,
                action="nao_fez",
                item=item,
                message="Chefe, ainda deixei isso pendente. Posso te lembrar de novo daqui a pouco.",
            )

        if self._looks_like_postpone_command(folded_text) or _looks_like_defer_without_time(folded_text):
            target = _extract_postpone_datetime(text, now=now) or (now + timedelta(minutes=30))
            updated = self.postpone_item_to_datetime(item, target, now=now)
            self._clear_confirmation_context()
            return PendingCommandResult(
                handled=True,
                action="adiar",
                item=updated,
                message="Chefe, adiei esse lembrete para mais tarde.",
            )

        return None

    def _handle_microplan_confirmation(
        self,
        text: str,
        *,
        folded_text: str,
        now: datetime,
    ) -> PendingCommandResult | None:
        if not self._pending_micro_plan:
            return None
        if (now - (self._pending_micro_plan_created_at or now)) > timedelta(minutes=15):
            self._pending_micro_plan = []
            self._pending_micro_plan_created_at = None
            return None
        if folded_text in {"sim", "pode fazer", "faz isso", "deixa assim", "pode"}:
            applied = self.apply_micro_plan_to_items(now=now)
            return PendingCommandResult(
                handled=True,
                action="microplano_aplicado",
                message=f"Chefe, apliquei o microplano em {applied} pendencia(s).",
            )
        if "so joga o estudo pra amanha" in folded_text or "só joga o estudo pra amanhã" in text.lower():
            self._apply_partial_micro_plan_for_theme("estudo", now=now)
            return PendingCommandResult(
                handled=True,
                action="microplano_parcial",
                message="Chefe, joguei o estudo para amanha e mantive o resto como estava.",
            )
        if "nao" in folded_text and "so me lembra depois" in folded_text:
            self._pending_micro_plan = []
            self._pending_micro_plan_created_at = None
            return PendingCommandResult(
                handled=True,
                action="microplano_cancelado",
                message="Chefe, fechado. Vou so te lembrar depois, sem aplicar microplano.",
            )
        return None

    def _apply_partial_micro_plan_for_theme(self, theme: str, *, now: datetime) -> None:
        tomorrow = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
        remaining: list[tuple[str, datetime]] = []
        for item_id, slot in self._pending_micro_plan:
            item = self._store.get(item_id)
            if item is None:
                continue
            if self._theme_for_item(item) == theme:
                self.postpone_item_to_datetime(item, tomorrow, now=now)
                item.modo_item = "scheduled"
                item.tipo = "lembrete"
                self._append_history(
                    item,
                    "microplano_parcial_aplicado",
                    detail=f"Tema {theme} movido para {_format_datetime(tomorrow)}.",
                    now=now,
                )
            else:
                remaining.append((item_id, slot))
        self._pending_micro_plan = remaining

    def _create_or_refresh(
        self,
        draft: PendingDraft,
        *,
        now: datetime | None = None,
    ) -> tuple[PendingItem, bool]:
        current_now = _coerce_now(now)
        for item in self._store.iter_reverse():
            if not item.is_open():
                continue
            same_tipo = item.tipo == draft.tipo
            same_descricao = _fold_text(item.descricao) == _fold_text(draft.descricao)
            same_pessoa = _fold_text(item.pessoa or "") == _fold_text(draft.pessoa or "")
            if not (same_tipo and same_descricao and same_pessoa):
                continue
            age_seconds = (current_now - item.horario_criacao).total_seconds()
            if age_seconds > 10 * 60:
                continue
            item.prioridade = draft.prioridade
            item.categoria = draft.categoria
            item.app_origem = draft.app_origem or item.app_origem
            item.texto_original = draft.texto_original or item.texto_original
            item.texto_normalizado = draft.texto_normalizado or item.texto_normalizado
            item.modo_item = draft.modo_item
            item.recorrencia_tipo = draft.recorrencia_tipo
            item.recorrencia_dia_semana = draft.recorrencia_dia_semana
            if draft.horario_lembrete is not None:
                item.horario_lembrete = draft.horario_lembrete
                if item.status == "adiado":
                    item.status = "pendente"
            self._append_history(
                item,
                "reconfirmada",
                detail="Pendencia relembrada sem duplicar.",
                now=current_now,
            )
            return item, False

        item = PendingItem(
            id=f"pend_{uuid4().hex[:12]}",
            tipo=draft.tipo,
            descricao=draft.descricao,
            texto_original=draft.texto_original,
            texto_normalizado=draft.texto_normalizado,
            pessoa=draft.pessoa,
            app_origem=draft.app_origem,
            categoria=draft.categoria,
            prioridade=draft.prioridade,
            modo_item=draft.modo_item,
            recorrencia_tipo=draft.recorrencia_tipo,
            recorrencia_dia_semana=draft.recorrencia_dia_semana,
            horario_criacao=current_now,
            horario_lembrete=draft.horario_lembrete,
        )
        self._append_history(item, "criada", detail="Pendencia registrada.", now=current_now)
        self._store.add(item)
        return item, True

    def _build_register_message(self, item: PendingItem) -> str:
        if item.tipo == "comunicacao":
            target = f" com {item.pessoa}" if item.pessoa else ""
            return f"Beleza, deixei uma comunicacao pendente{target}."
        if item.modo_item == "open":
            return "Chefe, deixei isso como pendencia. Posso te lembrar disso mais tarde."
        if item.tipo == "lembrete":
            if item.recorrencia_tipo != "none":
                if item.recorrencia_tipo == "daily":
                    return "Chefe, anotei isso como um lembrete diario."
                if item.recorrencia_tipo == "weekdays":
                    return "Chefe, anotei isso como um lembrete de segunda a sexta."
                return "Chefe, anotei isso como um lembrete semanal."
            if item.horario_lembrete is not None:
                return f"Chefe, vou te lembrar disso as {_format_datetime(item.horario_lembrete)}."
            return "Chefe, deixei isso como lembrete."
        return "Beleza, deixei essa tarefa pendente."

    def _build_register_context_note(self, item: PendingItem) -> str:
        detail = (
            f"[PENDENCIAS] Nova pendencia registrada tipo={item.tipo} "
            f"descricao={item.descricao!r} prioridade={item.prioridade} "
            f"categoria={item.categoria} modo={item.modo_item} app={item.app_origem or 'nenhum'}."
        )
        if item.horario_lembrete is not None:
            return (
                f"{detail[:-1]} Horario do lembrete={_format_datetime(item.horario_lembrete)}."
            )
        return detail

    def _build_due_message(self, item: PendingItem) -> str:
        if item.tipo == "comunicacao":
            target = item.pessoa or item.descricao
            return f"Voce ainda tem uma resposta pendente: {target}."
        if item.tipo == "lembrete":
            contexto = item.texto_normalizado or item.descricao or item.texto_original
            if item.modo_item == "open":
                aging = ""
                if item.adiado_vezes >= 3 or item.anunciado_count >= 4:
                    aging = " Isso continua pendente ha um tempo."
                return f"Chefe, voce ainda tem a pendencia de {contexto}.{aging}"
            return f"Chefe, voce deixou pendente: {contexto}."
        return f"Voce ainda tem essa tarefa pendente: {item.descricao}."

    def _next_attention_at(self, item: PendingItem) -> datetime | None:
        if not item.is_open():
            return None
        if item.tipo == "lembrete" and item.recorrencia_tipo != "none":
            return item.horario_lembrete
        last_emitted = self._last_history_at(item, "lembrete_emitido")
        if last_emitted is not None:
            return last_emitted + _PRIORITY_WINDOWS[item.prioridade]
        if item.horario_lembrete is not None:
            return item.horario_lembrete
        return item.horario_criacao + _PRIORITY_WINDOWS[item.prioridade]

    def _reschedule_recurring(self, item: PendingItem, *, now: datetime) -> None:
        if item.recorrencia_tipo == "none" or item.horario_lembrete is None:
            return
        next_run = _next_recurrence_datetime(
            tipo=item.recorrencia_tipo,
            base=item.horario_lembrete,
            day_of_week=item.recorrencia_dia_semana,
            now=now,
        )
        if next_run is None:
            return
        item.horario_lembrete = next_run
        item.status = "pendente"
        self._append_history(
            item,
            "recorrencia_reagendada",
            detail=f"Proxima execucao em {_format_datetime(next_run)}.",
            now=now,
        )

    def _last_history_at(self, item: PendingItem, action: str) -> datetime | None:
        return self._store.last_history_at(item, action)

    def _looks_like_query(self, folded_text: str) -> bool:
        return any(pattern.search(folded_text) for pattern in _QUERY_PATTERNS)

    def _looks_like_progress_query(self, folded_text: str) -> bool:
        return any(pattern.search(folded_text) for pattern in _PROGRESS_QUERY_PATTERNS)

    def _looks_like_plan_query(self, folded_text: str) -> bool:
        return any(pattern.search(folded_text) for pattern in _PLAN_QUERY_PATTERNS)

    def _looks_like_enhanced_day_query(self, folded_text: str) -> bool:
        return any(pattern.search(folded_text) for pattern in _ENHANCED_DAY_QUERY_PATTERNS)

    def _looks_like_most_postponed_query(self, folded_text: str) -> bool:
        return any(pattern.search(folded_text) for pattern in _MOST_POSTPONED_QUERY_PATTERNS)

    def _looks_like_postpone_command(self, folded_text: str) -> bool:
        return any(pattern.search(folded_text) for pattern in _POSTPONE_PATTERNS)

    def _looks_like_complete_command(self, folded_text: str) -> bool:
        return any(pattern.search(folded_text) for pattern in _COMPLETE_PATTERNS)

    def _append_history(
        self,
        item: PendingItem,
        action: str,
        *,
        detail: str,
        now: datetime | None = None,
    ) -> None:
        self._store.record_history(item, action, detail=detail, now=now)

    def _mark_item_completed(self, item: PendingItem, *, now: datetime, detail: str) -> None:
        item.status = "feito"
        item.tempo_parado = timedelta(0)
        item.concluido_count += 1
        item.ultima_conclusao_em = now
        seconds_to_complete = max(0.0, (now - item.horario_criacao).total_seconds())
        previous_count = max(item.concluido_count - 1, 0)
        if previous_count == 0:
            item.tempo_medio_para_concluir_segundos = seconds_to_complete
        else:
            item.tempo_medio_para_concluir_segundos = (
                (item.tempo_medio_para_concluir_segundos * previous_count) + seconds_to_complete
            ) / (previous_count + 1)
        self._append_history(item, "concluida", detail=detail, now=now)
        self._record_theme_behavior(item, event="completed", now=now)

    def _mark_item_ignored(self, item: PendingItem, *, now: datetime, detail: str) -> None:
        item.status = "cancelado"
        item.ignorado_count += 1
        self._append_history(item, "cancelada", detail=detail, now=now)
        self._record_theme_behavior(item, event="ignored", now=now)

    def _record_theme_behavior(self, item: PendingItem, *, event: str, now: datetime) -> None:
        theme = self._theme_for_item(item)
        stats = self._behavior_by_theme.setdefault(
            theme,
            {"completed": 0.0, "ignored": 0.0, "postponed": 0.0, "avg_complete_seconds": 0.0},
        )
        if event == "completed":
            stats["completed"] += 1
            if item.tempo_medio_para_concluir_segundos > 0:
                prev = max(stats["completed"] - 1, 0)
                stats["avg_complete_seconds"] = (
                    (stats["avg_complete_seconds"] * prev) + item.tempo_medio_para_concluir_segundos
                ) / max(stats["completed"], 1)
        elif event == "ignored":
            stats["ignored"] += 1
        elif event == "postponed":
            stats["postponed"] += 1
        stats["last_update_ts"] = now.timestamp()

    def _theme_for_item(self, item: PendingItem) -> str:
        folded = _fold_text(item.texto_normalizado or item.descricao or item.texto_original)
        for theme, hints in _THEME_HINTS:
            if any(hint in folded for hint in hints):
                return theme
        return "geral"


def interpret_pending_text(
    text: str,
    *,
    app_origem: str | None = None,
    now: datetime | None = None,
) -> PendingDraft | None:
    current_now = _coerce_now(now)
    original = _normalize_text(text)
    if not original:
        return None

    folded = _fold_text(original)
    if _looks_like_non_pending_text(folded):
        return None

    cleaned_text, remind_at = _extract_time_reference(original, now=current_now)
    cleaned_text, recurrence_tipo, recurrence_day, recurrence_run_at = _extract_recurrence_reference(
        cleaned_text,
        now=current_now,
    )
    if recurrence_run_at is not None and remind_at is None:
        remind_at = recurrence_run_at
    if remind_at is None and "mais tarde" in _fold_text(original):
        remind_at = current_now + _DEFAULT_VAGUE_REMINDER_DELAY
    folded_cleaned = _fold_text(cleaned_text)
    if not cleaned_text:
        return None

    communication_person = _extract_communication_person(cleaned_text)
    is_open_pending_candidate = _looks_like_open_pending_text(folded)
    modo_item: PendingMode = "scheduled" if (remind_at is not None or recurrence_tipo != "none") else "open"
    if remind_at is not None or recurrence_tipo != "none":
        tipo: PendingType = "lembrete"
    elif communication_person is not None:
        tipo = "comunicacao"
    elif is_open_pending_candidate:
        tipo = "tarefa"
    elif not _looks_like_task_text(folded_cleaned):
        return None
    else:
        tipo = "tarefa"

    descricao = _cleanup_description(cleaned_text)
    if not descricao:
        return None

    return PendingDraft(
        tipo=tipo,
        descricao=descricao,
        texto_original=original,
        texto_normalizado=descricao,
        pessoa=communication_person,
        app_origem=_normalize_text(app_origem) or None,
        categoria=_infer_category(folded_cleaned, app_origem=app_origem),
        prioridade=_infer_priority(
            folded_cleaned,
            remind_at=remind_at,
            now=current_now,
            recurrence_tipo=recurrence_tipo,
        ),
        modo_item=modo_item,
        horario_lembrete=remind_at,
        recorrencia_tipo=recurrence_tipo,
        recorrencia_dia_semana=recurrence_day,
    )


def _coerce_now(now: datetime | None) -> datetime:
    return coerce_pending_now(now)


def _extract_postpone_minutes(folded_text: str) -> int | None:
    match = re.search(
        r"\badiar?\s+(\d+)\s*(min|mins|minuto|minutos|h|hora|horas)\b",
        folded_text,
    )
    if match is None:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    if unit.startswith("h") or unit.startswith("hora"):
        return amount * 60
    return amount


def _extract_postpone_datetime(text: str, *, now: datetime) -> datetime | None:
    folded = _fold_text(text)
    if "amanha" in folded and ("adiar" in folded or "depois" in folded or "me lembra" in folded):
        return (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    cleaned, remind_at = extract_time_reference_from_brasilia(text, now=now)
    if remind_at is not None and (
        re.search(r"^(adiar?|adia|me lembra|depois)\b", folded) is not None
        or "me lembra" in _fold_text(cleaned)
    ):
        return remind_at
    if re.fullmatch(r"(depois|me lembra depois|adiar isso)", folded):
        return now + timedelta(minutes=30)
    return None


def _extract_schedule_or_snooze_from_reply(text: str, *, now: datetime) -> datetime | None:
    folded = _fold_text(text)
    if folded in {"depois", "me lembra depois", "adiar", "adiar isso"}:
        return None
    if "daqui" in folded or "amanha" in folded or "hoje" in folded or "me lembra" in folded:
        direct = _extract_postpone_datetime(text, now=now)
        if direct is not None:
            return direct
    _, parsed_clock = extract_time_reference_from_brasilia(text, now=now)
    if parsed_clock is not None:
        return parsed_clock
    if folded.startswith("sim"):
        _, remind_at = extract_time_reference_from_brasilia(text, now=now)
        if remind_at is not None:
            return remind_at
    return None


def _extract_time_reference(
    text: str,
    *,
    now: datetime,
) -> tuple[str, datetime | None]:
    cleaned, remind_at = extract_time_reference_from_brasilia(text, now=now)
    return _cleanup_description(cleaned), remind_at


def _extract_recurrence_reference(
    text: str,
    *,
    now: datetime,
) -> tuple[str, RecurrenceType, int | None, datetime | None]:
    normalized = _normalize_text(text)
    folded = _fold_text(normalized)
    cleaned, parsed_time = extract_time_reference_from_brasilia(normalized, now=now)

    if re.search(r"\b(todo dia|todos os dias)\b", folded):
        cleaned = re.sub(
            r"\b(todo dia|todos os dias)\b",
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )
        run_at = _next_recurrence_datetime("daily", parsed_time or now, None, now=now)
        return _cleanup_description(cleaned), "daily", None, run_at

    weekdays_match = re.search(r"\bde segunda a sexta\b", folded)
    if weekdays_match:
        cleaned = _remove_text_slice(cleaned, weekdays_match.span())
        run_at = _next_recurrence_datetime("weekdays", parsed_time or now, None, now=now)
        return _cleanup_description(cleaned), "weekdays", None, run_at

    weekly_match = re.search(r"\btoda\s+([a-z\-]+)\b", folded)
    if weekly_match:
        day_token = weekly_match.group(1)
        day_idx = _WEEKDAY_NAME_TO_INT.get(day_token)
        if day_idx is not None:
            cleaned = _remove_text_slice(cleaned, weekly_match.span())
            run_at = _next_recurrence_datetime("weekly", parsed_time or now, day_idx, now=now)
            return _cleanup_description(cleaned), "weekly", day_idx, run_at

    return _cleanup_description(normalized), "none", None, None


def _next_recurrence_datetime(
    tipo: RecurrenceType,
    base: datetime,
    day_of_week: int | None,
    *,
    now: datetime,
) -> datetime | None:
    start = base if base > now else now
    if tipo == "daily":
        candidate = start.replace(second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    if tipo == "weekdays":
        candidate = start.replace(second=0, microsecond=0)
        while candidate.weekday() >= 5 or candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    if tipo == "weekly" and day_of_week is not None:
        candidate = start.replace(second=0, microsecond=0)
        days_ahead = (day_of_week - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate
    return None


def _extract_communication_person(text: str) -> str | None:
    normalized = _normalize_text(text)
    if not normalized:
        return None
    for verb in _COMMUNICATION_VERBS:
        match = re.match(rf"^{re.escape(verb)}\b", normalized, flags=re.IGNORECASE)
        if match is None:
            continue
        remainder = _normalize_text(normalized[match.end():])
        remainder = re.sub(
            r"^(?:pra|pro|para|com|o|a|os|as)\s+",
            "",
            remainder,
            flags=re.IGNORECASE,
        )
        if not remainder:
            return None
        person_tokens: list[str] = []
        for token in remainder.split():
            clean_token = token.strip(".,:;!?")
            folded_token = _fold_text(clean_token)
            if not folded_token:
                continue
            if folded_token in _COMMUNICATION_STOP_WORDS:
                break
            if re.fullmatch(r"\d{1,2}(?::\d{2})?", folded_token):
                break
            person_tokens.append(clean_token)
            if len(person_tokens) >= 3:
                break
        person = _normalize_text(" ".join(person_tokens))
        return person or None
    return None


def _looks_like_task_text(folded_text: str) -> bool:
    tokens = folded_text.split()
    if not tokens:
        return False
    if len(tokens) > 12:
        return False
    return tokens[0] in _TASK_VERBS


def _looks_like_open_pending_text(folded_text: str) -> bool:
    if any(hint in folded_text for hint in _OPEN_PENDING_HINTS):
        # evita registrar frases vagas sem objeto.
        tail_tokens = folded_text.split()
        return len(tail_tokens) >= 4
    return False


def _looks_like_non_pending_text(folded_text: str) -> bool:
    if any(folded_text.startswith(prefix) for prefix in _QUESTION_PREFIXES):
        return True
    return folded_text.endswith("?")


def _looks_like_defer_without_time(folded_text: str) -> bool:
    if folded_text in {"me lembra depois", "depois", "adiar isso"}:
        return True
    return folded_text.startswith("me lembra depois de ")


def _cleanup_description(text: str) -> str:
    cleaned = _normalize_text(text)
    cleaned = re.sub(r"\b(?:depois|mais tarde)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:anota|anotar|lembrar|me lembra|lembra)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:isso|isto)\b$", " ", cleaned, flags=re.IGNORECASE)
    return _normalize_text(cleaned).strip(" -,:;.")


def _remove_text_slice(text: str, span: tuple[int, int]) -> str:
    start, end = span
    return _normalize_text(f"{text[:start]} {text[end:]}")


def _infer_category(folded_text: str, *, app_origem: str | None) -> str:
    app_folded = _fold_text(app_origem or "")
    if any(hint in folded_text for hint in _WORK_HINTS) or any(hint in app_folded for hint in _WORK_HINTS):
        return "trabalho"
    return "pessoal"


def _infer_priority(
    folded_text: str,
    *,
    remind_at: datetime | None,
    now: datetime,
    recurrence_tipo: RecurrenceType = "none",
) -> PendingPriority:
    if any(hint in folded_text for hint in _HIGH_PRIORITY_HINTS):
        return "alta"
    if recurrence_tipo != "none":
        return "media"
    if remind_at is not None and (remind_at - now) <= timedelta(hours=2):
        return "alta"
    if remind_at is None and any(hint in folded_text for hint in ("lembrar", "depois", "mais tarde")):
        return "baixa"
    if any(hint in folded_text for hint in _LOW_PRIORITY_HINTS):
        return "baixa"
    return "media"


def _priority_order(priority: PendingPriority) -> int:
    if priority == "alta":
        return 0
    if priority == "media":
        return 1
    return 2


def _format_datetime(value: datetime | None) -> str:
    return format_brasilia(value)


__all__ = [
    "PendingCommandResult",
    "PendingDraft",
    "PendingItem",
    "PendingPriority",
    "PendingRegistry",
    "PendingStatus",
    "PendingStore",
    "PendingType",
    "interpret_pending_text",
]
