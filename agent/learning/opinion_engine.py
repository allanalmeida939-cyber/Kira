"""
OpinionEngine — forma e mantem opinioes e preferencias proprias da Kira.
"""

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).parent / "data" / "opinions.json"
OPINION_DOMAINS = {
    "musica": "Musica e sons",
    "produtividade": "Produtividade e rotina",
    "tecnologia": "Tecnologia e gadgets",
    "alimentacao": "Alimentacao e saude",
    "humor": "Humor e entretenimento",
    "organizacao": "Organizacao e ambiente",
}


class OpinionEngine:
    def __init__(self):
        self._data = self._load()

    def _load(self) -> dict:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        if DATA_FILE.exists():
            try:
                return json.loads(DATA_FILE.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("OPINION_LOAD_FAILED error=%s", exc)
        return {
            "opinions": {},
            "user_reactions": {},
            "disagreements": [],
            "personality": {"directness": 0.7, "humor": 0.6, "proactivity": 0.5, "empathy": 0.8},
            "version": 1,
        }

    def _save(self) -> None:
        try:
            DATA_FILE.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("OPINION_SAVE_FAILED error=%s", exc)

    def record_reaction(self, topic: str, reaction: str, context: str = "") -> None:
        topic = str(topic or "").strip()
        reaction = str(reaction or "neutral").strip().lower()
        if reaction not in {"positive", "negative", "neutral"}:
            reaction = "neutral"
        if not topic:
            return
        reactions = self._data.setdefault("user_reactions", {})
        reactions.setdefault(topic, {"positive": 0, "negative": 0, "neutral": 0})
        reactions[topic][reaction] = int(reactions[topic].get(reaction, 0)) + 1
        if sum(reactions[topic].values()) >= 3:
            self._update_opinion(topic, reactions[topic], context)
        self._save()

    def _update_opinion(self, topic: str, reactions: dict, context: str) -> None:
        pos = int(reactions.get("positive", 0))
        neg = int(reactions.get("negative", 0))
        total = pos + neg + int(reactions.get("neutral", 0))
        if total == 0:
            return
        confidence = abs(pos - neg) / total
        stance = "positive" if pos > neg else "negative" if neg > pos else "neutral"
        self._data.setdefault("opinions", {})[topic] = {
            "stance": stance,
            "confidence": round(confidence, 2),
            "evidence": str(context or "")[:100],
            "formed_at": time.time(),
            "reactions": reactions,
        }
        logger.info("OPINION_FORMED topic=%r stance=%s confidence=%.2f", topic, stance, confidence)

    def get_opinion_context(self, text: str) -> str:
        relevant: list[str] = []
        text_lower = str(text or "").lower()
        for topic, opinion in self._data.get("opinions", {}).items():
            if str(topic).lower() in text_lower and float(opinion.get("confidence", 0.0)) > 0.4:
                stance = opinion.get("stance")
                conf = float(opinion.get("confidence", 0.0))
                if stance == "positive" and conf > 0.6:
                    relevant.append(f"Você gosta muito de {topic} (baseado em interações anteriores).")
                elif stance == "negative" and conf > 0.6:
                    relevant.append(f"Você não costuma gostar de {topic} (baseado em interações anteriores).")
        if relevant:
            return "Preferências conhecidas do usuário: " + " ".join(relevant) + " Use isso para personalizar a resposta."
        return ""

    def get_kira_opinion_prompt(self, topic: str) -> str:
        opinion = self._data.get("opinions", {}).get(topic)
        personality = self._data.get("personality", {})
        directness = float(personality.get("directness", 0.7))
        humor = float(personality.get("humor", 0.6))
        if not opinion:
            return (
                f"O usuário está perguntando sobre {topic}. Você pode formar uma opinião baseada no contexto da conversa. "
                f"Seja {'direto e assertivo' if directness > 0.6 else 'cuidadoso'} e "
                f"{'use humor quando apropriado' if humor > 0.5 else 'mantenha tom sério'}."
            )
        stance = opinion.get("stance")
        conf = float(opinion.get("confidence", 0.0))
        tone = "com convicção" if conf > 0.7 else "com alguma incerteza" if conf > 0.4 else "de forma aberta"
        stance_pt = "positiva" if stance == "positive" else "negativa" if stance == "negative" else "neutra"
        return (
            f"Baseado no histórico de interações, você tem uma opinião {stance_pt} sobre {topic}, {tone}. "
            f"Expresse isso {'diretamente' if directness > 0.6 else 'sutilmente'}, mantendo sua personalidade "
            f"{'bem-humorada' if humor > 0.5 else 'séria'}."
        )

    def record_disagreement(self, topic: str, kira_position: str, user_accepted: bool) -> None:
        self._data.setdefault("disagreements", []).append({
            "ts": time.time(),
            "topic": topic,
            "position": str(kira_position or "")[:100],
            "user_accepted": bool(user_accepted),
        })
        self._data["disagreements"] = self._data["disagreements"][-50:]
        personality = self._data.setdefault("personality", {})
        curr = float(personality.get("directness", 0.7))
        personality["directness"] = min(1.0, curr + 0.02) if user_accepted else max(0.3, curr - 0.01)
        self._save()

    def build_personality_prompt(self) -> str:
        p = self._data.get("personality", {})
        parts: list[str] = []
        parts.append("Seja direta e assertiva nas suas respostas." if float(p.get("directness", 0.5)) > 0.65 else "Seja gentil e cuidadosa nas suas respostas.")
        if float(p.get("humor", 0.5)) > 0.55:
            parts.append("Use humor leve quando o contexto permitir.")
        if float(p.get("proactivity", 0.5)) > 0.6:
            parts.append("Sugira proativamente quando identificar oportunidades.")
        if float(p.get("empathy", 0.5)) > 0.65:
            parts.append("Demonstre empatia genuína com o estado emocional do usuário.")
        return " ".join(parts)

    def get_stats(self) -> dict:
        return {
            "opinions_formed": len(self._data.get("opinions", {})),
            "topics_tracked": len(self._data.get("user_reactions", {})),
            "disagreements": len(self._data.get("disagreements", [])),
            "personality": self._data.get("personality", {}),
        }
