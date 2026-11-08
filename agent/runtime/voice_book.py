"""Vozes conhecidas da Kira: impressão de voz (TitaNet, via sherpa-onnx) e perfis com várias amostras.

Avaliado em fala real (Mini LibriSpeech, cadastro de 15 s): com trechos de 1 s o dono é recusado 1,5% das vezes
deixando passar só 1% de outras vozes (o Resemblyzer anterior: 9%); com ruído 1,3% (antes 49%). Roda em CPU:
~50 ms por segundo de fala, sem PyTorch. O modelo fica em models/speaker (não vai para o git); baixar de
https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
AGENT_DIR = Path(__file__).resolve().parents[1]
MODEL_FILE = Path(os.getenv("KIRA_SPEAKER_MODEL") or (AGENT_DIR.parent / "models" / "speaker" / "nemo_en_titanet_small.onnx"))
BOOK_FILE = AGENT_DIR.parent / "configs" / "voice_profiles" / "voicebook.json"
# Cada pessoa guarda as amostras do cadastro (fixas) e as que a Kira aprende no uso (as mais antigas saem primeiro).
MAX_EMBEDDINGS = 1000  # 2026-11-05: Chefe quer até 1000 amostras acumulando (todos os tons/situações = menos erro)
ENROLL_WINDOW_SECONDS = 3.0
ENROLL_STEP_SECONDS = 2.0
# Cadastro de 15 s em fala real (Mini LibriSpeech): cada janela da mesma pessoa fica a 0,79-0,95 do conjunto; janelas
# de outra voz misturada (TV, alguém do lado, eco) ficam a 0,23-0,50. Abaixo de 0,65 a janela não entra.
CONSISTENT_MIN_SIMILARITY = 0.65
# Média entre as janelas que ficaram: uma pessoa lendo dá 0,70-0,85; falando de jeitos diferentes (alto, baixo,
# rápido) cai até 0,53 na simulação e 0,58 no cadastro real do Chefe (2026-10-24); duas vozes meio a meio que o filtro
# não separa ficam em 0,43. Com mistura desigual, o filtro acima já tira a voz menor.
MIN_AGREEMENT = 0.5


@dataclass
class VoiceProfile:
    profile_id: str
    name: str
    embeddings: list[np.ndarray] = field(default_factory=list)
    pinned: int = 0  # as primeiras `pinned` vieram do cadastro e nunca saem

    @property
    def samples_count(self) -> int:
        return len(self.embeddings)

    def score(self, embedding: np.ndarray) -> float:
        """Maior entre o perfil médio e a média das 3 amostras mais parecidas (pega a voz em dias diferentes)."""
        if not self.embeddings:
            return 0.0
        matrix = np.stack(self.embeddings)
        centroid = matrix.mean(axis=0)
        centroid /= np.linalg.norm(centroid) + 1e-9
        sims = np.sort(matrix @ embedding)[::-1]
        return float(max(centroid @ embedding, sims[:3].mean()))


def _normalize(vector) -> np.ndarray:
    array = np.asarray(vector, dtype=np.float32).reshape(-1)
    return array / (np.linalg.norm(array) + 1e-9)


class VoiceBook:
    def __init__(self, path: Path = BOOK_FILE, model: Path = MODEL_FILE, *, extractor=None) -> None:
        self._path = Path(path)
        self._model = Path(model)
        self._extractor = extractor
        self._lock = threading.Lock()
        self._profiles: dict[str, VoiceProfile] = {}
        self._load()

    @property
    def enabled(self) -> bool:
        return self._extractor is not None or self._model.is_file()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in data.get("profiles") or []:
            profile = VoiceProfile(
                profile_id=str(item["id"]), name=str(item["name"]),
                embeddings=[_normalize(e) for e in item.get("embeddings") or []], pinned=int(item.get("pinned") or 0),
            )
            self._profiles[profile.profile_id] = profile

    def _save(self) -> None:
        payload = {"version": 1, "model": self._model.name, "profiles": [
            {"id": p.profile_id, "name": p.name, "pinned": p.pinned, "embeddings": [np.round(e, 5).tolist() for e in p.embeddings]}
            for p in self._profiles.values()
        ]}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self._path)

    def _get_extractor(self):
        if self._extractor is None:
            import sherpa_onnx

            started = time.monotonic()
            config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(self._model), num_threads=1)
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
            logger.info("VOICE_BOOK_MODEL_LOADED model=%s seconds=%.1f", self._model.name, time.monotonic() - started)
        return self._extractor

    def embed(self, audio: np.ndarray) -> np.ndarray:
        extractor = self._get_extractor()
        stream = extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, np.asarray(audio, dtype=np.float32).reshape(-1))
        stream.input_finished()
        return _normalize(extractor.compute(stream))

    def profiles(self) -> list[VoiceProfile]:
        return list(self._profiles.values())

    def scores(self, embedding: np.ndarray) -> list[tuple[VoiceProfile, float]]:
        """Perfis do mais parecido para o menos parecido."""
        return sorted(((p, p.score(embedding)) for p in self._profiles.values()), key=lambda item: item[1], reverse=True)

    def window_embeddings(self, audio: np.ndarray) -> list[np.ndarray]:
        """Uma impressão por janela de 3 s (passo de 2 s) da fala do cadastro."""
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        size, step = int(SAMPLE_RATE * ENROLL_WINDOW_SECONDS), int(SAMPLE_RATE * ENROLL_STEP_SECONDS)
        windows = [audio[s:s + size] for s in range(0, max(1, audio.size - size + 1), step)] or [audio]
        return [self.embed(w) for w in windows]

    def enroll(self, name: str, new: list[np.ndarray]) -> VoiceProfile:
        """Guarda as impressões do cadastro (fixas: o aprendizado no uso nunca as tira)."""
        with self._lock:
            profile = next((p for p in self._profiles.values() if p.name.lower() == name.lower()), None)
            if profile is None:
                profile = VoiceProfile(profile_id=uuid.uuid4().hex[:8], name=name)
                self._profiles[profile.profile_id] = profile
            fixed, learned = profile.embeddings[:profile.pinned], profile.embeddings[profile.pinned:]
            fixed = (fixed + new)[-MAX_EMBEDDINGS:]
            profile.embeddings = fixed + learned[-max(0, MAX_EMBEDDINGS - len(fixed)):]
            profile.pinned = len(fixed)
            self._save()
        return profile

    def learn(self, profile: VoiceProfile, embedding: np.ndarray) -> VoiceProfile:
        """Amostra do uso, aceita com folga: acumula até MAX_EMBEDDINGS; cheio, substitui a aprendida mais antiga."""
        with self._lock:
            learned = profile.embeddings[profile.pinned:] + [_normalize(embedding)]
            profile.embeddings = profile.embeddings[:profile.pinned] + learned[-max(0, MAX_EMBEDDINGS - profile.pinned):]
            self._save()
            return profile


def consistent(embeddings: list[np.ndarray]) -> list[np.ndarray]:
    """Só as janelas da voz principal: tira as que destoam do conjunto (duas passadas, o centro sem as intrusas)."""
    if len(embeddings) < 2:
        return list(embeddings)
    matrix = np.stack(embeddings)
    keep = np.ones(len(embeddings), dtype=bool)
    for _ in range(2):
        centroid = _normalize(matrix[keep].mean(axis=0)) if keep.any() else _normalize(matrix.mean(axis=0))
        keep = matrix @ centroid >= CONSISTENT_MIN_SIMILARITY
    return [e for e, ok in zip(embeddings, keep) if ok]


def agreement(embeddings: list[np.ndarray]) -> float:
    """Semelhança média entre as janelas (1,0 com uma janela só)."""
    count = len(embeddings)
    if count < 2:
        return 1.0
    matrix = np.stack(embeddings)
    return float(((matrix @ matrix.T).sum() - count) / (count * (count - 1)))


__all__ = ["BOOK_FILE", "MIN_AGREEMENT", "MODEL_FILE", "SAMPLE_RATE", "VoiceBook", "VoiceProfile", "agreement", "consistent"]
