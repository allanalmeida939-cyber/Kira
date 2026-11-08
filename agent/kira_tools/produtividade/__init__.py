from __future__ import annotations

from copy import deepcopy
import logging
import os

from livekit.agents import function_tool

from budget_planner import BudgetPlannerError
from document_builder import DocumentBuilderError

logger = logging.getLogger(__name__)

class ProdutividadeMixin:
    @function_tool(description="Cria, refaz ou edita uma planilha propria de orcamento mensal em Excel. Ela coleta renda total, categorias e valores de cada gasto, aceita frases soltas como 'gastei 200 de agua e 300 de energia', e ao editar altera apenas as categorias pedidas sem mexer nas outras.")
    async def gerenciar_orcamento_mensal(
        self,
        acao: str = "atualizar",
        nome_arquivo: str | None = None,
        periodo: str | None = None,
        renda_total: str | None = None,
        categorias: str | None = None,
        valores_gastos: str | None = None,
        observacoes: str | None = None,
    ) -> str:
        """Coleta dados e gera uma planilha de orcamento mensal.

        Args:
            acao: Use atualizar, status, cancelar, gerar, refazer ou editar.
            nome_arquivo: Nome do arquivo Excel.
            periodo: Periodo de referencia, por exemplo marco 2026.
            renda_total: Valor total recebido no mes.
            categorias: Categorias de gasto separadas por virgula.
            valores_gastos: Valores por categoria no formato Categoria=Valor ou em frases soltas como 'gastei 200 de agua e 300 de energia'.
            observacoes: Observacoes livres.
        """
        normalized_action = acao.lower()

        if normalized_action == "cancelar":
            self._budget_draft = self._budget_planner.new_draft()
            return "Rascunho do orcamento mensal cancelado."

        if normalized_action == "status":
            base_draft = (
                self._budget_draft
                if self._budget_planner.has_content(self._budget_draft)
                else self._last_budget_draft
            )
            draft_name = base_draft.file_name or "sem nome"
            draft_period = base_draft.period or "sem periodo"
            return f"Orcamento em andamento: arquivo={draft_name}, periodo={draft_period}."

        if normalized_action == "editar":
            if not valores_gastos and not any([renda_total, periodo, observacoes]):
                return "Para editar a planilha, me diga ao menos o valor ou campo que deve mudar."
            try:
                created_path, totals = await self._run_pc_action(
                    self._budget_planner.update_existing_workbook,
                    file_name=nome_arquivo,
                    last_path=self._last_budget_path,
                    expense_values=valores_gastos,
                    income_total=renda_total,
                    period=periodo,
                    notes=observacoes,
                )
            except BudgetPlannerError as exc:
                return str(exc)

            self._last_budget_path = created_path
            self._last_budget_draft = await self._run_pc_action(
                self._budget_planner.load_draft_from_workbook,
                created_path,
            )
            self._budget_draft = self._budget_planner.new_draft()
            return (
                f"Planilha editada: {created_path}\n"
                f"Pasta: {created_path.parent}\n"
                f"Total gasto no mes: R$ {totals['total_spent']:.2f}\n"
                f"Quanto sobrou: R$ {totals['remaining']:.2f}\n"
                f"Quanto economizou: R$ {totals['saved']:.2f}"
            )

        if normalized_action == "refazer":
            if not self._budget_planner.has_content(self._last_budget_draft):
                return "Nao tenho uma planilha mensal anterior para refazer ainda."
            self._budget_draft = deepcopy(self._last_budget_draft)

        elif (
            not self._budget_planner.has_content(self._budget_draft)
            and self._budget_planner.has_content(self._last_budget_draft)
            and valores_gastos
            and not any([nome_arquivo, periodo, renda_total, categorias])
        ):
            self._budget_draft = deepcopy(self._last_budget_draft)

        self._budget_draft = self._budget_planner.update_draft(
            self._budget_draft,
            file_name=nome_arquivo,
            period=periodo,
            income_total=renda_total,
            categories=categorias,
            expense_values=valores_gastos,
            notes=observacoes,
        )

        missing_fields = self._budget_planner.validate_draft(self._budget_draft)
        if missing_fields:
            return self._budget_planner.missing_fields_message(
                self._budget_draft,
                missing_fields,
            )

        if normalized_action not in {"atualizar", "gerar", "refazer"}:
            return "Acao invalida para orcamento mensal. Use atualizar, status, cancelar, gerar, refazer ou editar."

        try:
            final_draft = deepcopy(self._budget_draft)
            created_path, totals = await self._run_pc_action(
                self._budget_planner.build_workbook,
                final_draft,
            )
        except BudgetPlannerError as exc:
            return str(exc)

        self._last_budget_path = created_path
        self._last_budget_draft = final_draft
        self._budget_draft = self._budget_planner.new_draft()
        return (
            f"Planilha pronta: {created_path}\n"
            f"Pasta: {created_path.parent}\n"
            f"Total gasto no mes: R$ {totals['total_spent']:.2f}\n"
            f"Quanto sobrou: R$ {totals['remaining']:.2f}\n"
            f"Quanto economizou: R$ {totals['saved']:.2f}"
        )

    @function_tool(description="Cria listas, checklists e documentos simples. Nao use para planilhas financeiras mensais; para isso use a ferramenta de orcamento mensal.")
    async def gerenciar_documento(
        self,
        acao: str = "atualizar",
        tipo_documento: str | None = None,
        nome_arquivo: str | None = None,
        titulo: str | None = None,
        colunas: str | None = None,
        itens: str | None = None,
        categorias: str | None = None,
        periodo: str | None = None,
        destino: str | None = None,
        duracao: str | None = None,
        observacoes: str | None = None,
        total_automatico: bool | None = None,
        alerta_estoque_baixo: bool | None = None,
        abas_por_categoria: bool | None = None,
    ) -> str:
        """Coleta dados e gera documentos simples.

        Args:
            acao: Use atualizar, status, cancelar ou gerar.
            tipo_documento: Ex.: checklist_viagem, checklist_generico, lista_compras, lista_tarefas, lista_generica.
            nome_arquivo: Nome final do arquivo.
            titulo: Titulo exibido no arquivo.
            colunas: Colunas separadas por virgula para planilhas.
            itens: Itens separados por virgula ou quebra de linha para listas e checklists.
            categorias: Categorias separadas por virgula.
            periodo: Periodo da planilha, como mensal ou semanal.
            destino: Destino de viagem quando for checklist de viagem.
            duracao: Duracao da viagem ou prazo.
            observacoes: Observacoes livres.
            total_automatico: Se a planilha deve incluir linha de total.
            alerta_estoque_baixo: Preferencia para planilha de estoque.
            abas_por_categoria: Preferencia de separacao por categoria.
        """
        normalized_action = acao.lower()

        if normalized_action == "cancelar":
            self._document_draft = self._document_builder.new_draft()
            return "Rascunho de documento cancelado."

        if normalized_action == "status":
            draft_type = self._document_draft.doc_type or "nenhum"
            draft_name = self._document_draft.file_name or "sem nome"
            return f"Rascunho atual: tipo={draft_type}, arquivo={draft_name}."

        if tipo_documento:
            if self._document_draft.doc_type and self._document_draft.doc_type != tipo_documento:
                self._document_draft = self._document_builder.new_draft()

        self._document_draft = self._document_builder.update_draft(
            self._document_draft,
            doc_type=tipo_documento,
            file_name=nome_arquivo,
            title=titulo,
            columns=colunas,
            items=itens,
            categories=categorias,
            period=periodo,
            destination=destino,
            duration=duracao,
            notes=observacoes,
            total_automatic=total_automatico,
            low_stock_alert=alerta_estoque_baixo,
            split_by_category=abas_por_categoria,
        )

        missing_fields = self._document_builder.validate_draft(self._document_draft)
        if missing_fields:
            return self._document_builder.missing_fields_message(missing_fields)

        if normalized_action not in {"atualizar", "gerar"}:
            return "Acao invalida para documento. Use atualizar, status, cancelar ou gerar."

        manus_enabled = str(os.getenv("MANUS_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}
        local_fallback_allowed = str(os.getenv("KIRA_ALLOW_LOCAL_ARTIFACT_FALLBACK", "false")).strip().lower() in {"1", "true", "yes", "on"}
        if manus_enabled and not local_fallback_allowed:
            logger.warning("LOCAL_ARTIFACT_FALLBACK_BLOCKED reason=manus_required")
            logger.warning("LEGACY_LOCAL_FILE_CREATOR_BLOCKED reason=manus_creation_task")
            return "Interface: nao consegui concluir pelo Manus. Verifique a API ou tente novamente."

        try:
            created_path = await self._run_pc_action(
                self._document_builder.create_document,
                self._document_draft,
            )
        except DocumentBuilderError as exc:
            return str(exc)

        self._document_draft = self._document_builder.new_draft()
        return f"Arquivo pronto: {created_path}"


__all__ = ["ProdutividadeMixin"]
