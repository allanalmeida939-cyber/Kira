from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from resilience import log_exception_safely

logger = logging.getLogger(__name__)

Notifier = Callable[[str], Awaitable[None]]
CheckFn = Callable[[], bool]


class WatcherError(Exception):
    pass


@dataclass(slots=True)
class WatcherInfo:
    watcher_id: str
    mode: str
    description: str
    interval_seconds: float
    timeout_seconds: float | None
    created_at: float = field(default_factory=time.monotonic)
    status: str = "active"
    last_error: str = ""

    @property
    def age_seconds(self) -> int:
        return max(int(time.monotonic() - self.created_at), 0)


def _screen_signature(*, width: int = 96, height: int = 54):
    try:
        import pyautogui
    except ImportError as exc:
        raise WatcherError(
            "PyAutoGUI nao esta instalado. Rode 'python -m pip install -r requirements.txt'."
        ) from exc

    image = pyautogui.screenshot().convert("L")
    return image.resize((width, height))


def _screen_diff_score(current, previous) -> float:
    from PIL import ImageChops, ImageStat

    diff = ImageChops.difference(current, previous)
    return float(ImageStat.Stat(diff).mean[0])


def make_screen_change_checker(*, threshold: float = 10.0) -> CheckFn:
    state: dict[str, Any] = {"previous": None}

    def check() -> bool:
        current = _screen_signature()
        previous = state["previous"]
        state["previous"] = current
        if previous is None:
            return False
        return _screen_diff_score(current, previous) >= max(float(threshold), 1.0)

    return check


def make_screen_stable_checker(
    *,
    threshold: float = 6.0,
    stable_seconds: float = 3.0,
) -> CheckFn:
    state: dict[str, Any] = {
        "previous": None,
        "stable_since": None,
    }

    def check() -> bool:
        now = time.monotonic()
        current = _screen_signature()
        previous = state["previous"]
        state["previous"] = current

        if previous is None:
            state["stable_since"] = now
            return False

        score = _screen_diff_score(current, previous)
        if score >= max(float(threshold), 1.0):
            state["stable_since"] = None
            return False

        if state["stable_since"] is None:
            state["stable_since"] = now
            return False

        return (now - float(state["stable_since"])) >= max(float(stable_seconds), 1.0)

    return check


def _active_window_title() -> str:
    try:
        import pygetwindow as gw
    except ImportError as exc:
        raise WatcherError(
            "PyGetWindow nao esta instalado. Rode 'python -m pip install -r requirements.txt'."
        ) from exc

    title = gw.getActiveWindowTitle()
    return str(title or "").strip()


def make_window_title_contains_checker(text: str) -> CheckFn:
    needle = (text or "").strip().lower()
    if not needle:
        raise WatcherError("Informe o texto que devo procurar no titulo da janela.")

    def check() -> bool:
        return needle in _active_window_title().lower()

    return check


def make_window_title_not_contains_checker(text: str) -> CheckFn:
    needle = (text or "").strip().lower()
    if not needle:
        raise WatcherError("Informe o texto que deve sumir do titulo da janela.")

    def check() -> bool:
        title = _active_window_title().lower()
        return bool(title) and needle not in title

    return check


def make_playwright_text_checker(page: Any, text: str) -> CheckFn:
    target = (text or "").strip()
    if not target:
        raise WatcherError("Informe o texto que devo monitorar.")

    def check() -> bool:
        try:
            return page.locator(f"text={target}").count() > 0
        except Exception:
            return False

    return check


def make_playwright_selector_checker(page: Any, selector: str) -> CheckFn:
    target = (selector or "").strip()
    if not target:
        raise WatcherError("Informe o seletor que devo monitorar.")

    def check() -> bool:
        try:
            return page.locator(target).count() > 0
        except Exception:
            return False

    return check


class WatcherManager:
    def __init__(self) -> None:
        self._watchers: dict[str, WatcherInfo] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._notifier: Notifier | None = None
        self._counter = 0

    def set_notifier(self, notifier: Notifier | None) -> None:
        self._notifier = notifier

    def list_watchers(self) -> list[WatcherInfo]:
        return sorted(
            self._watchers.values(),
            key=lambda watcher: watcher.created_at,
        )

    def cancel_watcher(self, watcher_id: str) -> bool:
        target = (watcher_id or "").strip()
        if not target:
            return False

        task = self._tasks.get(target)
        watcher = self._watchers.get(target)
        if watcher:
            watcher.status = "cancelled"
        if task:
            task.cancel()
        self._watchers.pop(target, None)
        self._tasks.pop(target, None)
        return watcher is not None or task is not None

    def cancel_all(self) -> int:
        active_ids = list(self._watchers.keys())
        for watcher_id in active_ids:
            self.cancel_watcher(watcher_id)
        return len(active_ids)

    async def shutdown(self) -> None:
        active_tasks = list(self._tasks.values())
        for watcher in self._watchers.values():
            watcher.status = "cancelled"
        for task in active_tasks:
            task.cancel()

        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)

        self._tasks.clear()
        self._watchers.clear()

    def start_screen_change_watcher(
        self,
        *,
        interval_seconds: float = 1.0,
        timeout_seconds: float | None = 120.0,
        threshold: float = 10.0,
        success_message: str,
        timeout_message: str | None = None,
    ) -> WatcherInfo:
        _screen_signature()
        checker = make_screen_change_checker(threshold=threshold)
        return self.start_predicate_watcher(
            mode="mudanca_tela",
            description="Avisar quando a tela mudar",
            check_fn=checker,
            interval_seconds=interval_seconds,
            timeout_seconds=timeout_seconds,
            success_message=success_message,
            timeout_message=timeout_message,
            run_in_thread=True,
        )

    def start_screen_stable_watcher(
        self,
        *,
        interval_seconds: float = 1.0,
        timeout_seconds: float | None = 120.0,
        threshold: float = 6.0,
        stable_seconds: float = 3.0,
        success_message: str,
        timeout_message: str | None = None,
    ) -> WatcherInfo:
        _screen_signature()
        checker = make_screen_stable_checker(
            threshold=threshold,
            stable_seconds=stable_seconds,
        )
        return self.start_predicate_watcher(
            mode="tela_estavel",
            description="Avisar quando a tela estabilizar ou terminar de carregar",
            check_fn=checker,
            interval_seconds=interval_seconds,
            timeout_seconds=timeout_seconds,
            success_message=success_message,
            timeout_message=timeout_message,
            run_in_thread=True,
        )

    def start_window_title_contains_watcher(
        self,
        text: str,
        *,
        interval_seconds: float = 1.0,
        timeout_seconds: float | None = 120.0,
        success_message: str,
        timeout_message: str | None = None,
    ) -> WatcherInfo:
        _active_window_title()
        checker = make_window_title_contains_checker(text)
        return self.start_predicate_watcher(
            mode="janela_contem",
            description=f"Avisar quando o titulo da janela contiver '{text}'",
            check_fn=checker,
            interval_seconds=interval_seconds,
            timeout_seconds=timeout_seconds,
            success_message=success_message,
            timeout_message=timeout_message,
            run_in_thread=True,
        )

    def start_window_title_not_contains_watcher(
        self,
        text: str,
        *,
        interval_seconds: float = 1.0,
        timeout_seconds: float | None = 120.0,
        success_message: str,
        timeout_message: str | None = None,
    ) -> WatcherInfo:
        _active_window_title()
        checker = make_window_title_not_contains_checker(text)
        return self.start_predicate_watcher(
            mode="janela_nao_contem",
            description=f"Avisar quando o titulo da janela nao contiver mais '{text}'",
            check_fn=checker,
            interval_seconds=interval_seconds,
            timeout_seconds=timeout_seconds,
            success_message=success_message,
            timeout_message=timeout_message,
            run_in_thread=True,
        )

    def start_predicate_watcher(
        self,
        *,
        mode: str,
        description: str,
        check_fn: CheckFn,
        interval_seconds: float = 1.0,
        timeout_seconds: float | None = 120.0,
        success_message: str,
        timeout_message: str | None = None,
        run_in_thread: bool = True,
    ) -> WatcherInfo:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise WatcherError("Os watchers precisam de um loop async ativo para rodar.") from exc

        self._counter += 1
        watcher_id = f"watcher-{self._counter:03d}"
        watcher = WatcherInfo(
            watcher_id=watcher_id,
            mode=mode,
            description=description,
            interval_seconds=max(float(interval_seconds), 0.5),
            timeout_seconds=timeout_seconds if timeout_seconds is None else max(float(timeout_seconds), 1.0),
        )
        self._watchers[watcher_id] = watcher

        task = loop.create_task(
            self._run_watcher_loop(
                watcher,
                check_fn=check_fn,
                success_message=success_message,
                timeout_message=timeout_message,
                run_in_thread=run_in_thread,
            )
        )
        self._tasks[watcher_id] = task
        task.add_done_callback(lambda done, wid=watcher_id: self._finalize_watcher(wid, done))
        return watcher

    async def _run_watcher_loop(
        self,
        watcher: WatcherInfo,
        *,
        check_fn: CheckFn,
        success_message: str,
        timeout_message: str | None,
        run_in_thread: bool,
    ) -> None:
        deadline = None
        if watcher.timeout_seconds is not None:
            deadline = time.monotonic() + float(watcher.timeout_seconds)

        while True:
            if deadline is not None and time.monotonic() >= deadline:
                watcher.status = "timed_out"
                if timeout_message:
                    await self._notify(timeout_message)
                return

            try:
                if run_in_thread:
                    triggered = bool(await asyncio.to_thread(check_fn))
                else:
                    triggered = bool(check_fn())
                watcher.last_error = ""
            except asyncio.CancelledError:
                watcher.status = "cancelled"
                raise
            except Exception as exc:
                watcher.last_error = str(exc)
                log_exception_safely(
                    logger,
                    f"Falha no watcher {watcher.watcher_id}",
                    exc,
                    level=logging.WARNING,
                )
                await asyncio.sleep(watcher.interval_seconds)
                continue

            if triggered:
                watcher.status = "triggered"
                await self._notify(success_message)
                return

            await asyncio.sleep(watcher.interval_seconds)

    async def _notify(self, message: str) -> None:
        text = str(message or "").strip()
        if not text:
            return
        notifier = self._notifier
        if notifier is None:
            logger.info("Watcher disparado sem notifier configurado: %s", text)
            return
        try:
            await notifier(text)
        except Exception as exc:
            log_exception_safely(
                logger,
                f"Falha ao notificar watcher: {text}",
                exc,
                level=logging.WARNING,
            )

    def _finalize_watcher(self, watcher_id: str, task: asyncio.Task) -> None:
        self._tasks.pop(watcher_id, None)
        watcher = self._watchers.get(watcher_id)
        if watcher and watcher.status != "active":
            self._watchers.pop(watcher_id, None)

        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log_exception_safely(
                logger,
                f"Watcher {watcher_id} terminou com erro inesperado",
                exc,
                level=logging.WARNING,
            )
