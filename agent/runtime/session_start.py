from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request

from resilience import log_exception_safely


SESSION_START_TIMEOUT_SECONDS = max(
    float(os.getenv("KIRA_SESSION_START_TIMEOUT_SECONDS", "25")),
    5.0,
)
SESSION_START_MAX_ATTEMPTS = max(
    int(os.getenv("KIRA_SESSION_START_MAX_ATTEMPTS", "2")),
    1,
)
SESSION_START_RETRY_DELAY_SECONDS = max(
    float(os.getenv("KIRA_SESSION_START_RETRY_DELAY_SECONDS", "2.5")),
    0.1,
)
SESSION_START_CANCEL_TIMEOUT_SECONDS = max(
    float(os.getenv("KIRA_SESSION_START_CANCEL_TIMEOUT_SECONDS", "5.0")),
    0.5,
)
GEMINI_READY_CALLBACK_TIMEOUT_SECONDS = max(
    float(os.getenv("KIRA_GEMINI_READY_CALLBACK_TIMEOUT_SECONDS", "25.0")),
    1.0,
)
GEMINI_TIMEOUT_PUBLISH_URL = (
    os.getenv("KIRA_UI_BASE_URL", "http://127.0.0.1:3000").rstrip("/")
    + "/api/agent/error"
)
GEMINI_TIMEOUT_PUBLISH_HTTP_SECONDS = max(
    float(os.getenv("KIRA_GEMINI_TIMEOUT_PUBLISH_HTTP_SECONDS", "3.0")),
    0.5,
)


def _publish_gemini_timeout_sync(
    *,
    logger,
    mode_suffix: str,
    timeout_seconds: float,
    phase: str = "gemini_connect_timeout",
) -> None:
    payload = {
        "phase": phase,
        "error": f"Gemini Live nao ficou pronto em {timeout_seconds:.1f}s.",
        "timeout_seconds": timeout_seconds,
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        GEMINI_TIMEOUT_PUBLISH_URL,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=GEMINI_TIMEOUT_PUBLISH_HTTP_SECONDS,
        ) as response:
            status = int(getattr(response, "status", 0) or 0)
        logger.error(
            "GEMINI_CONNECT_TIMEOUT_PUBLISHED%s status=%s",
            mode_suffix,
            status,
        )
    except urllib.error.HTTPError as exc:
        logger.error(
            "GEMINI_CONNECT_TIMEOUT_PUBLISH_FAILED%s status=%s",
            mode_suffix,
            getattr(exc, "code", "unknown"),
        )
    except Exception as exc:
        logger.error(
            "GEMINI_CONNECT_TIMEOUT_PUBLISH_FAILED%s reason=%s",
            mode_suffix,
            exc,
        )


async def _cancel_gemini_ready_task(
    *,
    ready_task: asyncio.Task,
    logger,
    mode_suffix: str,
    cancel_timeout_seconds: float,
) -> None:
    if ready_task.done():
        logger.error("GEMINI_CONNECT_TASK_ALREADY_DONE%s", mode_suffix)
        return
    logger.error("GEMINI_CONNECT_TASK_CANCEL_REQUESTED%s", mode_suffix)
    ready_task.cancel()
    cancelled, _ = await asyncio.wait(
        {ready_task},
        timeout=cancel_timeout_seconds,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if cancelled:
        logger.error("GEMINI_CONNECT_TASK_CANCELLED%s", mode_suffix)
    else:
        logger.error(
            "GEMINI_CONNECT_TASK_CANCEL_TIMEOUT%s timeout=%.1fs",
            mode_suffix,
            cancel_timeout_seconds,
        )


async def start_agent_session(
    *,
    session,
    agent,
    room=None,
    room_options=None,
    logger,
    mode_label: str | None = None,
    ready_callback=None,
    timeout_seconds: float = SESSION_START_TIMEOUT_SECONDS,
    max_attempts: int = SESSION_START_MAX_ATTEMPTS,
    retry_delay_seconds: float = SESSION_START_RETRY_DELAY_SECONDS,
    cancel_timeout_seconds: float = SESSION_START_CANCEL_TIMEOUT_SECONDS,
) -> None:
    mode_suffix = f" modo={mode_label}" if mode_label else ""
    for attempt in range(1, max_attempts + 1):
        started_at = asyncio.get_running_loop().time()
        logger.info(
            "ANTES de session.start tentativa=%s timeout=%.1fs%s",
            attempt,
            timeout_seconds,
            mode_suffix,
        )
        start_kwargs = {
            "agent": agent,
        }
        if room is not None:
            start_kwargs["room"] = room
        if room_options is not None:
            start_kwargs["room_options"] = room_options
        start_task = asyncio.create_task(
            session.start(**start_kwargs),
            name=f"session.start[{mode_label or 'default'}]-{attempt}",
        )
        done, _ = await asyncio.wait(
            {start_task},
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            elapsed = asyncio.get_running_loop().time() - started_at
            logger.error(
                "TIMEOUT em session.start tentativa=%s elapsed=%.2fs timeout=%.1fs%s",
                attempt,
                elapsed,
                timeout_seconds,
                mode_suffix,
            )
            start_task.cancel()
            cancelled, _ = await asyncio.wait(
                {start_task},
                timeout=cancel_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not cancelled:
                logger.error(
                    "session.start nao respondeu ao cancelamento tentativa=%s cancel_timeout=%.1fs%s",
                    attempt,
                    cancel_timeout_seconds,
                    mode_suffix,
                )
                raise TimeoutError(
                    f"session.start excedeu {timeout_seconds:.1f}s e ignorou cancelamento"
                )

            if start_task.cancelled():
                logger.warning(
                    "session.start cancelado apos timeout tentativa=%s%s",
                    attempt,
                    mode_suffix,
                )
            else:
                try:
                    timeout_error = start_task.exception()
                except asyncio.CancelledError:
                    timeout_error = None
                if timeout_error is not None:
                    logger.error(
                        "session.start terminou com erro apos timeout tentativa=%s%s",
                        attempt,
                        mode_suffix,
                        exc_info=timeout_error,
                    )
            if attempt >= max_attempts:
                raise TimeoutError(
                    f"session.start excedeu {timeout_seconds:.1f}s na tentativa {attempt}"
                )
            logger.warning(
                "Nova tentativa de session.start em %.1fs%s",
                retry_delay_seconds,
                mode_suffix,
            )
            await asyncio.sleep(retry_delay_seconds)
            continue

        try:
            await start_task
        except asyncio.CancelledError:
            logger.error("session.start foi cancelado tentativa=%s%s", attempt, mode_suffix)
            raise
        except Exception as exc:
            log_exception_safely(
                logger,
                f"Falha em session.start tentativa={attempt}{mode_suffix}",
                exc,
                level=40,
            )
            raise

        elapsed = asyncio.get_running_loop().time() - started_at
        logger.info(
            "DEPOIS de session.start tentativa=%s elapsed=%.2fs%s",
            attempt,
            elapsed,
            mode_suffix,
        )
        logger.info(
            "SESSION_ROOM_CONNECTED attempt=%s elapsed=%.2fs%s",
            attempt,
            elapsed,
            mode_suffix,
        )
        if ready_callback is not None:
            logger.info("GEMINI_CONNECT_START%s timeout=%.1fs", mode_suffix, GEMINI_READY_CALLBACK_TIMEOUT_SECONDS)
            logger.info("Aguardando Gemini Live ficar pronto%s", mode_suffix)
            logger.info(
                "GEMINI_CONNECT_TIMEOUT_ARMED%s timeout_s=%.1f",
                mode_suffix,
                GEMINI_READY_CALLBACK_TIMEOUT_SECONDS,
            )
            ready_task = asyncio.create_task(
                ready_callback(),
                name=f"gemini.ready[{mode_label or 'default'}]-{attempt}",
            )
            logger.info("GEMINI_LIVE_CONNECT_TASK_CREATED%s", mode_suffix)
            try:
                logger.info("GEMINI_LIVE_CONNECT_AWAIT_START%s", mode_suffix)
                await asyncio.wait_for(
                    asyncio.shield(ready_task),
                    timeout=GEMINI_READY_CALLBACK_TIMEOUT_SECONDS,
                )
                logger.info("GEMINI_LIVE_CONNECT_AWAIT_DONE%s", mode_suffix)
            except TimeoutError as exc:
                logger.error(
                    "GEMINI_CONNECT_TIMEOUT%s timeout=%.1fs",
                    mode_suffix,
                    GEMINI_READY_CALLBACK_TIMEOUT_SECONDS,
                )
                logger.error(
                    "GEMINI_CONNECT_TIMEOUT_FIRED%s timeout_s=%.1f",
                    mode_suffix,
                    GEMINI_READY_CALLBACK_TIMEOUT_SECONDS,
                )
                logger.error("TIMEOUT aguardando Gemini Live pronto%s", mode_suffix)
                await _cancel_gemini_ready_task(
                    ready_task=ready_task,
                    logger=logger,
                    mode_suffix=mode_suffix,
                    cancel_timeout_seconds=cancel_timeout_seconds,
                )
                _publish_gemini_timeout_sync(
                    logger=logger,
                    mode_suffix=mode_suffix,
                    timeout_seconds=GEMINI_READY_CALLBACK_TIMEOUT_SECONDS,
                )
                raise TimeoutError(
                    f"Gemini Live nao ficou pronto em {GEMINI_READY_CALLBACK_TIMEOUT_SECONDS:.1f}s"
                ) from exc
            except asyncio.CancelledError:
                logger.error("GEMINI_LIVE_CONNECT_AWAIT_CANCELLED%s", mode_suffix)
                raise
            except Exception as exc:
                logger.exception(
                    "GEMINI_LIVE_CONNECT_AWAIT_EXCEPTION%s type=%s reason=%s",
                    mode_suffix,
                    exc.__class__.__name__,
                    exc,
                )
                logger.exception("GEMINI_BOOT_EXCEPTION%s reason=%s", mode_suffix, exc)
                logger.error("GEMINI_CONNECT_FAILED%s reason=%s", mode_suffix, exc)
                raise
            logger.info("GEMINI_CONNECT_OK%s", mode_suffix)
            logger.info("GEMINI_READY%s", mode_suffix)
            logger.info("Voice pipeline ready%s", mode_suffix)
        return
