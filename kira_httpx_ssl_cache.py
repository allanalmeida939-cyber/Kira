"""Cada httpx.AsyncClient() novo recarrega os certificados (certifi) e trava o loop ~400 ms neste PC.

Com 41 lugares criando cliente a cada chamada, isso virava EVENT_LOOP_LAG de 500-1000 ms e a fila de voz
atrasava. Aqui o contexto SSL padrao e criado uma vez e reaproveitado (SSLContext pode ser compartilhado).
"""

from __future__ import annotations

import threading

import httpx._transports.default as _default

_original = _default.create_ssl_context
_cache: dict = {}
_lock = threading.Lock()


def _cached_create_ssl_context(verify=True, cert=None, trust_env=True):
    if cert is not None or not isinstance(verify, bool):
        return _original(verify=verify, cert=cert, trust_env=trust_env)
    key = (verify, trust_env)
    ctx = _cache.get(key)
    if ctx is None:
        with _lock:
            ctx = _cache.get(key)
            if ctx is None:
                ctx = _cache[key] = _original(verify=verify, cert=cert, trust_env=trust_env)
    return ctx


def install() -> None:
    _default.create_ssl_context = _cached_create_ssl_context


install()
