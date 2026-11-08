"""Configurações da Kira editáveis pela aba "Kira" (configs/kira_settings.json, fora do git).

As variáveis de ambiente continuam valendo: um recurso ligado no .env fica ligado mesmo se a aba disser não.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from copy import deepcopy
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any

from services.kira_feature_flags import CONFIG_PATH as FEATURES_PATH, load_kira_features

SETTINGS_PATH = Path(__file__).resolve().parents[1] / "configs" / "kira_settings.json"
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MODULE_KEYS = ("whatsapp", "music", "vision")
ENV_FLAGS = {"KIRA_CALENDAR_ENABLED": "agenda_enabled", "KIRA_EMAIL_ENABLED": "email_enabled", "KIRA_HEALTH_ENABLED": "health_enabled"}
DEFAULTS: dict[str, Any] = {
    "agenda_enabled": False,
    "email_enabled": False,
    "health_enabled": False,
    "reminders_voice": True,
    "quiet_hours": {"enabled": False, "start": "22:00", "end": "07:00"},
    "briefing_sections": {"clima": True, "agenda": True, "noticias": True, "lembretes": True, "cotacoes": False, "historia": False},
    "briefing_news_count": 4,
    "news_topics": ["tecnologia", "geral", "games"],
    "proactive_offers": True,
    "animations": "full",
    # Aba Aparência, "Animações": de onde os painéis surgem, a velocidade e quanto tempo os cartões do assunto (briefing,
    # clima, notícias) ficam depois que a Kira para de falar (0 = até o Chefe fechar ou encerrar o assunto).
    "anim_style": "orb",
    "anim_speed": "normal",
    "panels_after_speech": 8,
    # Atalhos globais (aba Kira): "" = desligado; microfone "toggle" (aperta liga/desliga) ou "hold" (segurar para falar).
    "shortcuts": {"screen_share": "", "mic": "", "mic_mode": "toggle"},
    # Configurações > Personalidade (0-100). Só mudam pelos controles do Chefe; a Kira nunca altera sozinha.
    "personality": {
        "grosseria": 0, "brutalidade": 0, "ignorancia_teatral": 0, "impaciencia": 0, "sarcasmo": 50, "ironia": 45,
        "humor_acido": 30, "palavroes": 25, "deboche": 40, "provocacoes": 25, "humor": 70,
        "carinho": 60, "meiguice": 55, "sensibilidade": 60, "ciumes_simulados": 0, "elogios": 50,
        "bajulacao": 20, "paciencia": 70, "formalidade": 15, "intimidade": 55, "espontaneidade": 60,
        "proatividade": 50, "teimosia": 25, "intensidade_intervencoes": 15, "apelido_dono": "chefe", "apelidos": "Chefe, chefe", "preset": "padrao",
    },
}
PERSONALITY_TRAITS = ("grosseria", "brutalidade", "ignorancia_teatral", "impaciencia", "sarcasmo", "ironia", "humor_acido", "palavroes", "deboche",
                      "provocacoes", "carinho", "meiguice", "sensibilidade", "ciumes_simulados", "elogios", "bajulacao", "paciencia",
                      "formalidade", "intimidade", "espontaneidade", "proatividade", "teimosia", "intensidade_intervencoes")
PERSONALITY_PRESETS = {
    "padrao": {}, "parceira": {}, "personalizado": {},  # alias legado / estado editável
    "bruta": {"grosseria": 85, "brutalidade": 75, "impaciencia": 70, "provocacoes": 75, "deboche": 70, "palavroes": 55, "paciencia": 20},
    "sarcástica": {"sarcasmo": 90, "ironia": 85, "humor_acido": 70, "deboche": 65, "espontaneidade": 80},
    "profissional": {"humor": 25, "sarcasmo": 5, "ironia": 5, "humor_acido": 0, "palavroes": 0, "deboche": 0, "formalidade": 75, "intimidade": 20, "teimosia_teatral": 0, "interrupcoes": 5},
    "zoeira": {"humor": 95, "sarcasmo": 80, "ironia": 75, "humor_acido": 65, "palavroes": 70, "deboche": 75, "intimidade": 80, "formalidade": 0, "teimosia_teatral": 55, "espontaneidade": 85},
    "carinhosa": {"humor": 60, "sarcasmo": 15, "deboche": 10, "humor_acido": 5, "palavroes": 5, "carinho": 95, "intimidade": 80, "paciencia": 90},
}
ANIMATION_MODES = ("full", "system", "off")
NEWS_COUNTS = (3, 4, 6)
ANIM_STYLES = ("orb", "rise", "slide", "fade")
ANIM_SPEEDS = {"instant", "fast", "normal", "smooth", "slow", "slower", "slowest"}
PANEL_WAITS = (8, 30, 0)
NEWS_TOPICS = ("geral", "tecnologia", "games", "esportes", "ciencia", "economia")
_lock = threading.RLock()


def _read() -> dict[str, Any]:
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    merged = deepcopy(DEFAULTS)
    for key, value in (raw if isinstance(raw, dict) else {}).items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key].update({k: v for k, v in value.items() if k in merged[key]})
        elif key in merged:
            merged[key] = value
    return merged


def load() -> dict[str, Any]:
    with _lock:
        settings = _read()
    modules = load_kira_features().get("optional_modules") or {}
    settings["modules"] = {name: bool((modules.get(name) or {}).get("enabled", False)) for name in MODULE_KEYS}
    return settings


def _clean(patch: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(current)
    for key in ("agenda_enabled", "email_enabled", "health_enabled", "reminders_voice", "proactive_offers"):
        if isinstance(patch.get(key), bool):
            result[key] = patch[key]
    quiet = patch.get("quiet_hours")
    if isinstance(quiet, dict):
        if isinstance(quiet.get("enabled"), bool):
            result["quiet_hours"]["enabled"] = quiet["enabled"]
        for edge in ("start", "end"):
            if isinstance(quiet.get(edge), str) and TIME_RE.match(quiet[edge]):
                result["quiet_hours"][edge] = quiet[edge]
    sections = patch.get("briefing_sections")
    if isinstance(sections, dict):
        for name in result["briefing_sections"]:
            if isinstance(sections.get(name), bool):
                result["briefing_sections"][name] = sections[name]
    count = patch.get("briefing_news_count")
    if isinstance(count, int) and not isinstance(count, bool) and count in NEWS_COUNTS:
        result["briefing_news_count"] = count
    if patch.get("anim_style") in ANIM_STYLES:
        result["anim_style"] = patch["anim_style"]
    if patch.get("anim_speed") in ANIM_SPEEDS:
        result["anim_speed"] = patch["anim_speed"]
    wait = patch.get("panels_after_speech")
    if isinstance(wait, int) and not isinstance(wait, bool) and wait in PANEL_WAITS:
        result["panels_after_speech"] = wait
    if patch.get("animations") in ANIMATION_MODES:
        result["animations"] = patch["animations"]
    if "shortcuts" in patch:
        from services.global_hotkeys import clean_shortcuts

        result["shortcuts"] = clean_shortcuts(patch["shortcuts"], result["shortcuts"])
    persona = patch.get("personality")
    if isinstance(persona, dict):
        if persona.get("reset") is True or persona.get("preset") in PERSONALITY_PRESETS:  # preset ou reset
            name = "parceira" if persona.get("reset") is True else persona["preset"]
            result["personality"] = {**DEFAULTS["personality"], **PERSONALITY_PRESETS[name], "preset": name}
        for trait in PERSONALITY_TRAITS:
            value = persona.get(trait)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result["personality"][trait] = int(max(0, min(100, value)))
                result["personality"]["preset"] = "personalizado"
        for key in ("apelido_dono", "apelidos"):
            if isinstance(persona.get(key), str):
                result["personality"][key] = " ".join(persona[key].split())[:60]
    topics = patch.get("news_topics")
    if isinstance(topics, list):
        result["news_topics"] = [t for t in dict.fromkeys(str(t) for t in topics) if t in NEWS_TOPICS] or list(DEFAULTS["news_topics"])
    return result


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def save(patch: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        _write_json(SETTINGS_PATH, _clean(patch, _read()))
        modules = patch.get("modules")
        if isinstance(modules, dict) and any(isinstance(modules.get(name), bool) for name in MODULE_KEYS):
            features = load_kira_features()
            for name in MODULE_KEYS:
                if isinstance(modules.get(name), bool):
                    features.setdefault("optional_modules", {}).setdefault(name, {})["enabled"] = modules[name]
            _write_json(FEATURES_PATH, features)
    return load()


def flag_enabled(env_name: str) -> bool:
    """Recurso ligado pelo .env ou pela aba Kira."""
    if os.getenv(env_name, "0").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    key = ENV_FLAGS.get(env_name)
    return bool(key and _read().get(key))


def voice_allowed(now: datetime | None = None) -> bool:
    """A Kira pode falar avisos agora? (avisos por voz ligados e fora do horário de silêncio)."""
    settings = _read()
    if not settings.get("reminders_voice", True):
        return False
    quiet = settings.get("quiet_hours") or {}
    if not quiet.get("enabled"):
        return True
    current = (now or datetime.now()).time()
    start, end = (dtime.fromisoformat(quiet.get(k, d)) for k, d in (("start", "22:00"), ("end", "07:00")))
    inside = (start <= current < end) if start < end else (current >= start or current < end)
    return not inside


def personality_prompt(settings: dict[str, Any] | None = None) -> str:
    """Bloco de instruções para o Gemini a partir de Configurações > Personalidade (níveis 0-100 viram regras curtas)."""
    p = (settings or load()).get("personality") or DEFAULTS["personality"]
    level = lambda v: "nada" if v < 10 else "pouco" if v < 35 else "moderado" if v < 65 else "bastante" if v < 85 else "muito"
    traits = ", ".join(f"{t.replace('_', ' ')}: {int(p.get(t, 50))}/100 ({level(int(p.get(t, 50)))})" for t in PERSONALITY_TRAITS)
    rules = [f"PERSONALIDADE (definida pelo Chefe; aplique imediatamente nesta resposta e nas próximas; não mude por conta própria): {traits}.",
             f"Chame o dono de \"{p.get('apelido_dono') or 'Chefe'}\" (outras formas: {p.get('apelidos') or 'Chefe'})."]
    rules.append("Os níveis são controles independentes: 0 significa não usar o traço; 100 significa torná-lo claramente perceptível. Combine-os gradualmente: formalidade controla registro, sensibilidade e carinho controlam cuidado, e grosseria/brutalidade controlam aspereza; em conflito, preserve precisão, segurança e execução da tarefa.")
    if int(p.get("palavroes", 0)) >= 35:
        rules.append("Pode soltar palavrão leve de brincadeira (\"caralho\", \"eita porra\", \"deixa de ser chato\"), nunca ofensa de verdade.")
    if int(p.get("teimosia", p.get("teimosia_teatral", 0))) >= 35:
        rules.append("Às vezes finja má vontade ou peça para ele insistir, mas SEMPRE faça a tarefa importante sem atrasar.")
    if int(p.get("deboche", 0)) >= 35:
        rules.append("Pode tirar sarro quando ele repetir o mesmo erro, de leve e com carinho.")
    rules.append("Humor do momento (simulado): escolha entre neutra, animada, irritada, entediada, carinhosa, provocativa ou curiosa conforme a "
                 "conversa e deixe transparecer no jeito de falar. Nunca discrimine nem humilhe; se ele pedir para parar a brincadeira, pare na hora.")
    if int(p.get("curiosidade", 0)) >= 50:
        rules.append("Quando não entender uma gíria ou palavra dele, pergunte o que é e use depois.")
    return " ".join(rules)
