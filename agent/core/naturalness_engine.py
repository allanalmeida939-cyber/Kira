from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(slots=True)
class NaturalnessState:
    response_count: int = 0
    recent_responses: list[str] = field(default_factory=list)
    recent_openings: list[str] = field(default_factory=list)
    recent_structures: list[str] = field(default_factory=list)


_OPENINGS = ("Certo", "Beleza", "Fechado", "Ok", "Pronto", "Entao", "Bom")
_REWRITE_PATTERNS: tuple[tuple[re.Pattern[str], tuple[str, ...], str], ...] = (
    (
        re.compile(r"^Vou organizar isso agora\.$", re.IGNORECASE),
        ("Ja vou organizar isso.", "Organizando isso agora.", "Deixa comigo, ja organizo."),
        "organizar_agora",
    ),
    (
        re.compile(r"^Vou resolver isso agora\.$", re.IGNORECASE),
        ("Ja vou resolver isso.", "Resolvendo isso agora.", "Pode deixar, ja resolvo."),
        "resolver_agora",
    ),
)


def _choose_opening(state: NaturalnessState) -> str:
    last = state.recent_openings[-1] if state.recent_openings else ""
    idx = state.response_count % len(_OPENINGS)
    candidate = _OPENINGS[idx]
    if candidate == last:
        candidate = _OPENINGS[(idx + 1) % len(_OPENINGS)]
    return candidate


def _should_apply_naturalness(contexto: dict[str, object] | None, state: NaturalnessState) -> bool:
    tipo = str((contexto or {}).get("tipo", "") or "").strip().lower()
    if tipo == "tecnico":
        # Minimal changes for technical context.
        return (state.response_count % 10) < 2
    return (state.response_count % 10) < 7  # ~70%


def _apply_opening_variation(text: str, state: NaturalnessState) -> tuple[str, bool]:
    stripped = text.strip()
    if not stripped:
        return stripped, False
    if re.match(r"^(Certo|Beleza|Fechado|Ok|Pronto|Entao|Bom),", stripped, flags=re.IGNORECASE):
        opening = _choose_opening(state)
        varied = re.sub(
            r"^(Certo|Beleza|Fechado|Ok|Pronto|Entao|Bom),",
            f"{opening},",
            stripped,
            flags=re.IGNORECASE,
        )
        state.recent_openings.append(opening)
        state.recent_openings = state.recent_openings[-5:]
        return varied, True
    return stripped, False


def _apply_structure_variation(text: str, state: NaturalnessState) -> tuple[str, bool]:
    for pattern, variants, structure_id in _REWRITE_PATTERNS:
        if pattern.match(text.strip()):
            idx = state.response_count % len(variants)
            candidate = variants[idx]
            if state.recent_structures and state.recent_structures[-1] == structure_id:
                candidate = variants[(idx + 1) % len(variants)]
            state.recent_structures.append(structure_id)
            state.recent_structures = state.recent_structures[-5:]
            return candidate, True
    return text, False


def _apply_micro_imperfection(text: str, state: NaturalnessState) -> tuple[str, bool]:
    if "..." in text:
        return text, False
    if (state.response_count % 4) != 0:
        return text, False
    if "," in text:
        return text.replace(",", "...", 1), True
    return f"Entao... {text[0].lower() + text[1:] if len(text) > 1 else text.lower()}", True


def apply_kira_naturalness(
    texto_resposta: str,
    contexto: dict[str, object] | None,
    historico: NaturalnessState,
) -> tuple[str, dict[str, object]]:
    base = str(texto_resposta or "").strip()
    historico.response_count += 1
    if not base:
        return base, {"aplicado": False, "variacao_abertura": False, "variacao_estrutura": False}

    if not _should_apply_naturalness(contexto, historico):
        historico.recent_responses.append(base)
        historico.recent_responses = historico.recent_responses[-5:]
        return base, {"aplicado": False, "variacao_abertura": False, "variacao_estrutura": False}

    text = base
    opening_changed = False
    structure_changed = False

    text, opening_changed = _apply_opening_variation(text, historico)
    text, structure_changed = _apply_structure_variation(text, historico)
    text, _ = _apply_micro_imperfection(text, historico)

    if historico.recent_responses and text == historico.recent_responses[-1]:
        text = f"{text} Pode deixar."

    historico.recent_responses.append(text)
    historico.recent_responses = historico.recent_responses[-5:]
    return text, {
        "aplicado": True,
        "variacao_abertura": opening_changed,
        "variacao_estrutura": structure_changed,
    }
