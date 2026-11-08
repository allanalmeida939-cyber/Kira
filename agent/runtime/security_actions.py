"""Avisos do guarda de voz para o backend: rótulo da voz na transcrição, câmera de segurança e bloqueio do Windows.

Sempre numa thread própria: o guarda chama de dentro do reconhecimento ou do event loop do Gemini, e nada ali pode
esperar a rede.
"""

from __future__ import annotations

import logging
import os
import threading

import httpx

logger = logging.getLogger(__name__)
BACKEND = os.getenv("KIRA_BACKEND_URL", "http://127.0.0.1:3000").rstrip("/")
LOCK_DELAY_SECONDS = 6.0  # dá tempo de a Kira avisar antes de a tela travar


def post_backend(path: str, payload: dict) -> None:
    def send() -> None:
        try:
            headers = {"X-Kira-Internal": str(os.getenv("KIRA_INTERNAL_TOKEN") or "")}
            httpx.post(BACKEND + path, json=payload, headers=headers, timeout=3.0)
        except httpx.HTTPError as exc:
            logger.warning("SECURITY_ACTION_POST_FAILED path=%s error=%s", path, type(exc).__name__)

    threading.Thread(target=send, name="kira-security-post", daemon=True).start()


def show_voice_label(label: str) -> None:
    post_backend("/api/ui/voice-label", {"label": label})


def show_voice_enrollment(state: str, name: str, progress: float = 0.0, message: str = "") -> None:
    """Barra do cadastro de voz: 'gravando' (enchendo), 'salvando', 'salva', 'falhou' ou 'cancelado'."""
    post_backend("/api/ui/voice-enroll", {"state": state, "name": name, "progress": round(progress, 3), "message": message})


def escalate(level: int) -> None:
    """Nível 3: liga a webcam e grava; nível 4: bloqueia o Windows (Win+L)."""
    if level == 3:
        post_backend("/api/security/camera", {"action": "start"})
    elif level >= 4:
        post_backend("/api/security/lock", {"delay": LOCK_DELAY_SECONDS})


__all__ = ["escalate", "post_backend", "show_voice_enrollment", "show_voice_label"]
