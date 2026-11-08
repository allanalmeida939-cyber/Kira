"""Verificacao de que uma requisicao HTTP vem do proprio computador (sem Origin ou com Origin local)."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_local_request(request: Request) -> bool:
    origin = str(request.headers.get("origin") or "").strip()
    if not origin:
        client_host = str(request.client.host if request.client else "").lower()
        request_host = str(request.url.hostname or "").lower()
        return client_host in LOCAL_HOSTS and request_host in LOCAL_HOSTS
    try:
        parsed = urlsplit(origin)
        host = str(parsed.hostname or "").lower()
        request_port = request.url.port or (443 if request.url.scheme == "https" else 80)
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.scheme in {"http", "https"} and host in LOCAL_HOSTS and origin_port == request_port
    except ValueError:
        return False


__all__ = ["is_local_request"]
