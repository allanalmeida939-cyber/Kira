"""Quem está falando (o dono, alguém conhecido ou uma voz desconhecida) e o que cada um pode pedir à Kira.

Impressão de voz pelo VoiceBook (TitaNet). Tolerante com o dono: voz em dúvida conta como dele se ele foi confirmado
nos últimos 10 minutos; trecho curto não muda o veredito; amostras aceitas com folga entram no perfil (a voz grossa,
fina ou gripada vai sendo aprendida). Voz desconhecida que insiste em comandos sobe de nível: pergunta quem é, fica
agressiva, liga a webcam e grava, e por fim bloqueia o Windows. Sem a voz do dono cadastrada o guarda fica desligado.
O cadastro é gravado pela barra de progresso (runtime/voice_enrollment.py) e salvo aqui, só com a voz principal.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

from runtime.voice_book import MIN_AGREEMENT, agreement, consistent

logger = logging.getLogger(__name__)

OWNER_NAME = os.getenv("KIRA_OWNER_NAME", "Chefe").strip() or "Chefe"
SAMPLE_RATE = 16000
MIN_AUDIO_SECONDS = 0.8  # "sim", "não": mantém o veredito anterior
# TitaNet em fala real (LibriSpeech): com 0,40 passam 0,2-0,5% das outras pessoas e o dono é recusado 0,6-3,5%.
# Pelo microfone da Kira (processamento da chamada) todas as vozes sobem ~0,1: outra voz chegou a 0,44, o dono a
# 0,71-0,95. Por isso 0,45 (ajustável).
OWNER_MIN_SIMILARITY = float(os.getenv("KIRA_OWNER_MIN_SIMILARITY", "0.45"))
# Entre isto e o mínimo: dúvida (voz grossa, gripada), vale como dono se ele foi confirmado há pouco.
DOUBT_FLOOR = OWNER_MIN_SIMILARITY - 0.10
TRUST_SECONDS = 10 * 60
# Trecho curto (tosse, risada, fim de frase cortado, alguém passando) não desconfirma o dono ouvido há pouco. Medido na
# sessão de 2026-10-24: todos os "voz desconhecida" errados foram trechos de 0,8-1,4 s logo depois de o Chefe ser
# reconhecido, e um deles bloqueou o pedido de imagem que ele tinha acabado de fazer (4,7 s, nota 0,83).
SHORT_SECONDS = 1.5
SHORT_TRUST_SECONDS = 3 * 60
# Um único trecho ruim não deve bloquear um comando logo depois de várias confirmações
# do dono. Só vale quando o próprio perfil do dono continua sendo o melhor candidato,
# há pelo menos duas confirmações recentes e a semelhança ainda está acima deste piso.
OWNER_OUTLIER_MIN_SIMILARITY = float(os.getenv("KIRA_OWNER_OUTLIER_MIN_SIMILARITY", "0.20"))
OWNER_OUTLIER_WINDOW_SECONDS = float(os.getenv("KIRA_OWNER_OUTLIER_WINDOW_SECONDS", "120"))
OWNER_OUTLIER_MIN_CONFIRMATIONS = 2
LEARN_MIN_SIMILARITY = 0.55
LEARN_MIN_SECONDS = 1.5
LEARN_COOLDOWN_SECONDS = 30.0
VERDICT_TTL_SECONDS = 45.0
ASK_COOLDOWN_SECONDS = 10 * 60
# Cadastro: pelo menos 3 janelas boas e no mínimo metade da gravação com a mesma voz (o resto foi TV, outra pessoa).
ENROLL_MIN_WINDOWS = 3
ENROLL_MIN_CONSISTENT_SHARE = 0.5
# A própria voz da Kira voltando pelo microfone (eco): simulado, o eco dela fica a 0,25-0,43 da impressão da voz dela;
# pessoas reais, no máximo 0,19. Trecho assim não é ninguém falando: não muda o veredito nem pergunta "quem é?".
# Sessão de 2026-10-24: todo "voz desconhecida" com nota ~0,0-0,04 veio logo depois de a Kira falar.
OWN_VOICE_MIN_SIMILARITY = 0.25
STRIKE_WINDOW_SECONDS = 5 * 60
# Informação e exibição: qualquer voz pode pedir. Todo o resto (mensagens, arquivos, janelas, agenda, e-mail, saúde,
# lembretes, programas, downloads...) só o dono. Ferramenta nova fica bloqueada até alguém decidir que é inofensiva.
GUEST_TOOLS = frozenset(
    {
        "get_current_time", "get_weather", "get_news", "web_search", "show_images", "show_media", "show_videos",
        "plan_visuals", "recall_image", "focus_media", "close_panels", "show_scores_today", "show_live_score",
        "show_election", "get_ui_status", "list_active_panels", "control_music", "registrar_voz", "vozes_cadastradas",
    }
)
NAME_RE = re.compile(r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ' -]{1,29}$")


def clean_name(value: object) -> str:
    return " ".join(str(value or "").split()).strip(" .,!?")


def fold(value: object) -> str:
    return unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").strip().lower()


def voice_pitch_hz(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> float:
    """Frequência fundamental mediana (autocorrelação em janelas de 40 ms com voz); 0 se não deu para medir."""
    wave = np.asarray(audio, dtype=np.float32).reshape(-1)
    size = int(sample_rate * 0.04)
    low, high = int(sample_rate / 300), int(sample_rate / 70)  # 70-300 Hz cobre voz adulta
    pitches: list[float] = []
    for start in range(0, wave.size - size, size):
        frame = wave[start:start + size]
        frame = frame - frame.mean()
        energy = float(np.dot(frame, frame))
        if energy < 1e-4:
            continue
        corr = np.correlate(frame, frame, mode="full")[size - 1:]
        lag = low + int(np.argmax(corr[low:high]))
        if corr[lag] / energy <= 0.45:
            continue
        # erro de oitava (voz grave lida no dobro): o período dobrado vence só se correlacionar mais, por amostra
        while 2 * lag < high and corr[2 * lag] / (size - 2 * lag) > 1.1 * corr[lag] / (size - lag):
            lag *= 2
        pitches.append(sample_rate / lag)
    return float(np.median(pitches)) if len(pitches) >= 5 else 0.0


def voice_kind(audio: np.ndarray) -> str:
    pitch = voice_pitch_hz(audio)
    # Só afirma longe da zona ambígua: em fala real errou 4% assim (antes 10%, e chamava homem de "feminina").
    if not pitch:
        return ""
    if pitch < 130:
        return "masculina"
    return "feminina" if pitch > 190 else ""


@dataclass(frozen=True)
class Verdict:
    role: str  # "owner" | "known" | "unknown"
    name: str
    similarity: float
    kind: str
    at: float
    trusted: bool = False  # dono por tolerância (voz em dúvida logo depois de ter sido confirmado)


class SpeakerGuard:
    def __init__(self, book, *, owner_name: str = OWNER_NAME, clock: Callable[[], float] = time.monotonic, warm: bool = True,
                 on_label: Callable[[str], None] | None = None, on_escalate: Callable[[int], None] | None = None) -> None:
        self._book = book
        self._owner = fold(owner_name)
        self._owner_label = owner_name
        self._clock = clock
        self._on_label = on_label
        self._on_escalate = on_escalate
        self._lock = threading.Lock()
        self._verdict: Verdict | None = None
        self._asked_at = -ASK_COOLDOWN_SECONDS
        self._previous_asked_at = self._asked_at
        self._owner_seen_at = -TRUST_SECONDS
        self._recent_owner_confirmations: deque[float] = deque(maxlen=8)
        self._learned_at: dict[str, float] = {}
        self._strikes = 0
        self._strike_at = -STRIKE_WINDOW_SECONDS
        self._struck_verdict: Verdict | None = None
        self._own_voice: np.ndarray | None = None
        if warm and self.active:  # sem voz do dono cadastrada o modelo nem é carregado
            threading.Thread(target=self._warm, name="speaker-guard-warm", daemon=True).start()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._book, "enabled", False))

    def _owner_profile(self):
        return next((p for p in self._book.profiles() if fold(p.name) == self._owner), None) if self.enabled else None

    @property
    def active(self) -> bool:
        """Só protege depois que a voz do dono foi cadastrada."""
        return self._owner_profile() is not None

    def _warm(self) -> None:
        try:
            with self._lock:
                self._book.embed(np.random.default_rng(0).normal(0, 0.05, SAMPLE_RATE).astype(np.float32))
            logger.info("SPEAKER_GUARD_WARM")
        except Exception as exc:
            logger.warning("SPEAKER_GUARD_WARM_FAILED error=%s", exc)

    def set_own_voice(self, audio: np.ndarray) -> None:
        """Impressão da voz da própria Kira (do áudio que ela gera), para reconhecer o eco dela. Roda em thread."""
        if not self.enabled:
            return
        with self._lock:
            self._own_voice = self._book.embed(audio)
        logger.info("SPEAKER_OWN_VOICE_SET seconds=%.1f", audio.size / SAMPLE_RATE)

    def _own_voice_score(self, embedding: np.ndarray) -> float:
        own = self._own_voice
        return float(np.dot(embedding, own)) if own is not None else 0.0

    def _remember_owner_confirmation(self, now: float) -> None:
        self._recent_owner_confirmations.append(now)
        self._recent_owner_confirmation_count(now)

    def _recent_owner_confirmation_count(self, now: float) -> int:
        cutoff = now - OWNER_OUTLIER_WINDOW_SECONDS
        while self._recent_owner_confirmations and self._recent_owner_confirmations[0] < cutoff:
            self._recent_owner_confirmations.popleft()
        return len(self._recent_owner_confirmations)

    def identify(self, audio: np.ndarray) -> Verdict | None:
        """Roda em thread. Áudio curto demais não muda o veredito anterior."""
        seconds = audio.size / SAMPLE_RATE
        if not self.active or seconds < MIN_AUDIO_SECONDS:
            return None
        now = self._clock()
        with self._lock:
            embedding = self._book.embed(audio)
            ranked = self._book.scores(embedding)
        owner = next(((p, s) for p, s in ranked if fold(p.name) == self._owner), (None, 0.0))
        best, best_score = ranked[0]
        owner_score = owner[1]
        own_score = self._own_voice_score(embedding)
        if own_score >= OWN_VOICE_MIN_SIMILARITY and own_score > best_score + 0.05:
            logger.info("SPEAKER_OWN_VOICE_IGNORED own=%.3f best=%s:%.3f seconds=%.1f", own_score, best.name, best_score, seconds)
            return None
        trusted = False
        if owner_score >= OWNER_MIN_SIMILARITY and owner_score >= best_score - 0.05:
            role, name, learn = "owner", owner[0].name, owner[0]
            self._owner_seen_at = now
            self._remember_owner_confirmation(now)
            self._strikes = 0
        elif fold(best.name) != self._owner and best_score >= OWNER_MIN_SIMILARITY and best_score > owner_score + 0.05:
            role, name, learn = "known", best.name, best
        elif owner_score >= DOUBT_FLOOR and now - self._owner_seen_at <= TRUST_SECONDS:
            role, name, learn, trusted = "owner", owner[0].name, None, True
        elif seconds < SHORT_SECONDS and owner[0] is not None and now - self._owner_seen_at <= SHORT_TRUST_SECONDS:
            role, name, learn, trusted = "owner", owner[0].name, None, True
        elif (
            owner[0] is not None
            and fold(best.name) == self._owner
            and owner_score >= OWNER_OUTLIER_MIN_SIMILARITY
            and now - self._owner_seen_at <= OWNER_OUTLIER_WINDOW_SECONDS
            and self._recent_owner_confirmation_count(now) >= OWNER_OUTLIER_MIN_CONFIRMATIONS
        ):
            # Fallback estreito para uma janela degradada: não aceita outra pessoa só porque
            # o dono foi ouvido antes; o perfil do dono precisa continuar em primeiro lugar.
            role, name, learn, trusted = "owner", owner[0].name, None, True
            logger.info(
                "SPEAKER_OWNER_OUTLIER_TRUSTED owner_score=%.3f confirmations=%d window_s=%.1f",
                owner_score,
                self._recent_owner_confirmation_count(now),
                now - self._owner_seen_at,
            )
        else:
            role, name, learn = "unknown", "", None
        score = owner_score if role == "owner" else best_score
        # Acúmulo automático até 1000: toda fala CLARAMENTE do dono vira amostra (inclusive os casos "trusted" que antes
        # não aprendiam), desde que a semelhança seja alta. Assim o perfil cobre todos os tons com o tempo.
        if learn is None and role == "owner" and owner[0] is not None and owner_score >= OWNER_MIN_SIMILARITY:
            learn = owner[0]
        if learn is not None and score >= LEARN_MIN_SIMILARITY and seconds >= LEARN_MIN_SECONDS and now - self._learned_at.get(learn.profile_id, -LEARN_COOLDOWN_SECONDS) >= LEARN_COOLDOWN_SECONDS:
            self._learned_at[learn.profile_id] = now
            learned_profile = self._book.learn(learn, embedding)
            total = getattr(learned_profile, "samples_count", None)
            logger.info("SPEAKER_SAMPLE_LEARNED name=%s total=%s/1000", learn.name, total)
        verdict = Verdict(role=role, name=name, similarity=float(score), kind="" if role != "unknown" else voice_kind(audio), at=now, trusted=trusted)
        self._verdict = verdict
        logger.info("SPEAKER_VERDICT role=%s name=%s owner_score=%.3f best=%s:%.3f trusted=%s kind=%s seconds=%.1f",
                    role, name or "-", owner_score, best.name, best_score, trusted, verdict.kind or "-", seconds)
        if self._on_label is not None:
            self._on_label(self.label(verdict))
        return verdict

    @staticmethod
    def label(verdict: Verdict) -> str:
        return verdict.name if verdict.role in {"owner", "known"} else "desconhecida"

    def current(self) -> Verdict | None:
        verdict = self._verdict
        if verdict is None or self._clock() - verdict.at > VERDICT_TTL_SECONDS:
            return None
        return verdict

    def blocks(self, tool_name: str, *, typed: bool) -> Verdict | None:
        """Veredito que barra a ferramenta, ou None se pode executar. Texto digitado é de quem está no teclado."""
        if typed or tool_name in GUEST_TOOLS or not self.active:
            return None
        verdict = self.current()
        if verdict is None or verdict.role == "owner":
            return None
        return verdict

    def note_blocked(self, verdict: Verdict) -> int:
        """Mais um comando barrado: devolve o nível (1 a 4). Câmera no 3, bloqueio do Windows no 4.

        Conta uma vez por fala: ferramentas que o Gemini chama na mesma resposta (chegou a tentar ligar a webcam
        ele mesmo) não são insistência da pessoa e levavam direto ao bloqueio do Windows.
        """
        if verdict is self._struck_verdict:
            return self._strikes
        self._struck_verdict = verdict
        now = self._clock()
        if now - self._strike_at > STRIKE_WINDOW_SECONDS:
            self._strikes = 0
        self._strike_at = now
        self._strikes = min(self._strikes + 1, 4) if verdict.role == "unknown" else 1
        level = self._strikes
        logger.info("SPEAKER_STRIKE level=%d role=%s", level, verdict.role)
        if level >= 3 and self._on_escalate is not None:
            self._on_escalate(level)
        return level

    def refusal(self, verdict: Verdict, level: int = 1) -> str:
        if verdict.role == "known":
            return (f"BLOQUEADO: a voz deste pedido é de {verdict.name} (conhecida, mas não é o {self._owner_label}); só o "
                    f"{self._owner_label} pode pedir ações. Não execute nem diga que fez. Explique em uma frase.")
        kind = f" Parece uma voz {verdict.kind}." if verdict.kind else ""
        base = (f"BLOQUEADO: voz desconhecida; só o {self._owner_label} pode pedir ações.{kind} Não execute nem diga que fez. "
                f"Não chame a pessoa de {self._owner_label}. Se ela disser o nome, chame registrar_voz. ")
        steps = {
            1: "Diga que só obedece ao Chefe e pergunte, do seu jeito, quem está falando.",
            2: ("É a segunda tentativa: seja dura e agressiva de verdade, sem brincadeira. Diga que já avisou, que não "
                "obedece estranho e exija saber quem é, agora."),
            3: ("Terceira tentativa: o sistema JÁ LIGOU a câmera de segurança e está GRAVANDO a pessoa (não chame "
                "nenhuma ferramenta). Diga isso, agressiva, que o Chefe vai ver quem mexeu no computador, e exija o nome."),
            4: ("Quarta tentativa: diga, curta e agressiva, que avisou e que está bloqueando o computador agora. "
                "O bloqueio já foi acionado pelo sistema (não chame nenhuma ferramenta)."),
        }
        return base + steps[max(1, min(level, 4))]

    def should_ask(self) -> Verdict | None:
        """Voz desconhecida conversando: pergunta quem é, no máximo uma vez a cada 10 minutos."""
        verdict = self.current()
        if verdict is None or verdict.role != "unknown" or not self.active:
            return None
        if self._clock() - self._asked_at < ASK_COOLDOWN_SECONDS:
            return None
        self._previous_asked_at, self._asked_at = self._asked_at, self._clock()
        return verdict

    def cancel_ask(self) -> None:
        """A pergunta não saiu (era ruído): a próxima voz desconhecida de verdade ainda pode ser perguntada."""
        self._asked_at = self._previous_asked_at

    def enroll_problem(self, name: str) -> str:
        """Antes de gravar: o motivo para não cadastrar este nome agora ('' = pode gravar)."""
        if not self.enabled:
            return "O reconhecimento de voz não está disponível neste computador (modelo de voz não encontrado)."
        if not NAME_RE.match(clean_name(name)):
            return "Nome inválido para cadastro de voz: peça só o primeiro nome da pessoa."
        return ""  # se a voz é mesmo do dono (ou não é), a gravação inteira decide em enroll()

    def enroll(self, name: str, audio: np.ndarray) -> tuple[bool, str]:
        """Salva a voz gravada pela barra de cadastro (roda em thread). Só entra a voz principal da gravação."""
        clean = clean_name(name).title()
        problem = self.enroll_problem(clean)
        if problem:
            return False, problem
        seconds = audio.size / SAMPLE_RATE
        with self._lock:
            windows = self._book.window_embeddings(audio)
        # eco da Kira não entra no cadastro de ninguém
        windows = [w for w in windows if self._own_voice_score(w) < OWN_VOICE_MIN_SIMILARITY]
        is_owner = fold(clean) == self._owner
        owner = self._owner_profile()
        enough = max(ENROLL_MIN_WINDOWS, ENROLL_MIN_CONSISTENT_SHARE * len(windows))
        if is_owner and owner is not None:
            # Recadastro do dono: vale a voz que confere com o perfil que ele já tem (com a folga da dúvida, para a voz
            # grossa, fina ou rouca entrar); janela de outra pessoa fica de fora.
            kept = [w for w in windows if owner.score(w) >= DOUBT_FLOOR]
            if len(kept) < enough:
                logger.info("SPEAKER_ENROLL_OWNER_MISMATCH windows=%d kept=%d", len(windows), len(kept))
                return False, f"Recusado: esta voz não confere com a do {self._owner_label} já cadastrada. Não cadastre."
        else:
            kept = consistent(windows)
        if len(kept) < enough or agreement(kept) < MIN_AGREEMENT:
            logger.info("SPEAKER_ENROLL_INCONSISTENT name=%s windows=%d kept=%d agreement=%.2f", clean, len(windows),
                        len(kept), agreement(kept))
            return False, ("A gravação pegou mais de uma voz ou barulho demais (TV, outra pessoa falando junto). Peça "
                           "para repetir num lugar mais quieto, só a pessoa falando.")
        sample = np.mean(np.stack(kept), axis=0)
        sample /= np.linalg.norm(sample) + 1e-9
        if owner is not None:
            owner_score = owner.score(sample)
            if is_owner and owner_score < OWNER_MIN_SIMILARITY:
                return False, f"Recusado: esta voz não confere com a do {self._owner_label} já cadastrada. Não cadastre."
            if not is_owner and owner_score >= OWNER_MIN_SIMILARITY:
                return False, f"Recusado: esta é a voz do {self._owner_label}; não dá para cadastrá-la como {clean}."
        with self._lock:
            profile = self._book.enroll(clean, kept)
        logger.info("SPEAKER_REGISTERED name=%s samples=%d seconds=%.1f windows=%d kept=%d", profile.name,
                    profile.samples_count, seconds, len(windows), len(kept))
        now = self._clock()
        if is_owner:
            self._owner_seen_at = now
            self._remember_owner_confirmation(now)
            self._strikes = 0
        self._verdict = Verdict(role="owner" if is_owner else "known", name=profile.name, similarity=1.0, kind="", at=now)
        if self._on_label is not None:
            self._on_label(profile.name)
        if not is_owner:
            return True, f"Voz de {profile.name} salva. A Kira reconhece {profile.name} pelo nome, mas ações continuam só com o {self._owner_label}."
        return True, (f"Voz do {profile.name} salva ({seconds:.0f} s de fala, {len(kept)} amostras boas). Agora só ele dá "
                      "comandos por voz, e a Kira vai aprendendo a voz dele no uso (grossa, fina, gripado).")

    def known_voices(self) -> str:
        names = [p.name for p in self._book.profiles()] if self.enabled else []
        if not names:
            return "Nenhuma voz cadastrada ainda."
        owner = " Comandos: só o " + self._owner_label + "." if self.active else f" A voz do {self._owner_label} ainda não foi cadastrada: por enquanto qualquer voz pode pedir tudo."
        return "Vozes cadastradas: " + ", ".join(names) + "." + owner


__all__ = ["GUEST_TOOLS", "SpeakerGuard", "Verdict", "clean_name", "voice_kind", "voice_pitch_hz"]
