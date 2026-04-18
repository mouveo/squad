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

# Research depth values accepted on a session profile
RESEARCH_DEPTH_LIGHT = "light"
RESEARCH_DEPTH_NORMAL = "normal"
RESEARCH_DEPTH_DEEP = "deep"

RESEARCH_DEPTHS: tuple[str, ...] = (
    RESEARCH_DEPTH_LIGHT,
    RESEARCH_DEPTH_NORMAL,
    RESEARCH_DEPTH_DEEP,
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
    # Deterministic subject profile (set once by subject_detector)
    subject_type: str | None = None
    research_depth: str | None = None
    agents_by_phase: dict[str, list[str]] = field(default_factory=dict)
    # Resume-state counters
    phase_attempts: dict[str, int] = field(default_factory=dict)
    challenge_retry_count: int = 0
    # Phase id → skip reason (benchmark on light depth, etc.)
    skipped_phases: dict[str, str] = field(default_factory=dict)
    # Slack origin (set only when the session was created from Slack)
    slack_channel: str | None = None
    slack_thread_ts: str | None = None
    slack_user_id: str | None = None

    def __post_init__(self) -> None:
        if self.status not in SESSION_STATUSES:
            raise ValueError(f"Invalid status: {self.status!r}")
        if self.mode not in SESSION_MODES:
            raise ValueError(f"Invalid mode: {self.mode!r}")
        if self.research_depth is not None and self.research_depth not in RESEARCH_DEPTHS:
            raise ValueError(f"Invalid research_depth: {self.research_depth!r}")


@dataclass
class SubjectProfile:
    """Deterministic profile derived from the project and the idea.

    Produced once by ``squad.subject_detector`` and persisted on the
    ``sessions`` row. Pipeline code reads this profile on both start and
    resume without ever reclassifying the subject.
    """

    subject_type: str
    research_depth: str
    agents_by_phase: dict[str, list[str]] = field(default_factory=dict)
    rationale: str | None = None

    def __post_init__(self) -> None:
        if self.research_depth not in RESEARCH_DEPTHS:
            raise ValueError(f"Invalid research_depth: {self.research_depth!r}")


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
    attempt: int = 1
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
