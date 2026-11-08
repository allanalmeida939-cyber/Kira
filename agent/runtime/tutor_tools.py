"""Tool da professora (Fase 3): fala só com services/tutor.py pelo backend local."""

from __future__ import annotations

import asyncio
import logging

import httpx
from livekit.agents import function_tool

try:
    from runtime.media_tools import _post_local
except ImportError:  # pragma: no cover - execução como pacote agent.*
    from agent.runtime.media_tools import _post_local


class TutorToolsMixin:
    @function_tool(
        name="estudo",
        description=(
            "Professora da Kira: ensina idiomas (inglês: gramática, vocabulário, diálogo, pronúncia), matérias, faculdade e "
            "concursos. acao: 'pesquisar' (PRIMEIRO passo quando o Chefe escolhe um objetivo: 'quero estudar PMCE' -> curso 'PMCE'; "
            "acha na web o edital e as últimas provas/gabaritos em PDF e baixa até 5 para a biblioteca, com progresso no painel; devolve os links), 'baixar' (põe na biblioteca do curso um edital, prova, gabarito, apostila ou página: fonte = URL "
            "achada com a busca na web, de site oficial/banca, ou nome/caminho de arquivo do PC; um por chamada; o painel mostra "
            "o progresso real), 'aula' (devolve trechos da biblioteca sobre o tema: ensine com base neles, aos poucos, "
            "perguntando ao aluno; se vier sem trechos, diga que é conhecimento geral), 'simulado' (gera questões no painel; "
            "banca opcional; quantidade 1-20), 'finalizar' não é sua: o aluno aperta Finalizar no painel e você recebe a nota "
            "no chat; 'exportar' (PDF da prova e gabarito separado do último simulado), 'painel' (abre a área Estudos "
            "completa e devolve progresso, notas por tema e próxima aula sugerida), 'curso' (salva um objetivo persistente), "
             "'plano' (cria tarefas sem fingir conteúdo que não foi pesquisado), 'desempenho', 'revisoes', 'erros', 'aulas' e 'historico' "
            "(consultam registros reais), 'buscar' (procura na biblioteca). curso: nome curto do objetivo ('ENEM', 'inglês', "
            "'concurso PF', 'Cálculo 1'). 'mostrar' (cria um painel na tela durante a aula quando ajudar: tema = título, fonte = texto com linhas, "
            "'- [ ] item' vira checklist; imagem = termo da Wikipédia para foto com crédito, ex. 'Revolução Francesa'; use para "
            "explicação, trecho de edital/prova, flashcard (pergunta / resposta), plano de estudo, motivação; um por ideia, sem "
            "encher a tela). 'flashcards' (gera cards do tema, salva e mostra; o Chefe clica, vê a resposta e marca Acertei/Errei: revisão espaçada 1-3-7-15-30 dias), 'revisar_cards' (abre os cards vencidos de hoje; ofereça no começo de cada sessão). "
            "AULA COMPLETA (siga quando ele quiser aprender algo): 1) 'painel' para ver pontos fracos; 2) 'aula' para os trechos; "
            "3) explique em partes curtas, uma ideia por vez, com exemplo, e pergunte para checar; 4) mostre na tela com a ferramenta criar_painel "
            "painel: resumo/conceito = tipo info, passos/tópicos = lista, comparação = tabela, números = grafico, datas = lista numerada "
            "(linha do tempo), glossário = tabela termo|significado; mapa mental = design(acao='diagrama') com Mermaid 'mindmap'; "
            "5) 3-5 questões rápidas (criar_painel tipo questoes) ou 'simulado' se ele pedir prova; 6) 'flashcards' do que errou ou do tema; "
            "7) feche com resumo e, se ele quiser, PDF de resumo (design acao='pdf'). Como professora: use a personalidade definida, explique por etapas, faça perguntas e espere a "
            "resposta, cobre revisão; comece pelo 'painel' para saber notas e pontos fracos. Nunca invente fontes; nunca diga "
            "que retreinou o modelo."
        ),
    )
    async def estudo(self, acao: str, curso: str = "geral", tema: str = "", fonte: str = "", banca: str = "", quantidade: int = 5, imagem: str = "") -> str:
        body = {"imagem": imagem, "curso": curso, "tema": tema, "fonte": fonte, "banca": banca, "quantidade": max(1, min(20, int(quantidade or 5)))}
        normalized_action = str(acao or "painel").strip().lower()
        education_actions = {"painel", "curso", "plano", "planejar", "desempenho", "revisoes", "revisão", "revisao", "erros", "caderno", "caderno_de_erros", "aulas", "aula_anterior", "aulas_anteriores", "historico", "histórico"}
        endpoint = "/api/education/assistant" if normalized_action in education_actions else f"/api/tutor/{normalized_action}"
        try:
            r = await _post_local(endpoint, {**body, "acao": normalized_action, "name": curso, "target": tema}, read_timeout=120.0)
        except (httpx.HTTPError, TimeoutError):
            return "A parte de estudos não respondeu agora."
        if not r.get("ok"):
            return f"Não deu: {r.get('error') or 'falhou'}."
        if normalized_action == "mostrar":
            if r.get("delivered") == 0:
                return "A tela da Kira não recebeu o painel (nenhuma tela conectada). Diga que NÃO apareceu; não afirme que está na tela."
            return "Painel enviado à tela." + ("" if r.get("imagem") or not imagem else " Sem imagem na Wikipédia para esse termo.")
        if normalized_action == "pesquisar":
            return (f"Achei {len(r.get('editais') or [])} edital(is) e {len(r.get('provas') or [])} prova(s); baixando {r.get('baixando')} para a biblioteca. "
                    f"Links: {' '.join((r.get('editais') or [])[:2] + (r.get('provas') or [])[:4])}. NADA foi exibido na tela ainda: não diga que o edital está na tela; para exibir, chame estudo acao='mostrar'.")
        if normalized_action == "baixar":
            return "Baixando para a biblioteca; o progresso está no painel Estudos."
        if normalized_action == "simulado":
            return "Gerando o simulado; aparece no painel Estudos em alguns segundos. Diga isso curto e espere o Chefe responder e apertar Finalizar."
        if normalized_action in {"flashcards", "revisar_cards"}:
            if not r.get("cards"):
                return "Nenhum card para revisar hoje." if normalized_action == "revisar_cards" else "Não consegui gerar os cards."
            if r.get("delivered") == 0:
                return "Os cards foram salvos, mas a tela não recebeu o painel. Diga que NÃO apareceu."
            return f"{r['cards']} flashcard(s) na tela. Diga para ele clicar no card, ver a resposta e marcar Acertei ou Errei."
        if normalized_action == "aula":
            trechos = r.get("trechos") or []
            return ("Trechos da biblioteca:\n" + "\n".join(trechos)) if trechos else "Sem trechos na biblioteca sobre isso: ensine como conhecimento geral e avise."
        if normalized_action == "exportar":
            return "Salvei: " + ", ".join(r.get("arquivos") or [])
        if normalized_action == "buscar":
            return "\n".join(f"[{t['title']}] {t['text'][:400]}" for t in r.get("trechos") or []) or "Nada na biblioteca sobre isso."
        if normalized_action == "painel":
            summary = r.get("summary") or {}
            accuracy = summary.get("accuracy")
            accuracy_label = f"{accuracy}%" if accuracy is not None else "sem dados"
            return (f"Abri Estudos. Cursos: {summary.get('courses', 0)}; horas estudadas: {summary.get('study_hours', 0)}; "
                    f"simulados concluídos: {summary.get('quizzes_completed', 0)}; acurácia: {accuracy_label}.")
        if normalized_action == "curso":
            return f"Curso salvo: {(r.get('course') or {}).get('name') or curso}. Agora posso pesquisar fontes e montar o diagnóstico."
        if normalized_action in {"plano", "planejar"}:
            return f"Plano criado com {r.get('created', 0)} tarefa(s) persistente(s)."
        if normalized_action == "desempenho":
            summary = r.get("summary") or {}
            average = summary.get("average_score")
            average_label = average if average is not None else "sem nota"
            accuracy = summary.get("accuracy")
            accuracy_label = f"{accuracy}%" if accuracy is not None else "sem dados"
            return f"Desempenho real: média {average_label}, acurácia {accuracy_label}, sequência de {summary.get('streak_days', 0)} dia(s)."
        if normalized_action in {"revisoes", "revisão", "revisao"}:
            return f"Há {len(r.get('reviews') or [])} revisão(ões) registradas no histórico."
        if normalized_action in {"aulas", "aula_anterior", "aulas_anteriores"}:
            return f"Encontrei {len(r.get('lessons') or [])} aula(s) anterior(es) persistida(s)."
        if normalized_action in {"erros", "caderno", "caderno_de_erros"}:
            return f"O caderno tem {len(r.get('errors') or [])} erro(s) aberto(s) para revisar."
        if normalized_action in {"historico", "histórico"}:
            return f"Encontrei {len(r.get('history') or [])} evento(s) pedagógico(s) no histórico."
        return "Feito."


    @function_tool(
        name="conhecimento",
        description=(
            "Procura no conhecimento da Kira: arquivos que o Chefe soltou na tela (PDF, imagem, ZIP, provas, artigos, "
            "documentos) e a biblioteca de estudos. Use quando ele perguntar algo que pode estar num arquivo que mandou "
            "('o que dizia aquela prova da PM', 'procura no artigo que te mandei'). pergunta: o assunto em poucas palavras. "
            "Responda com base nos trechos e diga de qual arquivo veio."
        ),
    )
    async def conhecimento(self, pergunta: str) -> str:
        from urllib.parse import quote

        try:
            from runtime.media_tools import _get_local
        except ImportError:  # pragma: no cover
            from agent.runtime.media_tools import _get_local
        try:
            items = (await _get_local(f"/api/knowledge/search?q={quote(pergunta[:200])}", read_timeout=10.0)).get("items") or []
        except (httpx.HTTPError, TimeoutError):
            return "O conhecimento não respondeu agora."
        if not items:
            return "Não achei nada sobre isso nos arquivos guardados."
        return "\n\n".join(f"[{i['title']} — {i['course']}]\n{i['text'][:900]}" for i in items[:5])


class LockToolsMixin:
    @function_tool(
        name="bloqueio",
        description=(
            "'Kira, bloquear' / 'bloqueia a Kira' / 'bloqueia você' -> alvo 'kira': tela cheia em todos os monitores (e na TV), "
            "teclado e mouse presos e comandos travados; "
            "só sai com a senha digitada (voz nunca desbloqueia). 'Kira, bloquear o PC' / 'trava o computador' -> alvo 'pc': "
            "bloqueio real do Windows (Win+L), volta com a senha do Windows. Pedido de desbloqueio por voz: diga que precisa "
            "digitar a senha na tela."
        ),
    )
    async def bloqueio(self, alvo: str = "kira") -> str:
        try:
            if "pc" in str(alvo).lower() or "computador" in str(alvo).lower():
                r = await _post_local("/api/security/lock", {"delay": 1}, read_timeout=5.0)
                return "Bloqueando o Windows agora." if r.get("ok") else "Não consegui bloquear o Windows."
            r = await _post_local("/api/lock/lock", {}, read_timeout=5.0)
        except (httpx.HTTPError, TimeoutError):
            return "O bloqueio não respondeu."
        if r.get("error") == "sem_senha":
            return "Ainda não há senha da Kira. Abri na tela o campo para criar; peça para o Chefe digitar (nunca falar em voz)."
        return "Kira bloqueada. Para voltar, a senha na tela." if r.get("ok") else "Não consegui bloquear."




class DesignToolsMixin:
    _design_task: asyncio.Task | None = None

    async def _design_pdf_in_background(self, payload: dict) -> None:
        try:
            r = await _post_local("/api/design/pdf", payload, read_timeout=240.0)
            if r.get("ok") and r.get("arquivo"):
                self._last_design_pdf = r["arquivo"]
                message = f"Pronto, chefe: o PDF '{payload.get('titulo')}' está salvo em {r['arquivo']}. Quer que eu abra?"
            else:
                message = f"Não consegui terminar o PDF: {r.get('error') or 'falhou'}."
        except (httpx.HTTPError, TimeoutError):
            message = "O PDF demorou demais e não ficou pronto."
        coordinator = getattr(self, "_speech_coordinator", None)
        if coordinator is not None:
            await coordinator.speak_proactive_message(message, label="DESIGN", add_to_chat_ctx=True)
        else:
            logging.getLogger(__name__).info("DESIGN_NOTICE_NOT_SPOKEN reason=no_coordinator")

    @function_tool(
        name="design",
        description=(
            "Cria material visual de verdade. acao: 'pdf' | 'grafico' | 'diagrama' | 'qr' | 'abrir'.\n"
            "PDF: documento COMPLETO e bonito, em pt-BR (a capa com foto, data e sumário saem sozinhos). conteudo em "
            "Markdown com no mínimo 5 seções '## ', cada uma com 2+ parágrafos de fatos reais; inclua 'Pontos-chave' "
            "(lista), uma tabela 'a | b' ou linha do tempo quando couber, '> destaque' e '## Fontes' no fim. Imagens: "
            "escreva '![legenda descritiva](assunto da foto)' em linhas sozinhas (3 a 5): a foto real é buscada e "
            "embutida (sem foto, é gerada por IA). titulo: legível, com espaços (nunca 'Nome_Assim'); subtitulo curto. "
            "destino: onde o usuário pediu ('area de trabalho', 'downloads', 'documentos', nome de pasta); vazio = "
            "pasta da Kira. NÃO use show_images nem abra painel de imagens para PDF: só diga onde salvou.\n"
            "abrir: conteudo = caminho do PDF (ou pasta); tipo='pasta' mostra o arquivo no Explorer — use para "
            "'abre o PDF', 'abre a pasta'. grafico: conteudo = 'rótulo: valor' por linha ou JSON do Chart.js; tipo = "
            "bar, line, pie, doughnut, radar. diagrama: código Mermaid. qr: link ou texto. Gráfico, diagrama e QR "
            "aparecem num painel. Use dados reais; nunca invente números."
        ),
    )
    async def design(self, acao: str, titulo: str = "Kira", conteudo: str = "", tipo: str = "bar", subtitulo: str = "", destino: str = "") -> str:
        acao = str(acao).strip().lower()
        if acao == "abrir" and not conteudo.strip():
            conteudo = getattr(self, "_last_design_pdf", "")
        payload = {"titulo": titulo, "conteudo": conteudo, "tipo": tipo, "subtitulo": subtitulo, "destino": destino}
        if acao == "pdf":
            # Segundo plano + aviso falado no fim: segurar a ferramenta ~1 min deixava o Gemini cancelar a chamada
            # quando o Chefe falava, e o PDF ficava pronto sem ela avisar (2026-11-05).
            self._design_task = asyncio.create_task(self._design_pdf_in_background(payload))
            return "Comecei o PDF. Diga só que está fazendo, numa frase curta; eu aviso falando quando ficar pronto."
        try:
            r = await _post_local(f"/api/design/{acao}", payload, read_timeout=180.0)
        except (httpx.HTTPError, TimeoutError):
            return "O design não respondeu agora."
        if not r.get("ok"):
            return f"Não deu: {r.get('error') or 'falhou'}."
        if r.get("arquivo"):
            self._last_design_pdf = r["arquivo"]
            return f"PDF salvo em {r['arquivo']}. Diga onde salvou numa frase; para abrir use design(acao='abrir')."
        return f"Aberto: {r['aberto']}." if r.get("aberto") else "Pronto, está no painel da tela."


__all__ = ["TutorToolsMixin", "LockToolsMixin", "DesignToolsMixin"]
