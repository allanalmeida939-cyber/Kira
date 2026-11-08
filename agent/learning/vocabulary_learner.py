"""
VocabularyLearner — aprende girias, palavras e correcoes de transcricao.
"""

import json
import logging
import re
import time
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).parent / "data" / "vocabulary.json"

SEED_SLANG = {
    "bora": "vamos",
    "botar": "colocar",
    "ta": "esta",
    "tá": "está",
    "ne": "nao e",
    "né": "não é",
    "pra": "para",
    "pro": "para o",
    "vc": "você",
    "blz": "beleza",
    "vlw": "valeu",
    "tmj": "tamo junto",
    "mano": "cara",
    "cara": "cara",
    "brother": "amigo",
    "bichao": "amigo",
    "bichão": "amigo",
    "show": "otimo",
    "massa": "otimo",
    "dahora": "otimo",
    "da hora": "otimo",
    "sinistro": "incrivel",
    "brabo": "incrivel",
    "zueira": "brincadeira",
    "zoar": "brincar",
    "saudade": "saudade",
    "firmeza": "certo",
    "suave": "tranquilo",
    "trampo": "trabalho",
    "rolar": "acontecer",
    "curtir": "gostar",
    "vibe": "clima",
    "bora la": "vamos la",
    "bora lá": "vamos lá",
    "boa": "otimo",
    "salvo": "salvo",
    "mandou bem": "foi otimo",
}


# Gírias da própria Kira (personalidade): ela solta uma de vez em quando, sem forçar.
KIRA_SLANG = ("bora", "suave", "deu bom", "deu ruim", "tô dentro", "sem crise", "mó", "partiu")


class VocabularyLearner:
    def __init__(self):
        self._data = self._load()
        self._session_words: dict[str, int] = defaultdict(int)

    def _load(self) -> dict:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        if DATA_FILE.exists():
            try:
                return json.loads(DATA_FILE.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("VOCAB_LOAD_FAILED error=%s", exc)
        return {
            "slang": dict(SEED_SLANG),
            "corrections": {},
            "personal": {},
            "ignored": [],
            "version": 1,
        }

    def _save(self) -> None:
        try:
            DATA_FILE.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("VOCAB_SAVE_FAILED error=%s", exc)

    def observe_transcript(self, text: str, source: str = "user") -> None:
        if source != "user":
            return
        words = re.findall(r"\b\w+\b", str(text or "").lower())
        for word in words:
            if len(word) < 3 or word in self._data.get("ignored", []):
                continue
            self._session_words[word] += 1
            if self._session_words[word] >= 3:
                self._consider_learning_word(word, str(text or ""))

    def _consider_learning_word(self, word: str, context: str) -> None:
        personal = self._data.setdefault("personal", {})
        if word in personal:
            personal[word]["count"] = int(personal[word].get("count", 0)) + 1
            personal[word]["last_seen"] = time.time()
        else:
            personal[word] = {
                "count": 1,
                "context": context[:100],
                "first_seen": time.time(),
                "last_seen": time.time(),
            }
            logger.info("VOCAB_NEW_WORD_LEARNED word=%r context=%r", word, context[:50])
        self._save()

    def record_correction(self, wrong: str, correct: str) -> None:
        wrong_n = str(wrong or "").strip().lower()
        correct_n = str(correct or "").strip().lower()
        if wrong_n and correct_n and wrong_n != correct_n:
            self._data.setdefault("corrections", {})[wrong_n] = correct_n
            logger.info("VOCAB_CORRECTION_LEARNED wrong=%r correct=%r", wrong_n, correct_n)
            self._save()

    def add_slang(self, slang: str, meaning: str) -> None:
        self._data.setdefault("slang", {})[str(slang or "").lower()] = str(meaning or "").lower()
        logger.info("VOCAB_SLANG_ADDED slang=%r meaning=%r", slang, meaning)
        self._save()

    def learn_slang(self, word: str, meaning: str, example: str = "") -> dict:
        """Gíria/palavra que o Chefe ensinou: significado + exemplo; vira vocabulário da Kira."""
        key = " ".join(str(word or "").lower().split())[:40]
        clean = str(meaning or "").strip()[:160]
        if not key or not clean:
            raise ValueError("palavra e significado são obrigatórios")
        entry = {"meaning": clean, "example": str(example or "").strip()[:160], "learned_at": time.time()}
        self._data.setdefault("taught", {})[key] = entry
        self._data.setdefault("slang", {})[key] = clean.lower()
        self._save()
        logger.info("VOCAB_SLANG_TAUGHT word=%r", key)
        return {"word": key, **entry}

    def knows(self, word: str) -> bool:
        key = " ".join(str(word or "").lower().split())
        return key in self._data.get("slang", {}) or key in self._data.get("taught", {})

    def taught_summary(self, limit: int = 6) -> str:
        """Resumo curto (as mais recentes) para o prompt: não despeja o dicionário inteiro."""
        taught = sorted(self._data.get("taught", {}).items(), key=lambda kv: -float(kv[1].get("learned_at") or 0))
        items = [f"{word} = {info.get('meaning')}" for word, info in taught[:limit]]
        return "; ".join(items)

    def normalize_text(self, text: str) -> str:
        normalized = str(text or "")
        for wrong, correct in self._data.get("corrections", {}).items():
            pattern = r"\b" + re.escape(wrong) + r"\b"
            normalized = re.sub(pattern, str(correct), normalized, flags=re.IGNORECASE)
        return normalized

    def get_context_hint(self, text: str) -> str:
        lowered = str(text or "").lower()
        detected: list[str] = []
        for slang, meaning in self._data.get("slang", {}).items():
            if re.search(r"\b" + re.escape(str(slang).lower()) + r"\b", lowered):
                detected.append(f"'{slang}' significa '{meaning}' no contexto brasileiro")
        if detected:
            return "Contexto de linguagem: " + "; ".join(detected[:8]) + "."
        return ""

    def get_stats(self) -> dict:
        return {
            "slang_count": len(self._data.get("slang", {})),
            "corrections_count": len(self._data.get("corrections", {})),
            "personal_words": len(self._data.get("personal", {})),
            "taught_count": len(self._data.get("taught", {})),
            "session_words": len(self._session_words),
        }
