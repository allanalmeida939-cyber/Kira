"""AppFlowy (Notion grátis e local) pela Kira (2026-11-06): instalar (só com o Chefe confirmando), abrir, ensinar e criar.

O app desktop não tem API local (a API REST é só do AppFlowy Cloud): 'criar' põe o conteúdo em Markdown no
copiar/colar e abre o AppFlowy para o Chefe colar com Ctrl+V (o AppFlowy converte o Markdown em blocos).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from livekit.agents import function_tool

WINGET_ID = "AppFlowy.AppFlowy"
GUIDE = (
    "Guia rápido do AppFlowy: 1) Na barra da esquerda, '+' cria uma página (documento, quadro Kanban, tabela/grade ou "
    "calendário). 2) Dentro do documento, digite '/' para inserir blocos: título, lista, checklist, tabela, imagem, código, "
    "divisor, página dentro de página. 3) Banco de dados: crie uma Grade e troque a visualização para Quadro (Kanban) ou "
    "Calendário — os mesmos dados aparecem de jeitos diferentes; adicione colunas com tipo (data, seleção, checkbox). "
    "4) Arraste blocos pelo ícone de seis pontinhos para reordenar. 5) IA: selecione um texto e use 'Pedir à IA' (resumir, "
    "melhorar, traduzir) — precisa de conta. 6) Templates: em 'Modelos' dá para começar de um pronto. 7) Exportar: menu "
    "'...' da página > Exportar (Markdown, PDF, HTML). Tudo fica salvo no PC, sem precisar de internet."
)


def find_exe() -> Path | None:
    for base in (os.getenv("LOCALAPPDATA", ""), os.getenv("ProgramFiles", ""), os.getenv("ProgramFiles(x86)", "")):
        for path in (Path(base) / "Programs" / "AppFlowy" / "AppFlowy.exe", Path(base) / "AppFlowy" / "AppFlowy.exe"):
            if base and path.exists():
                return path
    return None


def open_app() -> bool:
    exe = find_exe()
    if not exe:
        return False
    subprocess.Popen([str(exe)], cwd=str(exe.parent), creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    return True


def install() -> str:
    run = subprocess.run(["winget", "install", "-e", "--id", WINGET_ID, "--silent", "--accept-package-agreements",
                          "--accept-source-agreements"], capture_output=True, text=True, timeout=900)
    return "ok" if run.returncode == 0 or find_exe() else (run.stdout or run.stderr)[-200:]


class AppFlowyToolsMixin:
    @function_tool(
        name="appflowy",
        description=(
            "AppFlowy = Notion grátis que roda no PC do Chefe (páginas, Kanban, tabelas, calendário). acao: "
            "ensinar (guia passo a passo; explique por partes, perguntando se ele quer ver o próximo) | abrir | "
            "instalar (NUNCA sem o Chefe pedir; a 1ª chamada só pede confirmação: pergunte e chame de novo com confirmar=true) | "
            "criar (conteudo = a página em Markdown, com # títulos, listas, - [ ] tarefas, tabelas: vai para o copiar/colar e o "
            "AppFlowy abre; diga a ele para criar uma página e colar com Ctrl+V)."
        ),
    )
    async def appflowy(self, acao: str, conteudo: str = "", confirmar: bool = False) -> str:
        acao = str(acao or "").strip().lower()
        if acao == "ensinar":
            return GUIDE
        if acao == "instalar":
            if find_exe():
                return "O AppFlowy já está instalado. Posso abrir."
            if not confirmar:
                return "Confirme com o Chefe antes: 'Posso instalar o AppFlowy (grátis, ~100 MB, pelo winget)?' Só então chame com confirmar=true."
            result = await asyncio.to_thread(install)
            return "Instalei o AppFlowy. Quer que eu abra?" if result == "ok" else f"A instalação falhou: {result}"
        if acao in {"abrir", "criar"}:
            if acao == "criar":
                if not conteudo.strip():
                    return "Falta o conteúdo da página (em Markdown)."
                from services.clipboard_manager import copy_text
                if not (await asyncio.to_thread(copy_text, conteudo)).ok:
                    return "Não consegui pôr a página no copiar/colar."
            if not await asyncio.to_thread(open_app):
                return "O AppFlowy não está instalado. Pergunte se ele quer que eu instale."
            return ("Abri o AppFlowy e a página está no copiar/colar: diga para ele criar uma página nova (+) e colar com Ctrl+V."
                    if acao == "criar" else "Abri o AppFlowy.")
        return "Ação desconhecida: use ensinar, abrir, instalar ou criar."
