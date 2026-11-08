import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


class MusicServiceError(Exception):
    pass


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_iso8601_duration(raw: str) -> tuple[int, str]:
    value = str(raw or "").upper()
    match = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", value)
    if not match:
        return 0, "00:00"
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    total = (hours * 3600) + (minutes * 60) + seconds
    if total < 3600:
        return total, f"{minutes:02d}:{seconds:02d}"
    hh = total // 3600
    mm = (total % 3600) // 60
    ss = total % 60
    return total, f"{hh:02d}:{mm:02d}:{ss:02d}"


class YouTubeMusicService:
    API_BASE = "https://www.googleapis.com/youtube/v3"

    def __init__(self, api_key_env: str = "YOUTUBE_API_KEY") -> None:
        self._api_key_env = api_key_env
        self._embed_cache: dict[str, tuple[float, bool]] = {}
        self._embed_cache_lock = threading.RLock()

    def get_api_key(self) -> str:
        key = str(os.getenv(self._api_key_env, "") or "").strip()
        if not key:
            print("[KIRA MUSIC] youtube_api_key_missing", flush=True)
            raise MusicServiceError(
                "YOUTUBE_API_KEY não configurada. Defina no arquivo .env para usar a aba Música."
            )
        print("[KIRA MUSIC] youtube_api_key_loaded", flush=True)
        return key

    def build_watch_url(self, video_id: str) -> str:
        return f"https://www.youtube.com/watch?v={video_id}"

    def _http_get_json(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        query = urllib.parse.urlencode(params)
        url = f"{self.API_BASE}/{endpoint}?{query}"
        try:
            req = urllib.request.Request(
                url=url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "KiraMusicModule/1.0",
                },
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=12) as response:
                raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw or "{}")
        except Exception as exc:
            raise MusicServiceError(f"Falha ao consultar YouTube API: {exc}") from exc

    def _is_embed_oembed_allowed(self, video_id: str) -> bool:
        vid = str(video_id or "").strip()
        if not vid:
            return False
        now = time.time()
        with self._embed_cache_lock:
            cached = self._embed_cache.get(vid)
            if cached and (now - cached[0]) < 1800:
                return bool(cached[1])

        url = (
            "https://www.youtube.com/oembed?"
            + urllib.parse.urlencode(
                {
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "format": "json",
                }
            )
        )
        allowed = True
        try:
            req = urllib.request.Request(
                url=url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "KiraMusicModule/1.0",
                },
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=6) as response:
                _ = response.read()
            allowed = True
        except urllib.error.HTTPError as exc:
            # 401/403/404 usually indicate the video is not embeddable/available by oEmbed.
            if int(getattr(exc, "code", 0) or 0) in {401, 403, 404}:
                allowed = False
            else:
                # Unknown HTTP issue: keep result to avoid over-filtering on transient failures.
                allowed = True
        except Exception:
            # Network/transient issue. Do not hide playable videos because of a probe failure.
            allowed = True

        with self._embed_cache_lock:
            self._embed_cache[vid] = (now, allowed)
        return allowed

    def get_video_details(self, video_ids: list[str]) -> dict[str, dict[str, Any]]:
        ids = [str(v).strip() for v in video_ids if str(v).strip()]
        if not ids:
            return {}
        data = self._http_get_json(
            "videos",
            {
                "part": "contentDetails,snippet,status",
                "id": ",".join(ids),
                "maxResults": min(50, len(ids)),
                "key": self.get_api_key(),
            },
        )
        out: dict[str, dict[str, Any]] = {}
        for item in data.get("items", []) or []:
            vid = str(item.get("id") or "").strip()
            if not vid:
                continue
            snippet = item.get("snippet", {}) or {}
            content_details = item.get("contentDetails", {}) or {}
            status = item.get("status", {}) or {}
            duration_raw = str(content_details.get("duration") or "")
            duration_seconds, duration_label = _parse_iso8601_duration(duration_raw)
            thumbnails = snippet.get("thumbnails", {}) or {}
            thumb = (
                (thumbnails.get("medium") or {}).get("url")
                or (thumbnails.get("high") or {}).get("url")
                or (thumbnails.get("default") or {}).get("url")
                or ""
            )
            region_restriction = content_details.get("regionRestriction", {}) or {}
            blocked_regions = region_restriction.get("blocked", []) if isinstance(region_restriction.get("blocked", []), list) else []
            allowed_regions = region_restriction.get("allowed", []) if isinstance(region_restriction.get("allowed", []), list) else []
            embeddable = bool(status.get("embeddable", True))
            privacy_status = str(status.get("privacyStatus") or "").strip().lower()
            upload_status = str(status.get("uploadStatus") or "").strip().lower()
            is_public_like = privacy_status in {"public", "unlisted", ""}
            is_processed = upload_status in {"processed", ""}
            is_embedded_reproducible = embeddable and is_public_like and is_processed

            out[vid] = {
                "videoId": vid,
                "title": html.unescape(str(snippet.get("title") or "")).strip(),
                "channel": html.unescape(str(snippet.get("channelTitle") or "")).strip(),
                "thumbnail": thumb,
                "duration": duration_label,
                "durationSeconds": duration_seconds,
                "publishedAt": str(snippet.get("publishedAt") or "").strip(),
                "watchUrl": self.build_watch_url(vid),
                "embeddable": embeddable,
                "privacyStatus": privacy_status or "unknown",
                "uploadStatus": upload_status or "unknown",
                "blockedRegionsCount": len(blocked_regions),
                "allowedRegionsCount": len(allowed_regions),
                "isReproducible": bool(is_embedded_reproducible),
            }
        return out

    def normalize_music_results(self, raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ids: list[str] = []
        base: list[dict[str, Any]] = []
        for item in raw_items:
            item_id = item.get("id", {}) or {}
            snippet = item.get("snippet", {}) or {}
            video_id = str(item_id.get("videoId") or "").strip()
            if not video_id:
                continue
            ids.append(video_id)
            thumbnails = snippet.get("thumbnails", {}) or {}
            thumb = (
                (thumbnails.get("medium") or {}).get("url")
                or (thumbnails.get("high") or {}).get("url")
                or (thumbnails.get("default") or {}).get("url")
                or ""
            )
            # A busca do YouTube devolve o título com entidades HTML ("Racionais MC&#39;s"): vira texto de verdade aqui.
            base.append(
                {
                    "videoId": video_id,
                    "title": html.unescape(str(snippet.get("title") or "")).strip(),
                    "channel": html.unescape(str(snippet.get("channelTitle") or "")).strip(),
                    "thumbnail": thumb,
                    "publishedAt": str(snippet.get("publishedAt") or "").strip(),
                    "watchUrl": self.build_watch_url(video_id),
                }
            )
        details = self.get_video_details(ids)
        # uma consulta oEmbed por vídeo: em sequência eram ~5 s por busca, em paralelo menos de 1 s
        with ThreadPoolExecutor(max_workers=8) as pool:
            embeddable = dict(zip(ids, pool.map(self._is_embed_oembed_allowed, ids)))
        normalized: list[dict[str, Any]] = []
        rejected = 0
        for item in base:
            video_id = item["videoId"]
            detail = details.get(video_id, {})
            if not detail:
                rejected += 1
                continue
            if not bool(detail.get("isReproducible", False)):
                rejected += 1
                continue
            if not embeddable.get(video_id, True):
                rejected += 1
                print(
                    f"[KIRA MUSIC] youtube_search_filtered_embed_blocked video_id={video_id}",
                    flush=True,
                )
                continue
            payload = dict(item)
            payload["duration"] = detail.get("duration", "00:00")
            payload["durationSeconds"] = int(detail.get("durationSeconds", 0) or 0)
            payload["embeddable"] = bool(detail.get("embeddable", True))
            payload["isReproducible"] = bool(detail.get("isReproducible", True))
            payload["oembedEmbeddable"] = True
            normalized.append(payload)
        if rejected:
            print(f"[KIRA MUSIC] youtube_search_filtered_unplayable rejected={rejected}", flush=True)
        return normalized

    def search_music(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        sanitized = " ".join(str(query or "").strip().split())
        if not sanitized:
            raise MusicServiceError("Consulta de música vazia.")
        print(f"[KIRA MUSIC] youtube_search_started query={sanitized!r}", flush=True)
        data = self._http_get_json(
            "search",
            {
                "part": "snippet",
                "q": sanitized,
                "maxResults": max(10, min(int(max_results or 10) * 2, 50)),
                "type": "video",
                "safeSearch": "none",
                "key": self.get_api_key(),
            },
        )
        items = data.get("items", []) or []
        out = self.normalize_music_results(items)
        print(
            f"[KIRA MUSIC] youtube_search_success query={sanitized!r} results={len(out)}",
            flush=True,
        )
        return out

    def latest_from_channel(self, channel: str) -> list[dict[str, Any]]:
        """Vídeos mais recentes de um canal ('Coisa de Nerd'), do mais novo para o mais antigo; Shorts por último."""
        name = " ".join(str(channel or "").strip().split())
        if not name:
            raise MusicServiceError("Nome do canal vazio.")
        key = self.get_api_key()
        found = self._http_get_json("search", {"part": "snippet", "q": name, "type": "channel", "maxResults": 1, "key": key})
        channel_id = str((((found.get("items") or [{}])[0].get("id") or {}).get("channelId")) or "")
        if not channel_id:
            raise MusicServiceError(f"Canal não encontrado: {name}.")
        info = self._http_get_json("channels", {"part": "contentDetails", "id": channel_id, "key": key})
        uploads = str(((((info.get("items") or [{}])[0].get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")) or "")
        if not uploads:
            raise MusicServiceError(f"O canal {name} não tem vídeos públicos.")
        recent = self._http_get_json("playlistItems", {"part": "snippet", "playlistId": uploads, "maxResults": 8, "key": key})
        as_search = [
            {"id": {"videoId": str(((item.get("snippet") or {}).get("resourceId") or {}).get("videoId") or "")}, "snippet": item.get("snippet") or {}}
            for item in recent.get("items") or []
        ]
        videos = self.normalize_music_results(as_search)
        videos.sort(key=lambda video: int(video.get("durationSeconds") or 0) <= 70)  # estável: mantém a ordem por data
        return videos


class PlaylistStore:
    def __init__(self, file_path: Path) -> None:
        self._path = Path(file_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if not self._path.exists():
            self._write({"playlists": []})

    def _read(self) -> dict[str, Any]:
        with self._lock:
            try:
                return json.loads(self._path.read_text(encoding="utf-8") or "{}")
            except Exception:
                return {"playlists": []}

    def _write(self, payload: dict[str, Any]) -> None:
        with self._lock:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)

    def list_playlists(self) -> list[dict[str, Any]]:
        data = self._read()
        playlists = data.get("playlists", [])
        if not isinstance(playlists, list):
            return []
        return playlists

    def create_playlist(self, name: str) -> dict[str, Any]:
        title = str(name or "").strip()
        if not title:
            raise MusicServiceError("Nome da playlist é obrigatório.")
        data = self._read()
        playlists = data.get("playlists", [])
        now = _now_iso()
        item = {
            "id": str(uuid.uuid4()),
            "name": title,
            "created_at": now,
            "updated_at": now,
            "items": [],
        }
        playlists.append(item)
        data["playlists"] = playlists
        self._write(data)
        print(f"[KIRA MUSIC] music_playlist_created id={item['id']}", flush=True)
        return item

    def update_playlist(self, playlist_id: str, *, name: str | None = None, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        data = self._read()
        playlists = data.get("playlists", [])
        for playlist in playlists:
            if str(playlist.get("id")) != str(playlist_id):
                continue
            if name is not None:
                title = str(name).strip()
                if not title:
                    raise MusicServiceError("Nome da playlist não pode ser vazio.")
                playlist["name"] = title
            if items is not None:
                playlist["items"] = items
            playlist["updated_at"] = _now_iso()
            self._write(data)
            print(f"[KIRA MUSIC] music_playlist_updated id={playlist_id}", flush=True)
            return playlist
        raise MusicServiceError("Playlist não encontrada.")

    def delete_playlist(self, playlist_id: str) -> None:
        data = self._read()
        playlists = data.get("playlists", [])
        filtered = [p for p in playlists if str(p.get("id")) != str(playlist_id)]
        if len(filtered) == len(playlists):
            raise MusicServiceError("Playlist não encontrada.")
        data["playlists"] = filtered
        self._write(data)
        print(f"[KIRA MUSIC] music_playlist_deleted id={playlist_id}", flush=True)

    def get_playlist(self, playlist_id: str) -> dict[str, Any]:
        for playlist in self.list_playlists():
            if str(playlist.get("id")) == str(playlist_id):
                return playlist
        raise MusicServiceError("Playlist não encontrada.")

    def add_item(self, playlist_id: str, item: dict[str, Any]) -> dict[str, Any]:
        playlist = self.get_playlist(playlist_id)
        video_id = str(item.get("videoId") or "").strip()
        if not video_id:
            raise MusicServiceError("videoId é obrigatório.")
        now = _now_iso()
        payload = {
            "id": str(uuid.uuid4()),
            "videoId": video_id,
            "title": str(item.get("title") or "").strip(),
            "channel": str(item.get("channel") or "").strip(),
            "thumbnail": str(item.get("thumbnail") or "").strip(),
            "duration": str(item.get("duration") or "").strip(),
            "durationSeconds": int(item.get("durationSeconds") or 0),
            "watchUrl": str(item.get("watchUrl") or "").strip() or f"https://www.youtube.com/watch?v={video_id}",
            "added_at": now,
        }
        items = list(playlist.get("items") or [])
        items.append(payload)
        updated = self.update_playlist(playlist_id, items=items)
        print(f"[KIRA MUSIC] music_playlist_item_added playlist_id={playlist_id} item_id={payload['id']}", flush=True)
        return updated

    def remove_item(self, playlist_id: str, item_id: str) -> dict[str, Any]:
        playlist = self.get_playlist(playlist_id)
        items = list(playlist.get("items") or [])
        filtered = [i for i in items if str(i.get("id")) != str(item_id)]
        if len(filtered) == len(items):
            raise MusicServiceError("Item não encontrado na playlist.")
        updated = self.update_playlist(playlist_id, items=filtered)
        print(f"[KIRA MUSIC] music_playlist_item_removed playlist_id={playlist_id} item_id={item_id}", flush=True)
        return updated


class MusicStateStore:
    def __init__(self, file_path: Path) -> None:
        self._path = Path(file_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        if not self._path.exists():
            self.set_state({})

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            try:
                return json.loads(self._path.read_text(encoding="utf-8") or "{}")
            except Exception:
                return {}

    def set_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = _now_iso()
        current = self.get_state()
        merged = dict(current)
        merged.update(payload or {})
        merged["updated_at"] = now
        with self._lock:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        print("[KIRA MUSIC] music_state_updated", flush=True)
        return merged
