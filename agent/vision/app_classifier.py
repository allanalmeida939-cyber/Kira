from __future__ import annotations

from dataclasses import dataclass, field


GAME_HINTS = {
    "minecraft": "Minecraft",
    "valorant": "Valorant",
    "league of legends": "League of Legends",
    "roblox": "Roblox",
    "fortnite": "Fortnite",
    "warzone": "Warzone",
    "counter-strike": "Counter-Strike",
    "cs2": "Counter-Strike 2",
    "steam": "Steam",
}

CODE_HINTS = {
    "visual studio code": "Visual Studio Code",
    "vscode": "Visual Studio Code",
    "pycharm": "PyCharm",
    "intellij": "IntelliJ IDEA",
    "terminal": "Terminal",
    "powershell": "PowerShell",
    "cmd": "Command Prompt",
    "traceback": "Python",
}

VIDEO_HINTS = {
    "youtube": "YouTube",
    "twitch": "Twitch",
    "netflix": "Netflix",
    "prime video": "Prime Video",
}

WEB_HINTS = {
    "chrome": "Google Chrome",
    "edge": "Microsoft Edge",
    "firefox": "Firefox",
    "github": "GitHub",
    "docs": "Documentacao",
    "dashboard": "Dashboard",
    "login": "Login",
}

DOCUMENT_HINTS = {
    "excel": "Excel",
    "word": "Word",
    "notion": "Notion",
    "pdf": "PDF",
    "powerpoint": "PowerPoint",
}


@dataclass(slots=True)
class ContextGuess:
    domain: str = "desktop"
    app_name: str = ""
    topic_guess: str = ""
    action_guess: str = ""
    entities: list[str] = field(default_factory=list)
    confidence: float = 0.0


def _guess_action(normalized_title: str, domain: str) -> str:
    if any(token in normalized_title for token in ("traceback", "exception", "error", "erro", "failed")):
        return "erro_visivel"
    if any(token in normalized_title for token in ("inventory", "inventario", "craft", "crafting")):
        return "inventario"
    if any(token in normalized_title for token in ("map", "mapa", "menu", "settings", "config")):
        return "menu"
    if any(token in normalized_title for token in ("tutorial", "lesson", "course")):
        return "tutorial"
    if any(token in normalized_title for token in ("login", "sign in", "entrar")):
        return "login"
    if domain == "code":
        return "editando_codigo"
    if domain == "video":
        return "assistindo_video"
    if domain == "game":
        return "jogando"
    if domain == "web":
        return "navegando"
    return ""


def _extract_topic(title: str) -> str:
    for separator in (" - ", " | ", " — "):
        if separator in title:
            left = title.split(separator, 1)[0].strip()
            if left:
                return left[:80]
    return title.strip()[:80]


def classify_window_context(title: str, *, visual_context_active: bool = False) -> ContextGuess:
    normalized = (title or "").strip().lower()
    guess = ContextGuess()

    if not normalized:
        guess.domain = "desktop"
        guess.confidence = 0.05
        return guess

    for keyword, app_name in GAME_HINTS.items():
        if keyword in normalized:
            guess.domain = "game"
            guess.app_name = app_name
            guess.entities = [app_name]
            guess.topic_guess = app_name
            guess.confidence = 0.88
            guess.action_guess = _guess_action(normalized, guess.domain)
            return guess

    for keyword, app_name in CODE_HINTS.items():
        if keyword in normalized:
            guess.domain = "code"
            guess.app_name = app_name
            guess.topic_guess = _extract_topic(title)
            guess.entities = [app_name]
            guess.confidence = 0.84
            guess.action_guess = _guess_action(normalized, guess.domain)
            return guess

    for keyword, app_name in VIDEO_HINTS.items():
        if keyword in normalized:
            guess.domain = "video"
            guess.app_name = app_name
            guess.topic_guess = _extract_topic(title)
            guess.entities = [app_name]
            guess.confidence = 0.8
            guess.action_guess = _guess_action(normalized, guess.domain)
            return guess

    for keyword, app_name in DOCUMENT_HINTS.items():
        if keyword in normalized:
            guess.domain = "document"
            guess.app_name = app_name
            guess.topic_guess = _extract_topic(title)
            guess.entities = [app_name]
            guess.confidence = 0.72
            guess.action_guess = _guess_action(normalized, guess.domain)
            return guess

    for keyword, app_name in WEB_HINTS.items():
        if keyword in normalized:
            guess.domain = "web"
            guess.app_name = app_name
            guess.topic_guess = _extract_topic(title)
            guess.entities = [app_name]
            guess.confidence = 0.68
            guess.action_guess = _guess_action(normalized, guess.domain)
            return guess

    guess.domain = "desktop" if visual_context_active else "unknown"
    guess.app_name = ""
    guess.topic_guess = _extract_topic(title)
    guess.entities = [guess.topic_guess] if guess.topic_guess else []
    guess.confidence = 0.38 if visual_context_active else 0.22
    guess.action_guess = _guess_action(normalized, guess.domain)
    return guess
