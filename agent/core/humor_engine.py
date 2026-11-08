from __future__ import annotations

from dataclasses import dataclass


HumorLevel = str


@dataclass(slots=True)
class HumorState:
    response_count: int = 0
    last_humor_response_index: int = -99


def should_block_humor(contexto: dict[str, object] | None) -> bool:
    tipo = str((contexto or {}).get("tipo", "") or "").strip().lower()
    intensidade = float((contexto or {}).get("intensidade", 0.0) or 0.0)
    if tipo in {"tecnico", "emocional"}:
        return True
    if intensidade >= 0.9:
        return True
    return False


def get_humor_level(contexto: dict[str, object] | None, intensidade: float) -> HumorLevel:
    tipo = str((contexto or {}).get("tipo", "") or "").strip().lower()
    if should_block_humor(contexto):
        return "none"
    if tipo == "provocacao" and intensidade >= 0.5:
        return "pesado"
    if tipo in {"casual", "brincadeira", "comando", "provocacao"}:
        return "leve"
    return "none"


def _can_inject_humor_now(state: HumorState) -> bool:
    # Max 1 humor every 3 responses.
    return (state.response_count - state.last_humor_response_index) >= 3


def _build_situational_humor(text: str, *, level: HumorLevel, contexto: dict[str, object] | None) -> str:
    lowered = text.lower()
    if level == "pesado":
        return f"{text} Eu ja esperava isso... esse padrao ta bem consistente."
    if "pendenc" in lowered:
        return f"{text} Isso aqui ja virou colecao."
    if str((contexto or {}).get("tipo", "")).lower() == "comando":
        return f"{text} Porque claramente nao ia se organizar sozinho."
    return f"{text} Alguem precisava fazer esse trabalho."


def apply_kira_humor(
    texto_resposta: str,
    contexto: dict[str, object] | None,
    state: HumorState,
) -> tuple[str, dict[str, object]]:
    text = str(texto_resposta or "").strip()
    if not text:
        state.response_count += 1
        return text, {"aplicado": False, "nivel": "none", "motivo": "texto_vazio"}

    state.response_count += 1
    if should_block_humor(contexto):
        return text, {"aplicado": False, "nivel": "none", "motivo": "bloqueado_contexto"}

    intensidade = float((contexto or {}).get("intensidade", 0.0) or 0.0)
    level = get_humor_level(contexto, intensidade)
    if level == "none":
        return text, {"aplicado": False, "nivel": "none", "motivo": "nivel_none"}

    if not _can_inject_humor_now(state):
        return text, {"aplicado": False, "nivel": level, "motivo": "frequencia"}

    enriched = _build_situational_humor(text, level=level, contexto=contexto)
    state.last_humor_response_index = state.response_count
    return enriched, {"aplicado": True, "nivel": level, "motivo": f"contexto_{str((contexto or {}).get('tipo', 'neutro')).lower()}"}
