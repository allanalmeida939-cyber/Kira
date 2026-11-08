import asyncio
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
import json
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

import sounddevice as sd
import tkinter as tk
from tkinter import messagebox, scrolledtext

from dotenv import load_dotenv
from livekit import api, rtc
from runtime.process_tracker import get_tracker

APP_TITLE = "Kira LiveKit Python UI"
AGENT_FILENAME = "agent.py"
AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNELS = 1
AUDIO_BLOCK_SIZE = 480
AGENT_READY_TIMEOUT = 45.0
REMOTE_AUDIO_ACTIVITY_THRESHOLD = 48
REMOTE_AUDIO_GRACE_SECONDS = 0.25
tracker = get_tracker()
VISION_CONTROL_URL = (
    os.getenv("KIRA_VISION_CONTROL_URL")
    or f"http://127.0.0.1:{int(os.getenv('KIRA_VISION_CONTROL_PORT', '8092') or '8092')}"
).strip().rstrip("/")


@dataclass
class EnvConfig:
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    room_name: str
    identity: str
    display_name: str
    agent_name: str


class LiveKitClient:
    def __init__(self, log) -> None:
        self.log = log
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

        self.room: Optional[rtc.Room] = None
        self.connected = False

        self.audio_source: Optional[rtc.AudioSource] = None
        self.audio_track: Optional[rtc.LocalAudioTrack] = None
        self.audio_publication = None
        self.audio_stream: Optional[sd.RawInputStream] = None
        self.audio_muted = False
        self.remote_audio_tasks: dict[str, Future] = {}
        self.remote_audio_playing = threading.Event()
        self.remote_audio_grace_until = 0.0
        self.agent_identity = ""

        self._vision_mode_enabled = False

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def connect(self, cfg: EnvConfig) -> None:
        self._call(self._connect(cfg))

    async def _connect(self, cfg: EnvConfig) -> None:
        if self.connected:
            self.log("Ja conectado ao LiveKit.")
            return

        token = self._build_token(cfg)
        room = rtc.Room(loop=self.loop)

        @room.on("participant_connected")
        def _participant_connected(participant, *_args):
            identity = getattr(participant, "identity", "desconhecido")
            self.log(f"Participante entrou: {identity}")

        @room.on("participant_disconnected")
        def _participant_disconnected(participant, *_args):
            identity = getattr(participant, "identity", "desconhecido")
            self.log(f"Participante saiu: {identity}")

        @room.on("track_subscribed")
        def _track_subscribed(track, publication, participant, *_args):
            try:
                identity = getattr(participant, "identity", "") or ""
                self.log(f"Track remota assinada: kind={track.kind} participant={identity}")

                if track.kind == rtc.TrackKind.KIND_AUDIO:
                    # aceita o participante real do worker/agent dispatch
                    is_agent_track = (
                        identity.startswith("agent-")
                        or identity == self.agent_identity
                    )

                    if not is_agent_track:
                        self.log(f"Ignorando audio remoto de {identity}")
                        return

                    existing_task = self.remote_audio_tasks.get(track.sid)
                    if existing_task and not existing_task.done():
                        return

                    future = asyncio.run_coroutine_threadsafe(
                        self._play_remote_audio_track(track, identity),
                        self.loop,
                    )
                    self.remote_audio_tasks[track.sid] = future
                    future.add_done_callback(
                        lambda finished_task, track_sid=track.sid, participant_identity=identity: self._on_remote_audio_task_done(
                            track_sid,
                            participant_identity,
                            finished_task,
                        )
                    )
            except Exception as exc:
                self.log(f"Erro no track_subscribed: {exc}")

        @room.on("track_unsubscribed")
        def _track_unsubscribed(track, publication, participant, *_args):
            identity = getattr(participant, "identity", "desconhecido")
            track_sid = getattr(track, "sid", None) or getattr(publication, "sid", None)
            if (
                (track is not None and track.kind == rtc.TrackKind.KIND_AUDIO)
                or getattr(publication, "kind", None) == rtc.TrackKind.KIND_AUDIO
            ):
                self.log(f"Track de audio encerrada para {identity}")
            self._cancel_remote_audio_task(track_sid)

        self.log(f"Conectando ao LiveKit em {cfg.livekit_url}...")
        options = rtc.RoomOptions(auto_subscribe=True)
        await room.connect(cfg.livekit_url, token, options=options)
        self.room = room
        self.connected = True
        self.log(f"Conectado na sala: {room.name}")

    def _cancel_remote_audio_task(self, track_sid: Optional[str]) -> None:
        if not track_sid:
            return

        task = self.remote_audio_tasks.pop(track_sid, None)
        if task and not task.done():
            task.cancel()

    def _on_remote_audio_task_done(
        self,
        track_sid: str,
        participant_identity: str,
        task: Future,
    ) -> None:
        self.remote_audio_tasks.pop(track_sid, None)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            self.log(f"Falha ao reproduzir audio remoto de {participant_identity}: {exc}")

    async def _play_remote_audio_track(self, track, participant_identity: str):
        self.log(f"Recebendo audio remoto de: {participant_identity}")

        stream = None
        audio_stream = None

        try:
            stream = sd.OutputStream(
                samplerate=48000,
                channels=1,
                dtype="int16",
                blocksize=4800,
            )
            stream.start()

            audio_stream = rtc.AudioStream(track)

            async for event in audio_stream:
                frame = event.frame
                if frame is None:
                    continue

                pcm = np.frombuffer(frame.data, dtype=np.int16)

                if frame.num_channels == 2:
                    pcm = pcm.reshape(-1, 2).mean(axis=1).astype(np.int16)

                peak = int(np.max(np.abs(pcm.astype(np.int32)))) if pcm.size else 0
                now = time.time()

                if peak > REMOTE_AUDIO_ACTIVITY_THRESHOLD:
                    self.remote_audio_playing.set()
                    self.remote_audio_grace_until = now + REMOTE_AUDIO_GRACE_SECONDS
                elif now >= self.remote_audio_grace_until:
                    self.remote_audio_playing.clear()

                stream.write(pcm)
        except Exception as exc:
            self.log(f"Erro ao reproduzir audio remoto de {participant_identity}: {exc}")
        finally:
            self.remote_audio_playing.clear()
            self.remote_audio_grace_until = 0.0

            try:
                if audio_stream:
                    await audio_stream.aclose()
            except Exception:
                pass

            try:
                if stream:
                    stream.stop()
                    stream.close()
            except Exception:
                pass

    def disconnect(self) -> None:
        self._call(self._disconnect())

    async def _disconnect(self) -> None:
        if not self.connected or not self.room:
            return

        await self._stop_microphone()
        await self._stop_remote_audio_playback()
        await self.room.disconnect()
        self.room = None
        self.connected = False
        self.log("Desconectado do LiveKit.")

    def _build_token(self, cfg: EnvConfig) -> str:
        return (
            api.AccessToken(cfg.livekit_api_key, cfg.livekit_api_secret)
            .with_identity(cfg.identity)
            .with_name(cfg.display_name)
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=cfg.room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=True,
                )
            )
            .to_jwt()
        )

    def request_agent_dispatch(self, cfg: EnvConfig) -> None:
        self._call(self._request_agent_dispatch(cfg))

    async def _request_agent_dispatch(self, cfg: EnvConfig) -> None:
        if not cfg.agent_name:
            raise RuntimeError("LIVEKIT_AGENT_NAME nao foi definido no .env.")

        async with api.LiveKitAPI(
            url=cfg.livekit_url,
            api_key=cfg.livekit_api_key,
            api_secret=cfg.livekit_api_secret,
        ) as lkapi:
            dispatch = await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=cfg.agent_name,
                    room=cfg.room_name,
                    metadata=f"python-ui:{cfg.identity}",
                )
            )
        self.log(
            f"Dispatch solicitado para o agente '{cfg.agent_name}' na sala '{cfg.room_name}'. "
            f"job={getattr(dispatch, 'id', 'ok')}"
        )

    def start_microphone(self) -> None:
        self._call(self._start_microphone())

    async def _start_microphone(self) -> None:
        if not self.connected or not self.room:
            raise RuntimeError("Conecte ao LiveKit antes de iniciar o microfone.")
        if self.audio_track is not None:
            self.log("Microfone ja publicado.")
            return

        self.audio_source = rtc.AudioSource(AUDIO_SAMPLE_RATE, AUDIO_CHANNELS)
        self.audio_track = rtc.LocalAudioTrack.create_audio_track("microfone", self.audio_source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        self.audio_publication = await self.room.local_participant.publish_track(self.audio_track, options)
        self.audio_muted = False
        self._open_audio_capture_stream()
        self.log("Microfone publicado.")

    def _open_audio_capture_stream(self) -> None:
        if not self.audio_source:
            return

        source = self.audio_source

        def _callback(indata, frames, _time_info, status):
            if status:
                self.log(f"Aviso do microfone: {status}")

            if self.audio_muted:
                return

            if self.remote_audio_playing.is_set():
                return

            if time.time() < self.remote_audio_grace_until:
                return

            try:
                audio_bytes = bytes(indata)
                samples_per_channel = int(frames)

                frame = rtc.AudioFrame(
                    audio_bytes,
                    AUDIO_SAMPLE_RATE,
                    AUDIO_CHANNELS,
                    samples_per_channel,
                )

                future = asyncio.run_coroutine_threadsafe(
                    source.capture_frame(frame),
                    self.loop,
                )

                def _done(fut):
                    try:
                        fut.result()
                    except Exception as exc:
                        self.log(f"Falha ao capturar audio: {exc}")

                future.add_done_callback(_done)
            except Exception as exc:
                self.log(f"Erro no callback do microfone: {exc}")

        self.audio_stream = sd.RawInputStream(
            samplerate=AUDIO_SAMPLE_RATE,
            blocksize=AUDIO_BLOCK_SIZE,
            channels=AUDIO_CHANNELS,
            dtype="int16",
            callback=_callback,
        )
        self.audio_stream.start()

    def toggle_mute(self) -> bool:
        return self._call(self._toggle_mute())

    async def _toggle_mute(self) -> bool:
        if not self.audio_track:
            raise RuntimeError("O microfone ainda nao foi iniciado.")

        if self.audio_muted:
            self.audio_track.unmute()
            self.audio_muted = False
            self.log("Microfone desmutado.")
        else:
            self.audio_track.mute()
            self.audio_muted = True
            self.log("Microfone mutado.")
        return self.audio_muted

    async def _stop_microphone(self) -> None:
        if self.audio_stream:
            try:
                self.audio_stream.stop()
            finally:
                self.audio_stream.close()
            self.audio_stream = None

        if self.room and self.audio_publication:
            try:
                await self.room.local_participant.unpublish_track(self.audio_publication.sid)
            except Exception:
                pass

        self.audio_publication = None
        self.audio_track = None
        self.audio_source = None
        self.audio_muted = False

    async def _stop_remote_audio_playback(self) -> None:
        tasks = list(self.remote_audio_tasks.values())
        self.remote_audio_tasks.clear()

        for task in tasks:
            if not task.done():
                task.cancel()

        for task in tasks:
            try:
                await asyncio.wrap_future(task)
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.log(f"Falha ao encerrar audio remoto: {exc}")

        self.remote_audio_playing.clear()
        self.remote_audio_grace_until = 0.0

    def toggle_vision_mode(self) -> bool:
        return self._call(self._toggle_vision_mode())

    async def _toggle_vision_mode(self) -> bool:
        new_state = not self._vision_mode_enabled
        payload = json.dumps({"enabled": new_state}).encode("utf-8")
        req = urllib_request.Request(
            f"{VISION_CONTROL_URL}/vision/toggle",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=3.0) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib_error.URLError as exc:
            raise RuntimeError(
                "Modo Visao indisponivel. Inicie o agent.py antes de alternar a visao."
            ) from exc

        self._vision_mode_enabled = bool(data.get("vision_mode_enabled"))
        if self._vision_mode_enabled:
            self.log("Modo Visao ativado.")
        else:
            self.log("Modo Visao desativado.")
        return self._vision_mode_enabled


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("940x680")

        self.project_dir = Path(__file__).resolve().parent
        self.agent_path = self.project_dir / AGENT_FILENAME
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.client = LiveKitClient(self.enqueue_log)
        self.agent_process: Optional[subprocess.Popen] = None
        self.agent_ready_event = threading.Event()
        self.agent_started_event = threading.Event()

        self._build_ui()
        self._load_defaults_from_env()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._flush_logs)

    def _build_ui(self) -> None:
        frame = tk.Frame(self.root, padx=12, pady=12)
        frame.pack(fill="both", expand=True)

        tk.Label(frame, text="Sala LiveKit").grid(row=0, column=0, sticky="w")
        self.room_var = tk.StringVar()
        tk.Entry(frame, textvariable=self.room_var, width=34).grid(row=0, column=1, sticky="ew", padx=(8, 16))

        tk.Label(frame, text="Identity UI").grid(row=0, column=2, sticky="w")
        self.identity_var = tk.StringVar()
        tk.Entry(frame, textvariable=self.identity_var, width=28).grid(row=0, column=3, sticky="ew", padx=(8, 0))

        tk.Label(frame, text="Nome exibido").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.display_name_var = tk.StringVar()
        tk.Entry(frame, textvariable=self.display_name_var, width=34).grid(row=1, column=1, sticky="ew", padx=(8, 16), pady=(8, 0))

        tk.Label(frame, text="Agent name").grid(row=1, column=2, sticky="w", pady=(8, 0))
        self.agent_name_var = tk.StringVar()
        tk.Entry(frame, textvariable=self.agent_name_var, width=28).grid(row=1, column=3, sticky="ew", padx=(8, 0), pady=(8, 0))

        buttons = tk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(16, 10))

        self.start_btn = tk.Button(buttons, text="Iniciar sessao", width=18, command=self.start_session)
        self.start_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = tk.Button(buttons, text="Parar tudo", width=18, command=self.stop_all)
        self.stop_btn.pack(side="left", padx=(0, 8))

        self.mic_btn = tk.Button(buttons, text="Mutar microfone", width=18, command=self.toggle_mic)
        self.mic_btn.pack(side="left", padx=(0, 8))

        self.screen_btn = tk.Button(buttons, text="Ativar visao", width=18, command=self.toggle_screen)
        self.screen_btn.pack(side="left", padx=(0, 8))

        self.dispatch_btn = tk.Button(buttons, text="Iniciar agente", width=18, command=self.dispatch_agent)
        self.dispatch_btn.pack(side="left")

        self.status_var = tk.StringVar(value="Pronto")
        tk.Label(frame, textvariable=self.status_var, anchor="w").grid(row=3, column=0, columnspan=4, sticky="ew", pady=(0, 8))

        info = (
            "Fluxo: 1) inicia agent.py em dev  2) espera o worker ficar pronto  "
            "3) conecta a UI na room  4) faz o dispatch do agente"
        )
        tk.Label(frame, text=info, anchor="w", justify="left").grid(row=4, column=0, columnspan=4, sticky="ew", pady=(0, 10))

        self.logs = scrolledtext.ScrolledText(frame, wrap="word", height=30)
        self.logs.grid(row=5, column=0, columnspan=4, sticky="nsew")

        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)
        frame.rowconfigure(5, weight=1)

    def _load_defaults_from_env(self) -> None:
        env_path = self.project_dir / ".env"
        if env_path.exists():
            load_dotenv(env_path, override=False)
            self.enqueue_log(f".env carregado de {env_path}")
        else:
            self.enqueue_log(f".env nao encontrado em {env_path}")

        self.room_var.set(os.getenv("LIVEKIT_ROOM", "minha-sala"))
        self.identity_var.set(os.getenv("LIVEKIT_UI_IDENTITY", f"python-ui-{uuid.uuid4().hex[:8]}"))
        self.display_name_var.set(os.getenv("LIVEKIT_UI_NAME", "Python UI"))
        self.agent_name_var.set(os.getenv("LIVEKIT_AGENT_NAME", ""))

    def build_config(self) -> EnvConfig:
        load_dotenv(self.project_dir / ".env", override=True)

        livekit_url = os.getenv("LIVEKIT_URL", "").strip()
        api_key = os.getenv("LIVEKIT_API_KEY", "").strip()
        api_secret = os.getenv("LIVEKIT_API_SECRET", "").strip()
        room_name = self.room_var.get().strip()
        identity = self.identity_var.get().strip()
        display_name = self.display_name_var.get().strip()
        agent_name = self.agent_name_var.get().strip() or os.getenv("LIVEKIT_AGENT_NAME", "").strip()

        if not livekit_url or not api_key or not api_secret:
            raise RuntimeError("Faltam LIVEKIT_URL, LIVEKIT_API_KEY ou LIVEKIT_API_SECRET no .env.")
        if not room_name:
            raise RuntimeError("Defina LIVEKIT_ROOM no .env ou no campo da interface.")
        if not identity:
            raise RuntimeError("Identity da UI esta vazia.")
        if not display_name:
            raise RuntimeError("Nome exibido da UI esta vazio.")
        if not agent_name:
            raise RuntimeError(
                "Defina LIVEKIT_AGENT_NAME no .env ou no campo 'Agent name'. "
                "Ele precisa ser exatamente o mesmo nome registrado no seu agent.py."
            )

        return EnvConfig(
            livekit_url=livekit_url,
            livekit_api_key=api_key,
            livekit_api_secret=api_secret,
            room_name=room_name,
            identity=identity,
            display_name=display_name,
            agent_name=agent_name,
        )

    def enqueue_log(self, message: str) -> None:
        self.log_queue.put(message)

    def _flush_logs(self) -> None:
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break
            timestamp = time.strftime("%H:%M:%S")
            self.logs.insert("end", f"[{timestamp}] {message}\n")
            self.logs.see("end")
        self.root.after(100, self._flush_logs)

    def set_status(self, text: str) -> None:
        self.status_var.set(text)
        self.enqueue_log(text)

    def run_safe(self, fn, success_status: Optional[str] = None) -> None:
        def _runner():
            try:
                fn()
                if success_status:
                    self.root.after(0, lambda: self.set_status(success_status))
            except Exception as exc:
                logging.exception("Erro na interface")
                err = str(exc)
                self.root.after(0, lambda err=err: self.set_status(f"Erro: {err}"))
                self.root.after(0, lambda err=err: messagebox.showerror(APP_TITLE, err))

        threading.Thread(target=_runner, daemon=True).start()

    def start_agent_process(self) -> None:
        if self.agent_process and self.agent_process.poll() is None:
            self.enqueue_log("agent.py ja esta em execucao.")
            return
        if not self.agent_path.exists():
            raise FileNotFoundError(f"Nao encontrei {self.agent_path}")

        self.agent_ready_event.clear()
        self.agent_started_event.clear()
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        cmd = [sys.executable, str(self.agent_path), "dev"]
        self.agent_process = tracker.start_and_register(
            name="livekit_ui_agent_dev",
            args=cmd,
            cwd=str(self.project_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            bufsize=1,
        )
        threading.Thread(target=self._drain_agent_logs, daemon=True).start()
        self.enqueue_log(f"agent.py iniciado: {' '.join(cmd)}")

    def _drain_agent_logs(self) -> None:
        if not self.agent_process or not self.agent_process.stdout:
            return
        for raw_line in self.agent_process.stdout:
            line = raw_line.rstrip()
            if not line:
                continue
            self.enqueue_log(f"[agent] {line}")
            lower = line.lower()
            if (
                "boot em modo dev" in lower
                or "worker pronto para conexoes via livekit" in lower
                or "[boot]" in lower and "worker pronto" in lower
                or "worker registered" in lower
                or "registered worker" in lower
            ):
                self.agent_ready_event.set()
            if "bootstrap do entrypoint iniciado" in lower or "kira iniciada com sucesso" in lower:
                self.agent_started_event.set()

    def wait_for_agent_ready(self, timeout: float = AGENT_READY_TIMEOUT) -> None:
        self.enqueue_log("Aguardando worker dev ficar pronto...")
        if not self.agent_ready_event.wait(timeout=timeout):
            raise TimeoutError(
                "O agent.py nao ficou pronto a tempo. Veja os logs da janela para checar dependencia ou erro no boot."
            )
        self.enqueue_log("Worker dev pronto para receber dispatch.")

    def stop_agent_process(self) -> None:
        if not self.agent_process or self.agent_process.poll() is not None:
            self.enqueue_log("agent.py ja esta parado.")
            return
        self.agent_process.terminate()
        try:
            self.agent_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.agent_process.kill()
        self.enqueue_log("agent.py encerrado.")

    def start_session(self) -> None:
        def _work() -> None:
            cfg = self.build_config()
            self.client.agent_identity = cfg.agent_name.strip()
            self.start_agent_process()
            self.wait_for_agent_ready()
            self.client.request_agent_dispatch(cfg)
            time.sleep(1.0)
            self.client.connect(cfg)
            self.client.start_microphone()
            self.enqueue_log("Sessao iniciada. Worker pronto, dispatch enviado e UI conectada.")

        self.run_safe(_work, success_status="Sessao LiveKit iniciada.")

    def stop_all(self) -> None:
        def _work() -> None:
            try:
                self.client.disconnect()
            finally:
                self.stop_agent_process()

        self.run_safe(_work, success_status="Tudo parado.")

    def toggle_mic(self) -> None:
        def _work() -> None:
            muted = self.client.toggle_mute()
            self.root.after(0, lambda: self.mic_btn.config(text="Desmutar microfone" if muted else "Mutar microfone"))

        self.run_safe(_work)

    def toggle_screen(self) -> None:
        def _work() -> None:
            running = self.client.toggle_vision_mode()
            self.root.after(0, lambda: self.screen_btn.config(text="Desativar visao" if running else "Ativar visao"))

        self.run_safe(_work, success_status="Modo Visao alterado.")

    def dispatch_agent(self) -> None:
        def _work() -> None:
            cfg = self.build_config()
            self.client.agent_identity = cfg.agent_name.strip()
            self.wait_for_agent_ready()
            if not self.client.connected:
                self.client.connect(cfg)
                self.client.start_microphone()
            self.client.request_agent_dispatch(cfg)

        self.run_safe(_work, success_status="Dispatch do agente solicitado.")

    def on_close(self) -> None:
        self.stop_all()
        tracker.cleanup_all()
        self.root.after(500, self.root.destroy)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
