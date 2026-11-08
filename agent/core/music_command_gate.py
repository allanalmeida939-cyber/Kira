from __future__ import annotations

from dataclasses import dataclass

from agent.core.command_router import normalize_router_text


@dataclass(frozen=True)
class MusicIntent:
    matched: bool = False
    action: str = ""
    query: str = ""
    value: int | None = None


WEB_EXPLICIT = ("entra no youtube", "abre o youtube", "pesquisa no youtube", "procura no youtube", "google")
MUSIC_WORDS = ("musica", "música", "som", "playlist")


def parse_music_intent(text: str | None) -> MusicIntent:
    normalized = normalize_router_text(text)
    if not normalized:
        return MusicIntent()
    if any(trigger in normalized for trigger in WEB_EXPLICIT):
        return MusicIntent()
    if "volume da musica" not in normalized and "volume do player" not in normalized:
        if normalized in {"aumenta o volume", "diminui o volume", "abaixa o volume", "sobe o volume"}:
            return MusicIntent()
    has_music = any(word in normalized for word in MUSIC_WORDS)
    if not has_music:
        return MusicIntent()
    if any(token in normalized for token in ("pausa", "pause")):
        return MusicIntent(True, "pause")
    if any(token in normalized for token in ("continua", "resume", "despausa")):
        return MusicIntent(True, "resume")
    if any(token in normalized for token in ("para a musica", "parar a musica", "stop")):
        return MusicIntent(True, "stop")
    if any(token in normalized for token in ("proxima", "próxima")):
        return MusicIntent(True, "next")
    if any(token in normalized for token in ("volta musica", "musica anterior", "anterior")):
        return MusicIntent(True, "previous")
    if "aumenta" in normalized and "volume" in normalized:
        return MusicIntent(True, "volume_up", value=85)
    if ("diminui" in normalized or "abaixa" in normalized) and "volume" in normalized:
        return MusicIntent(True, "volume_down", value=45)
    if any(token in normalized for token in ("toca", "toque", "play", "coloca")):
        query = normalized
        for prefix in ("kira", "toca", "toque", "uma", "um", "musica", "música", "de"):
            query = query.replace(f" {prefix} ", " ")
        query = query.replace("toca ", "").replace("toque ", "").strip()
        return MusicIntent(True, "play", query=query)
    return MusicIntent()


__all__ = ["MusicIntent", "parse_music_intent"]
