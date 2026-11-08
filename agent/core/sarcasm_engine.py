from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


SarcasmLevel = str


@dataclass(slots=True)
class SarcasmState:
    response_count: int = 0
    last_sarcasm_response_index: int = -99
    provocacao_count: int = 0
    last_context_type: str = ""
    last_update_at: datetime | None = None


def should_block_sarcasm(contexto: dict[str, object] | None) -> bool:
    tipo = str((contexto or {}).get("tipo", "") or "").strip().lower()
    intensidade = float((contexto or {}).get("intensidade", 0.0) or 0.0)
    if tipo in {"tecnico", "emocional"}:
        return True
    # High-intensity situations are treated as critical.
    if intensidade >= 0.9:
        return True
    return False


def _is_repeated_error_context(contexto: dict[str, object] | None) -> bool:
    words = [str(w).lower() for w in (contexto or {}).get("palavras_chave_detectadas", [])]
    return any(token in words for token in ("erro", "bug", "falha"))


def get_sarcasm_level(
    contexto: dict[str, object] | None,
    intensidade: float,
    historico: SarcasmState,
) -> SarcasmLevel:
    tipo = str((contexto or {}).get("tipo", "") or "").strip().lower()
    if should_block_sarcasm(contexto):
        return "none"
    if tipo != "provocacao" and not _is_repeated_error_context(contexto):
        return "none"

    if historico.provocacao_count >= 3 and intensidade >= 0.5:
        return "pesado"
    if historico.provocacao_count >= 2:
        return "medio"
    return "leve"


def _can_apply_sarcasm_now(state: SarcasmState) -> bool:
    # Maximum 1 sarcasm every 2 responses.
    return (state.response_count - state.last_sarcasm_response_index) >= 2


def _apply_sarcasm_text(text: str, level: SarcasmLevel, *, contexto: dict[str, object] | None) -> str:
    if level == "leve":
        return f"{text} Interessante... vindo de voce."
    if level == "medio":
        return f"{text} Curioso... porque quem errou foi voce."
    if level == "pesado":
        return f"{text} Olha... consistencia voce tem. So nao e do jeito certo."
    return text


def apply_kira_sarcasm(
    texto_resposta: str,
    contexto: dict[str, object] | None,
    historico: SarcasmState,
    *,
    now: datetime | None = None,
) -> tuple[str, dict[str, object]]:
    text = str(texto_resposta or "").strip()
    historico.response_count += 1

    current_now = now or datetime.now(timezone.utc)
    if historico.last_update_at is not None and (current_now - historico.last_update_at) > timedelta(minutes=10):
        historico.provocacao_count = 0
    historico.last_update_at = current_now

    current_type = str((contexto or {}).get("tipo", "") or "").strip().lower()
    if current_type != historico.last_context_type and current_type != "provocacao":
        historico.provocacao_count = 0
    historico.last_context_type = current_type

    if current_type == "provocacao":
        historico.provocacao_count += 1

    if not text:
        return text, {"aplicado": False, "nivel": "none", "motivo": "texto_vazio"}
    if should_block_sarcasm(contexto):
        return text, {"aplicado": False, "nivel": "none", "motivo": "bloqueado"}

    intensity = float((contexto or {}).get("intensidade", 0.0) or 0.0)
    level = get_sarcasm_level(contexto, intensity, historico)
    if level == "none":
        return text, {"aplicado": False, "nivel": "none", "motivo": "sem_provocacao"}
    if not _can_apply_sarcasm_now(historico):
        return text, {"aplicado": False, "nivel": level, "motivo": "frequencia"}

    enriched = _apply_sarcasm_text(text, level, contexto=contexto)
    historico.last_sarcasm_response_index = historico.response_count
    reason = "provocacao" if current_type == "provocacao" else "historico"
    return enriched, {"aplicado": True, "nivel": level, "motivo": reason}
