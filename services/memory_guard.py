from __future__ import annotations

import re
from dataclasses import dataclass


_CPF_RE = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_RG_RE = re.compile(r"\b(?:rg|documento)\s*[:#-]?\s*[a-z0-9.\-]{5,}\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?:\+?\d{1,3}\s*)?(?:\(?\d{2}\)?\s*)?\d{4,5}[-\s]?\d{4}")
_API_KEY_RE = re.compile(
    r"\b(?:sk-[a-z0-9_-]{10,}|[a-z0-9]{24,}\.[a-z0-9_-]{6,}\.[a-z0-9_-]{6,}|"
    r"(?:api[_ -]?key|token|secret|chave)\s*[:=]\s*\S{6,})",
    re.IGNORECASE,
)
_PASSWORD_RE = re.compile(
    r"\b(?:senha|password|passcode|pin)(?:\s+[\wÀ-ÿ-]+){0,5}\s*(?::|=|e|é|eh|is)\s*\S+",
    re.IGNORECASE,
)
_ADDRESS_RE = re.compile(
    r"\b(?:endereco|endereço|rua|avenida|av\.|bairro|cep|moro em|localizacao exata|localização exata)\b",
    re.IGNORECASE,
)

_SENSITIVE_TERMS = {
    "senha",
    "password",
    "token",
    "chave api",
    "chave de api",
    "api key",
    "secret",
    "saude",
    "saúde",
    "doenca",
    "doença",
    "diagnostico",
    "diagnóstico",
    "remedio",
    "remédio",
    "tratamento",
    "intimo",
    "íntimo",
    "relacao intima",
    "relação íntima",
    "cartao",
    "cartão",
    "banco",
    "conta bancaria",
    "conta bancária",
    "pix",
    "salario",
    "salário",
    "financeiro",
    "privado de terceiro",
    "privada de terceiro",
}

_NOISE_TERMS = {"ok", "sim", "nao", "não", "oi", "ola", "olá", "aham", "beleza", "valeu"}


@dataclass(frozen=True)
class MemoryClassification:
    category: str
    memory_type: str
    sensitivity: str
    should_persist: bool
    masked_preview: str


def _clean(text: str | None) -> str:
    return " ".join(str(text or "").strip().split())


def _lower(text: str | None) -> str:
    return _clean(text).lower()


def is_sensitive(text: str | None) -> bool:
    clean = _clean(text)
    if not clean:
        return False
    lower = clean.lower()
    if any(term in lower for term in _SENSITIVE_TERMS):
        return True
    patterns = (_CPF_RE, _RG_RE, _EMAIL_RE, _PHONE_RE, _API_KEY_RE, _PASSWORD_RE, _ADDRESS_RE)
    return any(pattern.search(clean) for pattern in patterns)


def mask_sensitive_preview(text: str | None) -> str:
    masked = _clean(text)
    if not masked:
        return ""
    masked = _PASSWORD_RE.sub("senha: ***", masked)
    masked = _API_KEY_RE.sub("***SECRET***", masked)
    if re.search(r"\b(?:senha|password|passcode|pin)\b", masked, flags=re.IGNORECASE):
        masked = re.sub(r"\b\d{3,}\b", "***", masked)
    masked = _CPF_RE.sub("***DOCUMENTO***", masked)
    masked = _RG_RE.sub("***DOCUMENTO***", masked)
    masked = _EMAIL_RE.sub("***EMAIL***", masked)
    masked = _PHONE_RE.sub("***TELEFONE***", masked)
    if _ADDRESS_RE.search(masked):
        masked = _ADDRESS_RE.sub("***LOCAL***", masked)
    return masked[:240]


def safe_log_text_preview(text: str | None, *, max_chars: int = 180) -> str:
    clean = _clean(text)
    if not clean:
        return ""
    if is_sensitive(clean):
        return mask_sensitive_preview(clean)[:max(1, int(max_chars))]
    return clean[:max(1, int(max_chars))]


def detect_memory_type(text: str | None) -> str:
    lower = _lower(text)
    if not lower:
        return "noise"
    if is_sensitive(lower):
        return "sensitive"
    if any(term in lower for term in ("prefiro", "preferencia", "preferência", "gosto de", "nao gosto", "não gosto")):
        return "preference"
    if any(term in lower for term in ("larissa", "mae", "mãe", "pai", "irma", "irmã", "irmao", "irmão", "amigo", "cliente")):
        return "person"
    if any(term in lower for term in ("projeto", "modulo", "módulo", "arquivo", "servico", "serviço", "sqlite", "kira")):
        return "project"
    if any(term in lower for term in ("erro", "bug", "falha", "causa", "solucao", "solução", "corrigido", "validado")):
        return "learned_lesson"
    if any(term in lower for term in ("sessao", "sessão", "temporario", "temporário", "agora", "hoje")):
        return "temporary"
    if lower in _NOISE_TERMS or len(lower) < 4:
        return "noise"
    if any(term in lower for term in ("alan", "meu nome", "sou eu")):
        return "alan_profile"
    return "persistent"


def classify_memory_text(text: str | None) -> MemoryClassification:
    memory_type = detect_memory_type(text)
    sensitivity = "sensitive" if is_sensitive(text) or memory_type == "sensitive" else "normal"
    category = memory_type
    persist = should_persist(text)
    return MemoryClassification(
        category=category,
        memory_type=memory_type,
        sensitivity=sensitivity,
        should_persist=persist,
        masked_preview=mask_sensitive_preview(text),
    )


def should_persist(text: str | None) -> bool:
    lower = _lower(text)
    if not lower or lower in _NOISE_TERMS or len(lower) < 4:
        return False
    if any(term in lower for term in ("lembra", "lembrar", "guarda", "salva", "sempre", "prefiro")):
        return True
    return detect_memory_type(lower) not in {"noise", "temporary"}
