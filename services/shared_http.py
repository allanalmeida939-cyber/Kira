"""Contexto TLS compartilhado: criar um httpx.AsyncClient novo custa ~300 ms bloqueando o event loop."""

from __future__ import annotations

import asyncio
import ssl

import httpx

_CONTEXT: ssl.SSLContext | None = None


def tls_context() -> ssl.SSLContext:
    global _CONTEXT
    if _CONTEXT is None:
        _CONTEXT = httpx.create_ssl_context()
    return _CONTEXT


async def ensure_tls() -> ssl.SSLContext:
    """Garante o contexto criado fora do event loop (so a primeira chamada paga o custo)."""
    if _CONTEXT is None:
        await asyncio.to_thread(tls_context)
    return tls_context()


__all__ = ["ensure_tls", "tls_context"]
