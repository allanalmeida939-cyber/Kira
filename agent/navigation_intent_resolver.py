from __future__ import annotations

from dataclasses import dataclass, asdict
import re
import unicodedata
import urllib.parse


YOUTUBE_BASE_URL = "https://www.youtube.com/"
INSTAGRAM_BASE_URL = "https://www.instagram.com/"

YOUTUBE_KNOWN_CHANNELS = {
    "coisa de nerd": "@coisadenerd",
    "coisas de nerd": "@coisadenerd",
    "coisadenerd": "@coisadenerd",
}

INSTAGRAM_ROUTE_ALIASES = {
    "direct": "/direct/inbox/",
    "mensagens": "/direct/inbox/",
    "mensagem": "/direct/inbox/",
    "inbox": "/direct/inbox/",
    "dm": "/direct/inbox/",
    "explorar": "/explore/",
    "explore": "/explore/",
}

PLATFORM_KEYWORDS = {
    "youtube": {"youtube", "you tube", "yt", "yotube", "yutube"},
    "instagram": {"instagram", "insta"},
    "google": {"google"},
    "gmail": {"gmail"},
    "whatsapp_web": {"whatsapp web", "whatsapp"},
    "spotify": {"spotify"},
}

PLATFORM_HOME = {
    "youtube": YOUTUBE_BASE_URL,
    "instagram": INSTAGRAM_BASE_URL,
    "google": "https://www.google.com/",
    "gmail": "https://mail.google.com/mail/u/0/#inbox",
    "whatsapp_web": "https://web.whatsapp.com/",
    "spotify": "https://open.spotify.com/",
}

INTERNAL_BROWSER_EXPLICIT_PHRASES = (
    "no teu navegador",
    "no seu navegador",
    "no navegador da kira",
    "no navegador dela",
    "no teu proprio navegador",
    "no seu proprio navegador",
    "no navegador interno",
    "no navegador interno da kira",
    "no navegador nativo",
    "no navegador nativo da kira",
    "dentro de voce",
    "dentro da kira",
    "ai dentro",
    "na tua tela",
    "na sua tela",
    "na tua interface",
    "na sua interface",
    "no teu browser",
    "no browser da kira",
)

# P1 — pedido EXPLICITO do usuario para usar o navegador DELE (local). So nesses
# casos abrimos o navegador do Windows; o resto vai pro Browser Agent dedicado.
EXTERNAL_BROWSER_EXPLICIT_PHRASES = (
    "no meu navegador", "no meu proprio navegador", "no navegador padrao",
    "no navegador do windows", "no meu navegador padrao", "no navegador do pc",
    "no meu opera", "no opera", "no meu chrome", "no google chrome", "no chrome",
    "no meu edge", "no microsoft edge", "no edge", "no meu firefox", "no firefox",
    "no meu brave", "no brave", "no navegador externo", "no navegador normal",
    "abre aqui pra mim ver", "abre aqui pra eu ver", "abre aqui pra ver",
    "aqui pra eu ver", "aqui pra mim ver", "no navegador de fora",
)


@dataclass
class NavigationResolution:
    platform: str
    target_type: str
    target_text: str
    normalized_target: str
    resolved_url: str
    fallback_url: str
    confidence: float
    fallback_used: bool
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def normalize_text(text: str | None) -> str:
    raw = (text or "").strip().lower()
    no_accents = "".join(ch for ch in unicodedata.normalize("NFD", raw) if unicodedata.category(ch) != "Mn")
    compact = re.sub(r"\s+", " ", no_accents).strip()
    return compact


def _strip_command_prefix(text: str) -> str:
    cleaned = normalize_text(text)
    cleaned = re.sub(
        r"^(abre|abrir|abre ai|abre aí|coloca|coloque|toca|toque|procura|pesquisa|pesquisar|abre o site|site da|site do)\s+",
        "",
        cleaned,
    )
    return cleaned.strip()


def _detect_platform(text: str) -> str:
    normalized = normalize_text(text)
    for platform, aliases in PLATFORM_KEYWORDS.items():
        for alias in aliases:
            if alias in normalized:
                return platform
    return ""


def _is_url_like(text: str) -> bool:
    t = (text or "").strip()
    return bool(
        re.match(r"^https?://", t, flags=re.I)
        or re.match(r"^www\.", t, flags=re.I)
        or re.match(r"^[a-z0-9-]+\.[a-z]{2,}([/:].*)?$", t, flags=re.I)
    )


def _sanitize_instagram_username(value: str) -> str:
    cleaned = normalize_text(value).replace("@", "").strip()
    cleaned = re.sub(r"[^a-z0-9._]", "", cleaned)
    return cleaned


def _extract_after_keywords(text: str, keywords: tuple[str, ...]) -> str:
    normalized = normalize_text(text)
    for keyword in keywords:
        marker = f"{keyword} "
        if marker in normalized:
            return normalized.split(marker, 1)[1].strip()
    return ""


def _resolve_youtube(command_text: str) -> NavigationResolution:
    normalized = normalize_text(command_text)
    home = PLATFORM_HOME["youtube"]

    is_channel = "canal" in normalized
    is_search_like = any(token in normalized for token in ("procura", "pesquisa", "pesquisar"))
    is_play_like = any(token in normalized for token in ("tocar", "toca", "coloca", "musica", "video", "som"))

    if is_channel:
        raw_target = _extract_after_keywords(normalized, ("canal do", "canal da", "canal"))
        raw_target = re.sub(r"\s+no\s+youtube$", "", raw_target).strip()
        lookup_key = normalize_text(raw_target)
        handle = YOUTUBE_KNOWN_CHANNELS.get(lookup_key)
        if handle:
            return NavigationResolution(
                platform="youtube",
                target_type="channel",
                target_text=raw_target,
                normalized_target=lookup_key,
                resolved_url=f"{YOUTUBE_BASE_URL}{handle}",
                fallback_url=f"{YOUTUBE_BASE_URL}results?search_query={urllib.parse.quote_plus(raw_target)}",
                confidence=0.96,
                fallback_used=False,
            )
        return NavigationResolution(
            platform="youtube",
            target_type="channel_search",
            target_text=raw_target,
            normalized_target=lookup_key,
            resolved_url=f"{YOUTUBE_BASE_URL}results?search_query={urllib.parse.quote_plus(raw_target)}",
            fallback_url=home,
            confidence=0.78,
            fallback_used=True,
            note="unknown_channel_alias",
        )

    if is_search_like or is_play_like:
        target = normalized
        target = re.sub(r"\b(no|na)\s+youtube\b", "", target).strip()
        target = re.sub(r"^(procura|pesquisa|pesquisar|coloca|toca|tocar|abre)\s+", "", target).strip()
        target = re.sub(r"\b(pra tocar|para tocar)\b", "", target).strip()
        return NavigationResolution(
            platform="youtube",
            target_type="search_play" if is_play_like else "search",
            target_text=target,
            normalized_target=target,
            resolved_url=f"{YOUTUBE_BASE_URL}results?search_query={urllib.parse.quote_plus(target)}",
            fallback_url=home,
            confidence=0.88,
            fallback_used=True,
        )

    return NavigationResolution(
        platform="youtube",
        target_type="home",
        target_text="",
        normalized_target="",
        resolved_url=home,
        fallback_url=home,
        confidence=0.99,
        fallback_used=False,
    )


def _resolve_instagram(command_text: str, instagram_username: str | None) -> NavigationResolution:
    normalized = normalize_text(command_text)
    home = PLATFORM_HOME["instagram"]

    for alias, route in INSTAGRAM_ROUTE_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", normalized):
            return NavigationResolution(
                platform="instagram",
                target_type="route",
                target_text=alias,
                normalized_target=alias,
                resolved_url=f"{INSTAGRAM_BASE_URL.rstrip('/')}{route}",
                fallback_url=home,
                confidence=0.95,
                fallback_used=False,
            )

    is_profile_request = "perfil" in normalized
    is_my_profile = bool(re.search(r"\b(meu|minha)\s+perfil\b", normalized))
    if is_profile_request and is_my_profile:
        username = _sanitize_instagram_username(instagram_username or "")
        if username:
            return NavigationResolution(
                platform="instagram",
                target_type="profile_self",
                target_text=username,
                normalized_target=username,
                resolved_url=f"{INSTAGRAM_BASE_URL}{username}/",
                fallback_url=home,
                confidence=0.92,
                fallback_used=False,
            )
        return NavigationResolution(
            platform="instagram",
            target_type="profile_self_fallback",
            target_text="",
            normalized_target="",
            resolved_url=home,
            fallback_url=home,
            confidence=0.65,
            fallback_used=True,
            note="instagram_username_not_configured",
        )

    profile_match = re.search(r"\bperfil\s+(?:do|da|de)\s+([@a-z0-9._]+)", normalized)
    if profile_match:
        username = _sanitize_instagram_username(profile_match.group(1))
        if username:
            return NavigationResolution(
                platform="instagram",
                target_type="profile",
                target_text=username,
                normalized_target=username,
                resolved_url=f"{INSTAGRAM_BASE_URL}{username}/",
                fallback_url=home,
                confidence=0.9,
                fallback_used=False,
            )

    return NavigationResolution(
        platform="instagram",
        target_type="home",
        target_text="",
        normalized_target="",
        resolved_url=home,
        fallback_url=home,
        confidence=0.98,
        fallback_used=False,
    )


def _resolve_generic_platform(platform: str, command_text: str) -> NavigationResolution:
    normalized = normalize_text(command_text)
    home = PLATFORM_HOME[platform]

    if platform == "google" and any(token in normalized for token in ("procura", "pesquisa", "pesquisar")):
        query = re.sub(r"^(procura|pesquisa|pesquisar)\s+", "", normalized).strip()
        query = query.replace(" no google", "").strip()
        return NavigationResolution(
            platform="google",
            target_type="search",
            target_text=query,
            normalized_target=query,
            resolved_url=f"https://www.google.com/search?q={urllib.parse.quote_plus(query)}",
            fallback_url=home,
            confidence=0.93,
            fallback_used=False,
        )

    if platform == "spotify" and any(token in normalized for token in ("procura", "pesquisa", "pesquisar")):
        query = re.sub(r"^(procura|pesquisa|pesquisar)\s+", "", normalized).strip()
        query = query.replace(" no spotify", "").strip()
        return NavigationResolution(
            platform="spotify",
            target_type="search",
            target_text=query,
            normalized_target=query,
            resolved_url=f"https://open.spotify.com/search/{urllib.parse.quote(query)}",
            fallback_url=home,
            confidence=0.9,
            fallback_used=False,
        )

    return NavigationResolution(
        platform=platform,
        target_type="home",
        target_text="",
        normalized_target="",
        resolved_url=home,
        fallback_url=home,
        confidence=0.97,
        fallback_used=False,
    )


def resolve_navigation_intent(
    command_text: str,
    *,
    instagram_username: str | None = None,
) -> NavigationResolution:
    original = (command_text or "").strip()
    normalized = normalize_text(original)
    cleaned = _strip_command_prefix(original)

    if _is_url_like(original):
        url = original if re.match(r"^https?://", original, flags=re.I) else f"https://{original}"
        return NavigationResolution(
            platform="generic",
            target_type="direct_url",
            target_text=original,
            normalized_target=normalized,
            resolved_url=url,
            fallback_url=url,
            confidence=1.0,
            fallback_used=False,
        )

    platform = _detect_platform(normalized) or _detect_platform(cleaned)
    if not platform:
        is_media_intent = any(token in normalized for token in ("musica", "video", "tocar", "toca", "coloca"))
        if is_media_intent:
            platform = "youtube"

    if platform == "youtube":
        return _resolve_youtube(original)
    if platform == "instagram":
        return _resolve_instagram(original, instagram_username=instagram_username)
    if platform in {"google", "gmail", "whatsapp_web", "spotify"}:
        return _resolve_generic_platform(platform, original)

    if _is_url_like(cleaned):
        url = cleaned if re.match(r"^https?://", cleaned, flags=re.I) else f"https://{cleaned}"
        return NavigationResolution(
            platform="generic",
            target_type="direct_url",
            target_text=cleaned,
            normalized_target=normalize_text(cleaned),
            resolved_url=url,
            fallback_url=url,
            confidence=0.9,
            fallback_used=False,
        )

    guessed = normalize_text(cleaned or original)
    guessed = re.sub(r"^site d[ao]\s+", "", guessed).strip()
    if "." in guessed:
        guessed_url = f"https://{guessed}"
    else:
        guessed_url = f"https://www.{guessed}.com"
    return NavigationResolution(
        platform="generic",
        target_type="generic_site",
        target_text=cleaned or original,
        normalized_target=guessed,
        resolved_url=guessed_url,
        fallback_url=guessed_url,
        confidence=0.55,
        fallback_used=True,
    )


def resolve_browser_target(
    command_text: str,
    intent: dict | None = None,
    entities: dict | None = None,
) -> dict[str, str]:
    normalized = normalize_text(command_text)
    # P1 — local (external) SO quando o usuario pede explicitamente o navegador dele.
    for phrase in EXTERNAL_BROWSER_EXPLICIT_PHRASES:
        if phrase in normalized:
            return {"target": "external", "matched_keyword": phrase}
    # Frases que pedem explicitamente o navegador da Kira (compat; ja e o default).
    for phrase in INTERNAL_BROWSER_EXPLICIT_PHRASES:
        if phrase in normalized:
            return {"target": "internal", "matched_keyword": phrase}
    # DEFAULT — navegacao/pesquisa/login usam o Browser Agent dedicado (internal),
    # NUNCA o navegador local do usuario.
    return {"target": "internal", "matched_keyword": ""}
