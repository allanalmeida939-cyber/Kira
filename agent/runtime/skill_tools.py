"""Ferramenta 'skills' (2026-11-05): a Kira consulta, instala do GitHub e explica as skills (services/kira_skills.py)."""

from __future__ import annotations

from urllib.parse import quote

import httpx
from livekit.agents import function_tool

try:
    from runtime.media_tools import _get_local, _post_local
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime.media_tools import _get_local, _post_local

HOW_TO = (
    "Como instalar uma skill na Kira: 1) ache a skill no GitHub (uma pasta com um arquivo SKILL.md; ex.: github.com/anthropics/skills); "
    "2) baixe o repositório em ZIP (botão verde Code > Download ZIP) ou só o SKILL.md; 3) arraste o arquivo para qualquer parte "
    "da tela da Kira. Ou peça falando: 'Kira, instala a skill do github ponto com barra dono barra repositório'. "
    "Elas ficam em Documentos\\Kira\\Skills; para tirar, peça 'remove a skill X'."
)


class SkillToolsMixin:
    @function_tool(
        name="skills",
        description=(
            "Skills = manuais de 'como fazer' instalados pelo Chefe (SKILL.md, formato do GitHub). "
            "SEMPRE antes de uma tarefa de produzir algo (PDF, gráfico, planilha, apresentação, site, texto, estudo, imagem), "
            "chame acao='buscar' com tarefa=o pedido; se vier uma skill, siga as instruções dela na tarefa. "
            "acao: buscar (tarefa) | listar | ler (nome) | instalar (url do GitHub, nome opcional da skill dentro do repo) | "
            "remover (nome) | como_instalar (explica ao Chefe como instalar, para ensinar). "
            "Scripts das skills nunca são executados: só as instruções valem."
        ),
    )
    async def skills(self, acao: str, tarefa: str = "", nome: str = "", url: str = "") -> str:
        acao = str(acao or "").strip().lower()
        try:
            if acao == "como_instalar":
                return HOW_TO
            if acao in {"buscar", "listar"}:
                query = f"?tarefa={quote(tarefa)}" if acao == "buscar" and tarefa.strip() else ""
                found = (await _get_local(f"/api/skills{query}")).get("skills") or []
                if not found:
                    return "Nenhuma skill instalada para isso: faça do seu jeito." if query else "Nenhuma skill instalada ainda. " + HOW_TO
                if acao == "listar":
                    return "Skills instaladas: " + "; ".join(f"{s['name']}: {s['description'][:120]}" for s in found)
                nome = found[0]["name"]
            if acao in {"buscar", "ler"}:
                r = await _get_local(f"/api/skills/read?name={quote(nome)}")
                if not r.get("ok"):
                    return f"A skill '{nome}' não está instalada."
                extra = f"\nArquivos de apoio: {', '.join(r['files'][:15])}" if r.get("files") else ""
                return f"Skill '{r['name']}' — siga estas instruções na tarefa:\n{r['body'][:7000]}{extra}"
            if acao == "instalar":
                r = await _post_local("/api/skills/github", {"url": url or nome, "skill": nome if url else ""}, read_timeout=90.0)
                if r.get("ok"):
                    return f"Instalei: {', '.join(r['installed'])} (em {r['folder']}). Conte ao Chefe em uma frase."
                return f"Não consegui instalar: {r.get('error') or 'falhou'}."
            if acao == "remover":
                r = await _post_local("/api/skills/remove", {"name": nome}, read_timeout=10.0)
                return "Removida." if r.get("ok") else f"Não achei a skill '{nome}'."
        except (httpx.HTTPError, TimeoutError):
            return "O gerenciador de skills não respondeu agora."
        return "Ação desconhecida: use buscar, listar, ler, instalar, remover ou como_instalar."
