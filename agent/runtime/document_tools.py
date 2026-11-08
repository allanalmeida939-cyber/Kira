"""Tool de documentos e PDF (services/documents_api.py): converter, juntar, separar, criar, ler, achar e abrir arquivos."""

from __future__ import annotations

import json

import httpx
from livekit.agents import function_tool

try:
    from runtime.media_tools import _post_local
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime.media_tools import _post_local

UNAVAILABLE = "Não consegui mexer nesse arquivo agora."


class DocumentToolsMixin:
    @function_tool(
        name="documento",
        description=(
            "Arquivos do Chefe: Word, PDF, Excel, PowerPoint, texto e imagens. acao: 'converter' ('transforma esse Word em PDF', "
            "'passa esse PDF para Word'; formato = pdf, word, texto, excel, png); 'juntar' ('junta esses 3 PDFs'; arquivos = os "
            "nomes, ou uma pista só como 'os PDFs do Downloads'); 'separar' (paginas '1-3, 5'); 'criar' ('faz um documento com "
            "a lista de compras': você escreve o conteudo completo; linhas com '# ' título, '- ' item, '[ ] ' caixinha, 'a | b' "
            "tabela; formato word/pdf/texto/planilha); 'ler' (o que tem no arquivo, para resumir ou responder); 'achar' (onde "
            "está um arquivo); 'abrir'; 'trabalho' (trabalho escolar/de faculdade completo, com capa, seções, imagens e "
            "referências: conteudo = o TEMA, curto; detalhes = pedidos dele; autor, escola e cidade se ele disser; formato pdf ou "
            "word; roda em segundo plano e a Kira avisa quando fica pronto — NUNCA escreva o trabalho você mesma). arquivos: nomes ou pistas que ele disse ('esse Word' = o Word mais recente; 'o contrato'; "
            "caminho completo). onde: o lugar que ELE disse para salvar ('área de trabalho', 'Downloads', 'pasta Faculdade'); se "
            "ele não disse um lugar claro, deixe vazio (salva em Documentos\\Kira). nome: nome do arquivo novo, se ele disser. "
            "Na resposta diga o nome do arquivo e onde salvou."
        ),
    )
    async def documento(self, acao: str, arquivos: list[str] | None = None, formato: str = "", onde: str = "", nome: str = "",
                        conteudo: str = "", paginas: str = "", detalhes: str = "", autor: str = "", escola: str = "", cidade: str = "") -> str:
        payload = {"action": acao, "files": list(arquivos or []), "format": formato, "where": onde, "name": nome, "content": conteudo, "pages": paginas,
                   "details": detalhes, "author": autor, "school": escola, "city": cidade}
        try:
            result = await _post_local("/api/ui/documento", payload, read_timeout=90.0)
        except (httpx.HTTPError, TimeoutError, ValueError):
            return UNAVAILABLE
        if not result.get("ok"):
            return str(result.get("message") or UNAVAILABLE)
        result.pop("ok", None)
        return json.dumps(result, ensure_ascii=False)


__all__ = ["DocumentToolsMixin"]
