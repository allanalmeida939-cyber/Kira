"""Ferramenta 'emergencia' (2026-11-07): o Chefe ajusta por voz folgas, turno, sono e contatos de emergência
(configs/emergencia.json), que a secretária do WhatsApp usa para dizer como ele está e para chamá-lo em emergência."""

from __future__ import annotations

import json
from pathlib import Path

from livekit.agents import function_tool

FILE = Path(__file__).resolve().parents[2] / "configs" / "emergencia.json"


class EmergencyToolsMixin:
    @function_tool(
        name="emergencia",
        description=(
            "Configura a parte de emergência do WhatsApp. acao: 'ver' | 'contato' (valor = nome do contato de emergência a "
            "adicionar; 'remover:Nome' tira) | 'folgas' (valor = dias separados por vírgula, ex. 'sabado, domingo', ou datas "
            "AAAA-MM-DD; 'hoje' marca hoje como folga) | 'turno' (valor = '07:00-17:00') | 'dorme' (valor = '23:00-07:30') | "
            "'em_casa' (valor = sim/nao). Em emergência de um contato desses, com o Chefe de folga e em casa, a Kira liga a TV, "
            "abre a tela dela e chama o Chefe por voz algumas vezes, e avisa o contato."
        ),
    )
    async def emergencia(self, acao: str = "ver", valor: str = "") -> str:
        try:
            cfg = json.loads(FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
        acao, valor = str(acao or "ver").strip().lower(), str(valor or "").strip()
        if acao == "contato" and valor:
            nomes = [n for n in cfg.get("contatos_emergencia", []) if n.lower() != valor.split(":")[-1].strip().lower()]
            if not valor.lower().startswith("remover:"):
                nomes.append(valor)
            cfg["contatos_emergencia"] = nomes
        elif acao == "folgas" and valor:
            if valor.lower() == "hoje":
                from datetime import date
                cfg.setdefault("folgas", []).append(date.today().isoformat())
            else:
                cfg["folgas"] = [d.strip().lower() for d in valor.split(",") if d.strip()]
        elif acao in {"turno", "dorme"} and "-" in valor:
            cfg[acao] = valor
        elif acao == "em_casa":
            cfg["em_casa_na_folga"] = valor.lower() in {"sim", "s", "true", "1"}
        elif acao != "ver":
            return "Ação inválida: use ver, contato, folgas, turno, dorme ou em_casa."
        if acao != "ver":
            FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return "Emergência: " + json.dumps(cfg, ensure_ascii=False)
