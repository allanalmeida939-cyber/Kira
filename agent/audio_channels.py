from __future__ import annotations

import logging
import threading


logger = logging.getLogger(__name__)

try:
    import pygame
except Exception:  # pragma: no cover - ambiente sem pygame
    pygame = None  # type: ignore[assignment]


class AudioChannelManager:
    """
    Gerencia canais pygame usados pelo app e aplica ducking quando a Kira fala.

    Observacao:
    - o ducking so consegue afetar audios reproduzidos pelo proprio pygame;
    - audio vindo do browser/sistema operacional continua fora desse controle.
    """

    DUCKING_VOLUME = 0.15
    NORMAL_VOLUME = 1.0
    TOTAL_CHANNELS = 6
    KIRA_CHANNEL_INDEX = 0
    MEDIA_CHANNEL_INDEX = 1
    TRIGGER_CHANNEL_INDEX = 2

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._initialized = False
        self._failed = False
        self._is_kira_speaking = False
        self._media_normal_volume = self.NORMAL_VOLUME
        self.kira_channel = None
        self.media_channel = None
        self.trigger_channel = None

    def ensure_initialized(self) -> bool:
        if self._failed or pygame is None:
            return False
        if self._initialized:
            return True

        with self._lock:
            if self._failed:
                return False
            if self._initialized:
                return True
            try:
                if not pygame.mixer.get_init():
                    pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
                if pygame.mixer.get_num_channels() < self.TOTAL_CHANNELS:
                    pygame.mixer.set_num_channels(self.TOTAL_CHANNELS)
                pygame.mixer.set_reserved(3)
                self.kira_channel = pygame.mixer.Channel(self.KIRA_CHANNEL_INDEX)
                self.media_channel = pygame.mixer.Channel(self.MEDIA_CHANNEL_INDEX)
                self.trigger_channel = pygame.mixer.Channel(self.TRIGGER_CHANNEL_INDEX)
                self._initialized = True
                logger.info("[AUDIO_CHANNELS] Inicializado ducking=true canais=%s", self.TOTAL_CHANNELS)
            except Exception as exc:
                self._failed = True
                logger.warning("[AUDIO_CHANNELS] Falha ao inicializar pygame mixer: %s", exc)
                return False
        return True

    def kira_started_speaking(self) -> None:
        self._is_kira_speaking = True
        if not self.ensure_initialized():
            return
        with self._lock:
            media_channel = self.media_channel
            if media_channel is None:
                return
            if media_channel.get_busy():
                media_channel.set_volume(self.DUCKING_VOLUME)
                logger.info("[AUDIO_CHANNELS] ducking_on media_volume=%.2f", self.DUCKING_VOLUME)

    def kira_stopped_speaking(self) -> None:
        self._is_kira_speaking = False
        if not self.ensure_initialized():
            return
        with self._lock:
            media_channel = self.media_channel
            if media_channel is None:
                return
            if media_channel.get_busy():
                media_channel.set_volume(self._media_normal_volume)
                logger.info("[AUDIO_CHANNELS] ducking_off media_volume=%.2f", self._media_normal_volume)

    def play_media(self, file_path: str, *, volume: float = 1.0) -> bool:
        if not self.ensure_initialized():
            return False
        try:
            sound = pygame.mixer.Sound(str(file_path))
            clamped_volume = max(0.0, min(1.0, float(volume)))
            with self._lock:
                self._media_normal_volume = clamped_volume
                if self.media_channel is None:
                    return False
                self.media_channel.set_volume(
                    self.DUCKING_VOLUME if self._is_kira_speaking else clamped_volume
                )
                self.media_channel.play(sound)
            logger.info("[AUDIO_CHANNELS] media_started path=%s", file_path)
            return True
        except Exception as exc:
            logger.warning("[AUDIO_CHANNELS] Falha ao tocar media path=%s reason=%s", file_path, exc)
            return False

    def play_trigger(self, file_path: str, *, volume: float = 0.25) -> bool:
        if not self.ensure_initialized():
            return False
        try:
            sound = pygame.mixer.Sound(str(file_path))
            clamped_volume = max(0.0, min(1.0, float(volume)))
            with self._lock:
                if self.trigger_channel is None:
                    return False
                self.trigger_channel.set_volume(clamped_volume)
                self.trigger_channel.stop()
                self.trigger_channel.play(sound)
            logger.info("[AUDIO_CHANNELS] trigger_started path=%s", file_path)
            return True
        except Exception as exc:
            logger.warning("[AUDIO_CHANNELS] Falha ao tocar trigger path=%s reason=%s", file_path, exc)
            return False

    def stop_media(self) -> None:
        if not self.ensure_initialized():
            return
        with self._lock:
            if self.media_channel is not None:
                self.media_channel.stop()

    def stop_all(self) -> None:
        if not self.ensure_initialized():
            return
        with self._lock:
            for channel in (self.kira_channel, self.media_channel, self.trigger_channel):
                if channel is not None:
                    channel.stop()


_audio_manager: AudioChannelManager | None = None


def get_audio_manager() -> AudioChannelManager:
    global _audio_manager
    if _audio_manager is None:
        _audio_manager = AudioChannelManager()
    return _audio_manager
