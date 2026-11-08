from __future__ import annotations

import asyncio
import logging
import os

from google import genai
from google.genai import types as genai_types

if __package__ and __package__.startswith("agent"):
    from agent.core.realtime import resolve_current_gemini_api_key
else:  # pragma: no cover - suporte a execucao direta em agent/
    from core.realtime import resolve_current_gemini_api_key


logger = logging.getLogger(__name__)


def _extract_generation_text(response_obj: object) -> str:
    text = " ".join(str(getattr(response_obj, "text", "") or "").split()).strip()
    if text:
        return text

    for candidate in getattr(response_obj, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        if content is None:
            continue
        for part in getattr(content, "parts", None) or []:
            candidate_text = " ".join(str(getattr(part, "text", "") or "").split()).strip()
            if candidate_text:
                return candidate_text
    return ""


def _model_candidates() -> list[str]:
    configured = str(os.getenv("KIRA_WHATSAPP_CONTEXTUAL_MODEL", "gemini-2.5-flash")).strip()
    candidates = [
        configured,
        "gemini-2.5-flash",
        "gemini-2.0-flash",
    ]
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        deduped.append(candidate)
    return deduped


async def generate_contextual_response(
    prompt: str,
    *,
    max_tokens: int = 100,
    temperature: float = 0.9,
) -> str:
    api_key = resolve_current_gemini_api_key()
    if not api_key:
        raise RuntimeError("Nenhuma chave Gemini disponivel para resposta contextual.")

    client = genai.Client(api_key=api_key)
    last_error: Exception | None = None

    for model_name in _model_candidates():
        try:
            response_obj = await asyncio.to_thread(
                client.models.generate_content,
                model=model_name,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=temperature,
                    top_p=0.95,
                    max_output_tokens=max(int(max_tokens), 32),
                    response_mime_type="text/plain",
                ),
            )
            text = _extract_generation_text(response_obj).strip().strip("\"' ")
            if not text:
                raise RuntimeError("Resposta contextual vazia.")
            logger.info(
                "[WHATSAPP_CONTEXTUAL_OK] model=%s chars=%d",
                model_name,
                len(text),
            )
            return text
        except Exception as exc:
            last_error = exc
            logger.warning(
                "[WHATSAPP_CONTEXTUAL_FAILED] model=%s error=%s",
                model_name,
                exc,
            )

    raise RuntimeError(f"Falha ao gerar resposta contextual: {last_error}")
