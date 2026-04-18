"""Global session registry — SQLite CRUD via sqlite-utils."""

import json
import uuid
from datetime import datetime
from pathlib import Path

from sqlite_utils import Database

from squad.config import get_global_db_path
from squad.constants import MODE_APPROVAL, STATUS_DONE, STATUS_FAILED
from squad.models import GeneratedPlan, PhaseOutput, Question, Session

# Statuses that mean a session is no longer in progress
_TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED)


def _decode_json(value: str | None, default):
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed is not None else default


# ── private helpers ────────────────────────────────────────────────────────────


def _open(db_path: Path | None) -> Database:
    path = db_path or get_global_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return Database(path)


def _now() -> str:
    return datetime.utcnow().isoformat()


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _to_session(row: dict) -> Session:
    return Session(
        id=row["id"],
        title=row["title"],
        project_path=row["project_path"],
        workspace_path=row["workspace_path"],
        idea=row["idea"],
        status=row["status"],
        mode=row["mode"],
        current_phase=row.get("current_phase"),
        created_at=_dt(row.get("created_at")) or datetime.utcnow(),
        updated_at=_dt(row.get("updated_at")) or datetime.utcnow(),
        subject_type=row.get("subject_type"),
        research_depth=row.get("research_depth"),
        agents_by_phase=_decode_json(row.get("agents_by_phase"), {}),
        phase_attempts=_decode_json(row.get("phase_attempts"), {}),
        challenge_retry_count=int(row.get("challenge_retry_count") or 0),
        skipped_phases=_decode_json(row.get("skipped_phases"), {}),
        slack_channel=row.get("slack_channel"),
        slack_thread_ts=row.get("slack_thread_ts"),
        slack_user_id=row.get("slack_user_id"),
        failure_reason=row.get("failure_reason"),
    )


def _to_phase_output(row: dict) -> PhaseOutput:
    return PhaseOutput(
        id=row["id"],
        session_id=row["session_id"],
        phase=row["phase"],
        agent=row["agent"],
        output=row["output"],
        file_path=row["file_path"],
        duration_seconds=row.get("duration_seconds"),
        tokens_used=row.get("tokens_used"),
        attempt=int(row.get("attempt") or 1),
        created_at=_dt(row.get("created_at")) or datetime.utcnow(),
    )


def _to_question(row: dict) -> Question:
    return Question(
        id=row["id"],
        session_id=row["session_id"],
        agent=row["agent"],
        phase=row["phase"],
        question=row["question"],
        answer=row.get("answer"),
        answered_at=_dt(row.get("answered_at")),
        created_at=_dt(row.get("created_at")) or datetime.utcnow(),
        slack_message_ts=row.get("slack_message_ts"),
    )


def _to_plan(row: dict) -> GeneratedPlan:
    return GeneratedPlan(
        id=row["id"],
        session_id=row["session_id"],
        title=row["title"],
        file_path=row["file_path"],
        content=row["content"],
        forge_status=row.get("forge_status"),
        created_at=_dt(row.get("created_at")) or datetime.utcnow(),
        slack_message_ts=row.get("slack_message_ts"),
    )


# ── schema ─────────────────────────────────────────────────────────────────────


def ensure_schema(db_path: Path | None = None) -> None:
    """Create tables and indexes if they do not already exist."""
    db = _open(db_path)

    db["sessions"].create(
        {
            "id": str,
            "title": str,
            "project_path": str,
            "workspace_path": str,
            "idea": str,
            "status": str,
            "mode": str,
            "current_phase": str,
            "created_at": str,
            "updated_at": str,
            # Profile columns (LOT 2)
            "subject_type": str,
            "research_depth": str,
            "agents_by_phase": str,
            "phase_attempts": str,
            "challenge_retry_count": int,
            "skipped_phases": str,
            # Slack origin (Plan 4 — LOT 1)
            "slack_channel": str,
            "slack_thread_ts": str,
            "slack_user_id": str,
            # Failure context (Plan 4 — LOT 2)
            "failure_reason": str,
        },
        pk="id",
        not_null={"title", "project_path", "workspace_path", "idea", "status"},
        defaults={"mode": MODE_APPROVAL, "challenge_retry_count": 0},
        if_not_exists=True,
    )
    db["sessions"].create_index(["status"], if_not_exists=True)
    db["sessions"].create_index(["project_path"], if_not_exists=True)

    # Additive migration for DBs created before LOT 2
    session_cols = set(db["sessions"].columns_dict)
    for col, col_type in (
        ("subject_type", str),
        ("research_depth", str),
        ("agents_by_phase", str),
        ("phase_attempts", str),
        ("skipped_phases", str),
        ("slack_channel", str),
        ("slack_thread_ts", str),
        ("slack_user_id", str),
        ("failure_reason", str),
    ):
        if col not in session_cols:
            db["sessions"].add_column(col, col_type)
    if "challenge_retry_count" not in session_cols:
        db["sessions"].add_column("challenge_retry_count", int, not_null_default=0)

    db["phase_outputs"].create(
        {
            "id": str,
            "session_id": str,
            "phase": str,
            "agent": str,
            "output": str,
            "file_path": str,
            "duration_seconds": float,
            "tokens_used": int,
            "attempt": int,
            "created_at": str,
        },
        pk="id",
        not_null={"session_id", "phase", "agent", "output", "file_path"},
        defaults={"attempt": 1},
        if_not_exists=True,
    )
    db["phase_outputs"].create_index(["session_id"], if_not_exists=True)

    if "attempt" not in db["phase_outputs"].columns_dict:
        db["phase_outputs"].add_column("attempt", int, not_null_default=1)

    db["questions"].create(
        {
            "id": str,
            "session_id": str,
            "agent": str,
            "phase": str,
            "question": str,
            "answer": str,
            "answered_at": str,
            "created_at": str,
            # Slack thread message id — lets chat_update keep the
            # in-thread question rendering in sync with its answer state
            # (Plan 4 — LOT 4).
            "slack_message_ts": str,
        },
        pk="id",
        not_null={"session_id", "agent", "phase", "question"},
        if_not_exists=True,
    )
    db["questions"].create_index(["session_id"], if_not_exists=True)

    # Additive migration for DBs created before LOT 4
    if "slack_message_ts" not in db["questions"].columns_dict:
        db["questions"].add_column("slack_message_ts", str)

    db["plans"].create(
        {
            "id": str,
            "session_id": str,
            "title": str,
            "file_path": str,
            "content": str,
            "forge_status": str,
            "created_at": str,
            # Slack thread message id for the plan's review card (LOT 5 — Plan 4).
            "slack_message_ts": str,
        },
        pk="id",
        not_null={"session_id", "title", "file_path", "content"},
        if_not_exists=True,
    )
    db["plans"].create_index(["session_id"], if_not_exists=True)

    # Additive migration for DBs created before LOT 5
    if "slack_message_ts" not in db["plans"].columns_dict:
        db["plans"].add_column("slack_message_ts", str)


# ── sessions ───────────────────────────────────────────────────────────────────


def create_session(
    title: str,
    project_path: str,
    workspace_path: str,
    idea: str,
    mode: str = MODE_APPROVAL,
    db_path: Path | None = None,
    session_id: str | None = None,
    slack_channel: str | None = None,
    slack_thread_ts: str | None = None,
    slack_user_id: str | None = None,
) -> Session:
    """Insert a new session and return it."""
    db = _open(db_path)
    now = _now()
    row = {
        "id": session_id or str(uuid.uuid4()),
        "title": title,
        "project_path": str(project_path),
        "workspace_path": str(workspace_path),
        "idea": idea,
        "status": "draft",
        "mode": mode,
        "current_phase": None,
        "created_at": now,
        "updated_at": now,
        "subject_type": None,
        "research_depth": None,
        "agents_by_phase": None,
        "phase_attempts": None,
        "challenge_retry_count": 0,
        "skipped_phases": None,
        "slack_channel": slack_channel,
        "slack_thread_ts": slack_thread_ts,
        "slack_user_id": slack_user_id,
        "failure_reason": None,
    }
    db["sessions"].insert(row)
    return _to_session(row)


def get_session(session_id: str, db_path: Path | None = None) -> Session | None:
    """Return a session by ID, or None if not found."""
    db = _open(db_path)
    try:
        row = db["sessions"].get(session_id)
        return _to_session(dict(row))
    except Exception:
        return None


def update_session_status(
    session_id: str,
    status: str,
    current_phase: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Update the status (and optionally current_phase) of a session."""
    db = _open(db_path)
    updates: dict = {"status": status, "updated_at": _now()}
    if current_phase is not None:
        updates["current_phase"] = current_phase
    db["sessions"].update(session_id, updates)


def update_session_slack_thread(
    session_id: str,
    slack_thread_ts: str,
    db_path: Path | None = None,
) -> None:
    """Persist the Slack thread timestamp once the root message has been posted."""
    db = _open(db_path)
    db["sessions"].update(
        session_id,
        {"slack_thread_ts": slack_thread_ts, "updated_at": _now()},
    )


def update_session_failure_reason(
    session_id: str,
    reason: str,
    db_path: Path | None = None,
) -> None:
    """Persist a short failure reason on the session row.

    Called when the pipeline terminates in ``failed`` and (later) when a
    human reviewer rejects the session with a reason — both flows share
    this column so Slack and the CLI can surface a single consistent
    explanation.
    """
    db = _open(db_path)
    db["sessions"].update(
        session_id,
        {"failure_reason": reason, "updated_at": _now()},
    )


def list_active_sessions(db_path: Path | None = None) -> list[Session]:
    """Return all sessions that are not in a terminal state."""
    db = _open(db_path)
    placeholders = ",".join("?" * len(_TERMINAL_STATUSES))
    rows = db["sessions"].rows_where(
        f"status NOT IN ({placeholders})",
        list(_TERMINAL_STATUSES),
        order_by="created_at DESC",
    )
    return [_to_session(dict(r)) for r in rows]


def list_session_history(
    project_path: str | None = None,
    limit: int = 10,
    db_path: Path | None = None,
) -> list[Session]:
    """Return recent sessions, optionally filtered by project path."""
    db = _open(db_path)
    if project_path:
        rows = db["sessions"].rows_where(
            "project_path = ?",
            [str(project_path)],
            order_by="created_at DESC",
            limit=limit,
        )
    else:
        rows = db["sessions"].rows_where(
            order_by="created_at DESC",
            limit=limit,
        )
    return [_to_session(dict(r)) for r in rows]


# ── session profile ────────────────────────────────────────────────────────────


def update_session_profile(
    session_id: str,
    subject_type: str,
    research_depth: str,
    agents_by_phase: dict[str, list[str]],
    db_path: Path | None = None,
) -> None:
    """Persist the deterministic subject profile on the session row.

    Called once by ``squad.subject_detector`` at session start. The
    pipeline reads these fields on start and resume without ever
    reclassifying the subject.
    """
    db = _open(db_path)
    db["sessions"].update(
        session_id,
        {
            "subject_type": subject_type,
            "research_depth": research_depth,
            "agents_by_phase": json.dumps(agents_by_phase, ensure_ascii=False),
            "updated_at": _now(),
        },
    )


def mark_phase_skipped(
    session_id: str,
    phase: str,
    reason: str,
    db_path: Path | None = None,
) -> None:
    """Mark a phase as skipped with a persisted reason."""
    db = _open(db_path)
    row = db["sessions"].get(session_id)
    current = _decode_json(dict(row).get("skipped_phases"), {})
    current[phase] = reason
    db["sessions"].update(
        session_id,
        {
            "skipped_phases": json.dumps(current, ensure_ascii=False),
            "updated_at": _now(),
        },
    )


def increment_phase_attempt(
    session_id: str,
    phase: str,
    db_path: Path | None = None,
) -> int:
    """Increment the attempt counter for a phase and return the new value."""
    db = _open(db_path)
    row = db["sessions"].get(session_id)
    attempts = _decode_json(dict(row).get("phase_attempts"), {})
    new_value = int(attempts.get(phase, 0)) + 1
    attempts[phase] = new_value
    db["sessions"].update(
        session_id,
        {
            "phase_attempts": json.dumps(attempts, ensure_ascii=False),
            "updated_at": _now(),
        },
    )
    return new_value


def get_phase_attempt(
    session_id: str,
    phase: str,
    db_path: Path | None = None,
) -> int:
    """Return the current attempt count for a phase (0 if never run)."""
    db = _open(db_path)
    row = db["sessions"].get(session_id)
    attempts = _decode_json(dict(row).get("phase_attempts"), {})
    return int(attempts.get(phase, 0))


def increment_challenge_retry_count(
    session_id: str,
    db_path: Path | None = None,
) -> int:
    """Increment and return the challenge-driven retry counter."""
    db = _open(db_path)
    row = db["sessions"].get(session_id)
    current = int(dict(row).get("challenge_retry_count") or 0)
    new_value = current + 1
    db["sessions"].update(
        session_id,
        {
            "challenge_retry_count": new_value,
            "updated_at": _now(),
        },
    )
    return new_value


# ── phase outputs ──────────────────────────────────────────────────────────────


def create_phase_output(
    session_id: str,
    phase: str,
    agent: str,
    output: str,
    file_path: str,
    duration_seconds: float | None = None,
    tokens_used: int | None = None,
    attempt: int = 1,
    db_path: Path | None = None,
) -> PhaseOutput:
    """Insert a phase output record and return it.

    ``attempt`` distinguishes the first run of a phase from later retries
    (e.g. a second conception pass after challenge blockers). It lets the
    context builder pull only the latest valid attempt when re-injecting
    previous deliverables.
    """
    db = _open(db_path)
    row = {
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "phase": phase,
        "agent": agent,
        "output": output,
        "file_path": file_path,
        "duration_seconds": duration_seconds,
        "tokens_used": tokens_used,
        "attempt": attempt,
        "created_at": _now(),
    }
    db["phase_outputs"].insert(row)
    return _to_phase_output(row)


def list_phase_outputs(
    session_id: str,
    phase: str | None = None,
    attempt: int | None = None,
    db_path: Path | None = None,
) -> list[PhaseOutput]:
    """Return phase outputs for a session, optionally filtered by phase/attempt."""
    db = _open(db_path)
    clauses = ["session_id = ?"]
    params: list = [session_id]
    if phase:
        clauses.append("phase = ?")
        params.append(phase)
    if attempt is not None:
        clauses.append("attempt = ?")
        params.append(attempt)
    rows = db["phase_outputs"].rows_where(
        " AND ".join(clauses),
        params,
        order_by="created_at ASC",
    )
    return [_to_phase_output(dict(r)) for r in rows]


# ── questions ──────────────────────────────────────────────────────────────────


def create_question(
    session_id: str,
    agent: str,
    phase: str,
    question: str,
    db_path: Path | None = None,
) -> Question:
    """Insert a question and return it."""
    db = _open(db_path)
    row = {
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "agent": agent,
        "phase": phase,
        "question": question,
        "answer": None,
        "answered_at": None,
        "created_at": _now(),
    }
    db["questions"].insert(row)
    return _to_question(row)


def list_pending_questions(session_id: str, db_path: Path | None = None) -> list[Question]:
    """Return unanswered questions for a session."""
    db = _open(db_path)
    rows = db["questions"].rows_where(
        "session_id = ? AND answer IS NULL",
        [session_id],
        order_by="created_at ASC",
    )
    return [_to_question(dict(r)) for r in rows]


def answer_question(question_id: str, answer: str, db_path: Path | None = None) -> None:
    """Record an answer for a question."""
    db = _open(db_path)
    db["questions"].update(
        question_id,
        {"answer": answer, "answered_at": _now()},
    )


def get_question(question_id: str, db_path: Path | None = None) -> Question | None:
    """Return a question by ID, or None if not found."""
    db = _open(db_path)
    try:
        row = db["questions"].get(question_id)
        return _to_question(dict(row))
    except Exception:
        return None


def update_question_slack_message_ts(
    question_id: str,
    slack_message_ts: str,
    db_path: Path | None = None,
) -> None:
    """Persist the Slack message timestamp for a question (used by chat_update)."""
    db = _open(db_path)
    db["questions"].update(
        question_id,
        {"slack_message_ts": slack_message_ts},
    )


# ── plans ──────────────────────────────────────────────────────────────────────


def create_plan(
    session_id: str,
    title: str,
    file_path: str,
    content: str,
    db_path: Path | None = None,
) -> GeneratedPlan:
    """Insert a generated plan and return it."""
    db = _open(db_path)
    row = {
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "title": title,
        "file_path": file_path,
        "content": content,
        "forge_status": None,
        "created_at": _now(),
    }
    db["plans"].insert(row)
    return _to_plan(row)


def list_plans(session_id: str, db_path: Path | None = None) -> list[GeneratedPlan]:
    """Return all plans for a session."""
    db = _open(db_path)
    rows = db["plans"].rows_where(
        "session_id = ?",
        [session_id],
        order_by="created_at ASC",
    )
    return [_to_plan(dict(r)) for r in rows]


def get_plan(plan_id: str, db_path: Path | None = None) -> GeneratedPlan | None:
    """Return a generated plan by ID, or None when missing."""
    db = _open(db_path)
    try:
        row = db["plans"].get(plan_id)
        return _to_plan(dict(row))
    except Exception:
        return None


def update_plan_slack_message_ts(
    plan_id: str,
    slack_message_ts: str,
    db_path: Path | None = None,
) -> None:
    """Persist the Slack review-message ``ts`` for a plan (used by chat_update)."""
    db = _open(db_path)
    db["plans"].update(plan_id, {"slack_message_ts": slack_message_ts})
