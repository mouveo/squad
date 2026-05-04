"""Read-only data layer backing the Streamlit dashboard.

This module centralises every query the dashboard pages need so no SQL
or filesystem access leaks into Streamlit. It returns typed dataclasses
(``SessionRow``, ``SessionDetail``, ``PhaseView``, ``PhaseAttemptView``,
``PlanReviewItem``) and pure helpers (``humanize_age_fr``) so the filter,
sort and aggregation logic stays testable without a running Streamlit.

Lookups delegate to the existing primitives in ``squad.db``,
``squad.workspace``, ``squad.attachment_service``, ``squad.forge_format``
and ``squad.slack_service`` — the dashboard never re-implements what is
already in those modules.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlite_utils import Database

from squad.attachment_service import list_attachments
from squad.config import get_global_db_path
from squad.constants import (
    ACTIVE_STATUSES,
    PHASE_LABELS,
    PHASES,
    STATUS_FAILED,
    STATUS_INTERVIEWING,
    STATUS_LABELS,
    STATUS_TONES,
    STATUS_WORKING,
)
from squad.db import (
    get_session,
    list_pending_questions,
    list_phase_outputs,
    list_plans,
    list_sessions,
)
from squad.forge_format import extract_lots, validate_plan
from squad.models import (
    AttachmentMeta,
    GeneratedPlan,
    PhaseOutput,
    Question,
    Session,
)
from squad.slack_service import summarize_plan

# ── Phase states ──────────────────────────────────────────────────────────────

PHASE_STATE_PENDING = "pending"
PHASE_STATE_RUNNING = "running"
PHASE_STATE_DONE = "done"
PHASE_STATE_FAILED = "failed"
PHASE_STATE_SKIPPED = "skipped"

PHASE_STATES: tuple[str, ...] = (
    PHASE_STATE_PENDING,
    PHASE_STATE_RUNNING,
    PHASE_STATE_DONE,
    PHASE_STATE_FAILED,
    PHASE_STATE_SKIPPED,
)

# Plan content origin used by the review page so the user knows whether
# they are seeing the workspace file (potentially edited) or the DB
# snapshot the pipeline wrote at generation time.
PLAN_SOURCE_WORKSPACE = "workspace"
PLAN_SOURCE_DB = "db"


# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass
class SessionRow:
    """A single row for the session list page."""

    id: str
    title: str
    project_path: str
    status: str
    status_label: str
    status_tone: str
    mode: str
    current_phase: str | None
    current_phase_label: str | None
    updated_at: datetime
    created_at: datetime
    age_fr: str
    is_active: bool
    pending_questions: int
    plans_count: int
    input_richness: str | None


@dataclass
class PhaseAttemptView:
    """One attempt of a phase with the outputs it produced."""

    attempt: int
    outputs: list[PhaseOutput]
    agents: list[str]
    total_duration_seconds: float | None
    total_tokens: int | None
    started_at: datetime | None


@dataclass
class PhaseView:
    """Aggregated view of a phase across all its attempts."""

    id: str
    label: str
    state: str  # one of PHASE_STATES
    is_current: bool
    skip_reason: str | None
    attempts_count: int
    attempts: list[PhaseAttemptView]


@dataclass
class SessionDetail:
    """Full aggregation of a session for the detail page."""

    session: Session
    idea: str
    context: str | None
    phases: list[PhaseView]
    attachments: list[AttachmentMeta]
    pending_questions: list[Question]
    failure_reason: str | None
    status_label: str
    status_tone: str
    age_fr: str
    plans_count: int


@dataclass
class PlanReviewItem:
    """One plan ready for review on the dashboard."""

    plan: GeneratedPlan
    title: str
    content: str
    source: str  # PLAN_SOURCE_WORKSPACE or PLAN_SOURCE_DB
    file_path: str | None
    lot_count: int
    files: list[str]
    forge_status: str | None
    validation_errors: list[str]


# ── Simple counters kept for the landing page ─────────────────────────────────


def count_sessions(db_path: Path | None = None) -> int:
    """Return the total number of sessions stored in the registry."""
    path = db_path or get_global_db_path()
    if not path.exists():
        return 0
    db = Database(path)
    if "sessions" not in db.table_names():
        return 0
    return db["sessions"].count


# ── French humanisation ───────────────────────────────────────────────────────


def humanize_age_fr(
    when: datetime | None,
    *,
    now: datetime | None = None,
) -> str:
    """Return a short French age string like ``il y a 5 min``.

    ``now`` is exposed so tests can inject a deterministic reference
    time. The function is pure (no I/O) so it can be unit-tested without
    Streamlit or the database.
    """
    if when is None:
        return "—"
    now_dt = now or datetime.utcnow()
    delta: timedelta = now_dt - when
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "dans le futur"
    if seconds < 10:
        return "à l'instant"
    if seconds < 60:
        return f"il y a {seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"il y a {minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"il y a {hours} h"
    days = hours // 24
    if days < 30:
        return f"il y a {days} j"
    months = days // 30
    if months < 12:
        return f"il y a {months} mois"
    years = days // 365
    return f"il y a {years} an" + ("s" if years > 1 else "")


# ── Session list ──────────────────────────────────────────────────────────────


def list_sessions_for_dashboard(
    *,
    status: str | Iterable[str] | None = None,
    project_path: str | None = None,
    sort: str = "created_at_desc",
    limit: int | None = None,
    db_path: Path | None = None,
    now: datetime | None = None,
) -> list[SessionRow]:
    """Return typed rows for the session list, already filtered and sorted.

    The actual filtering and ordering happen in ``squad.db.list_sessions``
    so the dashboard can stay free of SQL. This function enriches each
    row with derived display fields (label, tone, humanised age, counts).
    """
    sessions = list_sessions(
        status=status,
        project_path=project_path,
        sort=sort,
        limit=limit,
        db_path=db_path,
    )
    now_dt = now or datetime.utcnow()
    rows: list[SessionRow] = []
    for session in sessions:
        pending = len(list_pending_questions(session.id, db_path=db_path))
        plans = len(list_plans(session.id, db_path=db_path))
        rows.append(
            SessionRow(
                id=session.id,
                title=session.title,
                project_path=session.project_path,
                status=session.status,
                status_label=STATUS_LABELS.get(session.status, session.status),
                status_tone=STATUS_TONES.get(session.status, "neutral"),
                mode=session.mode,
                current_phase=session.current_phase,
                current_phase_label=(
                    PHASE_LABELS.get(session.current_phase)
                    if session.current_phase
                    else None
                ),
                updated_at=session.updated_at,
                created_at=session.created_at,
                age_fr=humanize_age_fr(session.created_at, now=now_dt),
                is_active=session.status in ACTIVE_STATUSES,
                pending_questions=pending,
                plans_count=plans,
                input_richness=session.input_richness,
            )
        )
    return rows


# ── Phase aggregation ─────────────────────────────────────────────────────────


def _build_phase_attempt_view(
    attempt: int,
    outputs: list[PhaseOutput],
) -> PhaseAttemptView:
    outputs_sorted = sorted(outputs, key=lambda o: o.created_at)
    durations = [
        o.duration_seconds for o in outputs_sorted if o.duration_seconds is not None
    ]
    tokens = [o.tokens_used for o in outputs_sorted if o.tokens_used is not None]
    return PhaseAttemptView(
        attempt=attempt,
        outputs=outputs_sorted,
        agents=[o.agent for o in outputs_sorted],
        total_duration_seconds=sum(durations) if durations else None,
        total_tokens=sum(tokens) if tokens else None,
        started_at=outputs_sorted[0].created_at if outputs_sorted else None,
    )


def _phase_state(
    phase: str,
    session: Session,
    attempts: list[PhaseAttemptView],
) -> str:
    """Derive a phase state from the session status, outputs and skip map.

    Order of precedence matches the pipeline's own: a phase explicitly
    marked skipped wins over any other signal, then the active state
    tracked on the session row drives ``running`` / ``failed`` for the
    phase that currently owns it, then the presence of at least one
    produced output marks the phase as ``done``. Anything else is still
    ``pending``.
    """
    if phase in session.skipped_phases:
        return PHASE_STATE_SKIPPED
    if session.current_phase == phase:
        if session.status == STATUS_FAILED:
            return PHASE_STATE_FAILED
        if session.status in (STATUS_WORKING, STATUS_INTERVIEWING):
            return PHASE_STATE_RUNNING
    # A produced output means the phase completed at least one attempt;
    # retries in flight are carried by ``attempts_count`` so the count
    # never flattens a retry into a single-attempt phase.
    if any(a.outputs for a in attempts):
        return PHASE_STATE_DONE
    return PHASE_STATE_PENDING


def _build_phase_view(
    phase: str,
    session: Session,
    db_path: Path | None,
) -> PhaseView:
    outputs = list_phase_outputs(session.id, phase=phase, db_path=db_path)
    by_attempt: dict[int, list[PhaseOutput]] = {}
    for output in outputs:
        by_attempt.setdefault(output.attempt, []).append(output)

    recorded_max = max(by_attempt) if by_attempt else 0
    logged_max = int(session.phase_attempts.get(phase, 0))
    highest = max(recorded_max, logged_max)

    attempts: list[PhaseAttemptView] = [
        _build_phase_attempt_view(n, by_attempt.get(n, []))
        for n in range(1, highest + 1)
    ]
    state = _phase_state(phase, session, attempts)
    return PhaseView(
        id=phase,
        label=PHASE_LABELS.get(phase, phase),
        state=state,
        is_current=(session.current_phase == phase),
        skip_reason=session.skipped_phases.get(phase),
        attempts_count=highest,
        attempts=attempts,
    )


# ── Session detail ────────────────────────────────────────────────────────────


def _read_text_if_exists(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def get_session_detail(
    session_id: str,
    db_path: Path | None = None,
    *,
    now: datetime | None = None,
) -> SessionDetail | None:
    """Return an aggregated view of a session or ``None`` when missing."""
    session = get_session(session_id, db_path=db_path)
    if session is None:
        return None
    workspace = Path(session.workspace_path)
    idea = _read_text_if_exists(workspace / "idea.md") or session.idea
    context = _read_text_if_exists(workspace / "context.md")
    phases = [_build_phase_view(phase, session, db_path) for phase in PHASES]
    attachments = list_attachments(session.id, db_path=db_path)
    pending = list_pending_questions(session.id, db_path=db_path)
    plans = list_plans(session.id, db_path=db_path)
    now_dt = now or datetime.utcnow()
    return SessionDetail(
        session=session,
        idea=idea,
        context=context,
        phases=phases,
        attachments=list(attachments),
        pending_questions=list(pending),
        failure_reason=session.failure_reason,
        status_label=STATUS_LABELS.get(session.status, session.status),
        status_tone=STATUS_TONES.get(session.status, "neutral"),
        age_fr=humanize_age_fr(session.created_at, now=now_dt),
        plans_count=len(plans),
    )


# ── Plan review ───────────────────────────────────────────────────────────────


def _read_plan_file(file_path: str | None) -> str | None:
    """Return the workspace plan content or ``None`` when the file is missing."""
    if not file_path:
        return None
    path = Path(file_path)
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def get_review_plans(
    session_id: str,
    db_path: Path | None = None,
) -> list[PlanReviewItem]:
    """Return every plan for a session with its effective content.

    The workspace file takes precedence over ``plan.content`` when it
    exists: this lets a reviewer hand-edit a plan before approving and
    see the edited version on the dashboard. The DB snapshot is only
    used as a fallback when the workspace file is missing.
    """
    items: list[PlanReviewItem] = []
    for plan in list_plans(session_id, db_path=db_path):
        workspace_content = _read_plan_file(plan.file_path)
        if workspace_content is not None:
            content = workspace_content
            source = PLAN_SOURCE_WORKSPACE
        else:
            content = plan.content or ""
            source = PLAN_SOURCE_DB

        effective = GeneratedPlan(
            id=plan.id,
            session_id=plan.session_id,
            title=plan.title,
            file_path=plan.file_path,
            content=content,
            forge_status=plan.forge_status,
            created_at=plan.created_at,
            slack_message_ts=plan.slack_message_ts,
        )
        summary = summarize_plan(effective)
        validation = validate_plan(content)
        lot_count = summary.get("lot_count")
        if not lot_count:
            lot_count = len(extract_lots(content))
        items.append(
            PlanReviewItem(
                plan=effective,
                title=plan.title,
                content=content,
                source=source,
                file_path=plan.file_path or None,
                lot_count=lot_count,
                files=list(summary.get("files") or []),
                forge_status=plan.forge_status,
                validation_errors=list(validation.errors),
            )
        )
    return items
