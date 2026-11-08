"""Vídeos só por pedido: painel com 3 ou 4 opções; o usuário escolhe e só então o player é carregado."""

from __future__ import annotations

from typing import Any

from services.controllers.research import ResearchController, SectionPlan
from services.providers.video import VideoProvider, VideoUnavailable, as_dicts

PRESENTATION_ID = "video-kira"


class VideoController:
    def __init__(self, research: ResearchController, provider: VideoProvider | None = None) -> None:
        self._research = research
        self._provider = provider or VideoProvider()

    async def ask(self, *, query: str, turn_id: str | None = None) -> dict[str, Any]:
        holder: dict[str, list[dict[str, Any]]] = {}

        async def load() -> dict[str, Any]:
            try:
                videos = as_dicts(await self._provider.search(query))
            except VideoUnavailable as exc:
                raise ValueError(str(exc)) from exc
            if not videos:
                raise ValueError("nenhum vídeo encontrado")
            holder["videos"] = videos
            return {"data": {"videos": videos}}

        await self._research.run(presentation_id=PRESENTATION_ID, title=f"Vídeos: {query}"[:120], turn_id=turn_id, sections=[SectionPlan("videos", "video", "Escolha um vídeo", load, media=True)])
        videos = holder.get("videos")
        if not videos:
            return {"ok": False, "message": "Vídeos indisponíveis agora."}
        return {"ok": True, "opcoes": [{"titulo": v["title"], "canal": v["channel"], "duracao_s": v["durationSeconds"]} for v in videos]}


__all__ = ["VideoController"]
