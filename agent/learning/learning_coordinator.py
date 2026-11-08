"""
LearningCoordinator — interface unica que integra todos os modulos de aprendizado.
"""

import logging

from .behavior_tracker import BehaviorTracker
from .command_learner import CommandLearner
from .opinion_engine import OpinionEngine
from .vocabulary_learner import KIRA_SLANG, VocabularyLearner

logger = logging.getLogger(__name__)


class LearningCoordinator:
    def __init__(self):
        self.vocabulary = VocabularyLearner()
        self.commands = CommandLearner()
        self.behavior = BehaviorTracker()
        self.opinions = OpinionEngine()
        self._turns = 0
        self.slang_question_used = False  # no máximo uma pergunta de gíria por conversa
        logger.info(
            "LEARNING_COORDINATOR_READY vocab=%s commands=%s behavior=%s opinions=%s",
            self.vocabulary.get_stats(),
            self.commands.get_stats(),
            self.behavior.get_stats(),
            self.opinions.get_stats(),
        )

    def on_user_transcript(self, text: str) -> str:
        try:
            self.vocabulary.observe_transcript(text, source="user")
            normalized = self.vocabulary.normalize_text(text)
            if normalized != text:
                logger.info("LEARNING_TEXT_NORMALIZED original=%r normalized=%r", str(text)[:50], str(normalized)[:50])
            cmd = self.commands.match_command(normalized)
            if cmd:
                logger.info("LEARNING_COMMAND_MATCHED pattern=%r action=%s", cmd.get("pattern"), cmd.get("action"))
            return normalized
        except Exception as exc:
            logger.warning("LEARNING_TRANSCRIPT_FAILED error=%s", exc)
            return str(text or "")

    def on_action_taken(self, user_text: str, action_type: str, action_data: dict | None = None) -> None:
        try:
            self.commands.observe_interaction(user_text=user_text, action_taken=action_type, action_data=action_data)
            self.behavior.record_action(action_type=action_type, context=str(user_text or "")[:80], metadata=action_data)
        except Exception as exc:
            logger.warning("LEARNING_ACTION_RECORD_FAILED error=%s", exc)

    def on_user_reaction(self, topic: str, reaction: str, context: str = "") -> None:
        try:
            self.opinions.record_reaction(topic=topic, reaction=reaction, context=context)
        except Exception as exc:
            logger.warning("LEARNING_REACTION_FAILED error=%s", exc)

    def build_context_injection(self, user_text: str) -> str:
        try:
            parts: list[str] = []
            vocab_hint = self.vocabulary.get_context_hint(user_text)
            if vocab_hint:
                parts.append(vocab_hint)
            opinion_ctx = self.opinions.get_opinion_context(user_text)
            if opinion_ctx:
                parts.append(opinion_ctx)
            slang_hint = self.slang_hint()
            if slang_hint:
                parts.append(slang_hint)
            suggestion = self.behavior.get_proactive_suggestion()
            if suggestion:
                parts.append(f"Sugestão proativa disponível: {suggestion}")
            if parts:
                block = "\n[CONTEXTO DE APRENDIZADO]\n" + "\n".join(parts) + "\n[FIM DO CONTEXTO]\n"
                logger.info("LEARNING_CONTEXT_INJECTION parts=%d", len(parts))
                return block
        except Exception as exc:
            logger.warning("LEARNING_CONTEXT_BUILD_FAILED error=%s", exc)
        return ""

    def slang_hint(self) -> str:
        """Lembrete curto de vocabulário, só a cada 4 turnos (não pesa o prompt)."""
        self._turns += 1
        if self._turns % 4:
            return "Você já perguntou uma gíria nesta conversa: não pergunte outra." if self.slang_question_used and self._turns % 4 == 1 else ""
        taught = self.vocabulary.taught_summary()
        hint = f"Gírias suas (solte uma de vez em quando, nunca forçado): {', '.join(KIRA_SLANG[:5])}."
        if taught:
            hint += f" Gírias que o Chefe te ensinou (use às vezes, com o sentido certo): {taught}."
        return hint

    def learn_slang(self, word: str, meaning: str, example: str = "") -> dict:
        self.slang_question_used = True
        return self.vocabulary.learn_slang(word, meaning, example)

    def build_personality_injection(self) -> str:
        try:
            return self.opinions.build_personality_prompt()
        except Exception as exc:
            logger.warning("LEARNING_PERSONALITY_BUILD_FAILED error=%s", exc)
            return ""

    def match_learned_command(self, text: str) -> dict | None:
        try:
            return self.commands.match_command(text)
        except Exception as exc:
            logger.warning("LEARNING_COMMAND_MATCH_FAILED error=%s", exc)
            return None

    def get_full_stats(self) -> dict:
        return {
            "vocabulary": self.vocabulary.get_stats(),
            "commands": self.commands.get_stats(),
            "behavior": self.behavior.get_stats(),
            "opinions": self.opinions.get_stats(),
        }
