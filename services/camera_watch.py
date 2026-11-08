"""Webcam do PC: olhar agora ("tem alguém na porta?"), vigia (avisa quando aparece alguém) e controle por gestos.

Uma câmera só, aberta pelo backend (OpenCV) enquanto algum recurso usa e solta 3 s depois. As imagens são analisadas
com MediaPipe na CPU: detector de objetos EfficientDet-Lite0 (~110 ms por imagem nesta máquina; o vigia olha 1 vez por
segundo) e reconhecedor de gestos (~32 ms; ~6 vezes por segundo). Os modelos ficam em models/vision e são baixados na
primeira vez. Fotos de "olhar" ficam em cache/vision/imagens (no máximo 30); as do vigia, em Vídeos\\Kira Segurança.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

logger = logging.getLogger("kira.app.camera")

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "models" / "vision"
MODELS = {
    "efficientdet_lite0.tflite": "https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/int8/latest/efficientdet_lite0.tflite",
    "gesture_recognizer.task": "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/latest/gesture_recognizer.task",
}
ACTIONS = ("olhar", "vigiar", "parar_vigia", "gestos", "parar_gestos", "estado", "cadastrar_rosto", "esquecer_rosto", "rostos")
ENROLL_SHOTS = 5  # fotos para cadastrar um rosto (a pessoa olhando para a câmera)
IMAGE_RE = re.compile(r"^cam-[0-9a-f]{10}\.jpg$")
MAX_IMAGES = 30
SCORE_MIN = 0.5
RELEASE_AFTER_S = 3.0
WATCH_INTERVAL_S = 1.0
WATCH_CONFIRM_FRAMES = 2       # pessoa em 2 olhadas seguidas: não avisa por um vulto de uma imagem só
WATCH_REALERT_S = 120.0        # a mesma pessoa parada não gera um aviso por segundo
WATCH_GONE_S = 10.0            # sumiu por 10 s: a próxima aparição é nova
GESTURE_INTERVAL_S = 0.16
GESTURE_MIN_SCORE = 0.6
GESTURE_REPEAT_S = 1.0         # volume segurando o gesto: um passo por segundo
GESTURE_IDLE_OFF_S = 60 * 60   # controle por gestos desliga sozinho depois de 1 h sem gesto
# gesto do MediaPipe -> (ação, nome falado, repete segurando?)
GESTURES = {
    "Open_Palm": ("play_pause", "mão aberta: tocar/pausar", False),
    "Victory": ("next", "paz e amor: próxima", False),
    "Pointing_Up": ("previous", "dedo para cima: anterior", False),
    "Thumb_Up": ("volume_up", "joinha: aumenta o volume", True),
    "Thumb_Down": ("volume_down", "polegar para baixo: diminui o volume", True),
}
LABELS_PT = {
    "person": "pessoa", "bicycle": "bicicleta", "car": "carro", "motorcycle": "moto", "bus": "ônibus", "truck": "caminhão",
    "bird": "pássaro", "cat": "gato", "dog": "cachorro", "backpack": "mochila", "umbrella": "guarda-chuva",
    "handbag": "bolsa", "suitcase": "mala", "bottle": "garrafa", "wine glass": "taça", "cup": "copo", "fork": "garfo",
    "knife": "faca", "spoon": "colher", "bowl": "tigela", "banana": "banana", "apple": "maçã", "orange": "laranja",
    "chair": "cadeira", "couch": "sofá", "potted plant": "planta", "bed": "cama", "dining table": "mesa",
    "tv": "TV", "laptop": "notebook", "mouse": "mouse", "remote": "controle remoto", "keyboard": "teclado",
    "cell phone": "celular", "microwave": "micro-ondas", "oven": "forno", "sink": "pia", "refrigerator": "geladeira",
    "book": "livro", "clock": "relógio", "vase": "vaso", "scissors": "tesoura", "teddy bear": "ursinho",
    "toothbrush": "escova de dente", "sports ball": "bola", "tie": "gravata",
}


def label_pt(name: str) -> str:
    return LABELS_PT.get(name, name)


def summarize(detections: list[tuple[str, float]]) -> dict[str, Any]:
    """[(rótulo, confiança)] -> pessoas e objetos contados, em português."""
    counts: dict[str, int] = {}
    for name, score in detections:
        if score >= SCORE_MIN:
            counts[label_pt(name)] = counts.get(label_pt(name), 0) + 1
    people = counts.pop("pessoa", 0)
    return {"pessoas": people, "objetos": dict(sorted(counts.items(), key=lambda item: -item[1]))}


class GestureDebounce:
    """Gesto vale quando aparece em 3 das últimas 4 olhadas. Depois de disparar, só dispara de novo quando a mão sai
    (ou, nos de volume, a cada GESTURE_REPEAT_S segurando)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._recent: deque[str] = deque(maxlen=4)
        self._fired: str | None = None
        self._fired_at = 0.0
        self._clock = clock

    def feed(self, gesture: str | None) -> str | None:
        self._recent.append(gesture or "")
        if not gesture or gesture not in GESTURES or self._recent.count(gesture) < 3:
            if not any(self._recent):
                self._fired = None
            return None
        now = self._clock()
        if gesture == self._fired and not (GESTURES[gesture][2] and now - self._fired_at >= GESTURE_REPEAT_S):
            return None
        self._fired, self._fired_at = gesture, now
        return gesture


class CameraHub:
    """Abre a webcam enquanto alguém usa; guarda a última imagem. Tudo que toca no OpenCV fica na thread dela."""

    def __init__(self, index: int = 0, opener: Callable[[int], Any] | None = None) -> None:
        self._index = index
        self._opener = opener or self._open_default
        self._users: set[str] = set()
        self._lock = threading.Lock()
        self._frame = None
        self._frame_at = 0.0
        self._thread: threading.Thread | None = None
        self.error = ""

    @staticmethod
    def _open_default(index: int):
        import cv2

        capture = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        return capture if capture.isOpened() else None

    def acquire(self, user: str) -> None:
        with self._lock:
            self._users.add(user)
            if self._thread is None:
                self.error = ""
                self._thread = threading.Thread(target=self._run, name="kira-camera", daemon=True)
                self._thread.start()

    def release(self, user: str) -> None:
        with self._lock:
            self._users.discard(user)

    @property
    def in_use(self) -> bool:
        with self._lock:
            return bool(self._users)

    def latest(self, newer_than: float = 0.0):
        with self._lock:
            return self._frame if self._frame is not None and self._frame_at > newer_than else None

    def _run(self) -> None:
        capture = self._opener(self._index)
        if capture is None:
            self.error = "A webcam não abriu (outro programa pode estar usando)."
            logger.info("CAMERA_OPEN_FAILED")
            with self._lock:
                self._users.clear()
                self._thread = None
            return
        logger.info("CAMERA_OPENED")
        skipped = 0
        idle_since = None
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    self.error = "A webcam parou de mandar imagem."
                    break
                skipped += 1
                now = time.monotonic()
                with self._lock:
                    if skipped > 5:  # primeiras imagens escuras enquanto a exposição ajusta
                        self._frame, self._frame_at = frame, now
                    if self._users:
                        idle_since = None
                    elif idle_since is None:
                        idle_since = now
                    elif now - idle_since > RELEASE_AFTER_S:
                        # decide sair dentro da trava: quem chegar agora abre uma câmera nova
                        self._thread = None
                        break
                time.sleep(0.05)
        finally:
            capture.release()
            with self._lock:
                self._frame = None
                if self._thread is threading.current_thread():
                    self._thread = None
            logger.info("CAMERA_RELEASED")

    async def snapshot(self, user: str = "olhar", timeout_s: float = 8.0):
        """Uma imagem fresca (tirada depois do pedido)."""
        asked = time.monotonic()
        self.acquire(user)
        try:
            while time.monotonic() - asked < timeout_s:
                frame = self.latest(newer_than=asked)
                if frame is not None:
                    return frame
                if self.error:
                    return None
                await asyncio.sleep(0.05)
            return None
        finally:
            self.release(user)


class Models:
    """Detector e reconhecedor de gestos do MediaPipe, criados na primeira vez (o import leva ~1 s)."""

    def __init__(self, folder: Path = MODEL_DIR) -> None:
        self._folder = folder
        self._detector = None
        self._gestures = None
        self._lock = threading.Lock()
        self._gesture_ts = 0

    async def ensure(self, name: str) -> Path:
        path = self._folder / name
        if path.exists() and path.stat().st_size > 0:
            return path
        self._folder.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=True) as client:
            response = await client.get(MODELS[name])
            response.raise_for_status()
        temp = path.with_suffix(".part")
        temp.write_bytes(response.content)
        temp.replace(path)
        logger.info("CAMERA_MODEL_DOWNLOADED name=%s bytes=%d", name, len(response.content))
        return path

    def detect(self, frame_bgr) -> list[tuple[str, float, tuple[int, int, int, int]]]:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks import python as mp_tasks
        from mediapipe.tasks.python import vision

        with self._lock:
            if self._detector is None:
                self._detector = vision.ObjectDetector.create_from_options(vision.ObjectDetectorOptions(
                    base_options=mp_tasks.BaseOptions(model_asset_path=str(self._folder / "efficientdet_lite0.tflite")),
                    score_threshold=0.35, max_results=15))
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
            result = self._detector.detect(image)
        out = []
        for item in result.detections:
            box = item.bounding_box
            out.append((item.categories[0].category_name, float(item.categories[0].score), (box.origin_x, box.origin_y, box.width, box.height)))
        return out

    def gesture(self, frame_bgr) -> tuple[str | None, float]:
        import cv2
        import mediapipe as mp
        from mediapipe.tasks import python as mp_tasks
        from mediapipe.tasks.python import vision

        with self._lock:
            if self._gestures is None:
                self._gestures = vision.GestureRecognizer.create_from_options(vision.GestureRecognizerOptions(
                    base_options=mp_tasks.BaseOptions(model_asset_path=str(self._folder / "gesture_recognizer.task")),
                    running_mode=vision.RunningMode.VIDEO))
            # o modo vídeo exige tempo sempre crescente
            self._gesture_ts = max(self._gesture_ts + 1, int(time.monotonic() * 1000))
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
            result = self._gestures.recognize_for_video(image, self._gesture_ts)
        if not result.gestures:
            return None, 0.0
        top = result.gestures[0][0]
        return top.category_name, float(top.score)


def annotate_and_save(frame_bgr, detections, folder: Path, name: str, *, keep: int | None = MAX_IMAGES) -> Path:
    """Desenha as caixas (pessoas em destaque) e grava o JPEG; apaga as fotos mais velhas além de `keep`."""
    import cv2

    image = frame_bgr.copy()
    for label, score, (x, y, w, h) in detections:
        if score < SCORE_MIN:
            continue
        color = (80, 220, 120) if label == "person" else (240, 180, 90)
        cv2.rectangle(image, (x, y), (x + w, y + h), color, 2)
        cv2.putText(image, label_pt(label), (x + 4, max(14, y - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    ok, data = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise OSError("jpeg")
    path.write_bytes(data.tobytes())
    if keep:
        old = sorted(folder.glob("cam-*.jpg"), key=lambda item: item.stat().st_mtime)[:-keep]
        for item in old:
            item.unlink(missing_ok=True)
    return path


class CameraWatch:
    def __init__(self, cache_dir: Path, alerts_dir: Path, *, hub: CameraHub | None = None, models: Models | None = None,
                 on_alert: Callable[[dict[str, Any], Path], Awaitable[None]] | None = None,
                 on_gesture: Callable[[str], Awaitable[None]] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._images = cache_dir / "imagens"
        self._alerts = alerts_dir
        self.hub = hub or CameraHub()
        self.models = models or Models()
        self._on_alert = on_alert
        self._on_gesture = on_gesture
        self._clock = clock
        self._watch_task: asyncio.Task | None = None
        self._watch_until = 0.0
        self._gesture_task: asyncio.Task | None = None
        self.alerts = 0
        self.last_gesture = ""
        from services.face_book import FaceBook

        self.faces = FaceBook()  # quem é quem: rostos cadastrados pelo Chefe

    def image_path(self, name: str) -> Path | None:
        if not IMAGE_RE.fullmatch(str(name or "")):
            return None
        path = self._images / name
        return path if path.exists() else None

    async def look(self) -> dict[str, Any]:
        await self.models.ensure("efficientdet_lite0.tflite")
        frame = await self.hub.snapshot("olhar")
        if frame is None:
            return {"ok": False, "acao": "olhar", "erro": self.hub.error or "A webcam não mandou imagem."}
        detections = await asyncio.to_thread(self.models.detect, frame)
        name = f"cam-{secrets.token_hex(5)}.jpg"
        await asyncio.to_thread(annotate_and_save, frame, detections, self._images, name)
        try:
            seen_faces = await asyncio.to_thread(self.faces.identify, frame)
        except Exception as exc:  # noqa: BLE001 - rosto é extra: o resto da olhada continua
            logger.info("CAMERA_FACES_FAILED reason=%s", type(exc).__name__)
            seen_faces = []
        facts = {"ok": True, "acao": "olhar", **summarize([(label, score) for label, score, _ in detections]),
                 "rostos_conhecidos": [f["nome"] for f in seen_faces if f["nome"]],
                 "rostos_desconhecidos": sum(1 for f in seen_faces if not f["nome"]),
                 "hora": datetime.now().strftime("%H:%M"), "imagem": name, "url": f"/api/vision/image/{name}",
                 "brilho": round(float(frame.mean()), 1)}
        logger.info("CAMERA_LOOK people=%d objects=%d", facts["pessoas"], sum(facts["objetos"].values()))
        return facts

    # ---- vigia ----------------------------------------------------------------------------------------------------
    @property
    def watching(self) -> bool:
        return self._watch_task is not None and not self._watch_task.done()

    async def start_watch(self, minutes: int = 0) -> dict[str, Any]:
        await self.models.ensure("efficientdet_lite0.tflite")
        minutes = max(1, min(int(minutes or 0) or 12 * 60, 24 * 60))
        self._watch_until = self._clock() + minutes * 60
        if not self.watching:
            self.hub.acquire("vigia")
            self._watch_task = asyncio.ensure_future(self._watch_loop())
        logger.info("CAMERA_WATCH_ON minutes=%d", minutes)
        return {"ok": True, "acao": "vigiar", "minutos": minutes}

    async def stop_watch(self) -> dict[str, Any]:
        was = self.watching
        if self._watch_task is not None:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except (asyncio.CancelledError, Exception):
                pass
        self._watch_task = None
        self.hub.release("vigia")
        logger.info("CAMERA_WATCH_OFF alerts=%d", self.alerts)
        return {"ok": True, "acao": "parar_vigia", "estava_ligado": was, "avisos": self.alerts}

    async def _watch_loop(self) -> None:
        seen = 0
        last_alert = float("-inf")
        last_person = float("-inf")
        last_frame_at = 0.0
        try:
            while self._clock() < self._watch_until:
                await asyncio.sleep(WATCH_INTERVAL_S)
                if self.hub.error:
                    logger.info("CAMERA_WATCH_STOPPED reason=camera")
                    break
                frame = self.hub.latest(newer_than=last_frame_at)
                if frame is None:
                    continue
                last_frame_at = time.monotonic()
                detections = await asyncio.to_thread(self.models.detect, frame)
                people = [item for item in detections if item[0] == "person" and item[1] >= SCORE_MIN]
                now = self._clock()
                if not people:
                    if now - last_person > WATCH_GONE_S:
                        seen = 0
                    continue
                last_person = now
                seen += 1
                if seen < WATCH_CONFIRM_FRAMES or now - last_alert < WATCH_REALERT_S:
                    continue
                last_alert = now
                self.alerts += 1
                stamp = datetime.now()
                name = f"vigia-{stamp.strftime('%Y%m%d-%H%M%S')}.jpg"
                try:
                    path = await asyncio.to_thread(annotate_and_save, frame, detections, self._alerts, name, keep=None)
                except OSError as exc:
                    logger.info("CAMERA_WATCH_SAVE_FAILED reason=%s", type(exc).__name__)
                    continue
                try:
                    who = await asyncio.to_thread(self.faces.identify, frame)
                except Exception:  # noqa: BLE001 - sem rosto visível (de costas, longe): segue como pessoa
                    who = []
                facts = {"pessoas": len(people), "hora": stamp.strftime("%H:%M"), "arquivo": str(path),
                         "conhecidos": [f["nome"] for f in who if f["nome"]], "desconhecidos": sum(1 for f in who if not f["nome"])}
                logger.info("CAMERA_WATCH_ALERT people=%d", len(people))
                if self._on_alert is not None:
                    try:
                        await self._on_alert(facts, path)
                    except Exception as exc:  # aviso é extra: o vigia continua olhando
                        logger.info("CAMERA_WATCH_ALERT_FAILED reason=%s", type(exc).__name__)
        finally:
            self.hub.release("vigia")

    # ---- gestos ---------------------------------------------------------------------------------------------------
    @property
    def gestures_on(self) -> bool:
        return self._gesture_task is not None and not self._gesture_task.done()

    async def start_gestures(self) -> dict[str, Any]:
        await self.models.ensure("gesture_recognizer.task")
        if not self.gestures_on:
            self.hub.acquire("gestos")
            self._gesture_task = asyncio.ensure_future(self._gesture_loop())
        logger.info("CAMERA_GESTURES_ON")
        return {"ok": True, "acao": "gestos", "gestos": [spoken for _, spoken, _ in GESTURES.values()]}

    async def stop_gestures(self) -> dict[str, Any]:
        was = self.gestures_on
        if self._gesture_task is not None:
            self._gesture_task.cancel()
            try:
                await self._gesture_task
            except (asyncio.CancelledError, Exception):
                pass
        self._gesture_task = None
        self.hub.release("gestos")
        logger.info("CAMERA_GESTURES_OFF")
        return {"ok": True, "acao": "parar_gestos", "estava_ligado": was}

    async def _gesture_loop(self) -> None:
        debounce = GestureDebounce(self._clock)
        last_used = self._clock()
        last_frame_at = 0.0
        try:
            while self._clock() - last_used < GESTURE_IDLE_OFF_S:
                await asyncio.sleep(GESTURE_INTERVAL_S)
                if self.hub.error:
                    break
                frame = self.hub.latest(newer_than=last_frame_at)
                if frame is None:
                    continue
                last_frame_at = time.monotonic()
                name, score = await asyncio.to_thread(self.models.gesture, frame)
                fired = debounce.feed(name if score >= GESTURE_MIN_SCORE else None)
                if fired is None:
                    continue
                last_used = self._clock()
                self.last_gesture = GESTURES[fired][1]
                logger.info("CAMERA_GESTURE name=%s action=%s", fired, GESTURES[fired][0])
                if self._on_gesture is not None:
                    try:
                        await self._on_gesture(GESTURES[fired][0])
                    except Exception as exc:
                        logger.info("CAMERA_GESTURE_ACTION_FAILED reason=%s", type(exc).__name__)
        finally:
            self.hub.release("gestos")

    def state(self) -> dict[str, Any]:
        return {"ok": True, "acao": "estado", "vigia": self.watching, "gestos": self.gestures_on, "avisos_do_vigia": self.alerts,
                "ultimo_gesto": self.last_gesture, "camera_aberta": self.hub.in_use}

    async def enroll_face(self, name: str) -> dict[str, Any]:
        """Algumas fotos seguidas da pessoa olhando para a câmera viram a impressão do rosto dela."""
        frames = []
        self.hub.acquire("rosto")
        try:
            started = time.monotonic()
            last = started
            while len(frames) < ENROLL_SHOTS and time.monotonic() - started < 12:
                frame = self.hub.latest(newer_than=last)
                if frame is not None:
                    frames.append(frame)
                    last = time.monotonic()
                    await asyncio.sleep(0.4)
                elif self.hub.error:
                    break
                else:
                    await asyncio.sleep(0.05)
        finally:
            self.hub.release("rosto")
        if not frames:
            return {"ok": False, "acao": "cadastrar_rosto", "erro": self.hub.error or "A webcam não mandou imagem."}
        return {"acao": "cadastrar_rosto", **(await asyncio.to_thread(self.faces.enroll, name, frames))}

    async def ask(self, action: str, minutes: int = 0, name: str = "") -> dict[str, Any]:
        try:
            if action == "olhar":
                return await self.look()
            if action == "vigiar":
                return await self.start_watch(minutes)
            if action == "parar_vigia":
                return await self.stop_watch()
            if action == "gestos":
                return await self.start_gestures()
            if action == "parar_gestos":
                return await self.stop_gestures()
            if action == "cadastrar_rosto":
                return await self.enroll_face(name)
            if action == "esquecer_rosto":
                return {"ok": self.faces.forget(name), "acao": action, "nome": name}
            if action == "rostos":
                return {"ok": True, "acao": action, "conhecidos": self.faces.names()}
            return self.state()
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            logger.info("CAMERA_ASK_FAILED action=%s reason=%s", action, type(exc).__name__)
            return {"ok": False, "acao": action, "erro": "A câmera ou o modelo de visão não respondeu agora."}

    async def close(self) -> None:
        await self.stop_watch()
        await self.stop_gestures()


def narration(facts: dict[str, Any]) -> str:
    """O que a Kira fala: só os fatos que voltaram."""
    action = facts.get("acao")
    if not facts.get("ok"):
        return f"Não deu: {facts.get('erro') or 'a câmera não respondeu'}. Diga isso ao Chefe."
    if action == "olhar":
        objects = ", ".join(f"{count} {name}" for name, count in list(facts["objetos"].items())[:8]) or "nada que eu reconheça"
        dark = " A imagem está bem escura." if facts.get("brilho", 100) < 35 else ""
        known = facts.get("rostos_conhecidos") or []
        who = (f" Reconheci: {', '.join(known)}." if known else "") + (
            f" {facts['rostos_desconhecidos']} rosto(s) que eu não conheço." if facts.get("rostos_desconhecidos") else "")
        return (f"Câmera às {facts['hora']}: {facts['pessoas']} pessoa(s);{who} objetos: {objects}.{dark} A foto está na tela. "
                "A detecção é automática e pode errar: diga isso se não tiver certeza. Daqui a pouco a própria imagem "
                "chega na conversa: se o Chefe perguntar detalhes, olhe a imagem.")
    if action == "vigiar":
        return (f"Vigia ligado por até {facts['minutos']} minutos: olho a câmera 1 vez por segundo; se aparecer alguém, "
                "aviso falando, no sininho e nos destinos de aviso cadastrados, e guardo a foto em Vídeos, Kira Segurança.")
    if action == "parar_vigia":
        return f"Vigia desligado. Avisos dados: {facts.get('avisos', 0)}." if facts.get("estava_ligado") else "O vigia já estava desligado."
    if action == "gestos":
        return "Controle por gestos ligado. " + "; ".join(facts["gestos"]) + ". Desliga sozinho depois de 1 hora sem gesto."
    if action == "parar_gestos":
        return "Controle por gestos desligado." if facts.get("estava_ligado") else "O controle por gestos já estava desligado."
    if action == "cadastrar_rosto":
        return (f"Rosto de {facts['nome']} cadastrado ({facts['fotos']} fotos). Agora eu reconheço {facts['nome']} pela câmera."
                if facts.get("nome") else f"Não deu para cadastrar: {facts.get('message') or facts.get('erro')}")
    if action == "esquecer_rosto":
        return "Rosto apagado." if facts.get("ok") else "Não tenho esse rosto cadastrado."
    if action == "rostos":
        return "Rostos que eu conheço: " + (", ".join(facts["conhecidos"]) or "nenhum ainda") + "."
    on = [name for name, value in (("vigia", facts.get("vigia")), ("gestos", facts.get("gestos"))) if value]
    return f"Ligados agora: {', '.join(on) or 'nada'}. Avisos do vigia: {facts.get('avisos_do_vigia', 0)}."


def build_presentation(facts: dict[str, Any], request_id: str) -> dict[str, Any] | None:
    if facts.get("acao") != "olhar" or not facts.get("ok"):
        return None
    objects = ", ".join(f"{count} {name}" for name, count in facts["objetos"].items()) or "nenhum reconhecido"
    return {
        "type": "present", "presentationId": "camera-kira", "requestId": request_id, "title": f"Câmera às {facts['hora']}",
        "status": "ready",
        "sections": [
            {"id": "foto", "type": "image_gallery", "heading": "Agora", "status": "ready",
             "data": {"items": [{"imageUrl": facts["url"], "caption": "Webcam do PC · detecção automática (MediaPipe)"}]}},
            {"id": "resumo", "type": "text", "heading": "O que apareceu", "status": "ready",
             "data": {"points": [f"Pessoas: {facts['pessoas']}"]
                      + ([f"Reconhecidos: {', '.join(facts['rostos_conhecidos'])}"] if facts.get("rostos_conhecidos") else [])
                      + ([f"Rostos desconhecidos: {facts['rostos_desconhecidos']}"] if facts.get("rostos_desconhecidos") else [])
                      + [f"Objetos: {objects}"]}},
        ],
        "sources": [],
    }


__all__ = ["ACTIONS", "CameraHub", "CameraWatch", "GESTURES", "GestureDebounce", "Models", "annotate_and_save",
           "build_presentation", "label_pt", "narration", "summarize"]
