"""Modo "só responde quando chamada" (estilo Alexa), 2026-11-07.

Ligado, a Kira continua ouvindo, mas só age em falas que tenham uma palavra de ativação
(configs/economy_mode.json -> wake_words). Liga/desliga por voz (frase local ou ferramenta
'modo_economia' do Gemini). Estado em runtime/wake_only_mode.json (lido pelo agente e pelo app)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from services.wake_word_runtime import detect_wake_word, normalize_wake_text

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "runtime" / "wake_only_mode.json"
CONFIG_FILE = ROOT / "configs" / "economy_mode.json"

ENTER_PHRASES = ("entra no modo economia", "entrar no modo economia", "ativa o modo economia", "modo economia ligado",
                 "so responde quando eu te chamar", "so fala quando eu te chamar", "so responde quando eu chamar")
EXIT_PHRASES = ("sai do modo economia", "sair do modo economia", "desativa o modo economia", "desliga o modo economia",
                "pode conversar normal", "volta ao normal", "conversa normal")

_cache: dict[str, object] = {"mtime": -1.0, "active": False, "words_mtime": -1.0, "words": []}


def is_active() -> bool:
    try:
        mtime = STATE_FILE.stat().st_mtime
    except OSError:
        return False
    if mtime != _cache["mtime"]:
        try:
            _cache["active"] = bool(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("active"))
        except (OSError, ValueError):
            _cache["active"] = False
        _cache["mtime"] = mtime
    return bool(_cache["active"])


def set_active(active: bool, *, source: str = "voz") -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"active": bool(active), "since": time.time(), "source": source}), encoding="utf-8")


def wake_words() -> list[str]:
    try:
        mtime = CONFIG_FILE.stat().st_mtime
        if mtime != _cache["words_mtime"]:
            _cache["words"] = list(json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("wake_words") or [])
            _cache["words_mtime"] = mtime
    except (OSError, ValueError):
        pass
    return list(_cache["words"] or ["kira"])


def voice_command(text: str) -> str:
    """'enter' / 'exit' / '' para a frase falada."""
    norm = normalize_wake_text(text)
    if any(p in norm for p in EXIT_PHRASES):
        return "exit"
    if any(p in norm for p in ENTER_PHRASES):
        return "enter"
    return ""


def should_ignore(text: str) -> bool:
    """True quando o modo está ligado e a fala não chama a Kira (nem liga/desliga o modo)."""
    if not is_active():
        return False
    command = voice_command(text)
    if command == "exit":
        set_active(False, source="frase")
        return False
    if command == "enter":
        return False
    return detect_wake_word(text, wake_words()) is None
