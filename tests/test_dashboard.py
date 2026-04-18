"""Tests for the dashboard read layer (LOT 1 — count_sessions only)."""

from pathlib import Path

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
