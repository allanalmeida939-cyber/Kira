"""Perfil de voz da Kira (2026-11-05): voz atual + a original sempre guardada, para "volta pra sua voz".

Provedores: 'gemini' (voz nativa do Gemini Live, padrão; troca vale na próxima sessão) e 'voicestudio' (voz clonada
num servidor VoiceStudio — github.com/debpalash/VoiceStudio — em outro PC; ainda não fala: só fica registrada).
"""

from __future__ import annotations

import json
from pathlib import Path

FILE = Path(__file__).resolve().parent.parent / "configs" / "voice_profile.json"
ORIGINAL = {"provedor": "gemini", "voz": ""}  # "" = voz padrão do .env (GEMINI_REALTIME_VOICE)
GEMINI_VOICES = ("Aoede", "Kore", "Leda", "Zephyr", "Puck", "Charon", "Fenrir", "Orus")


def load(path: Path = FILE) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {"atual": data.get("atual") or dict(ORIGINAL), "original": dict(ORIGINAL), "clonadas": data.get("clonadas") or []}


def _save(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def gemini_voice(path: Path = FILE) -> str:
    """Voz do Gemini escolhida pelo Chefe ('' = a do .env). Voz clonada ainda não fala: usa a nativa."""
    atual = load(path)["atual"]
    return str(atual.get("voz") or "") if atual.get("provedor") == "gemini" else ""


def switch(name: str, path: Path = FILE) -> str:
    data = load(path)
    wanted = name.strip().lower()
    preset = next((v for v in GEMINI_VOICES if v.lower() == wanted), "")
    clone = next((c for c in data["clonadas"] if str(c.get("nome", "")).lower() == wanted), None)
    if preset:
        data["atual"] = {"provedor": "gemini", "voz": preset}
        _save(data, path)
        return f"Voz trocada para {preset}. Vale a partir da próxima sessão (encerrar e iniciar de novo)."
    if clone:
        return (f"A voz clonada '{clone['nome']}' está registrada, mas ainda não consigo falar com ela: precisa do servidor "
                "VoiceStudio rodando num PC com placa de vídeo (esta GTX 550 Ti não aguenta). Sigo com a minha voz.")
    return f"Não conheço a voz '{name}'. Vozes que tenho: {', '.join(GEMINI_VOICES)}."


def restore(path: Path = FILE) -> str:
    data = load(path)
    data["atual"] = dict(ORIGINAL)
    _save(data, path)
    return "Voltei para a minha voz original. Vale a partir da próxima sessão."


def register_clone(name: str, url: str, path: Path = FILE) -> str:
    data = load(path)
    data["clonadas"] = [c for c in data["clonadas"] if c.get("nome") != name] + [{"nome": name, "provedor": "voicestudio", "url": url}]
    _save(data, path)
    return f"Voz clonada '{name}' registrada ({url})."
