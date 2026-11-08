from __future__ import annotations

import logging
import threading

if __package__ and __package__.startswith("agent"):
    from agent.runtime.user_activity_monitor import UserActivityMonitor
else:  # pragma: no cover - suporte a execucao direta em agent/
    from runtime.user_activity_monitor import UserActivityMonitor


logger = logging.getLogger(__name__)


class VisionAutoShutdown:
    """Auto-desliga a visao quando o usuario fica ausente por muito tempo."""

    def __init__(
        self,
        *,
        activity_monitor: UserActivityMonitor,
        speech_pipeline,
        idle_threshold_minutes: int = 10,
        check_interval_seconds: int = 60,
    ) -> None:
        self.activity_monitor = activity_monitor
        self.speech_pipeline = speech_pipeline
        self.idle_threshold = max(int(idle_threshold_minutes), 1) * 60
        self.check_interval = max(int(check_interval_seconds), 1)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False
        logger.info(
            "[AUTO_SHUTDOWN] Inicializado - idle_threshold=%dmin check_interval=%ds",
            max(int(idle_threshold_minutes), 1),
            self.check_interval,
        )

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="KiraVisionAutoShutdown",
            daemon=True,
        )
        self._thread.start()
        self._running = True
        logger.info("[AUTO_SHUTDOWN] Monitoramento iniciado")

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        logger.info("[AUTO_SHUTDOWN] Monitoramento parado")

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(self.check_interval):
            try:
                self._check_and_shutdown()
            except Exception as exc:
                logger.error("[AUTO_SHUTDOWN] Erro no loop: %s", exc)

    def _check_and_shutdown(self) -> None:
        visual_router = getattr(self.speech_pipeline, "_visual_router", None)
        vision_enabled = bool(getattr(visual_router, "vision_mode_enabled", False))
        if not vision_enabled:
            return
        if not self.activity_monitor.is_idle(self.idle_threshold):
            return

        idle_minutes = self.activity_monitor.get_idle_time() / 60.0
        logger.warning(
            "[AUTO_SHUTDOWN] Usuario ausente por %.1f minutos - desligando visao",
            idle_minutes,
        )
        toggle_method = getattr(self.speech_pipeline, "toggle_vision_mode", None)
        if callable(toggle_method):
            try:
                toggle_method(False, source="auto_shutdown")
            except TypeError:
                toggle_method(False)


__all__ = ["VisionAutoShutdown"]
