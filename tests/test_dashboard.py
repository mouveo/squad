"""Tests for the dashboard read layer and shared status semantics."""

from pathlib import Path

from squad.constants import (
    ACTIVE_STATUSES,
    SESSION_STATUSES,
    STATUS_APPROVED,
    STATUS_DONE,
    STATUS_DRAFT,
    STATUS_FAILED,
    STATUS_INTERVIEWING,
    STATUS_LABELS,
    STATUS_QUEUED,
    STATUS_REVIEW,
    STATUS_TONES,
    STATUS_WORKING,
    TERMINAL_STATUSES,
)
from squad.dashboard.data import count_sessions
from squad.db import create_session, ensure_schema


def test_count_sessions_zero_on_fresh_db(tmp_path: Path) -> None:
    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    assert count_sessions(db_path=db_path) == 0


def test_count_sessions_missing_db_file(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"
    assert count_sessions(db_path=db_path) == 0


def test_count_sessions_after_inserts(tmp_path: Path) -> None:
    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    for i in range(3):
        create_session(
            title=f"session-{i}",
            project_path=str(tmp_path),
            workspace_path=str(tmp_path / f"ws-{i}"),
            idea=f"idea {i}",
            db_path=db_path,
        )
    assert count_sessions(db_path=db_path) == 3


# ── Shared status semantics used by the dashboard ──────────────────────────────


def test_terminal_statuses_are_only_done_and_failed() -> None:
    assert TERMINAL_STATUSES == {STATUS_DONE, STATUS_FAILED}


def test_active_statuses_exclude_only_terminal() -> None:
    assert STATUS_DONE not in ACTIVE_STATUSES
    assert STATUS_FAILED not in ACTIVE_STATUSES
    # Every non-terminal status must be considered active.
    for status in SESSION_STATUSES:
        if status in TERMINAL_STATUSES:
            continue
        assert status in ACTIVE_STATUSES


def test_active_statuses_aligned_with_db_layer(tmp_path: Path) -> None:
    """The dashboard's notion of "active" must match the DB's own filter.

    The CLI `squad status` relies on `list_active_sessions`, which
    itself uses `TERMINAL_STATUSES`. If these two ever drift, a session
    would appear "active" in one surface and "terminated" in another.
    """
    from squad.db import list_active_sessions

    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    # Seed one session per status, then confirm only active ones list.
    active_ids: set[str] = set()
    for i, status in enumerate(SESSION_STATUSES):
        sess = create_session(
            title=f"s-{status}",
            project_path=str(tmp_path),
            workspace_path=str(tmp_path / f"ws-{i}"),
            idea="x",
            db_path=db_path,
        )
        # Flip to target status (create_session defaults to draft)
        from squad.db import update_session_status

        update_session_status(sess.id, status, db_path=db_path)
        if status in ACTIVE_STATUSES:
            active_ids.add(sess.id)
    listed = {s.id for s in list_active_sessions(db_path=db_path)}
    assert listed == active_ids


def test_status_labels_cover_all_eight_statuses() -> None:
    expected = {
        STATUS_DRAFT,
        STATUS_WORKING,
        STATUS_INTERVIEWING,
        STATUS_REVIEW,
        STATUS_APPROVED,
        STATUS_QUEUED,
        STATUS_DONE,
        STATUS_FAILED,
    }
    assert set(STATUS_LABELS.keys()) == expected
    # No empty label.
    assert all(isinstance(v, str) and v for v in STATUS_LABELS.values())


def test_status_tones_cover_all_eight_statuses() -> None:
    assert set(STATUS_TONES.keys()) == set(STATUS_LABELS.keys())
    allowed = {"neutral", "progress", "info", "warning", "success", "muted", "danger"}
    for status, tone in STATUS_TONES.items():
        assert tone in allowed, f"{status} has unexpected tone {tone!r}"


# ── Shared reject service ─────────────────────────────────────────────────────


def test_reject_session_persists_reason_and_flips_status(tmp_path: Path) -> None:
    from squad.db import get_session
    from squad.review_service import reject_session

    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    sess = create_session(
        title="t",
        project_path=str(tmp_path),
        workspace_path=str(tmp_path / "ws"),
        idea="x",
        db_path=db_path,
    )
    reject_session(sess.id, "not clear enough", db_path=db_path)
    refreshed = get_session(sess.id, db_path=db_path)
    assert refreshed.status == STATUS_FAILED
    assert refreshed.failure_reason == "not clear enough"
