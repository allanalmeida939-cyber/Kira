from __future__ import annotations

import logging
import os

from livekit.agents import function_tool

from file_editor import (
    build_ambiguity_prompt,
    detect_edit_ambiguity,
    edit_file_content,
    resolve_edit_candidates,
)
from file_manager import KiraFileManagerError, resolve_user_path, save_file
from pc_control import PCControlError

if __package__ and __package__.startswith("agent"):
    from agent.core.command_intent import classify_explicit_command
else:  # pragma: no cover - suporte a execucao direta em agent/
    from core.command_intent import classify_explicit_command

logger = logging.getLogger(__name__)


def _require_explicit_command(instance, *, action_label: str, fallback_text: str = "") -> str | None:
    guard = getattr(instance, "ensure_explicit_user_command", None)
    if callable(guard):
        return guard(action_label=action_label, fallback_text=fallback_text)
    decision = classify_explicit_command(fallback_text)
    logger.info(
        "[INTENT] text=%r is_command=%s confidence=%.2f action=%s reason=%s",
        fallback_text[:120],
        decision.is_command,
        decision.confidence,
        action_label,
        decision.reason,
    )
    if decision.is_command:
        return None
    return (
        f"Entendi isso mais como conversa do que como comando para {action_label}. "
        "Se quiser que eu aja, me pede de forma direta."
    )


class GerenciamentoArquivosMixin:
    @function_tool(
        description=(
            "Edita um arquivo existente em background sem abrir editor visual. "
            "Cria backup .bak antes de salvar."
        )
    )
    async def editar_arquivo_background(
        self,
        instrucao: str,
        arquivo_alvo: str | None = None,
        tipo_arquivo: str | None = None,
    ) -> str:
        """Edita arquivo com backup automatico.

        Args:
            instrucao: Pedido natural de alteracao.
            arquivo_alvo: Nome/caminho do arquivo opcional.
            tipo_arquivo: Extensao opcional para guiar edicao estruturada.
        """
        instruction = str(instrucao or "").strip()
        if not instruction:
            return "Me diga a instrucao de edicao que devo aplicar."

        candidates = resolve_edit_candidates(instruction, explicit_path=arquivo_alvo)
        ambiguity = detect_edit_ambiguity(
            instruction=instruction,
            candidates=candidates,
            file_type=tipo_arquivo,
        )
        if ambiguity is not None:
            self._pending_file_edit = {
                "instruction": instruction,
                "target_path": str(candidates[0]) if len(candidates) == 1 else None,
                "file_type": tipo_arquivo,
                "candidates": [str(path) for path in candidates[:8]],
                "ambiguity_type": str(ambiguity.get("ambiguity_type") or "file"),
            }
            return str(ambiguity.get("message") or build_ambiguity_prompt("file", candidates=candidates))

        target = candidates[0] if candidates else None
        if target is None:
            self._pending_file_edit = {
                "instruction": instruction,
                "target_path": None,
                "file_type": tipo_arquivo,
                "candidates": [],
                "ambiguity_type": "file",
            }
            return "Qual arquivo voce quer que eu edite?"

        try:
            result = edit_file_content(
                path=target,
                instruction=instruction,
                file_type=tipo_arquivo,
            )
        except Exception as exc:
            return (
                "[KIRA EDIT]\n"
                f"target={target}\n"
                "backup_created=False\n"
                "edited=False\n"
                f"file_type={tipo_arquivo or target.suffix.lower()}\n"
                f"instruction=\"{instruction}\"\n"
                f"error={exc}"
            )

        return (
            "[KIRA EDIT]\n"
            f"target={result['target']}\n"
            f"backup_created={result['backup_created']}\n"
            f"edited={result['edited']}\n"
            f"file_type={result['file_type']}\n"
            f"instruction=\"{instruction}\"\n"
            "error=None\n"
            f"Resumo: {result['summary']}\n"
            f"Backup: {result['backup_path']}\n"
            "[KIRA EDIT DIFF]\n"
            f"target={result['diff_log']['target']}\n"
            f"changed={result['diff_log']['changed']}\n"
            f"summary=\"{result['diff_log']['summary']}\"\n"
            f"backup_created={result['diff_log']['backup_created']}"
        )

    @function_tool(
        description=(
            "Salva um conteudo gerado pela Kira em arquivo. Se local_destino nao for informado, "
            "a Kira pergunta onde salvar e guarda estado pendente para concluir no proximo texto do usuario."
        )
    )
    async def salvar_arquivo_gerado(
        self,
        conteudo: str,
        nome_arquivo: str,
        tipo_arquivo: str | None = None,
        local_destino: str | None = None,
    ) -> str:
        """Gera e salva um arquivo com fallback em Documents/Kira.

        Args:
            conteudo: Conteudo final do arquivo.
            nome_arquivo: Nome sugerido do arquivo.
            tipo_arquivo: Extensao, como .py, .txt, .pdf, .html, .js, .cs, .json, .md.
            local_destino: Desktop, Documents, Downloads ou caminho absoluto.
        """
        content = str(conteudo or "")
        filename = str(nome_arquivo or "").strip()
        filetype = str(tipo_arquivo or "").strip() or None
        normalized_type = str(filetype or "").lower().lstrip(".")
        manus_enabled = str(os.getenv("MANUS_ENABLED", "false")).strip().lower() in {"1", "true", "yes", "on"}
        local_fallback_allowed = str(os.getenv("KIRA_ALLOW_LOCAL_ARTIFACT_FALLBACK", "false")).strip().lower() in {"1", "true", "yes", "on"}
        if manus_enabled and not local_fallback_allowed and normalized_type in {
            "pdf", "txt", "md", "text", "docx", "py", "python", "html", "css", "js", "json", "csv", "xlsx", "pptx",
        }:
            logger.warning("LOCAL_ARTIFACT_FALLBACK_BLOCKED reason=manus_required")
            logger.warning("LEGACY_LOCAL_FILE_CREATOR_BLOCKED reason=manus_creation_task")
            return "Interface: nao consegui concluir pelo Manus. Verifique a API ou tente novamente."
        resolved_path = resolve_user_path(local_destino)

        if local_destino and resolved_path is None:
            return (
                "Nao consegui interpretar esse local. Use 'area de trabalho', 'documentos', "
                "'downloads' ou um caminho absoluto."
            )

        if local_destino and resolved_path is not None:
            try:
                result = save_file(
                    content=content,
                    filename=filename,
                    filetype=filetype,
                    target_path=resolved_path,
                )
            except KiraFileManagerError as exc:
                return str(exc)
            return (
                "[KIRA FILE]\n"
                f"created={result['created']}\n"
                f"saved={result['saved']}\n"
                f"path={result['path']}\n"
                f"tipo={result['tipo']}"
            )

        self._pending_file_save = {
            "content": content,
            "filename": filename,
            "type": filetype,
        }
        return (
            "Onde voce quer que eu salve esse arquivo? "
            "Pode ser area de trabalho, documentos, downloads ou caminho completo. "
            "Se voce nao especificar, eu salvo automaticamente em Documents/Kira."
        )

    @function_tool(
        description=(
            "Mostra o estado de salvamento pendente de arquivo para confirmar se a Kira "
            "esta aguardando apenas o local."
        )
    )
    async def status_salvamento_arquivo(self) -> str:
        pending = getattr(self, "_pending_file_save", None)
        if not pending:
            return "Nenhum salvamento pendente."
        return (
            "Salvamento pendente aguardando local. "
            f"arquivo={pending.get('filename') or 'arquivo_sem_nome'} tipo={pending.get('type') or 'auto'}"
        )

    @function_tool(description="Cria uma pasta ou arquivo no Windows (padrao: area de trabalho). Use para pedidos como 'criar pasta projetos', 'faz uma pasta chamada fotos' ou 'cria uma nota chamada compras com arroz e feijao' (arquivo com conteudo: grava o texto e abre no Bloco de Notas).")
    async def criar_item(
        self,
        tipo: str,
        nome: str,
        local: str | None = None,
        conteudo: str = "",
    ) -> str:
        """Cria pasta ou arquivo.

        Args:
            tipo: Use 'pasta' ou 'arquivo'.
            nome: Nome final do item; arquivo sem extensao vira .txt.
            local: Local opcional como desktop, downloads, documentos ou um caminho.
            conteudo: Texto do arquivo, quando o usuario ditar.
        """
        try:
            if tipo.lower() == "pasta":
                return await self._run_pc_action(self._pc.create_folder, nome, local)
            if tipo.lower() == "arquivo":
                return await self._run_pc_action(self._pc.create_file, nome, local, conteudo)
            raise PCControlError("Tipo invalido. Use pasta ou arquivo.")
        except PCControlError as exc:
            return str(exc)

    @function_tool(description="Abre uma pasta, arquivo, desktop, downloads ou outro local conhecido no PC.")
    async def abrir_item(
        self,
        nome: str | None = None,
        tipo: str | None = None,
        local: str | None = None,
    ) -> str:
        """Abre arquivos, pastas e locais conhecidos.

        Args:
            nome: Nome do arquivo ou pasta.
            tipo: Opcional, use 'arquivo' ou 'folder'.
            local: Opcional, use para desktop, downloads, documentos ou caminho direto.
        """
        try:
            blocked = _require_explicit_command(
                self,
                action_label="abrir um item no PC",
                fallback_text=f"abrir {nome or ''} {local or ''}".strip(),
            )
            if blocked is not None:
                return blocked
            expected_type = "file" if (tipo or "").lower() == "arquivo" else "folder" if (tipo or "").lower() in {"pasta", "folder"} else None
            return await self._run_pc_action(
                self._pc.open_item,
                name=nome,
                item_type=expected_type,
                location=local,
            )
        except PCControlError as exc:
            return str(exc)

    @function_tool(description="Renomeia um arquivo ou pasta existente.")
    async def renomear_item(
        self,
        tipo: str,
        nome_atual: str,
        novo_nome: str,
    ) -> str:
        """Renomeia um item.

        Args:
            tipo: Use 'arquivo' ou 'pasta'.
            nome_atual: Nome atual do item.
            novo_nome: Novo nome do item.
        """
        try:
            blocked = _require_explicit_command(
                self,
                action_label="renomear um item",
                fallback_text=f"renomear {nome_atual} para {novo_nome}".strip(),
            )
            if blocked is not None:
                return blocked
            expected_type = "file" if tipo.lower() == "arquivo" else "folder" if tipo.lower() == "pasta" else None
            return await self._run_pc_action(
                self._pc.rename_item,
                nome_atual,
                novo_nome,
                item_type=expected_type,
            )
        except PCControlError as exc:
            return str(exc)

    @function_tool(description="Exclui um arquivo ou pasta, normalmente movendo para a lixeira. Use apenas depois de confirmar com o usuario.")
    async def excluir_item(
        self,
        tipo: str | None = None,
        nome: str | None = None,
        local: str | None = None,
        mover_para_lixeira: bool = True,
        confirmado: bool = False,
    ) -> str:
        """Exclui um item do PC.

        Args:
            tipo: Use 'arquivo' ou 'pasta' quando souber.
            nome: Nome do arquivo ou pasta.
            local: Caminho ou local conhecido.
            mover_para_lixeira: Se verdadeiro, tenta mandar para a lixeira.
            confirmado: Deve ser verdadeiro somente depois da confirmacao explicita do usuario.
        """
        if not confirmado:
            return "Preciso de confirmacao explicita antes de excluir algo."

        try:
            expected_type = "file" if (tipo or "").lower() == "arquivo" else "folder" if (tipo or "").lower() in {"pasta", "folder"} else None
            return await self._run_pc_action(
                self._pc.delete_item,
                name=nome,
                item_type=expected_type,
                location=local,
                recycle=mover_para_lixeira,
            )
        except PCControlError as exc:
            return str(exc)

    @function_tool(description="Move ou copia um arquivo ou pasta para outro local.")
    async def mover_ou_copiar_item(
        self,
        operacao: str,
        nome_origem: str,
        destino: str,
        tipo: str | None = None,
    ) -> str:
        """Move ou copia arquivos e pastas.

        Args:
            operacao: Use 'mover' ou 'copiar'.
            nome_origem: Nome do arquivo ou pasta de origem.
            destino: Pasta de destino.
            tipo: Opcional, use 'arquivo' ou 'pasta'.
        """
        try:
            blocked = _require_explicit_command(
                self,
                action_label=f"{operacao.lower()} um item",
                fallback_text=f"{operacao} {nome_origem} para {destino}".strip(),
            )
            if blocked is not None:
                return blocked
            if operacao.lower() not in {"mover", "copiar"}:
                raise PCControlError("Operacao invalida. Use mover ou copiar.")
            expected_type = "file" if (tipo or "").lower() == "arquivo" else "folder" if (tipo or "").lower() in {"pasta", "folder"} else None
            return await self._run_pc_action(
                self._pc.move_or_copy_item,
                nome_origem,
                destino,
                operacao.lower(),
                item_type=expected_type,
            )
        except PCControlError as exc:
            return str(exc)

    @function_tool(description="Compacta um arquivo ou pasta em .rar (WinRAR) ou .zip, ao lado do original.")
    async def compactar_item(self, nome: str, local: str | None = None, formato: str = "rar") -> str:
        """Compacta um item.

        Args:
            nome: Nome do arquivo ou pasta.
            local: Opcional, pasta onde ele esta (desktop, documentos, downloads...).
            formato: 'rar' (padrao) ou 'zip'.
        """
        try:
            return await self._run_pc_action(self._pc.compress_item, nome, local, formato)
        except PCControlError as exc:
            return str(exc)

    @function_tool(description="Descompacta um .zip, .rar, .7z ou .tar numa pasta nova com o mesmo nome, ao lado do arquivo.")
    async def descompactar_item(self, nome: str, local: str | None = None) -> str:
        """Descompacta um arquivo.

        Args:
            nome: Nome do arquivo compactado.
            local: Opcional, pasta onde ele esta.
        """
        try:
            return await self._run_pc_action(self._pc.extract_item, nome, local)
        except PCControlError as exc:
            return str(exc)

    @function_tool(description="Gerencia arquivos e pastas com acoes como organizar, buscar, renomear, mover e copiar. Use para pedidos como 'organiza a pasta downloads', 'procura o arquivo contrato' ou 'renomeia notas.txt para notas-final.txt'.")
    async def gerenciar_arquivos(
        self,
        acao: str,
        nome: str | None = None,
        local: str | None = None,
        novo_nome: str | None = None,
        destino: str | None = None,
        tipo: str | None = "arquivo",
        modo: str | None = "tipo",
        limite: int = 10,
    ) -> str:
        """Executa operacoes de organizacao e busca de arquivos.

        Args:
            acao: Use organizar, buscar, renomear, mover ou copiar.
            nome: Nome do arquivo, pasta ou termo de busca.
            local: Pasta base da operacao, como downloads ou desktop.
            novo_nome: Novo nome para renomear.
            destino: Pasta de destino para mover ou copiar.
            tipo: arquivo ou pasta.
            modo: Modo de organizacao. Hoje suporta tipo.
            limite: Limite de resultados da busca.
        """
        try:
            normalized_action = acao.lower()
            if normalized_action == "organizar":
                target_location = local or nome
                if not target_location:
                    return "Informe a pasta que deve ser organizada."
                blocked = _require_explicit_command(
                    self,
                    action_label="organizar arquivos",
                    fallback_text=f"{acao} {target_location}".strip(),
                )
                if blocked is not None:
                    return blocked
                return await self._run_pc_action(
                    self._pc.organize_folder,
                    target_location,
                    modo or "tipo",
                )

            if normalized_action == "buscar":
                if not nome:
                    return "Informe o nome do arquivo ou pasta que deve ser buscado."
                return await self._run_pc_action(
                    self._pc.search_files_by_name,
                    nome,
                    local,
                    tipo or "arquivo",
                    limite,
                )

            if normalized_action == "renomear":
                if not nome or not novo_nome:
                    return "Informe o nome atual e o novo nome para renomear."
                return await self.renomear_item(tipo or "arquivo", nome, novo_nome)

            if normalized_action in {"mover", "copiar"}:
                if not nome or not destino:
                    return "Informe o item de origem e a pasta de destino."
                return await self.mover_ou_copiar_item(normalized_action, nome, destino, tipo)

            return "Acao invalida. Use organizar, buscar, renomear, mover ou copiar."
        except PCControlError as exc:
            return str(exc)


__all__ = ["GerenciamentoArquivosMixin"]
