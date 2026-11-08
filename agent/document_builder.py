from __future__ import annotations

import re
import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from time_utils import format_brasilia, now_brasilia


class DocumentBuilderError(Exception):
    pass


@dataclass
class DocumentDraft:
    doc_type: str | None = None
    file_name: str | None = None
    title: str | None = None
    columns: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    period: str | None = None
    destination: str | None = None
    duration: str | None = None
    notes: str | None = None
    total_automatic: bool | None = None
    low_stock_alert: bool | None = None
    split_by_category: bool | None = None


class DocumentBuilder:
    def __init__(self, documents_dir: Path | str | None = None) -> None:
        if not documents_dir:
            documents_dir = Path.home() / "Documents"
        else:
            documents_dir = Path(documents_dir).expanduser()
        self._documents_dir = documents_dir / "Kira" / "Arquivos"
        self._documents_dir.mkdir(parents=True, exist_ok=True)

        self._required_fields = {
            "checklist_viagem": ["file_name", "destination", "duration"],
            "checklist_generico": ["file_name", "items"],
            "lista_compras": ["file_name", "items"],
            "lista_tarefas": ["file_name", "items"],
            "lista_generica": ["file_name", "items"],
        }

        self._field_questions = {
            "file_name": "qual o nome do arquivo",
            "columns": "quais colunas voce quer",
            "period": "qual o periodo",
            "destination": "qual o destino",
            "duration": "qual a duracao",
            "items": "quais itens voce quer incluir",
        }

    @property
    def documents_dir(self) -> Path:
        return self._documents_dir

    def new_draft(self) -> DocumentDraft:
        return DocumentDraft()

    def update_draft(
        self,
        draft: DocumentDraft,
        *,
        doc_type: str | None = None,
        file_name: str | None = None,
        title: str | None = None,
        columns: str | None = None,
        items: str | None = None,
        categories: str | None = None,
        period: str | None = None,
        destination: str | None = None,
        duration: str | None = None,
        notes: str | None = None,
        total_automatic: bool | None = None,
        low_stock_alert: bool | None = None,
        split_by_category: bool | None = None,
    ) -> DocumentDraft:
        if doc_type:
            draft.doc_type = doc_type
        if file_name:
            draft.file_name = file_name
        if title:
            draft.title = title
        if columns:
            draft.columns = self._parse_list(columns)
        if items:
            draft.items = self._parse_list(items)
        if categories:
            draft.categories = self._parse_list(categories)
        if period:
            draft.period = period
        if destination:
            draft.destination = destination
        if duration:
            draft.duration = duration
        if notes:
            draft.notes = notes
        if total_automatic is not None:
            draft.total_automatic = total_automatic
        if low_stock_alert is not None:
            draft.low_stock_alert = low_stock_alert
        if split_by_category is not None:
            draft.split_by_category = split_by_category

        if not draft.title and draft.file_name:
            draft.title = draft.file_name

        return draft

    def validate_draft(self, draft: DocumentDraft) -> list[str]:
        if not draft.doc_type:
            return ["tipo do documento"]

        required = self._required_fields.get(draft.doc_type, ["file_name"])
        missing: list[str] = []
        for field_name in required:
            value = getattr(draft, field_name)
            if value is None:
                missing.append(field_name)
            elif isinstance(value, list) and not value:
                missing.append(field_name)
            elif isinstance(value, str) and not value.strip():
                missing.append(field_name)
        return missing

    def missing_fields_message(self, missing_fields: list[str]) -> str:
        prompts = [self._field_questions.get(field_name, field_name) for field_name in missing_fields]
        joined = "\n".join(f"- {prompt}" for prompt in prompts)
        return "Ainda faltam algumas informacoes para eu montar isso direito:\n" + joined

    def create_document(self, draft: DocumentDraft) -> Path:
        if not draft.doc_type:
            raise DocumentBuilderError("O tipo do documento nao foi definido.")

        if draft.doc_type.startswith("planilha"):
            raise DocumentBuilderError(
                "Planilhas agora usam o fluxo proprio em Excel com openpyxl."
            )
        return self.create_pdf(draft)

    def create_pdf(self, draft: DocumentDraft) -> Path:
        safe_name = self._slugify(draft.file_name or draft.title or "arquivo_kira")
        destination_path = Path(__file__).resolve().parents[1] / "services" / "artifact_destination_manager.py"
        spec = importlib.util.spec_from_file_location("_kira_document_artifact_destination", str(destination_path))
        if spec is None or spec.loader is None:
            raise DocumentBuilderError("Nao consegui resolver a pasta padrao de PDFs.")
        destination_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = destination_module
        spec.loader.exec_module(destination_module)
        destination = destination_module.resolve_artifact_destination("pdf", title=safe_name, extension="pdf")
        target = destination.path
        if target is None:
            raise DocumentBuilderError("Nao consegui resolver o nome final do PDF.")

        styles = getSampleStyleSheet()
        story: list[Any] = []
        title = draft.title or draft.file_name or "Documento Kira"

        story.append(Paragraph(title, styles["Title"]))
        story.append(Spacer(1, 6 * mm))
        story.append(
            Paragraph(
                f"Gerado em {format_brasilia(now_brasilia(), include_date=True)}",
                styles["Normal"],
            )
        )
        story.append(Spacer(1, 6 * mm))

        metadata = self._build_metadata(draft)
        if metadata:
            for line in metadata:
                story.append(Paragraph(line, styles["Normal"]))
            story.append(Spacer(1, 6 * mm))

        if draft.doc_type.startswith("checklist"):
            story.extend(self._build_checklist_story(draft, styles))
        else:
            story.extend(self._build_list_story(draft, styles))

        doc = SimpleDocTemplate(
            str(target),
            pagesize=A4,
            leftMargin=16 * mm,
            rightMargin=16 * mm,
            topMargin=16 * mm,
            bottomMargin=16 * mm,
        )
        doc.build(story)
        return target


    def _build_metadata(self, draft: DocumentDraft) -> list[str]:
        lines: list[str] = []
        if draft.period:
            lines.append(f"<b>Periodo:</b> {draft.period}")
        if draft.destination:
            lines.append(f"<b>Destino:</b> {draft.destination}")
        if draft.duration:
            lines.append(f"<b>Duracao:</b> {draft.duration}")
        if draft.categories:
            lines.append(f"<b>Categorias:</b> {', '.join(draft.categories)}")
        if draft.notes:
            lines.append(f"<b>Observacoes:</b> {draft.notes}")
        return lines

    def _build_checklist_story(self, draft: DocumentDraft, styles: Any) -> list[Any]:
        items = draft.items or self._default_checklist_items(draft.doc_type)
        rows = [[f"[ ] {item}"] for item in items]
        table = Table(rows, colWidths=[170 * mm])
        table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        return [Paragraph("Checklist", styles["Heading2"]), table]

    def _build_list_story(self, draft: DocumentDraft, styles: Any) -> list[Any]:
        items = draft.items or []
        story: list[Any] = [Paragraph("Lista", styles["Heading2"])]
        for item in items:
            story.append(Paragraph(f"- {item}", styles["Normal"]))
            story.append(Spacer(1, 2 * mm))
        return story

    def _default_checklist_items(self, doc_type: str) -> list[str]:
        if doc_type == "checklist_viagem":
            return [
                "Documentos",
                "Carregador",
                "Roupas",
                "Higiene pessoal",
                "Remedios",
                "Dinheiro ou cartao",
            ]
        return ["Item 1", "Item 2", "Item 3"]

    def _parse_list(self, raw_value: str) -> list[str]:
        tokens = re.split(r"[\n,;|]+", raw_value)
        return [token.strip() for token in tokens if token.strip()]

    def _slugify(self, value: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip())
        cleaned = cleaned.strip("_")
        return cleaned or "arquivo_kira"
