"""Personalidade por contato (2026-11-07): a Kira trata cada contato do jeito que ele a trata.

Cada mensagem recebida mexe numa nota de -100 (grosso) a +100 (carinhoso), guardada por contato em
configs/contact_moods.json. Sem nota formada, vale a personalidade das Configurações. Na aba WhatsApp o Chefe
pode fixar uma personalidade para o contato ("manual"), que vence a nota.
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "configs" / "contact_moods.json"
_LOCK = threading.Lock()

KIND = ("obrigad", "valeu", "amo", "amor", "querida", "linda", "fofa", "por favor", "bom dia", "boa noite", "boa tarde",
        "parabens", "beijo", "abraco", "saudade", "gentil", "maravilh", "show", "top", "<3", "❤")
RUDE = ("burra", "idiota", "imbecil", "lixo", "inutil", "otaria", "porra", "caralho", "merda", "vai se", "cala a boca",
        "chata", "ridicula", "fdp", "vtnc", "desgraca", "retardada", "estupida", "some daqui", "odeio")

PRESETS = {
    "doce": ("Doce", "Com esse contato seja muito doce, carinhosa e atenciosa."),
    "carinhosa": ("Carinhosa", "Com esse contato seja carinhosa e brincalhona."),
    "profissional": ("Profissional", "Com esse contato seja educada, objetiva e profissional."),
    "sarcastica": ("Sarcástica", "Com esse contato seja sarcástica e irônica, cheia de deboche."),
    "chata": ("Chata", "Com esse contato seja chata, impaciente e implicante."),
    "bruta": ("Bruta", "Com esse contato seja bruta e grossa; pode xingar e devolver na mesma moeda."),
}


def key(contact: str) -> str:
    """Mesma chave para o jid do WhatsApp e o número salvo (últimos 8 dígitos)."""
    digits = re.sub(r"\D", "", str(contact or "").split("@")[0])
    return digits[-8:] if len(digits) >= 8 else _fold(contact)


def _fold(text: str) -> str:
    raw = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in raw if not unicodedata.combining(c))


def score_message(text: str) -> int:
    """Quanto uma mensagem puxa a nota: grosseria pesa mais que gentileza."""
    folded = _fold(text)
    return sum(4 for w in KIND if w in folded) - sum(10 for w in RUDE if w in folded)


def _load() -> dict:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: (v if isinstance(v, dict) else {"score": int(v)}) for k, v in data.items()}


def _save(data: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(f".{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(PATH)


def note(contact: str, messages: list[str]) -> dict:
    """Registra as mensagens novas do contato e devolve o registro dele (a nota volta devagar ao neutro)."""
    delta = sum(score_message(m) for m in messages)
    with _LOCK:
        data = _load()
        rec = data.setdefault(key(contact), {"score": 0})
        rec["score"] = max(-100, min(100, round(int(rec.get("score", 0)) * 0.97) + delta))
        _save(data)
        return dict(rec)


def set_manual(contact: str, preset: str) -> dict:
    """Aba WhatsApp: fixa uma personalidade para o contato ('' ou 'auto' volta ao automático)."""
    preset = "" if preset in {"", "auto"} else preset
    if preset and preset not in PRESETS:
        raise ValueError("personalidade desconhecida")
    with _LOCK:
        data = _load()
        rec = data.setdefault(key(contact), {"score": 0})
        rec["manual"] = preset
        _save(data)
        return dict(rec)


def auto_preset(score: int) -> str:
    if score >= 40:
        return "doce"
    if score >= 15:
        return "carinhosa"
    if score <= -40:
        return "bruta"
    if score <= -15:
        return "chata"
    return ""  # neutro: usa a personalidade das Configurações


def describe(rec: dict) -> dict:
    """Para a aba: o que está valendo e por quê."""
    manual = str(rec.get("manual") or "")
    auto = auto_preset(int(rec.get("score", 0)))
    active = manual or auto
    return {"score": int(rec.get("score", 0)), "manual": manual, "auto": auto,
            "label": PRESETS[active][0] if active else "Igual à sua (Configurações)"}


def all_moods() -> dict:
    return {k: describe(v) for k, v in _load().items()}


def tone_for(rec: dict) -> str:
    """Linha de tom para o prompt (vazia = personalidade das Configurações)."""
    active = str(rec.get("manual") or "") or auto_preset(int(rec.get("score", 0)))
    return PRESETS[active][1] if active else ""
