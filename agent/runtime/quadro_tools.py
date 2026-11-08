"""Ferramenta 'criar_painel' (2026-11-05; variações em 2026-11-07): a Kira cria painéis na hora (services/quadro_panels.py)."""

from __future__ import annotations

import json

import httpx
from livekit.agents import function_tool

try:
    from runtime.media_tools import _post_local
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime.media_tools import _post_local


class QuadroToolsMixin:
    @function_tool(
        name="criar_painel",
        description=(
            "Cria um painel na tela sobre QUALQUER assunto, na hora (sai do orbe). Use quando o Chefe pedir painel, gráfico, "
            "lista, questões, notícia, tabela, flashcards ou 'me mostra'. Se ele disser 'na TV', o roteamento leva o painel para lá. "
            "acao='mostrar': tipo + titulo curto + conteudo (JSON). Escolha o tipo pelo que ele quer VER:\n"
            "info = um assunto com números/fatos (clima, perfil, resumo de algo): {subtitulo, resumo, itens:[{rotulo, valor}], destaque, fonte}\n"
            "lista = passos, tópicos, ranking, compras: {subtitulo, itens:[texto curto], numerada:true|false}\n"
            "grafico = comparar quantidades ou evolução no tempo: {subtitulo, formato:'barras'|'linha', dados:[{rotulo, valor:número}], fonte} (dados reais, nunca inventados)\n"
            "questoes = treinar/prova (clicáveis): {questoes:[{enunciado, alternativas:[...], resposta:índice da certa (0=A), explicacao}]} (até 4 = um painel por questão)\n"
            "noticia = uma notícia/história: {subtitulo, imagem:'https://...jpg' (opcional), artigo:[2-4 parágrafos curtos], fonte}\n"
            "tabela = vários itens com as mesmas colunas: {subtitulo, colunas:[...], linhas:[[...]]}\n"
            "flashcards = revisar/decorar: {cards:[{frente, verso}]}\n"
            "Pouco texto: o painel não rola, a letra encolhe para caber. O visual é sorteado sozinho entre 6 variações; só passe "
            "variacao se o Chefe pedir um jeito: solto (sem caixa, letras grandes sobre o fundo), janela (clássica com barra), cards "
            "(itens em cartões), faixa (faixa lateral, números alinhados), destaque (um número/item gigante), editorial (papel claro, jornal). "
            "'Gostei, mantém esse' = acao='fixar' com o tipo (para o painel de imagens/fotos, tipo='imagens'); 'volta a variar' = acao='variar'."
        ),
    )
    async def painel(self, acao: str = "mostrar", tipo: str = "info", titulo: str = "", conteudo: str = "{}", variacao: str = "") -> str:
        acao = str(acao or "mostrar").strip().lower()
        try:
            if acao in {"fixar", "variar"}:
                r = await _post_local("/api/quadro/fixar", {"tipo": tipo, "variar": acao == "variar"}, read_timeout=5.0)
                if not r.get("ok"):
                    return f"Não deu: {r.get('error')}."
                return "Salvo: painéis desse tipo vão sair sempre nesse formato." if acao == "fixar" else "Ok, os painéis desse tipo voltam a variar."
            try:
                data = json.loads(conteudo) if isinstance(conteudo, str) else conteudo
            except ValueError:
                return "conteudo precisa ser JSON válido; tente de novo."
            r = await _post_local("/api/quadro", {"tipo": tipo, "titulo": titulo, "conteudo": data if isinstance(data, dict) else {}, "variacao": variacao}, read_timeout=5.0)
        except (httpx.HTTPError, TimeoutError):
            return "O painel não abriu agora."
        if not r.get("ok"):
            return f"Não deu: {r.get('error')}."
        return f"Painel na tela ({r.get('paineis', 1)}). Comente em uma frase curta, sem ler tudo."
