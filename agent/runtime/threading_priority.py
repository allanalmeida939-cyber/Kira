from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


logger = logging.getLogger(__name__)


class PriorityThreadManager:
    def __init__(self) -> None:
        self.voice_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kira-voice")
        self.mic_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kira-mic")
        self.gemini_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kira-gemini")
        self.background_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kira-bg")
        logger.info("[THREADS] pools_ready voice=2 mic=2 gemini=2 bg=2")

    async def run_voice(self, func: Callable[..., Any], *args, **kwargs) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.voice_executor, lambda: func(*args, **kwargs))

    async def run_mic(self, func: Callable[..., Any], *args, **kwargs) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.mic_executor, lambda: func(*args, **kwargs))

    async def run_gemini(self, func: Callable[..., Any], *args, **kwargs) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.gemini_executor, lambda: func(*args, **kwargs))

    def run_background(self, func: Callable[..., Any], *args, **kwargs):
        return self.background_executor.submit(func, *args, **kwargs)

    def shutdown(self) -> None:
        self.voice_executor.shutdown(wait=False)
        self.mic_executor.shutdown(wait=False)
        self.gemini_executor.shutdown(wait=False)
        self.background_executor.shutdown(wait=False)


_thread_manager: PriorityThreadManager | None = None


def get_thread_manager() -> PriorityThreadManager:
    global _thread_manager
    if _thread_manager is None:
        _thread_manager = PriorityThreadManager()
    return _thread_manager
