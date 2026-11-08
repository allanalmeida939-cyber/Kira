import ctypes
import json
import logging
import re
import threading
import unicodedata
import uuid
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path

from time_utils import (
    combine_date_time_brasilia,
    ensure_brasilia,
    extract_time_reference_from_brasilia,
    now_brasilia,
    today_brasilia,
)


logger = logging.getLogger(__name__)


class AgendaError(Exception):
    pass


class AgendaManager:
    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = Path(base_dir or Path.cwd())
        self._agenda_path = self._base_dir / "agenda.json"
        self._reminders_path = self._base_dir / "lembretes.json"
        self._lock = threading.RLock()
        self._active_jobs: dict[str, threading.Event] = {}
        self._event_watcher_stop = threading.Event()
        self._ensure_storage()
        self._restore_schedules()
        self._start_event_watcher()

    def create_event(self, titulo: str, data_texto: str, hora_texto: str) -> dict:
        event_date = self._parse_date(data_texto)
        event_time = self._parse_time(hora_texto)
        evento = {
            "id": self._new_id(),
            "titulo": titulo.strip(),
            "data": event_date.isoformat(),
            "hora": event_time.strftime("%H:%M"),
            "status": "pendente",
            "avisos_emitidos": [],
            "criado_em": now_brasilia().isoformat(),
        }
        with self._lock:
            agenda = self._read_json(self._agenda_path)
            agenda.append(evento)
            agenda.sort(key=lambda item: (item.get("data", ""), item.get("hora", ""), item.get("titulo", "")))
            self._write_json(self._agenda_path, agenda)
        return evento

    def create_event_from_text(self, frase: str) -> dict:
        titulo, event_date, event_time = self._parse_event_text(frase)
        return self.create_event(titulo, event_date.isoformat(), event_time.strftime("%H:%M"))

    def events_for_today(self) -> list[dict]:
        return self.events_for_date(today_brasilia().isoformat())

    def events_for_date(self, data_texto: str) -> list[dict]:
        wanted = self._parse_date(data_texto).isoformat()
        with self._lock:
            agenda = self._read_json(self._agenda_path)
        eventos = [item for item in agenda if item.get("data") == wanted]
        eventos.sort(key=lambda item: (item.get("hora", ""), item.get("titulo", "")))
        return eventos

    def create_one_time_reminder(
        self,
        texto: str,
        horario_texto: str,
        data_texto: str | None = None,
    ) -> dict:
        reminder_date = self._parse_date(data_texto) if data_texto else today_brasilia()
        reminder_time = self._parse_time(horario_texto)
        run_at = combine_date_time_brasilia(reminder_date, reminder_time)
        if run_at <= now_brasilia():
            raise AgendaError("O lembrete precisa ficar no futuro.")

        lembrete = {
            "id": self._new_id(),
            "tipo": "unico",
            "texto": texto.strip(),
            "data": reminder_date.isoformat(),
            "hora": reminder_time.strftime("%H:%M"),
            "proxima_execucao": run_at.isoformat(),
            "ativo": True,
            "criado_em": now_brasilia().isoformat(),
        }
        with self._lock:
            lembretes = self._read_json(self._reminders_path)
            lembretes.append(lembrete)
            self._write_json(self._reminders_path, lembretes)

        self._schedule_reminder(lembrete)
        return lembrete

    def create_one_time_reminder_from_text(self, frase: str) -> dict:
        texto, run_at = self._parse_one_time_reminder_text(frase)
        return self.create_one_time_reminder(
            texto=texto,
            horario_texto=run_at.strftime("%H:%M"),
            data_texto=run_at.date().isoformat(),
        )

    def create_recurring_reminder(
        self,
        texto: str,
        frequencia: str,
        horario_texto: str | None = None,
        dia_semana: str | None = None,
        dia_mes: int | None = None,
    ) -> dict:
        frequency = self._normalize_frequency(frequencia)
        run_time = self._parse_time(horario_texto or "09:00")

        lembrete = {
            "id": self._new_id(),
            "tipo": "recorrente",
            "texto": texto.strip(),
            "frequencia": frequency,
            "hora": run_time.strftime("%H:%M"),
            "dia_semana": self._normalize_weekday(dia_semana) if dia_semana else None,
            "dia_mes": dia_mes,
            "ativo": True,
            "criado_em": now_brasilia().isoformat(),
        }
        next_run = self._next_recurring_run(lembrete, now=now_brasilia())
        lembrete["proxima_execucao"] = next_run.isoformat()

        with self._lock:
            lembretes = self._read_json(self._reminders_path)
            lembretes.append(lembrete)
            self._write_json(self._reminders_path, lembretes)

        self._schedule_reminder(lembrete)
        return lembrete

    def create_recurring_reminder_from_text(self, frase: str) -> dict:
        texto, frequencia, horario, dia_semana, dia_mes = self._parse_recurring_reminder_text(frase)
        return self.create_recurring_reminder(
            texto=texto,
            frequencia=frequencia,
            horario_texto=horario,
            dia_semana=dia_semana,
            dia_mes=dia_mes,
        )

    def list_active_reminders(self) -> list[dict]:
        with self._lock:
            lembretes = self._read_json(self._reminders_path)
        ativos = [item for item in lembretes if item.get("ativo", True)]
        ativos.sort(key=lambda item: item.get("proxima_execucao", ""))
        return ativos

    def cancel_event(self, identificador: str) -> dict:
        with self._lock:
            agenda = self._read_json(self._agenda_path)
            index, evento = self._find_matching_item(agenda, identificador, "titulo")
            if evento is None:
                raise AgendaError("Nao encontrei evento com esse id ou titulo.")
            removido = agenda.pop(index)
            self._write_json(self._agenda_path, agenda)
        return removido

    def check_upcoming_events(self) -> list[str]:
        now = now_brasilia()
        avisos: list[str] = []
        changed = False
        with self._lock:
            agenda = self._read_json(self._agenda_path)
            for evento in agenda:
                if not isinstance(evento, dict):
                    continue
                if evento.get("status", "pendente") == "feito":
                    continue
                event_dt = self._event_datetime(evento)
                if event_dt is None:
                    continue

                issued = set(evento.get("avisos_emitidos", []))
                diff = event_dt - now
                checks = [
                    ("30_min", timedelta(minutes=29), timedelta(minutes=31), f"Faltam 30 minutos para {evento.get('titulo', 'o evento')}."),
                    ("10_min", timedelta(minutes=9), timedelta(minutes=11), f"Faltam 10 minutos para {evento.get('titulo', 'o evento')}."),
                    ("na_hora", timedelta(seconds=0), timedelta(minutes=1), f"Esta na hora de {evento.get('titulo', 'o evento')}."),
                ]
                for code, lower, upper, message in checks:
                    if code not in issued and lower <= diff <= upper:
                        avisos.append(message)
                        issued.add(code)
                        changed = True

                if (
                    "atrasado" not in issued
                    and timedelta(minutes=-15) <= diff < timedelta(seconds=0)
                    and evento.get("status", "pendente") == "pendente"
                ):
                    avisos.append(f"Tu vai se atrasar ou ja se atrasou para {evento.get('titulo', 'o evento')}.")
                    issued.add("atrasado")
                    changed = True

                if changed:
                    evento["avisos_emitidos"] = sorted(issued)

            if changed:
                self._write_json(self._agenda_path, agenda)
        return avisos

    def time_remaining_for_event(self, identificador: str, margem_min: int = 10) -> str:
        with self._lock:
            agenda = self._read_json(self._agenda_path)
        _, evento = self._find_matching_item(agenda, identificador, "titulo")
        if evento is None:
            raise AgendaError("Nao encontrei evento com esse id ou titulo.")
        event_dt = self._event_datetime(evento)
        if event_dt is None:
            raise AgendaError("Evento com data ou hora invalida.")

        minutos = int((event_dt - now_brasilia()).total_seconds() / 60)
        if minutos <= 0:
            return f"O horario de {evento['titulo']} ja chegou ou passou."
        if minutos <= margem_min:
            return f"Faltam {minutos} minutos para {evento['titulo']}. Tu vai se atrasar."
        return f"Ainda faltam {minutos} minutos para {evento['titulo']}."

    def detect_overdue_items(self) -> list[str]:
        now = now_brasilia()
        atrasadas: list[str] = []
        with self._lock:
            agenda = self._read_json(self._agenda_path)
        for evento in agenda:
            if not isinstance(evento, dict):
                continue
            if evento.get("status", "pendente") != "pendente":
                continue
            event_dt = self._event_datetime(evento)
            if event_dt is None:
                continue
            if now > event_dt:
                atrasadas.append(f"Tu esqueceu: {evento.get('titulo', 'evento sem titulo')}.")
        return atrasadas

    def mark_event_status(self, identificador: str, status: str) -> dict:
        normalized = self._normalize_text(status)
        allowed = {"pendente", "em_andamento", "feito", "atrasado"}
        if normalized not in allowed:
            raise AgendaError("Status invalido. Use pendente, em_andamento, feito ou atrasado.")
        with self._lock:
            agenda = self._read_json(self._agenda_path)
            index, evento = self._find_matching_item(agenda, identificador, "titulo")
            if evento is None:
                raise AgendaError("Nao encontrei evento com esse id ou titulo.")
            agenda[index]["status"] = normalized
            agenda[index]["atualizado_em"] = now_brasilia().isoformat()
            atualizado = dict(agenda[index])
            self._write_json(self._agenda_path, agenda)
        return atualizado

    def cancel_reminder(self, identificador: str) -> dict:
        with self._lock:
            lembretes = self._read_json(self._reminders_path)
            index, lembrete = self._find_matching_item(lembretes, identificador, "texto")
            if lembrete is None:
                raise AgendaError("Nao encontrei lembrete com esse id ou texto.")
            lembretes[index]["ativo"] = False
            lembrete_atualizado = dict(lembretes[index])
            self._write_json(self._reminders_path, lembretes)

        stop_event = self._active_jobs.get(str(lembrete_atualizado.get("id")))
        if stop_event is not None:
            stop_event.set()
        return lembrete_atualizado

    def _ensure_storage(self) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)
        for path in (self._agenda_path, self._reminders_path):
            if not path.exists():
                path.write_text("[]", encoding="utf-8")

    def _read_json(self, path: Path) -> list[dict]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.exception("Falha ao ler %s. Recriando arquivo.", path)
            return []
        return data if isinstance(data, list) else []

    def _write_json(self, path: Path, payload: list[dict]) -> None:
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _event_datetime(self, evento: dict) -> datetime | None:
        data = evento.get("data")
        hora = evento.get("hora")
        if not data or not hora:
            return None
        try:
            target_date = datetime.strptime(str(data), "%Y-%m-%d").date()
            target_time = datetime.strptime(str(hora), "%H:%M").time()
            return combine_date_time_brasilia(target_date, target_time)
        except ValueError:
            return None

    def _restore_schedules(self) -> None:
        with self._lock:
            lembretes = self._read_json(self._reminders_path)
        for lembrete in lembretes:
            if not isinstance(lembrete, dict) or not lembrete.get("ativo", True):
                continue
            self._schedule_reminder(lembrete)

    def _start_event_watcher(self) -> None:
        worker = threading.Thread(
            target=self._event_watcher_loop,
            daemon=True,
        )
        worker.start()

    def _event_watcher_loop(self) -> None:
        while not self._event_watcher_stop.is_set():
            try:
                avisos = self.check_upcoming_events()
                for aviso in avisos:
                    self._notify("Agenda", aviso)
            except Exception:
                logger.exception("Falha ao verificar eventos proximos.")
            self._event_watcher_stop.wait(timeout=30)

    def _schedule_reminder(self, lembrete: dict) -> None:
        reminder_id = str(lembrete.get("id", ""))
        if not reminder_id:
            return

        stop_event = self._active_jobs.get(reminder_id)
        if stop_event is not None:
            stop_event.set()

        stop_event = threading.Event()
        self._active_jobs[reminder_id] = stop_event
        worker = threading.Thread(
            target=self._reminder_loop,
            args=(dict(lembrete), stop_event),
            daemon=True,
        )
        worker.start()

    def _reminder_loop(self, lembrete: dict, stop_event: threading.Event) -> None:
        reminder_id = str(lembrete.get("id", ""))
        while not stop_event.is_set():
            next_run_text = lembrete.get("proxima_execucao")
            if not next_run_text:
                return

            try:
                next_run = ensure_brasilia(datetime.fromisoformat(next_run_text))
            except ValueError:
                logger.warning("Lembrete %s com data invalida: %s", reminder_id, next_run_text)
                return
            if next_run is None:
                return

            delay = (next_run - now_brasilia()).total_seconds()
            if delay > 0:
                waited = stop_event.wait(timeout=min(delay, 60))
                if waited:
                    return
                continue

            self._notify("Lembrete", str(lembrete.get("texto", "")))
            if lembrete.get("tipo") == "unico":
                self._mark_reminder_inactive(reminder_id)
                stop_event.set()
                return

            lembrete["proxima_execucao"] = self._next_recurring_run(
                lembrete,
                now=max(now_brasilia(), next_run),
            ).isoformat()
            self._update_reminder(lembrete)

    def _mark_reminder_inactive(self, reminder_id: str) -> None:
        with self._lock:
            lembretes = self._read_json(self._reminders_path)
            for item in lembretes:
                if item.get("id") == reminder_id:
                    item["ativo"] = False
                    break
            self._write_json(self._reminders_path, lembretes)

    def _update_reminder(self, lembrete_atualizado: dict) -> None:
        with self._lock:
            lembretes = self._read_json(self._reminders_path)
            for index, item in enumerate(lembretes):
                if item.get("id") == lembrete_atualizado.get("id"):
                    lembretes[index] = dict(lembrete_atualizado)
                    break
            self._write_json(self._reminders_path, lembretes)

    def _notify(self, titulo: str, mensagem: str) -> None:
        logger.info("%s: %s", titulo, mensagem)
        print(f"{titulo}: {mensagem}")
        try:
            ctypes.windll.user32.MessageBeep(0xFFFFFFFF)
        except Exception:
            logger.exception("Falha ao emitir alerta sonoro.")

    def _parse_event_text(self, frase: str) -> tuple[str, date, dt_time]:
        original = frase.strip()
        normalized = self._normalize_text(original)
        date_match = self._extract_date_match(normalized)
        time_match = self._extract_time_match(normalized)
        if date_match is None:
            raise AgendaError("Nao consegui entender a data do evento.")
        if time_match is None:
            raise AgendaError("Nao consegui entender o horario do evento.")

        titulo = original
        for token in [date_match.group(0), time_match.group(0)]:
            titulo = re.sub(re.escape(token), " ", titulo, flags=re.IGNORECASE)
        titulo = re.sub(r"\b(marca|marcar|agenda|agendar|cria|criar)\b", " ", titulo, flags=re.IGNORECASE)
        titulo = re.sub(r"\s+", " ", titulo).strip(" -,:")
        if not titulo:
            raise AgendaError("Nao consegui entender o titulo do evento.")

        return titulo, self._parse_date(date_match.group(0)), self._parse_time(time_match.group(0))

    def _parse_one_time_reminder_text(self, frase: str) -> tuple[str, datetime]:
        original = frase.strip()
        normalized = self._normalize_text(original)
        now = now_brasilia()
        cleaned_by_time_utils, parsed_run_at = extract_time_reference_from_brasilia(
            original,
            now=now,
        )
        if parsed_run_at is not None:
            texto = self._cleanup_reminder_text(cleaned_by_time_utils)
            return texto, parsed_run_at

        date_match = self._extract_date_match(normalized)
        time_match = self._extract_time_match(normalized)
        if time_match is None:
            raise AgendaError("Nao consegui entender o horario do lembrete.")

        reminder_date = self._parse_date(date_match.group(0)) if date_match else today_brasilia()
        run_at = combine_date_time_brasilia(reminder_date, self._parse_time(time_match.group(0)))
        if run_at <= now:
            if date_match is None:
                run_at += timedelta(days=1)
            else:
                raise AgendaError("O lembrete precisa ficar no futuro.")

        texto = original
        if date_match:
            texto = re.sub(re.escape(date_match.group(0)), " ", texto, flags=re.IGNORECASE)
        texto = re.sub(re.escape(time_match.group(0)), " ", texto, flags=re.IGNORECASE)
        texto = self._cleanup_reminder_text(texto)
        return texto, run_at

    def _parse_recurring_reminder_text(self, frase: str) -> tuple[str, str, str | None, str | None, int | None]:
        original = frase.strip()
        normalized = self._normalize_text(original)

        if any(token in normalized for token in ["todo dia", "todos os dias", "diario", "diaria"]):
            frequencia = "daily"
        elif any(token in normalized for token in ["toda semana", "toda as semanas", "semanal"]):
            frequencia = "weekly"
        elif any(token in normalized for token in ["todo mes", "todos os meses", "mensal"]):
            frequencia = "monthly"
        else:
            raise AgendaError("Nao consegui entender a frequencia do lembrete recorrente.")

        time_match = self._extract_time_match(normalized)
        weekday_match = self._extract_weekday_match(normalized)
        month_day_match = re.search(r"\bdia\s+(\d{1,2})\b", normalized)

        texto = original
        for token in [
            time_match.group(0) if time_match else None,
            weekday_match.group(0) if weekday_match else None,
            month_day_match.group(0) if month_day_match else None,
        ]:
            if token:
                texto = re.sub(re.escape(token), " ", texto, flags=re.IGNORECASE)
        texto = re.sub(r"\b(me lembra|lembrar|lembra)\b", " ", texto, flags=re.IGNORECASE)
        texto = re.sub(
            r"\b(todo dia|todos os dias|diario|diaria|toda semana|semanal|todo mes|todos os meses|mensal)\b",
            " ",
            texto,
            flags=re.IGNORECASE,
        )
        texto = re.sub(r"\s+", " ", texto).strip(" -,:")
        if not texto:
            raise AgendaError("Nao consegui entender o texto do lembrete recorrente.")

        return (
            texto,
            frequencia,
            time_match.group(0) if time_match else None,
            weekday_match.group(0) if weekday_match else None,
            int(month_day_match.group(1)) if month_day_match else None,
        )

    def _cleanup_reminder_text(self, texto: str) -> str:
        texto = re.sub(r"\b(me lembra|lembrar|lembra)\b", " ", texto, flags=re.IGNORECASE)
        texto = re.sub(r"\bde\b", " ", texto, flags=re.IGNORECASE)
        texto = re.sub(r"\s+", " ", texto).strip(" -,:")
        if not texto:
            raise AgendaError("Nao consegui entender o texto do lembrete.")
        return texto

    def _parse_date(self, value: str | None) -> date:
        if not value:
            return today_brasilia()

        cleaned = self._normalize_text(value)
        today = today_brasilia()
        if cleaned in {"hoje", "today"}:
            return today
        if cleaned in {"amanha", "tomorrow"}:
            return today + timedelta(days=1)

        weekday = self._weekday_from_text(cleaned)
        if weekday is not None:
            return self._next_weekday(today, weekday)

        for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(value.strip(), pattern).date()
            except ValueError:
                continue

        raise AgendaError("Nao consegui entender a data. Use hoje, amanha, um dia da semana, YYYY-MM-DD ou DD/MM/YYYY.")

    def _parse_time(self, value: str) -> dt_time:
        normalized = self._normalize_text(value).replace(" ", "")
        normalized = re.sub(r"^(as|a)", "", normalized)

        patterns = [
            (r"^(\d{1,2}):(\d{2})$", "%H:%M"),
            (r"^(\d{1,2})h(\d{2})$", None),
            (r"^(\d{1,2})h$", None),
            (r"^(\d{1,2})$", None),
        ]
        for regex, fmt in patterns:
            match = re.match(regex, normalized)
            if not match:
                continue
            if fmt:
                return datetime.strptime(normalized, fmt).time()
            hour = int(match.group(1))
            minute = int(match.group(2)) if len(match.groups()) > 1 and match.group(2) else 0
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return dt_time(hour=hour, minute=minute)

        raise AgendaError("Nao consegui entender o horario. Use formatos como 18h, 18:00 ou 14h30.")

    def _normalize_frequency(self, value: str) -> str:
        cleaned = self._normalize_text(value)
        mapping = {
            "todo dia": "daily",
            "todos os dias": "daily",
            "diario": "daily",
            "diaria": "daily",
            "toda semana": "weekly",
            "semanal": "weekly",
            "todo mes": "monthly",
            "todos os meses": "monthly",
            "mensal": "monthly",
        }
        if cleaned in mapping:
            return mapping[cleaned]
        if cleaned in {"daily", "weekly", "monthly"}:
            return cleaned
        raise AgendaError("Frequencia invalida. Use diaria, semanal ou mensal.")

    def _normalize_weekday(self, value: str) -> int:
        cleaned = self._normalize_text(value)
        weekday = self._weekday_from_text(cleaned)
        if weekday is None:
            raise AgendaError("Dia da semana invalido para lembrete recorrente.")
        return weekday

    def _next_recurring_run(self, lembrete: dict, now: datetime) -> datetime:
        reminder_time = self._parse_time(str(lembrete.get("hora", "09:00")))
        base = (ensure_brasilia(now) or now_brasilia()).replace(second=0, microsecond=0)
        frequency = lembrete.get("frequencia")

        if frequency == "daily":
            candidate = combine_date_time_brasilia(base.date(), reminder_time)
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate

        if frequency == "weekly":
            target_weekday = lembrete.get("dia_semana")
            if target_weekday is None:
                target_weekday = now.weekday()
            days_ahead = (int(target_weekday) - base.weekday()) % 7
            candidate = combine_date_time_brasilia(
                base.date() + timedelta(days=days_ahead),
                reminder_time,
            )
            if candidate <= now:
                candidate += timedelta(days=7)
            return candidate

        if frequency == "monthly":
            day = int(lembrete.get("dia_mes") or now.day)
            candidate = self._build_monthly_datetime(now.year, now.month, day, reminder_time)
            if candidate <= now:
                month = now.month + 1
                year = now.year
                if month > 12:
                    month = 1
                    year += 1
                candidate = self._build_monthly_datetime(year, month, day, reminder_time)
            return candidate

        raise AgendaError("Frequencia recorrente nao suportada.")

    def _build_monthly_datetime(self, year: int, month: int, day: int, reminder_time: dt_time) -> datetime:
        if month == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month + 1, 1)
        last_day = (next_month - date(year, month, 1)).days
        safe_day = min(max(day, 1), last_day)
        return combine_date_time_brasilia(
            date(year, month, safe_day),
            reminder_time,
        )

    def _normalize_text(self, value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value.strip().lower())
        return "".join(char for char in normalized if not unicodedata.combining(char))

    def _new_id(self) -> str:
        return uuid.uuid4().hex[:10]

    def _weekday_from_text(self, value: str) -> int | None:
        weekdays = {
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
        return weekdays.get(value)

    def _next_weekday(self, base: date, weekday: int) -> date:
        days_ahead = (weekday - base.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return base + timedelta(days=days_ahead)

    def _find_matching_item(self, items: list[dict], identificador: str, text_field: str) -> tuple[int, dict | None]:
        key = self._normalize_text(identificador)
        if not key:
            return -1, None

        for index, item in enumerate(items):
            if str(item.get("id", "")).lower() == key:
                return index, item

        exact_matches: list[tuple[int, dict]] = []
        partial_matches: list[tuple[int, dict]] = []
        for index, item in enumerate(items):
            text = self._normalize_text(str(item.get(text_field, "")))
            if text == key:
                exact_matches.append((index, item))
            elif key in text or text in key:
                partial_matches.append((index, item))

        matches = exact_matches or partial_matches
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AgendaError("Encontrei mais de um item com esse nome. Use o id para cancelar o certo.")
        return -1, None

    def _extract_date_match(self, normalized_text: str):
        patterns = [
            r"\bhoje\b",
            r"\bamanha\b",
            r"\bsegunda(?:-feira)?\b",
            r"\bterca(?:-feira)?\b",
            r"\bquarta(?:-feira)?\b",
            r"\bquinta(?:-feira)?\b",
            r"\bsexta(?:-feira)?\b",
            r"\bsabado\b",
            r"\bdomingo\b",
            r"\b\d{4}-\d{2}-\d{2}\b",
            r"\b\d{1,2}/\d{1,2}/\d{4}\b",
            r"\b\d{1,2}-\d{1,2}-\d{4}\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, normalized_text)
            if match:
                return match
        return None

    def _extract_weekday_match(self, normalized_text: str):
        return re.search(
            r"\b(segunda(?:-feira)?|terca(?:-feira)?|quarta(?:-feira)?|quinta(?:-feira)?|sexta(?:-feira)?|sabado|domingo)\b",
            normalized_text,
        )

    def _extract_time_match(self, normalized_text: str):
        for pattern in [
            r"\bas\s+\d{1,2}:\d{2}\b",
            r"\b\d{1,2}:\d{2}\b",
            r"\b\d{1,2}h\d{2}\b",
            r"\b\d{1,2}h\b",
            r"\ba\s+\d{1,2}\b",
            r"\bas\s+\d{1,2}\b",
        ]:
            match = re.search(pattern, normalized_text)
            if match:
                value = re.sub(r"^(as|a)\s+", "", match.group(0))
                return _ValueMatch(value)
        return None


class _ValueMatch:
    def __init__(self, value: str) -> None:
        self._value = value

    def group(self, _index: int = 0) -> str:
        return self._value
