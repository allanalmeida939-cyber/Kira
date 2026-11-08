"""Tools do Gemini para janelas e aplicativos do Windows (ver runtime.desktop_windows)."""

from __future__ import annotations

import asyncio

from livekit.agents import function_tool

try:
    from runtime import desktop_windows as desk
except ImportError:  # pragma: no cover - execução como pacote
    from agent.runtime import desktop_windows as desk


class DesktopToolsMixin:
    @function_tool(
        name="listar_aplicativos_abertos",
        description=(
            "Lista as janelas abertas na barra de tarefas (app, título, se está minimizada) e os aplicativos rodando "
            "em segundo plano sem janela (ícones ocultos da bandeja). Use antes de fechar, minimizar ou alternar janelas."
        ),
    )
    async def listar_aplicativos_abertos(self) -> str:
        windows = await asyncio.to_thread(desk.list_windows)
        background = await asyncio.to_thread(desk.background_apps)
        lines = [f"- {w.app}: {w.title[:70]}" + (" (minimizada)" if w.minimized else "") for w in windows[:30]]
        text = "Janelas abertas:\n" + ("\n".join(lines) if lines else "nenhuma")
        if background:
            text += "\nEm segundo plano (sem janela): " + ", ".join(background)
        return text

    @function_tool(
        name="controlar_janela",
        description=(
            "Age sobre a janela de um aplicativo aberto. acao: 'minimizar', 'maximizar', 'restaurar' (mostrar de novo uma "
            "minimizada), 'focar' (trazer para frente) ou 'fechar' (fecha só a janela, como clicar no X; o programa pode "
            "pedir para salvar). alvo: nome do app ou parte do título (ex.: 'chrome', 'vs code', 'planilha de gastos')."
        ),
    )
    async def controlar_janela(self, acao: str, alvo: str) -> str:
        action = desk.fold(acao)
        if action not in {"minimizar", "maximizar", "restaurar", "mostrar", "focar", "fechar"}:
            return "Ação inválida: use minimizar, maximizar, restaurar, focar ou fechar."
        return await asyncio.to_thread(desk.window_action, action, alvo)

    @function_tool(
        name="encerrar_processo",
        description=(
            "Encerra à força o processo de um programa (todas as janelas, sem salvar). Só quando o usuário disser "
            "'encerra o processo', 'mata', 'força o fechamento' ou o programa travou. Para 'fecha o X', use controlar_janela."
        ),
    )
    async def encerrar_processo(self, programa: str) -> str:
        return await asyncio.to_thread(desk.end_process, programa)

    @function_tool(
        name="aplicativos_instalados",
        description="Lista os aplicativos instalados no PC (menu Iniciar). filtro opcional: parte do nome.",
    )
    async def aplicativos_instalados(self, filtro: str = "") -> str:
        apps = await asyncio.to_thread(desk.installed_apps)
        wanted = desk.fold(filtro)
        names = [a["name"] for a in apps if not wanted or wanted in desk.fold(a["name"])]
        if not names:
            return "Não encontrei aplicativos com esse nome." if wanted else "Não consegui listar os aplicativos."
        return f"{len(names)} aplicativo(s): " + ", ".join(sorted(names, key=str.lower)[:80])

    @function_tool(
        name="abrir_aplicativo",
        description="Abre um aplicativo instalado pelo nome do menu Iniciar (ex.: 'Spotify', 'Calculadora', 'Xbox').",
    )
    async def abrir_aplicativo(self, nome: str) -> str:
        return await asyncio.to_thread(desk.open_installed_app, nome)

    @function_tool(
        name="ajustar_brilho",
        description="Ajusta o brilho da tela (0 a 100). Funciona em notebooks e monitores que aceitam controle pelo Windows.",
    )
    async def ajustar_brilho(self, percentual: int) -> str:
        return await asyncio.to_thread(desk.set_brightness, percentual)
