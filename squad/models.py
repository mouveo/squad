"""Business-domain types for Squad — dataclasses and enums only, no DB logic."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from squad.constants import (
    MODE_APPROVAL,
    SESSION_MODES,
    SESSION_STATUSES,
    STATUS_DRAFT,
)


class SessionStatus(StrEnum):
    DRAFT = "draft"
    INTERVIEWING = "interviewing"
    WORKING = "working"
    REVIEW = "review"
    APPROVED = "approved"
    QUEUED = "queued"
    DONE = "done"
    FAILED = "failed"

    @classmethod
    def values(cls) -> list[str]:
        return [m.value for m in cls]


class SessionMode(StrEnum):
    APPROVAL = "approval"
    AUTONOMOUS = "autonomous"

    @classmethod
    def values(cls) -> list[str]:
        return [m.value for m in cls]


@dataclass
class Session:
    id: str
    title: str
    project_path: str
    workspace_path: str
    idea: str
    status: str = STATUS_DRAFT
    mode: str = MODE_APPROVAL
    current_phase: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        if self.status not in SESSION_STATUSES:
            raise ValueError(f"Invalid status: {self.status!r}")
        if self.mode not in SESSION_MODES:
            raise ValueError(f"Invalid mode: {self.mode!r}")


@dataclass
class PhaseOutput:
    id: str
    session_id: str
    phase: str
    agent: str
    output: str
    file_path: str
    duration_seconds: float | None = None
    tokens_used: int | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class Question:
    id: str
    session_id: str
    agent: str
    phase: str
    question: str
    answer: str | None = None
    answered_at: datetime | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class GeneratedPlan:
    id: str
    session_id: str
    title: str
    file_path: str
    content: str
    forge_status: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
