"""Validated tutoring intake state and deterministic, one-question-at-a-time replies."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ExtractionError(ValueError):
    """The model response or stored state cannot safely be applied."""


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ExtractionError(f"{name} must be a string")
    value = value.strip()
    if not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ExtractionError(f"{name} is empty, too long, or contains control characters")
    return value


def _subjects(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 8:
        raise ExtractionError("subjects must be a list of at most 8 subjects")
    result = [_text(item, "subject", 20) for item in value]
    if len(set(result)) != len(result):
        raise ExtractionError("subjects must be unique")
    return result


def _scores(value: Any) -> dict[str, str | None]:
    if not isinstance(value, dict) or len(value) > 8:
        raise ExtractionError("scores must be an object of at most 8 subjects")
    result = {}
    for subject, score in value.items():
        subject = _text(subject, "score subject", 20)
        if subject in result:
            raise ExtractionError("duplicate score subject")
        result[subject] = None if score is None else _text(score, "score", 80)
    return result


@dataclass
class ConsultationState:
    grade: str | None = None
    subjects: list[str] = field(default_factory=list)
    scores: dict[str, str | None] = field(default_factory=dict)
    unknown_scores: list[str] = field(default_factory=list)
    completed: bool = False
    introduced: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "grade": self.grade,
            "subjects": list(self.subjects),
            "scores": dict(self.scores),
            "unknown_scores": list(self.unknown_scores),
            "completed": self.completed,
            "introduced": self.introduced,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ConsultationState":
        if not isinstance(value, dict):
            raise ExtractionError("consultation state must be an object")
        required = {"grade", "subjects", "scores", "unknown_scores", "completed"}
        if not required.issubset(value) or set(value) - required - {"introduced"}:
            raise ExtractionError("consultation state has missing or unexpected fields")
        grade = None if value["grade"] is None else _text(value["grade"], "grade", 30)
        subjects = _subjects(value["subjects"])
        scores = _scores(value["scores"])
        unknown = _subjects(value["unknown_scores"])
        if set(scores) - set(subjects):
            raise ExtractionError("stored scores refer to unselected subjects")
        if set(unknown) != {subject for subject, score in scores.items() if score is None}:
            raise ExtractionError("unknown_scores must match explicitly unknown scores")
        if type(value["completed"]) is not bool or type(value.get("introduced", False)) is not bool:
            raise ExtractionError("completed and introduced must be booleans")
        complete = bool(grade and subjects and all(subject in scores for subject in subjects))
        if value["completed"] and not complete:
            raise ExtractionError("incomplete state cannot be marked completed")
        return cls(grade, subjects, scores, unknown, value["completed"], value.get("introduced", False))


@dataclass(frozen=True)
class ExtractionPatch:
    grade: str | None
    subjects: list[str] | None
    scores: dict[str, str | None]
    clarification: str | None
    off_topic: bool
    start_new_consultation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "grade": self.grade,
            "subjects": None if self.subjects is None else list(self.subjects),
            "scores": dict(self.scores),
            "clarification": self.clarification,
            "off_topic": self.off_topic,
            "start_new_consultation": self.start_new_consultation,
        }


def validate_patch(value: Any, state: ConsultationState | None = None) -> ExtractionPatch:
    """Validate extraction, accepting legacy five-key patches without the restart flag."""
    if isinstance(value, ExtractionPatch):
        value = value.to_dict()
    fields = {"grade", "subjects", "scores", "clarification", "off_topic"}
    if not isinstance(value, dict) or set(value) not in (fields, fields | {"start_new_consultation"}):
        raise ExtractionError("extraction has missing or unexpected fields")
    grade = None if value["grade"] is None else _text(value["grade"], "grade", 30)
    subjects = None if value["subjects"] is None else _subjects(value["subjects"])
    scores = _scores(value["scores"])
    clarification = None if value["clarification"] is None else _text(value["clarification"], "clarification", 200)
    if type(value["off_topic"]) is not bool:
        raise ExtractionError("off_topic must be a boolean")
    start_new = value.get("start_new_consultation", False)
    if type(start_new) is not bool:
        raise ExtractionError("start_new_consultation must be a boolean")
    if state is not None:
        state = ConsultationState.from_dict(state.to_dict())
    allowed = subjects if subjects is not None else ([] if start_new else (state.subjects if state is not None else None))
    if allowed is not None and set(scores) - set(allowed):
        raise ExtractionError("scores must refer to selected subjects")
    return ExtractionPatch(grade, subjects, scores, clarification, value["off_topic"], start_new)


def _next_question(state: ConsultationState) -> str | None:
    if state.grade is None:
        return "孩子现在读几年级？"
    if not state.subjects:
        return "希望给孩子补习哪一科或哪几科？"
    for subject in state.subjects:
        if subject not in state.scores:
            return f"孩子上次期末的{subject}成绩是多少？暂时不知道也可以告诉我。"
    return None


def _summary(state: ConsultationState) -> str:
    scores = "；".join(
        f"{subject}成绩暂不清楚" if state.scores[subject] is None else f"{subject}{state.scores[subject]}"
        for subject in state.subjects
    )
    return f"我记下了：孩子{state.grade}，希望补习{'、'.join(state.subjects)}；{scores}。后续由老师确认安排。"


def apply_patch(
    state: ConsultationState, patch: ExtractionPatch | dict[str, Any]
) -> tuple[ConsultationState, str | None]:
    """Apply explicit changes without mutating inputs, then choose the next reply."""
    current = ConsultationState.from_dict(state.to_dict())
    patch = validate_patch(patch, current)
    result = ConsultationState(introduced=current.introduced) if patch.start_new_consultation else ConsultationState.from_dict(current.to_dict())
    if patch.grade is not None:
        result.grade = patch.grade
    if patch.subjects is not None:
        result.subjects = list(patch.subjects)
        result.scores = {subject: score for subject, score in result.scores.items() if subject in result.subjects}
    result.scores.update(patch.scores)
    result.unknown_scores = [subject for subject in result.subjects if subject in result.scores and result.scores[subject] is None]
    question = _next_question(result)
    result.completed = question is None
    changed = (current.grade, current.subjects, current.scores) != (result.grade, result.subjects, result.scores)

    if patch.clarification is not None:
        reply = patch.clarification
    elif patch.off_topic and not changed:
        reply = "这个问题需要老师进一步确认。" + (question or "")
    elif question is not None:
        reply = question
    elif patch.start_new_consultation or not current.completed or changed:
        reply = _summary(result)
    else:
        reply = None
    if reply is not None and not result.introduced:
        reply = "您好，我是老师的咨询助理。" + reply
        result.introduced = True
    return result, reply
