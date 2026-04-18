"""Business-domain types for Squad — dataclasses and enums only, no DB logic."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

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
    # Persisted failure explanation (pipeline crash or human rejection)
    failure_reason: str | None = None

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
    # Slack thread message id used by the handler's chat_update (LOT 4)
    slack_message_ts: str | None = None


@dataclass
class GeneratedPlan:
    id: str
    session_id: str
    title: str
    file_path: str
    content: str
    forge_status: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    # Slack review-card message id (LOT 5 — used by chat_update)
    slack_message_ts: str | None = None


# ── Slack attachments (Plan 4 — LOT 3) ────────────────────────────────────────


@dataclass
class AttachmentMeta:
    """Metadata for a file attached to a Slack session thread.

    The actual bytes live in ``{workspace}/attachments/{filename}``;
    this dataclass carries only the descriptive fields used by the
    context builder, the listing API and (later) the audit logs.
    ``mime_type`` is whatever Slack reports — Squad does not infer it.
    """

    session_id: str
    filename: str
    path: str
    size_bytes: int
    mime_type: str | None = None
    extension: str = ""
    slack_file_id: str | None = None
    uploaded_at: datetime = field(default_factory=datetime.utcnow)

    def __post_init__(self) -> None:
        if not self.extension:
            self.extension = Path(self.filename).suffix.lstrip(".").lower()


# ── Pipeline events (Plan 4 — LOT 2) ──────────────────────────────────────────

# Event type constants — emitted by ``squad.pipeline`` as the session
# moves through its states. Consumers (Slack live-updates, future UIs)
# match on ``PipelineEvent.type``.
EVENT_WORKING = "working"
EVENT_INTERVIEWING = "interviewing"
EVENT_REVIEW = "review"
EVENT_FAILED = "failed"

PIPELINE_EVENT_TYPES: tuple[str, ...] = (
    EVENT_WORKING,
    EVENT_INTERVIEWING,
    EVENT_REVIEW,
    EVENT_FAILED,
)


@dataclass
class PipelineEvent:
    """Structured event emitted by the pipeline for async consumers.

    ``type`` is the session state the event represents. ``phase`` is the
    canonical phase identifier when relevant (set on every
    ``working`` event, and on ``interviewing`` events where the pause
    originates in a specific phase). ``elapsed_seconds`` is measured
    from the session's ``created_at`` so resume flows still report a
    useful duration.
    """

    type: str
    session_id: str
    timestamp_utc: datetime
    elapsed_seconds: float
    phase: str | None = None
    pending_questions: int = 0
    plans_count: int = 0
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if self.type not in PIPELINE_EVENT_TYPES:
            raise ValueError(f"Invalid pipeline event type: {self.type!r}")
