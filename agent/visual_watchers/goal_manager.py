from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class VisualGoal:
    id: str
    source_command: str
    goal_type: str
    target_value: int | None = None
    target_label: str = ""
    completion_policy: str = "debounced"
    created_at: float = field(default_factory=time.monotonic)
    enabled: bool = True
    last_observed_value: int | None = None
    matched: bool = False
    completed: bool = False
    completed_at: float | None = None
    speak_on_completion: bool = True
    cooldown_seconds: float = 8.0
    confidence: float = 0.0
    region_hint: str = ""
    app_hint: str = ""
    status: str = "created"
    candidate_hits: int = 0
    last_reason: str = ""


class VisualGoalManager:
    def __init__(self) -> None:
        self._goals: dict[str, VisualGoal] = {}

    def create_goal(
        self,
        *,
        goal_id: str,
        source_command: str,
        goal_type: str,
        target_value: int | None = None,
        target_label: str = "",
        app_hint: str = "",
        region_hint: str = "",
    ) -> VisualGoal:
        goal = VisualGoal(
            id=goal_id,
            source_command=source_command,
            goal_type=goal_type,
            target_value=target_value,
            target_label=target_label,
            app_hint=app_hint,
            region_hint=region_hint,
            status="active",
        )
        self._goals[goal_id] = goal
        logger.info(
            "visual_goal_created goal_id=%s goal_type=%s target_value=%s target_label=%s",
            goal_id,
            goal_type,
            target_value,
            target_label,
        )
        logger.info("visual_goal_registered goal_id=%s status=%s", goal_id, goal.status)
        return goal

    def cancel_goal(self, goal_id: str, *, reason: str = "cancelled") -> None:
        goal = self._goals.get(goal_id)
        if goal is None:
            return
        goal.enabled = False
        goal.status = "cancelled"
        goal.last_reason = reason
        logger.info("visual_goal_cancelled goal_id=%s reason=%s", goal_id, reason)

    def archive_goal(self, goal_id: str, *, reason: str = "archived") -> None:
        goal = self._goals.get(goal_id)
        if goal is None:
            return
        goal.enabled = False
        goal.status = "archived"
        goal.last_reason = reason
        logger.info("visual_goal_archived goal_id=%s reason=%s", goal_id, reason)

    def frame_evaluated(
        self,
        goal_id: str,
        *,
        observed_value: int | None = None,
        matched: bool = False,
        reason: str = "",
    ) -> None:
        goal = self._goals.get(goal_id)
        if goal is None:
            return
        goal.last_observed_value = observed_value
        goal.matched = matched
        goal.last_reason = reason
        goal.status = "monitoring"
        logger.debug(
            "visual_goal_frame_evaluated goal_id=%s observed_value=%s matched=%s reason=%s",
            goal_id,
            observed_value,
            matched,
            reason,
        )

    def candidate_match(self, goal_id: str, *, observed_value: int | None = None, confidence: float = 0.0) -> None:
        goal = self._goals.get(goal_id)
        if goal is None:
            return
        goal.candidate_hits += 1
        goal.last_observed_value = observed_value
        goal.confidence = max(goal.confidence, confidence)
        goal.status = "matched_candidate"
        logger.info(
            "visual_goal_candidate_match goal_id=%s candidate_hits=%s observed_value=%s confidence=%.2f",
            goal_id,
            goal.candidate_hits,
            observed_value,
            goal.confidence,
        )

    def completed(self, goal_id: str, *, observed_value: int | None = None, reason: str = "") -> None:
        goal = self._goals.get(goal_id)
        if goal is None:
            return
        goal.completed = True
        goal.enabled = False
        goal.completed_at = time.monotonic()
        goal.status = "completed"
        goal.last_reason = reason
        goal.last_observed_value = observed_value
        logger.info(
            "visual_goal_completed goal_id=%s observed_value=%s reason=%s",
            goal_id,
            observed_value,
            reason,
        )

    def get_goal(self, goal_id: str) -> VisualGoal | None:
        return self._goals.get(goal_id)
