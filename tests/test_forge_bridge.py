"""Tests for squad/forge_bridge.py — availability, queue ops, submission."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from squad.constants import STATUS_QUEUED
from squad.db import (
    create_plan,
    create_session,
    ensure_schema,
    get_session,
)
from squad.forge_bridge import (
    ForgeQueueBusy,
    ForgeUnavailable,
    QueueStatus,
    SubmitOutcome,
    add_plan_to_queue,
    get_queue_status,
    is_forge_available,
    run_queue,
    submit_session_to_forge,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "s.db"
    ensure_schema(path)
    return path


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    p = tmp_path / "target"
    p.mkdir()
    return p


@pytest.fixture
def session(db_path, project_dir, tmp_path):
    return create_session(
        title="Test",
        project_path=str(project_dir),
        workspace_path=str(tmp_path / "ws"),
        idea="idea",
        db_path=db_path,
    )


def _completed(stdout="", returncode=0, stderr=""):
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.stdout = stdout
    m.returncode = returncode
    m.stderr = stderr
    return m


# ── is_forge_available ─────────────────────────────────────────────────────────


class TestIsForgeAvailable:
    def test_true_when_on_path(self):
        with patch("squad.forge_bridge.shutil.which", return_value="/usr/bin/forge"):
            assert is_forge_available() is True

    def test_false_when_missing(self):
        with patch("squad.forge_bridge.shutil.which", return_value=None):
            assert is_forge_available() is False


# ── get_queue_status ───────────────────────────────────────────────────────────


class TestGetQueueStatus:
    def test_unavailable_when_no_binary(self):
        with patch("squad.forge_bridge.is_forge_available", return_value=False):
            status = get_queue_status("/tmp/p")
        assert status.available is False
        assert "not installed" in (status.reason or "")

    def test_available_and_idle(self):
        with (
            patch("squad.forge_bridge.is_forge_available", return_value=True),
            patch(
                "squad.forge_bridge._run_forge",
                return_value=_completed("idle", 0),
            ),
        ):
            status = get_queue_status("/tmp/p")
        assert status.available is True
        assert status.busy is False

    def test_busy_detected_from_output(self):
        with (
            patch("squad.forge_bridge.is_forge_available", return_value=True),
            patch(
                "squad.forge_bridge._run_forge",
                return_value=_completed("currently running plan-1", 0),
            ),
        ):
            status = get_queue_status("/tmp/p")
        assert status.busy is True

    def test_error_return_code_reports_unavailable(self):
        with (
            patch("squad.forge_bridge.is_forge_available", return_value=True),
            patch(
                "squad.forge_bridge._run_forge",
                return_value=_completed("", 2, stderr="forge err"),
            ),
        ):
            status = get_queue_status("/tmp/p")
        assert status.available is False
        assert "forge err" in (status.reason or "")

    def test_returns_queue_status_dataclass(self):
        with patch("squad.forge_bridge.is_forge_available", return_value=False):
            status = get_queue_status("/tmp/p")
        assert isinstance(status, QueueStatus)


# ── add_plan_to_queue / run_queue ──────────────────────────────────────────────


class TestAddPlanToQueue:
    def test_success(self):
        with patch(
            "squad.forge_bridge._run_forge",
            return_value=_completed("ok", 0),
        ) as m:
            add_plan_to_queue("/tmp/p", Path("/tmp/plan-1.md"))
        m.assert_called_once()
        assert "queue" in m.call_args.args[0]
        assert "add" in m.call_args.args[0]

    def test_failure_raises(self):
        with (
            patch(
                "squad.forge_bridge._run_forge",
                return_value=_completed("", 1, stderr="bad plan"),
            ),
            pytest.raises(ForgeUnavailable, match="bad plan"),
        ):
            add_plan_to_queue("/tmp/p", Path("/tmp/plan.md"))


class TestRunQueue:
    def test_success(self):
        with patch(
            "squad.forge_bridge._run_forge",
            return_value=_completed("queue started", 0),
        ):
            run_queue("/tmp/p")

    def test_failure_raises(self):
        with (
            patch(
                "squad.forge_bridge._run_forge",
                return_value=_completed("", 1, stderr="run broken"),
            ),
            pytest.raises(ForgeUnavailable, match="run broken"),
        ):
            run_queue("/tmp/p")


# ── submit_session_to_forge ────────────────────────────────────────────────────


class TestSubmitSessionToForge:
    def test_happy_path(self, session, db_path):
        create_plan(session.id, "plan-1", "/tmp/p1.md", "# plan", db_path=db_path)
        with (
            patch(
                "squad.forge_bridge.get_queue_status",
                return_value=QueueStatus(available=True, busy=False),
            ),
            patch("squad.forge_bridge.add_plan_to_queue") as m_add,
            patch("squad.forge_bridge.run_queue") as m_run,
        ):
            outcome = submit_session_to_forge(session.id, db_path=db_path)
        assert isinstance(outcome, SubmitOutcome)
        assert outcome.plans_sent == 1
        assert outcome.queue_started is True
        m_add.assert_called_once()
        m_run.assert_called_once()
        # Session transitioned to queued
        assert get_session(session.id, db_path=db_path).status == STATUS_QUEUED

    def test_skip_queue_run(self, session, db_path):
        create_plan(session.id, "plan-1", "/tmp/p.md", "#", db_path=db_path)
        with (
            patch(
                "squad.forge_bridge.get_queue_status",
                return_value=QueueStatus(available=True, busy=False),
            ),
            patch("squad.forge_bridge.add_plan_to_queue"),
            patch("squad.forge_bridge.run_queue") as m_run,
        ):
            outcome = submit_session_to_forge(session.id, db_path=db_path, start_queue=False)
        assert outcome.queue_started is False
        m_run.assert_not_called()

    def test_forge_unavailable_raises(self, session, db_path):
        create_plan(session.id, "p", "/tmp/p.md", "#", db_path=db_path)
        with (
            patch(
                "squad.forge_bridge.get_queue_status",
                return_value=QueueStatus(available=False, busy=False, reason="no forge"),
            ),
            pytest.raises(ForgeUnavailable, match="no forge"),
        ):
            submit_session_to_forge(session.id, db_path=db_path)
        # Status unchanged
        assert get_session(session.id, db_path=db_path).status != STATUS_QUEUED

    def test_queue_busy_raises(self, session, db_path):
        create_plan(session.id, "p", "/tmp/p.md", "#", db_path=db_path)
        with (
            patch(
                "squad.forge_bridge.get_queue_status",
                return_value=QueueStatus(available=True, busy=True),
            ),
            pytest.raises(ForgeQueueBusy),
        ):
            submit_session_to_forge(session.id, db_path=db_path)

    def test_no_plans_raises(self, session, db_path):
        with (
            patch(
                "squad.forge_bridge.get_queue_status",
                return_value=QueueStatus(available=True, busy=False),
            ),
            pytest.raises(ValueError, match="No plans"),
        ):
            submit_session_to_forge(session.id, db_path=db_path)

    def test_unknown_session_raises(self, db_path):
        with pytest.raises(ValueError, match="Session not found"):
            submit_session_to_forge("ghost", db_path=db_path)
