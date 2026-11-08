"""Compacta as instruções da Kira sem mudar o jeito dela.

As instruções tinham ~64 mil caracteres (~29 mil tokens com as ferramentas) e cada resposta pagava isso em
latência. Duas fontes de inchaço, ambas mecânicas:
- blocos copiados duas vezes (AGENT_INSTRUCTION e SESSION_INSTRUCTION repetem os mesmos bancos de frases);
- bancos de frases de exemplo com 50-90 itens separados por ";" — o modelo pega o estilo com ~10.
"""
from __future__ import annotations

import re

BANK_MIN_ITEMS = 14
BANK_KEEP = 10


def _trim_bank(line: str) -> str:
    items = [item.strip() for item in line.split(";") if item.strip()]
    if len(items) < BANK_MIN_ITEMS:
        return line
    step = len(items) / BANK_KEEP
    kept = [items[int(i * step)] for i in range(BANK_KEEP)]
    return "; ".join(kept) + " (exemplos: varie no mesmo estilo, sem repetir literalmente)."


EXAMPLE_LINE = re.compile(r'^\s*(?:[-•*]\s*)?["“«]')
EXAMPLE_RUN_KEEP = 4


def compact_prompt(text: str) -> str:
    seen: set[str] = set()
    out: list[str] = []
    run = 0
    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        # sequência de frases-exemplo entre aspas, uma por linha: 4 bastam para o tom
        run = run + 1 if EXAMPLE_LINE.match(line) else 0
        if run > EXAMPLE_RUN_KEEP:
            continue
        key = re.sub(r"\s+", " ", line.strip().lower())
        if len(key) > 40:
            if key in seen:
                continue  # bloco repetido em outra parte das instruções
            seen.add(key)
        out.append(_trim_bank(line) if line.count(";") >= BANK_MIN_ITEMS - 1 else line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
