"""Global session registry — SQLite CRUD via sqlite-utils."""

import uuid
from datetime import datetime
from pathlib import Path

from sqlite_utils import Database

from squad.config import get_global_db_path
from squad.constants import MODE_APPROVAL, STATUS_DONE, STATUS_FAILED
from squad.models import GeneratedPlan, PhaseOutput, Question, Session

# Statuses that mean a session is no longer in progress
_TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED)


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
        },
        pk="id",
        not_null={"title", "project_path", "workspace_path", "idea", "status"},
        defaults={"mode": MODE_APPROVAL},
        if_not_exists=True,
    )
    db["sessions"].create_index(["status"], if_not_exists=True)
    db["sessions"].create_index(["project_path"], if_not_exists=True)

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
            "created_at": str,
        },
        pk="id",
        not_null={"session_id", "phase", "agent", "output", "file_path"},
        if_not_exists=True,
    )
    db["phase_outputs"].create_index(["session_id"], if_not_exists=True)

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
        },
        pk="id",
        not_null={"session_id", "agent", "phase", "question"},
        if_not_exists=True,
    )
    db["questions"].create_index(["session_id"], if_not_exists=True)

    db["plans"].create(
        {
            "id": str,
            "session_id": str,
            "title": str,
            "file_path": str,
            "content": str,
            "forge_status": str,
            "created_at": str,
        },
        pk="id",
        not_null={"session_id", "title", "file_path", "content"},
        if_not_exists=True,
    )
    db["plans"].create_index(["session_id"], if_not_exists=True)


# ── sessions ───────────────────────────────────────────────────────────────────


def create_session(
    title: str,
    project_path: str,
    workspace_path: str,
    idea: str,
    mode: str = MODE_APPROVAL,
    db_path: Path | None = None,
) -> Session:
    """Insert a new session and return it."""
    db = _open(db_path)
    now = _now()
    row = {
        "id": str(uuid.uuid4()),
        "title": title,
        "project_path": str(project_path),
        "workspace_path": str(workspace_path),
        "idea": idea,
        "status": "draft",
        "mode": mode,
        "current_phase": None,
        "created_at": now,
        "updated_at": now,
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


# ── phase outputs ──────────────────────────────────────────────────────────────


def create_phase_output(
    session_id: str,
    phase: str,
    agent: str,
    output: str,
    file_path: str,
    duration_seconds: float | None = None,
    tokens_used: int | None = None,
    db_path: Path | None = None,
) -> PhaseOutput:
    """Insert a phase output record and return it."""
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
        "created_at": _now(),
    }
    db["phase_outputs"].insert(row)
    return _to_phase_output(row)


def list_phase_outputs(
    session_id: str,
    phase: str | None = None,
    db_path: Path | None = None,
) -> list[PhaseOutput]:
    """Return phase outputs for a session, optionally filtered by phase."""
    db = _open(db_path)
    if phase:
        rows = db["phase_outputs"].rows_where(
            "session_id = ? AND phase = ?",
            [session_id, phase],
            order_by="created_at ASC",
        )
    else:
        rows = db["phase_outputs"].rows_where(
            "session_id = ?",
            [session_id],
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


def list_pending_questions(
    session_id: str, db_path: Path | None = None
) -> list[Question]:
    """Return unanswered questions for a session."""
    db = _open(db_path)
    rows = db["questions"].rows_where(
        "session_id = ? AND answer IS NULL",
        [session_id],
        order_by="created_at ASC",
    )
    return [_to_question(dict(r)) for r in rows]


def answer_question(
    question_id: str, answer: str, db_path: Path | None = None
) -> None:
    """Record an answer for a question."""
    db = _open(db_path)
    db["questions"].update(
        question_id,
        {"answer": answer, "answered_at": _now()},
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
