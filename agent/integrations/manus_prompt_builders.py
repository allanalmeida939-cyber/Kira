from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ManusTaskSpec:
    job_id: str
    task_type: str
    title: str
    user_request: str
    prompt: str
    expected_outputs: list[str]
    output_category: str
    language: str = "pt-BR"
    quality_level: str = "professional"
    attachments: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    safety_rules: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def _base_prompt(job: dict, section: str, output_desc: str, reqs: list[str]) -> str:
    user_request = str(job.get("user_request") or "")
    lines = [
        "CONTEXTO: voce e o Manus executando uma tarefa tecnica orquestrada pela Kira.",
        "OBJETIVO: criar o artefato solicitado com alta qualidade.",
        f"PEDIDO ORIGINAL DO USUARIO: {user_request}",
        f"FORMATO FINAL ESPERADO: {output_desc}",
        f"SECAO PRINCIPAL: {section}",
        "REQUISITOS:",
    ]
    lines.extend([f"- {r}" for r in reqs])
    lines.extend([
        "- Nao inventar dados sensiveis.",
        "- Se precisar assumir algo, declarar explicitamente no resumo.",
        "- Entregar arquivo final quando aplicavel e resumo do que foi feito.",
        "- Nao expor credenciais/chaves.",
    ])
    return "\n".join(lines)


def _pdf_builder(job: dict) -> ManusTaskSpec:
    user_request = str(job.get("user_request") or "").lower()
    wants_image = any(token in user_request for token in ("imagem", "image", "figura", "grafico", "gráfico", "ilustracao", "ilustração"))
    one_page = any(token in user_request for token in ("uma pagina", "uma página", "1 pagina", "1 página"))
    reqs = [
        "Criar PDF final em portugues do Brasil.",
        "Entregar um arquivo .pdf real como anexo ou arquivo baixavel.",
        "Nao responder apenas com texto no chat e nao dizer que o arquivo foi criado sem anexar o PDF.",
        "O resultado so sera aceito se houver arquivo PDF disponivel para download.",
        "Se nao conseguir criar ou anexar PDF, responder claramente: NAO_CONSEGUI_GERAR_ARQUIVO_PDF.",
        "Incluir capa profissional.",
        "Incluir sumario quando o pedido for profissional ou longo.",
        "Estruturar secoes claras: introducao, desenvolvimento e conclusao.",
        "Incluir referencias/fontes ao final quando houver pesquisa.",
        "Incluir quadros e graficos simples quando solicitado.",
        "Usar imagens quando possivel sem violar direitos.",
    ]
    if wants_image:
        reqs.append("O PDF precisa conter no minimo 1 imagem, ilustracao, grafico visual ou figura relevante.")
    if one_page:
        reqs.append("Respeitar estritamente o limite de uma pagina solicitado pelo usuario.")
    prompt = _base_prompt(job, "PDF", "Arquivo .pdf pronto para download", reqs)
    return ManusTaskSpec(job_id=str(job.get("job_id") or ""), task_type="pdf", title="PDF profissional", user_request=str(job.get("user_request") or ""), prompt=prompt, expected_outputs=["pdf", "resumo_execucao"], output_category="PDFs")


def _document_builder(job: dict) -> ManusTaskSpec:
    prompt = _base_prompt(job, "Documento", "Arquivo .docx ou .pdf", ["Texto formal e bem organizado.", "Secoes claras e coesas."])
    return ManusTaskSpec(job_id=str(job.get("job_id") or ""), task_type="document", title="Documento", user_request=str(job.get("user_request") or ""), prompt=prompt, expected_outputs=["docx_ou_pdf", "resumo_execucao"], output_category="Textos")


def _text_builder(job: dict) -> ManusTaskSpec:
    prompt = _base_prompt(job, "Texto", "Arquivo .txt ou .md", ["Texto objetivo e revisado.", "Estrutura em topicos quando util."])
    return ManusTaskSpec(job_id=str(job.get("job_id") or ""), task_type="text", title="Texto", user_request=str(job.get("user_request") or ""), prompt=prompt, expected_outputs=["txt_ou_md", "resumo_execucao"], output_category="Textos")


def _prompt_builder(job: dict) -> ManusTaskSpec:
    prompt = _base_prompt(job, "Prompt", "Prompt final pronto para copiar", ["Entregar apenas prompt estruturado.", "Nao executar a tarefa final solicitada no prompt."])
    return ManusTaskSpec(job_id=str(job.get("job_id") or ""), task_type="prompt", title="Prompt estruturado", user_request=str(job.get("user_request") or ""), prompt=prompt, expected_outputs=["prompt_final", "resumo_execucao"], output_category="Prompts")


def _code_builder(job: dict) -> ManusTaskSpec:
    reqs = [
        "Definir linguagem/stack e gerar arquivo(s) de codigo.",
        "Codigo limpo, com comentarios uteis e foco em manutencao.",
        "Nao mexer em arquivos nao enviados/nao solicitados.",
        "Incluir instrucoes de teste e validacao quando possivel.",
    ]
    prompt = _base_prompt(job, "Codigo", "Arquivos de codigo prontos", reqs)
    return ManusTaskSpec(job_id=str(job.get("job_id") or ""), task_type="code", title="Codigo", user_request=str(job.get("user_request") or ""), prompt=prompt, expected_outputs=["arquivos_codigo", "resumo_execucao"], output_category="Codigos")


def _slide_builder(job: dict) -> ManusTaskSpec:
    reqs = ["Gerar apresentacao PPTX quando possivel.", "Incluir capa e agenda/sumario.", "Slides objetivos e visual profissional."]
    prompt = _base_prompt(job, "Slides", "Arquivo .pptx", reqs)
    return ManusTaskSpec(job_id=str(job.get("job_id") or ""), task_type="slides", title="Apresentacao", user_request=str(job.get("user_request") or ""), prompt=prompt, expected_outputs=["pptx", "resumo_execucao"], output_category="Slides")


def _spreadsheet_builder(job: dict) -> ManusTaskSpec:
    reqs = ["Entregar planilha em xlsx/csv quando possivel.", "Abas e colunas claras.", "Incluir formulas quando solicitado."]
    prompt = _base_prompt(job, "Planilha", "Arquivo .xlsx ou .csv", reqs)
    return ManusTaskSpec(job_id=str(job.get("job_id") or ""), task_type="spreadsheet", title="Planilha", user_request=str(job.get("user_request") or ""), prompt=prompt, expected_outputs=["xlsx_ou_csv", "resumo_execucao"], output_category="Planilhas")


def _image_builder(job: dict) -> ManusTaskSpec:
    prompt = _base_prompt(job, "Imagem", "Arquivo de imagem final", ["Descrever estilo, composicao e formato esperado."])
    return ManusTaskSpec(job_id=str(job.get("job_id") or ""), task_type="image", title="Imagem", user_request=str(job.get("user_request") or ""), prompt=prompt, expected_outputs=["imagem", "resumo_execucao"], output_category="Imagens")


def _generic_builder(job: dict) -> ManusTaskSpec:
    prompt = _base_prompt(job, "Generico", "Artefato solicitado", ["Se faltar contexto, assumir padrao profissional e declarar suposicoes."])
    return ManusTaskSpec(job_id=str(job.get("job_id") or ""), task_type=str(job.get("task_type") or "unknown"), title="Tarefa de criacao", user_request=str(job.get("user_request") or ""), prompt=prompt, expected_outputs=["artefato", "resumo_execucao"], output_category=str(job.get("output_category") or "Outros"))


def build_manus_task_spec(job: dict) -> ManusTaskSpec:
    if str(job.get("task_type") or "").lower() == "web_action":
        return _web_action_builder(job)
    task_type = str(job.get("task_type") or "unknown").lower()
    if task_type == "pdf":
        return _pdf_builder(job)
    if task_type == "document":
        return _document_builder(job)
    if task_type == "text":
        return _text_builder(job)
    if task_type == "prompt":
        return _prompt_builder(job)
    if task_type in {"code", "python", "html", "javascript", "csharp"}:
        return _code_builder(job)
    if task_type in {"slides", "powerpoint"}:
        return _slide_builder(job)
    if task_type == "spreadsheet":
        return _spreadsheet_builder(job)
    if task_type == "image":
        return _image_builder(job)
    return _generic_builder(job)


def _web_action_builder(job: dict) -> ManusTaskSpec:
    action_type = str(job.get("action_type") or "unknown_action")
    blocked_assessment = bool(job.get("blocked_assessment"))
    reqs = [
        "Execute apenas o que foi pedido.",
        "Nao envie/submeta sem autorizacao explicita.",
        "Pare antes do botao final quando houver envio.",
        "Retorne resumo dos campos preenchidos e campos faltantes.",
        "Nao invente dados pessoais.",
        "Nao capture dados sensiveis desnecessarios.",
    ]
    if blocked_assessment:
        reqs.append("Se for prova/quiz/avaliacao, nao responder nem enviar; atuar apenas como tutor de estudo.")
    section = f"WEB_ACTION:{action_type}"
    output_desc = "Resumo da acao preparada e estado pronto para aprovacao"
    prompt = _base_prompt(job, section, output_desc, reqs)
    prompt += "\nTRAVA: NAO ENVIE SEM AUTORIZACAO."
    if blocked_assessment:
        prompt += "\nBLOQUEIO: tarefa avaliativa detectada; apenas explicacao/tutoria."
    return ManusTaskSpec(
        job_id=str(job.get("job_id") or ""),
        task_type="web_action",
        title="Acao web controlada",
        user_request=str(job.get("user_request") or ""),
        prompt=prompt,
        expected_outputs=["acao_preparada", "resumo_execucao"],
        output_category="Outros",
        metadata={
            "action_type": action_type,
            "requires_approval": bool(job.get("requires_approval", True)),
            "blocked_assessment": blocked_assessment,
        },
    )
