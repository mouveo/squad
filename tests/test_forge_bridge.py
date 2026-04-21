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
                return_value=_completed("1  executing  my-plan.md", 0),
            ),
        ):
            status = get_queue_status("/tmp/p")
        assert status.busy is True

    def test_invokes_queue_list_subcommand(self):
        """Regression: ``forge queue status`` was removed in favour of
        ``list`` when Forge reorganised its queue CLI. The adapter must
        call ``list`` or the caller surfaces an "Unknown queue sub-command"
        error on every session submission.
        """
        with (
            patch("squad.forge_bridge.is_forge_available", return_value=True),
            patch(
                "squad.forge_bridge._run_forge",
                return_value=_completed("Queue is empty.", 0),
            ) as mock_run,
        ):
            get_queue_status("/tmp/p")
        args = mock_run.call_args.args[0]
        assert args[:2] == ["queue", "list"], f"unexpected forge args: {args}"

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
    def test_spawns_detached_process(self):
        """``run_queue`` uses Popen with ``start_new_session=True`` so the
        forge runner survives the parent's exit. We assert the right
        kwargs are passed, without actually spawning anything."""
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None  # still running after 500ms
        fake_proc.pid = 1234
        with patch("subprocess.Popen", return_value=fake_proc) as popen:
            run_queue("/tmp/p")
        args, kwargs = popen.call_args
        assert args[0][:3] == ["forge", "queue", "run"]
        assert args[0][3] == "/tmp/p"
        assert kwargs.get("start_new_session") is True
        assert kwargs.get("stdout") == subprocess.DEVNULL
        assert kwargs.get("stderr") == subprocess.DEVNULL

    def test_early_exit_raises(self):
        """If the runner crashes within the 500ms sentinel window (bad
        args, missing CLI), surface as ``ForgeUnavailable``."""
        fake_proc = MagicMock()
        fake_proc.poll.return_value = 1  # exited with error
        fake_proc.returncode = 1
        with (
            patch("subprocess.Popen", return_value=fake_proc),
            pytest.raises(ForgeUnavailable, match="exited with code 1"),
        ):
            run_queue("/tmp/p")

    def test_missing_cli_raises(self):
        with (
            patch("subprocess.Popen", side_effect=FileNotFoundError("no forge")),
            pytest.raises(ForgeUnavailable, match="not found on PATH"),
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


# ── approve_and_submit (LOT 5 — Plan 4) ───────────────────────────────────────


from squad.constants import STATUS_APPROVED, STATUS_REVIEW  # noqa: E402
from squad.db import update_session_status  # noqa: E402
from squad.forge_bridge import approve_and_submit  # noqa: E402


class TestApproveAndSubmit:
    def test_happy_path_transitions_to_queued(self, session, db_path):
        update_session_status(session.id, STATUS_REVIEW, db_path=db_path)
        create_plan(session.id, "p", "/tmp/p.md", "# plan", db_path=db_path)
        with (
            patch(
                "squad.forge_bridge.get_queue_status",
                return_value=QueueStatus(available=True, busy=False),
            ),
            patch(
                "squad.forge_bridge._run_forge",
                return_value=_completed(returncode=0),
            ),
        ):
            outcome = approve_and_submit(session.id, db_path=db_path)
        assert isinstance(outcome, SubmitOutcome)
        assert outcome.plans_sent == 1
        assert get_session(session.id, db_path=db_path).status == STATUS_QUEUED

    def test_forge_unavailable_reverts_to_review(self, session, db_path):
        update_session_status(session.id, STATUS_REVIEW, db_path=db_path)
        create_plan(session.id, "p", "/tmp/p.md", "# plan", db_path=db_path)
        with (
            patch(
                "squad.forge_bridge.get_queue_status",
                return_value=QueueStatus(available=False, busy=False, reason="no forge"),
            ),
            pytest.raises(ForgeUnavailable),
        ):
            approve_and_submit(session.id, db_path=db_path)
        # Must revert to review so the session is still actionable
        assert get_session(session.id, db_path=db_path).status == STATUS_REVIEW

    def test_queue_busy_reverts_to_review(self, session, db_path):
        update_session_status(session.id, STATUS_REVIEW, db_path=db_path)
        create_plan(session.id, "p", "/tmp/p.md", "# plan", db_path=db_path)
        with (
            patch(
                "squad.forge_bridge.get_queue_status",
                return_value=QueueStatus(available=True, busy=True),
            ),
            pytest.raises(ForgeQueueBusy),
        ):
            approve_and_submit(session.id, db_path=db_path)
        assert get_session(session.id, db_path=db_path).status == STATUS_REVIEW

    def test_no_plans_reverts_to_review(self, session, db_path):
        update_session_status(session.id, STATUS_REVIEW, db_path=db_path)
        with (
            patch(
                "squad.forge_bridge.get_queue_status",
                return_value=QueueStatus(available=True, busy=False),
            ),
            pytest.raises(ValueError, match="No plans"),
        ):
            approve_and_submit(session.id, db_path=db_path)
        assert get_session(session.id, db_path=db_path).status == STATUS_REVIEW
