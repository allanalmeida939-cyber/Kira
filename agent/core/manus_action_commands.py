from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


@dataclass
class ManusActionCommand:
    action_type: str
    raw_text: str
    normalized_text: str
    confidence: float
    requires_approval: bool
    blocked_assessment: bool
    block_reason: str | None = None


def normalize_action_text(text: str) -> str:
    v = str(text or "").lower().strip()
    v = unicodedata.normalize("NFD", v)
    v = "".join(ch for ch in v if unicodedata.category(ch) != "Mn")
    v = re.sub(r"[^\w\s]", " ", v)
    v = re.sub(r"\s+", " ", v).strip()
    v = re.sub(r"^(kira|pira|chefe|cara)\s+", "", v)
    return v


def _is_simple_open_site(n: str) -> bool:
    return bool(re.search(r"\b(abre o google|abre o youtube|abre o site)\b", n))


def detect_manus_action_command(text: str) -> ManusActionCommand | None:
    n = normalize_action_text(text)
    if not n or _is_simple_open_site(n):
        return None

    assessment_terms = r"\b(prova|quiz|questionario avaliativo|avaliacao|nota|tentativa|finalizar tentativa|atividade avaliativa|exame)\b"
    if re.search(assessment_terms, n):
        return ManusActionCommand(
            action_type="blocked_assessment",
            raw_text=text,
            normalized_text=n,
            confidence=0.98,
            requires_approval=True,
            blocked_assessment=True,
            block_reason="assessment_detected",
        )

    action_type = None
    if re.search(r"\b(preenche esse formulario|preencher formulario|completa esse cadastro|entra nesse link e preenche|preenche mas nao envia|envia o formulario)\b", n):
        action_type = "form_fill"
    elif re.search(r"\b(pesquisa nesse site e monta arquivo|pesquisa nesse site)\b", n):
        action_type = "web_research"
    elif re.search(r"\b(extrai dados|extrair dados|coleta dados do site)\b", n):
        action_type = "web_extraction"
    elif re.search(r"\b(prepara mensagem no whatsapp web|whatsapp web)\b", n):
        action_type = "whatsapp_prepare"
    elif re.search(r"\b(manda o manus abrir esse site|usa o navegador para preencher|workflow no navegador)\b", n):
        action_type = "browser_workflow"

    if not action_type:
        return None

    requires_approval = bool(re.search(r"\b(nao envia|envia o formulario|enviar|submeter|confirmar|comprar|finalizar)\b", n))
    return ManusActionCommand(
        action_type=action_type,
        raw_text=text,
        normalized_text=n,
        confidence=0.9,
        requires_approval=requires_approval,
        blocked_assessment=False,
        block_reason=None,
    )
