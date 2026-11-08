"""Rostos conhecidos da Kira: acha rostos na imagem da webcam (YuNet) e diz de quem são (SFace), tudo na CPU.

Cadastro: "Kira, cadastra o rosto do Breno" tira algumas fotos e guarda a impressão do rosto (só números, sem foto)
em configs/face_profiles/faces.json. Reconhecer: cada rosto da imagem é comparado com as impressões guardadas; acima
de MATCH_MIN é a pessoa, abaixo é "desconhecido". Modelos do opencv_zoo em models/vision (fora do git).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("kira.app.faces")

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "models" / "vision"
DETECTOR = "face_detection_yunet_2023mar.onnx"
RECOGNIZER = "face_recognition_sface_2021dec.onnx"
MODEL_URLS = {
    DETECTOR: "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    RECOGNIZER: "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}
BOOK = ROOT / "configs" / "face_profiles" / "faces.json"
# SFace (cosseno): o opencv_zoo indica 0,363 como corte para "mesma pessoa"; um pouco acima para errar menos de nome
MATCH_MIN = 0.40
DETECT_MIN_SCORE = 0.8
MIN_FACE_PX = 48
MAX_SAMPLES = 30


class FaceBook:
    def __init__(self, path: Path = BOOK, model_dir: Path = MODEL_DIR) -> None:
        self._path = Path(path)
        self._models = Path(model_dir)
        self._lock = threading.Lock()
        self._detector = None
        self._recognizer = None
        self.people: dict[str, list[list[float]]] = {}
        self._load()

    # ---------- modelos e arquivo ----------
    def ensure_models(self) -> None:
        import httpx

        self._models.mkdir(parents=True, exist_ok=True)
        for name, url in MODEL_URLS.items():
            path = self._models / name
            if not path.exists():
                response = httpx.get(url, follow_redirects=True, timeout=180)
                response.raise_for_status()
                path.write_bytes(response.content)

    def _nets(self):
        import cv2

        if self._detector is None:
            self.ensure_models()
            self._detector = cv2.FaceDetectorYN.create(str(self._models / DETECTOR), "", (320, 320), DETECT_MIN_SCORE, 0.3, 50)
            self._recognizer = cv2.FaceRecognizerSF.create(str(self._models / RECOGNIZER), "")
        return self._detector, self._recognizer

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self.people = {str(k): v for k, v in (data.get("people") or {}).items() if isinstance(v, list)}
        except (OSError, ValueError):
            self.people = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"people": self.people}), encoding="utf-8")
        tmp.replace(self._path)

    # ---------- rostos ----------
    def faces(self, frame: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
        """[(caixa YuNet, impressão do rosto)], maiores primeiro."""
        detector, recognizer = self._nets()
        with self._lock:
            height, width = frame.shape[:2]
            detector.setInputSize((width, height))
            _, found = detector.detect(frame)
            out = []
            for face in (found if found is not None else []):
                if min(face[2], face[3]) < MIN_FACE_PX:
                    continue
                aligned = recognizer.alignCrop(frame, face)
                out.append((face, recognizer.feature(aligned).flatten().astype(np.float32)))
        return sorted(out, key=lambda item: -(item[0][2] * item[0][3]))

    def _match(self, feature: np.ndarray) -> tuple[str, float]:
        best_name, best = "", -1.0
        unit = feature / (np.linalg.norm(feature) + 1e-9)
        for name, samples in self.people.items():
            matrix = np.asarray(samples, dtype=np.float32)
            matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
            score = float(np.max(matrix @ unit))
            if score > best:
                best_name, best = name, score
        return best_name, best

    def identify(self, frame: np.ndarray) -> list[dict[str, Any]]:
        """Quem está na imagem: [{nome ('' = desconhecido), confianca, caixa}]."""
        people = []
        for box, feature in self.faces(frame):
            name, score = self._match(feature) if self.people else ("", 0.0)
            people.append({"nome": name if score >= MATCH_MIN else "", "confianca": round(max(score, 0.0), 2),
                           "caixa": [int(v) for v in box[:4]]})
        return people

    def enroll(self, name: str, frames: list[np.ndarray]) -> dict[str, Any]:
        """Guarda o rosto de uma pessoa (o maior rosto de cada foto). Pede pelo menos 2 fotos com rosto."""
        clean = " ".join(str(name or "").split()).strip().title()[:40]
        if not clean:
            return {"ok": False, "message": "Diga o nome da pessoa."}
        features = [faces[0][1] for faces in (self.faces(frame) for frame in frames) if faces]
        if len(features) < 2:
            return {"ok": False, "message": "Não vi um rosto claro na câmera. Peça para a pessoa olhar para a câmera, com luz no rosto."}
        samples = (self.people.get(clean, []) + [f.tolist() for f in features])[-MAX_SAMPLES:]
        self.people[clean] = samples
        self._save()
        logger.info("FACE_ENROLLED samples=%d", len(samples))
        return {"ok": True, "nome": clean, "fotos": len(features), "total_amostras": len(samples)}

    def forget(self, name: str) -> bool:
        key = next((k for k in self.people if k.lower() == str(name or "").strip().lower()), None)
        if key is None:
            return False
        del self.people[key]
        self._save()
        return True

    def names(self) -> list[str]:
        return sorted(self.people)


__all__ = ["FaceBook", "MATCH_MIN"]
