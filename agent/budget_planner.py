from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side


class BudgetPlannerError(Exception):
    pass


@dataclass
class MonthlyBudgetDraft:
    file_name: str | None = None
    period: str | None = None
    income_total: float | None = None
    categories: list[str] = field(default_factory=list)
    expense_values: dict[str, float] = field(default_factory=dict)
    notes: str | None = None


class BudgetPlanner:
    def __init__(self, documents_dir: Path | str | None) -> None:
        if not documents_dir:
            documents_dir = Path.home() / "Documents"
        else:
            documents_dir = Path(documents_dir).expanduser()
        self._documents_dir = documents_dir / "Kira" / "Arquivos"
        self._documents_dir.mkdir(parents=True, exist_ok=True)

    def new_draft(self) -> MonthlyBudgetDraft:
        return MonthlyBudgetDraft()

    def update_draft(
        self,
        draft: MonthlyBudgetDraft,
        *,
        file_name: str | None = None,
        period: str | None = None,
        income_total: str | float | None = None,
        categories: str | None = None,
        expense_values: str | None = None,
        notes: str | None = None,
    ) -> MonthlyBudgetDraft:
        if file_name:
            draft.file_name = file_name
        if period:
            draft.period = period
        if income_total is not None:
            draft.income_total = self._parse_money(income_total)
        if categories:
            draft.categories = self._parse_list(categories)
        if expense_values:
            parsed_values = self._parse_value_map(expense_values)
            for category in parsed_values:
                if category not in draft.categories:
                    draft.categories.append(category)
            draft.expense_values.update(parsed_values)
            if not draft.categories:
                draft.categories = list(parsed_values.keys())
        if notes:
            draft.notes = notes
        return draft

    def has_content(self, draft: MonthlyBudgetDraft) -> bool:
        return bool(
            draft.file_name
            or draft.period
            or draft.income_total is not None
            or draft.categories
            or draft.expense_values
            or draft.notes
        )

    def validate_draft(self, draft: MonthlyBudgetDraft) -> list[str]:
        missing: list[str] = []
        if not draft.file_name:
            missing.append("nome_arquivo")
        if not draft.period:
            missing.append("periodo")
        if draft.income_total is None:
            missing.append("renda_total")
        if not draft.categories:
            missing.append("categorias")
        if draft.categories:
            missing_categories = [
                category for category in draft.categories if category not in draft.expense_values
            ]
            if missing_categories:
                missing.append("valores_das_categorias")
        return missing

    def missing_fields_message(self, draft: MonthlyBudgetDraft, missing_fields: list[str]) -> str:
        prompts: list[str] = []
        for field_name in missing_fields:
            if field_name == "nome_arquivo":
                prompts.append("- qual o nome do arquivo")
            elif field_name == "periodo":
                prompts.append("- qual o periodo, por exemplo: marco 2026")
            elif field_name == "renda_total":
                prompts.append("- qual a renda total do mes")
            elif field_name == "categorias":
                prompts.append("- quais categorias de gastos voce quer usar")
            elif field_name == "valores_das_categorias":
                pending = [
                    category for category in draft.categories if category not in draft.expense_values
                ]
                prompts.append(
                    "- me passe os valores de cada categoria neste formato: "
                    + ", ".join(f"{category}=0" for category in pending)
                )
        return "Pra montar essa planilha direito, ainda preciso de:\n" + "\n".join(prompts)

    def build_workbook(self, draft: MonthlyBudgetDraft) -> tuple[Path, dict[str, float]]:
        missing_fields = self.validate_draft(draft)
        if missing_fields:
            raise BudgetPlannerError(self.missing_fields_message(draft, missing_fields))

        assert draft.file_name is not None
        assert draft.period is not None
        assert draft.income_total is not None

        safe_name = self._sanitize_filename(draft.file_name)
        target = self._documents_dir / f"{safe_name}.xlsx"

        workbook = Workbook()
        summary = workbook.active
        summary.title = "Resumo"
        details = workbook.create_sheet("Lancamentos")

        total_spent = sum(draft.expense_values.values())
        remaining = draft.income_total - total_spent
        saved = max(remaining, 0.0)

        self._build_summary_sheet(summary, draft, total_spent, remaining, saved)
        self._build_details_sheet(details, draft)

        workbook.save(target)
        if not target.exists():
            raise BudgetPlannerError("A planilha foi gerada, mas nao apareceu na pasta de destino.")
        return target, {
            "income_total": draft.income_total,
            "total_spent": total_spent,
            "remaining": remaining,
            "saved": saved,
        }

    def update_existing_workbook(
        self,
        *,
        file_name: str | None = None,
        last_path: Path | None = None,
        expense_values: str | None = None,
        income_total: str | float | None = None,
        period: str | None = None,
        notes: str | None = None,
    ) -> tuple[Path, dict[str, float]]:
        target = self.resolve_workbook_path(file_name=file_name, last_path=last_path)
        draft = self.load_draft_from_workbook(target)
        draft = self.update_draft(
            draft,
            income_total=income_total,
            period=period,
            expense_values=expense_values,
            notes=notes,
        )
        rebuilt_path, totals = self.build_workbook(draft)
        return rebuilt_path, totals

    def resolve_workbook_path(self, *, file_name: str | None = None, last_path: Path | None = None) -> Path:
        if file_name:
            sanitized = self._sanitize_filename(file_name)
            candidate = self._documents_dir / f"{sanitized}.xlsx"
            if candidate.exists():
                return candidate

            matches = list(self._documents_dir.glob(f"*{sanitized}*.xlsx"))
            if matches:
                return matches[0]

            raise BudgetPlannerError(f"Nao encontrei a planilha '{file_name}' em {self._documents_dir}.")

        if last_path and Path(last_path).exists():
            return Path(last_path)

        raise BudgetPlannerError("Nao encontrei uma planilha mensal anterior para editar.")

    def load_draft_from_workbook(self, path: Path) -> MonthlyBudgetDraft:
        workbook = load_workbook(path)
        summary = workbook["Resumo"] if "Resumo" in workbook.sheetnames else workbook.active
        details = workbook["Lancamentos"] if "Lancamentos" in workbook.sheetnames else None

        draft = self.new_draft()
        draft.file_name = path.stem

        title_value = summary["A1"].value or ""
        if " - " in title_value:
            _, period = str(title_value).split(" - ", 1)
            draft.period = period.strip()

        income_value = summary["B3"].value
        if income_value is not None:
            draft.income_total = float(income_value)

        if details is not None:
            row = 4
            while True:
                category = details.cell(row, 1).value
                if not category or str(category).strip().lower() == "total":
                    break
                category_name = str(category).strip()
                value = details.cell(row, 2).value or 0
                draft.categories.append(category_name)
                draft.expense_values[category_name] = float(value)
                row += 1

        return draft

    def _build_summary_sheet(
        self,
        ws,
        draft: MonthlyBudgetDraft,
        total_spent: float,
        remaining: float,
        saved: float,
    ) -> None:
        self._setup_columns(ws, [28, 18, 22])
        self._title(ws, "A1:C1", f"Orcamento Mensal - {draft.period}")

        labels = [
            ("Renda total do mes", draft.income_total),
            ("Total gasto no mes", total_spent),
            ("Quanto sobrou", remaining),
            ("Quanto economizou", saved),
        ]

        row = 3
        for label, value in labels:
            ws.cell(row, 1, label)
            value_cell = ws.cell(row, 2, value)
            value_cell.number_format = 'R$ #,##0.00'
            ws.cell(row, 3, "")
            for column in range(1, 4):
                cell = ws.cell(row, column)
                cell.border = self._thin_border()
                if column == 1:
                    cell.font = Font(bold=True)
            row += 1

        detail_header_row = row + 1
        summary_headers = ["Categoria", "Valor informado"]
        for index, header in enumerate(summary_headers, start=1):
            cell = ws.cell(detail_header_row, index, header)
            cell.fill = PatternFill("solid", fgColor="D9EAF7")
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center")
            cell.border = self._thin_border()

        detail_row = detail_header_row + 1
        for category in draft.categories:
            value = draft.expense_values.get(category, 0.0)
            ws.cell(detail_row, 1, category)
            value_cell = ws.cell(detail_row, 2, value)
            value_cell.number_format = 'R$ #,##0.00'
            for column in range(1, 3):
                ws.cell(detail_row, column).border = self._thin_border()
            detail_row += 1

        total_row = detail_row
        ws.cell(total_row, 1, "Total detalhado")
        total_value_cell = ws.cell(total_row, 2, f"=SUM(B{detail_header_row + 1}:B{detail_row - 1})")
        total_value_cell.number_format = 'R$ #,##0.00'
        for column in range(1, 3):
            cell = ws.cell(total_row, column)
            cell.fill = PatternFill("solid", fgColor="E2F0D9")
            cell.font = Font(bold=True)
            cell.border = self._thin_border()

        if draft.notes:
            notes_row = total_row + 2
            ws.merge_cells(start_row=notes_row, start_column=1, end_row=notes_row, end_column=3)
            notes_cell = ws.cell(notes_row, 1, f"Observacoes: {draft.notes}")
            notes_cell.alignment = Alignment(wrap_text=True)

    def _build_details_sheet(self, ws, draft: MonthlyBudgetDraft) -> None:
        self._setup_columns(ws, [28, 18, 18, 24])
        self._title(ws, "A1:D1", "Detalhamento dos Gastos")
        headers = ["Categoria", "Valor", "% da renda", "Observacoes"]
        for index, header in enumerate(headers, start=1):
            cell = ws.cell(3, index, header)
            cell.fill = PatternFill("solid", fgColor="D9EAF7")
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center")
            cell.border = self._thin_border()

        row = 4
        for category in draft.categories:
            value = draft.expense_values.get(category, 0.0)
            ws.cell(row, 1, category)
            value_cell = ws.cell(row, 2, value)
            value_cell.number_format = 'R$ #,##0.00'
            percent_cell = ws.cell(row, 3, f"=B{row}/Resumo!B3")
            percent_cell.number_format = "0.00%"
            ws.cell(row, 4, "")
            for column in range(1, 5):
                ws.cell(row, column).border = self._thin_border()
            row += 1

        total_row = row
        ws.cell(total_row, 1, "Total")
        total_value = ws.cell(total_row, 2, f"=SUM(B4:B{row - 1})")
        total_value.number_format = 'R$ #,##0.00'
        total_percent = ws.cell(total_row, 3, f"=SUM(C4:C{row - 1})")
        total_percent.number_format = "0.00%"
        for column in range(1, 5):
            cell = ws.cell(total_row, column)
            cell.fill = PatternFill("solid", fgColor="E2F0D9")
            cell.font = Font(bold=True)
            cell.border = self._thin_border()

        ws.freeze_panes = "A4"

    def _parse_money(self, value: str | float) -> float:
        if value is None:
            raise BudgetPlannerError("Valor ausente.")

        if isinstance(value, (int, float)):
            return float(value)

        cleaned = str(value).strip().lower()
        if cleaned == "":
            raise BudgetPlannerError("Valor vazio.")

        cleaned = cleaned.replace("r$", "").replace(" ", "")
        if not re.fullmatch(r"[\d.,]+", cleaned):
            raise BudgetPlannerError(f"Nao consegui entender o valor '{value}'.")

        if "." in cleaned and "," in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            integer_part, decimal_part = cleaned.rsplit(",", 1)
            if len(decimal_part) == 3 and integer_part.replace(",", "").isdigit():
                cleaned = cleaned.replace(",", "")
            else:
                cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "." in cleaned:
            integer_part, decimal_part = cleaned.rsplit(".", 1)
            if len(decimal_part) == 3 and integer_part.replace(".", "").isdigit():
                cleaned = cleaned.replace(".", "")

        try:
            return float(cleaned)
        except ValueError as exc:
            raise BudgetPlannerError(f"Nao consegui entender o valor '{value}'.") from exc

    def _parse_list(self, raw_value: str) -> list[str]:
        tokens = re.split(r"[\n,;|]+", raw_value)
        return [token.strip() for token in tokens if token.strip()]

    def _parse_value_map(self, raw_value: str) -> dict[str, float]:
        natural_matches = self._parse_natural_expense_text(raw_value)
        if natural_matches:
            return natural_matches

        result: dict[str, float] = {}
        normalized = raw_value.replace("\n", ";").replace("|", ";")
        if ";" not in normalized and normalized.count("=") > 1:
            normalized = normalized.replace(",", ";")
        entries = [entry.strip() for entry in normalized.split(";") if entry.strip()]
        for entry in entries:
            if "=" not in entry and ":" not in entry:
                raise BudgetPlannerError(
                    "Use o formato categoria=valor. Exemplo: Moradia=1200, Transporte=300"
                )
            separator = "=" if "=" in entry else ":"
            category, value = entry.split(separator, 1)
            category = category.strip()
            value = value.strip()
            if not category or value == "":
                continue
            result[category] = self._parse_money(value)
        return result

    def _parse_natural_expense_text(self, raw_value: str) -> dict[str, float]:
        normalized_text = self._normalize_text(raw_value)
        matches = list(
            re.finditer(
                r"(\d+(?:[.,]\d+)?)\s*(?:reais?)?\s*(?:de|do|da|com|em)?\s+([a-z0-9_ ]+?)(?=,| e |$)",
                normalized_text,
            )
        )
        if not matches:
            return {}

        result: dict[str, float] = {}
        for match in matches:
            value_raw = match.group(1)
            category_raw = match.group(2).strip(" ,.")
            if not category_raw:
                continue
            result[self._restore_category_name(category_raw)] = self._parse_money(value_raw)
        return result

    def _normalize_text(self, value: str) -> str:
        normalized = unicodedata.normalize("NFKD", value)
        ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
        return ascii_only.lower().strip()

    def _restore_category_name(self, value: str) -> str:
        words = [word for word in value.split() if word]
        if not words:
            return value
        return " ".join(word.capitalize() for word in words)

    def _setup_columns(self, ws, widths: list[int]) -> None:
        for index, width in enumerate(widths, start=1):
            ws.column_dimensions[chr(64 + index)].width = width

    def _title(self, ws, merged_range: str, value: str) -> None:
        ws.merge_cells(merged_range)
        cell = ws[merged_range.split(":")[0]]
        cell.value = value
        cell.fill = PatternFill("solid", fgColor="4472C4")
        cell.font = Font(color="FFFFFF", bold=True, size=15)
        cell.alignment = Alignment(horizontal="center", vertical="center")

    def _thin_border(self) -> Border:
        side = Side(style="thin", color="7A7A7A")
        return Border(left=side, right=side, top=side, bottom=side)

    def _sanitize_filename(self, value: str) -> str:
        cleaned = re.sub(r'[<>:"/\\|?*]+', "_", value.strip())
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
        return cleaned or "orcamento_mensal"
