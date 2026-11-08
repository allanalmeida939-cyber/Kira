"""Vídeos sob demanda pelo YouTube Data API (só com YOUTUBE_API_KEY). A chave nunca sai do backend nem vai a log."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from services.image_proxy import proxied_url
from services.providers.news import plain_text

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
DURATION_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")
TIMEOUT = httpx.Timeout(6.0, connect=3.0)


class VideoUnavailable(RuntimeError):
    pass


@dataclass
class Video:
    videoId: str
    title: str
    channel: str
    durationSeconds: int | None
    publishedAt: str | None
    url: str
    thumbnailUrl: str
    description: str
    embeddable: bool


def duration_seconds(value: object) -> int | None:
    match = DURATION_RE.match(str(value or ""))
    if not match or not any(match.groups()):
        return None
    hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def api_key() -> str:
    return os.getenv("YOUTUBE_API_KEY", "").strip()


class VideoProvider:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None, key_provider=api_key) -> None:
        self._transport = transport
        self._key = key_provider

    async def search(self, query: str, *, limit: int = 4) -> list[Video]:
        key = str(self._key() or "").strip()
        if not key:
            raise VideoUnavailable("vídeos indisponíveis: falta a chave do YouTube")
        text = " ".join(str(query or "").split())[:100]
        if len(text) < 2:
            raise VideoUnavailable("assunto vazio")
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport) as client:
                found = await client.get(SEARCH_URL, params={
                    "part": "snippet", "q": text, "type": "video", "maxResults": str(limit), "safeSearch": "strict",
                    "relevanceLanguage": "pt", "regionCode": "BR", "key": key,
                })
                if found.status_code != 200:
                    raise VideoUnavailable("o YouTube recusou a busca")
                ids = [
                    str((item.get("id") or {}).get("videoId") or "")
                    for item in found.json().get("items") or [] if isinstance(item, dict)
                ]
                ids = [i for i in ids if VIDEO_ID_RE.fullmatch(i)][:limit]
                if not ids:
                    return []
                details = await client.get(VIDEOS_URL, params={"part": "snippet,contentDetails,status", "id": ",".join(ids), "key": key})
        except httpx.HTTPError as exc:
            raise VideoUnavailable("o YouTube não respondeu") from exc
        if details.status_code != 200:
            raise VideoUnavailable("o YouTube recusou a consulta")
        by_id = {str(item.get("id")): item for item in details.json().get("items") or [] if isinstance(item, dict)}
        videos = []
        for video_id in ids:
            item = by_id.get(video_id)
            if not item:
                continue
            snippet = item.get("snippet") or {}
            thumbs = snippet.get("thumbnails") or {}
            thumb = str(((thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url")) or "")
            videos.append(Video(
                videoId=video_id, title=plain_text(snippet.get("title"), 140), channel=plain_text(snippet.get("channelTitle"), 80),
                durationSeconds=duration_seconds((item.get("contentDetails") or {}).get("duration")),
                publishedAt=str(snippet.get("publishedAt") or "") or None, url=f"https://www.youtube.com/watch?v={video_id}",
                thumbnailUrl=proxied_url(thumb) if thumb.startswith("https://i.ytimg.com/") else "",
                description=plain_text(snippet.get("description"), 200), embeddable=bool((item.get("status") or {}).get("embeddable")),
            ))
        return videos


def as_dicts(videos: list[Video]) -> list[dict[str, Any]]:
    return [asdict(v) for v in videos]


__all__ = ["Video", "VideoProvider", "VideoUnavailable", "api_key", "as_dicts", "duration_seconds"]
