"""Tests for squad/cli.py — start, status, history commands."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from squad.cli import _derive_title, cli
from squad.db import ensure_schema, list_active_sessions

# ── fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / ".squad" / "squad.db"
    ensure_schema(path)
    return path


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    p = tmp_path / "my-project"
    p.mkdir()
    return p


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _run(runner: CliRunner, db_path: Path, *args):
    """Invoke the CLI with a fake DB and a no-op pipeline.

    The pipeline is mocked so ``squad start`` does not try to spawn Claude
    CLI subprocesses during CLI-level tests. Pipeline behavior itself is
    covered in ``test_pipeline.py``.
    """
    with (
        patch("squad.cli.get_global_db_path", return_value=db_path),
        patch("squad.cli.run_pipeline", return_value=None),
    ):
        return runner.invoke(cli, list(args), catch_exceptions=False)


# ── _derive_title ──────────────────────────────────────────────────────────────


class TestDeriveTitle:
    def test_short_idea_unchanged(self):
        assert _derive_title("Improve CRM") == "Improve CRM"

    def test_long_idea_truncated(self):
        long = "x" * 70
        result = _derive_title(long)
        assert len(result) <= 62  # 60 chars + ellipsis

    def test_truncated_ends_with_ellipsis(self):
        result = _derive_title("a" * 70)
        assert result.endswith("…")

    def test_exactly_max_len_unchanged(self):
        idea = "a" * 60
        assert _derive_title(idea) == idea

    def test_strips_leading_whitespace(self):
        assert _derive_title("  hello  ") == "hello"


# ── squad version ──────────────────────────────────────────────────────────────


class TestVersion:
    def test_prints_version(self, runner: CliRunner):
        result = runner.invoke(cli, ["version"])
        assert result.exit_code == 0
        assert "squad" in result.output


# ── squad start ────────────────────────────────────────────────────────────────


class TestStart:
    def test_creates_session(self, runner: CliRunner, db_path: Path, project_dir: Path):
        result = _run(runner, db_path, "start", str(project_dir), "Build a CRM module")
        assert result.exit_code == 0
        sessions = list_active_sessions(db_path=db_path)
        assert len(sessions) == 1

    def test_output_contains_session_id(self, runner, db_path, project_dir):
        result = _run(runner, db_path, "start", str(project_dir), "some idea")
        assert "Session started:" in result.output

    def test_output_contains_title(self, runner, db_path, project_dir):
        result = _run(runner, db_path, "start", str(project_dir), "Build a CRM module")
        assert "Build a CRM module" in result.output

    def test_default_mode_is_approval(self, runner, db_path, project_dir):
        _run(runner, db_path, "start", str(project_dir), "my idea")
        sessions = list_active_sessions(db_path=db_path)
        assert sessions[0].mode == "approval"

    def test_autonomous_mode_flag(self, runner, db_path, project_dir):
        _run(runner, db_path, "start", str(project_dir), "my idea", "--mode", "autonomous")
        sessions = list_active_sessions(db_path=db_path)
        assert sessions[0].mode == "autonomous"

    def test_workspace_directory_created(self, runner, db_path, project_dir):
        _run(runner, db_path, "start", str(project_dir), "my idea")
        sessions = list_active_sessions(db_path=db_path)
        workspace = Path(sessions[0].workspace_path)
        assert workspace.exists()
        assert (workspace / "idea.md").exists()

    def test_idea_md_contains_idea(self, runner, db_path, project_dir):
        _run(runner, db_path, "start", str(project_dir), "Build something great")
        sessions = list_active_sessions(db_path=db_path)
        idea_file = Path(sessions[0].workspace_path) / "idea.md"
        assert "Build something great" in idea_file.read_text()

    def test_context_md_created(self, runner, db_path, project_dir):
        _run(runner, db_path, "start", str(project_dir), "my idea")
        sessions = list_active_sessions(db_path=db_path)
        assert (Path(sessions[0].workspace_path) / "context.md").exists()

    def test_project_path_must_exist(self, runner, db_path, tmp_path):
        result = _run(runner, db_path, "start", str(tmp_path / "ghost"), "idea")
        assert result.exit_code != 0

    def test_session_status_is_draft(self, runner, db_path, project_dir):
        # With the pipeline mocked as a no-op (see _run), the session stays
        # in its initial 'draft' state. Real pipeline transitions are tested
        # in tests/test_pipeline.py.
        _run(runner, db_path, "start", str(project_dir), "my idea")
        sessions = list_active_sessions(db_path=db_path)
        assert sessions[0].status == "draft"

    def test_calls_run_pipeline_with_session_id(self, runner, db_path, project_dir):
        with (
            patch("squad.cli.get_global_db_path", return_value=db_path),
            patch("squad.cli.run_pipeline", return_value=None) as mock_pipeline,
        ):
            result = runner.invoke(
                cli,
                ["start", str(project_dir), "my idea"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert mock_pipeline.call_count == 1
        sessions = list_active_sessions(db_path=db_path)
        assert mock_pipeline.call_args.args[0] == sessions[0].id

    def test_pipeline_error_surfaces_as_click_exception(self, runner, db_path, project_dir):
        from squad.pipeline import PipelineError

        with (
            patch("squad.cli.get_global_db_path", return_value=db_path),
            patch("squad.cli.run_pipeline", side_effect=PipelineError("kaboom")),
        ):
            result = runner.invoke(
                cli, ["start", str(project_dir), "my idea"], catch_exceptions=False
            )
        assert result.exit_code != 0
        assert "kaboom" in result.output or "Pipeline failed" in result.output


# ── squad status ───────────────────────────────────────────────────────────────


class TestStatus:
    def _start(self, runner, db_path, project_dir, idea="test idea"):
        _run(runner, db_path, "start", str(project_dir), idea)
        return list_active_sessions(db_path=db_path)[0]

    def test_no_args_lists_active_sessions(self, runner, db_path, project_dir):
        self._start(runner, db_path, project_dir)
        result = _run(runner, db_path, "status")
        assert result.exit_code == 0
        assert "draft" in result.output

    def test_no_sessions_message(self, runner, db_path):
        result = _run(runner, db_path, "status")
        assert "No active sessions" in result.output

    def test_with_session_id_shows_detail(self, runner, db_path, project_dir):
        session = self._start(runner, db_path, project_dir, "Build CRM")
        result = _run(runner, db_path, "status", session.id)
        assert result.exit_code == 0
        assert session.id in result.output
        assert "Build CRM" in result.output

    def test_unknown_session_id_exits_nonzero(self, runner, db_path):
        result = _run(runner, db_path, "status", "nonexistent-id")
        assert result.exit_code != 0

    def test_detail_shows_project_path(self, runner, db_path, project_dir):
        session = self._start(runner, db_path, project_dir)
        result = _run(runner, db_path, "status", session.id)
        assert str(project_dir.resolve()) in result.output


# ── squad history ──────────────────────────────────────────────────────────────


class TestHistory:
    def test_empty_history_message(self, runner, db_path):
        result = _run(runner, db_path, "history")
        assert "No sessions found" in result.output

    def test_shows_started_sessions(self, runner, db_path, project_dir):
        _run(runner, db_path, "start", str(project_dir), "idea one")
        _run(runner, db_path, "start", str(project_dir), "idea two")
        result = _run(runner, db_path, "history")
        assert result.exit_code == 0
        lines = [line for line in result.output.splitlines() if line.strip()]
        assert len(lines) == 2

    def test_limit_flag(self, runner, db_path, project_dir):
        for i in range(5):
            _run(runner, db_path, "start", str(project_dir), f"idea {i}")
        result = _run(runner, db_path, "history", "--limit", "3")
        lines = [line for line in result.output.splitlines() if line.strip()]
        assert len(lines) == 3

    def test_project_filter(self, runner, db_path, project_dir, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        _run(runner, db_path, "start", str(project_dir), "idea A")
        _run(runner, db_path, "start", str(other), "idea B")
        result = _run(runner, db_path, "history", "--project", str(project_dir.resolve()))
        assert "idea A" in result.output
        assert "idea B" not in result.output

    def test_output_contains_status(self, runner, db_path, project_dir):
        _run(runner, db_path, "start", str(project_dir), "my idea")
        result = _run(runner, db_path, "history")
        assert "draft" in result.output


# ── squad answer / squad resume (LOT 5) ────────────────────────────────────────


from squad.db import create_question as _create_question  # noqa: E402
from squad.db import list_pending_questions  # noqa: E402


class TestAnswerCommand:
    def _prepare_session(self, runner, db_path, project_dir):
        _run(runner, db_path, "start", str(project_dir), "Build CRM")
        from squad.db import list_active_sessions

        return list_active_sessions(db_path=db_path)[0]

    def test_records_answer_and_syncs_pending(self, runner, db_path, project_dir):
        session = self._prepare_session(runner, db_path, project_dir)
        q = _create_question(session.id, "pm", "cadrage", "What?", db_path=db_path)

        with patch("squad.cli.get_global_db_path", return_value=db_path):
            result = runner.invoke(
                cli, ["answer", session.id, q.id, "SMBs"], catch_exceptions=False
            )
        assert result.exit_code == 0
        pending = list_pending_questions(session.id, db_path=db_path)
        assert pending == []
        pending_json = Path(session.workspace_path) / "questions" / "pending.json"
        assert pending_json.read_text().strip() == "[]"

    def test_unknown_session_errors(self, runner, db_path):
        with patch("squad.cli.get_global_db_path", return_value=db_path):
            result = runner.invoke(cli, ["answer", "ghost", "q1", "answer"], catch_exceptions=False)
        assert result.exit_code != 0

    def test_reports_remaining_count(self, runner, db_path, project_dir):
        session = self._prepare_session(runner, db_path, project_dir)
        q1 = _create_question(session.id, "pm", "cadrage", "Q1?", db_path=db_path)
        _create_question(session.id, "pm", "cadrage", "Q2?", db_path=db_path)

        with patch("squad.cli.get_global_db_path", return_value=db_path):
            result = runner.invoke(cli, ["answer", session.id, q1.id, "A"], catch_exceptions=False)
        assert "Remaining pending questions: 1" in result.output


class TestResumeCommand:
    def _prepare_session(self, runner, db_path, project_dir):
        _run(runner, db_path, "start", str(project_dir), "Build CRM")
        from squad.db import list_active_sessions

        return list_active_sessions(db_path=db_path)[0]

    def test_unknown_session_errors(self, runner, db_path):
        with patch("squad.cli.get_global_db_path", return_value=db_path):
            result = runner.invoke(cli, ["resume", "ghost"], catch_exceptions=False)
        assert result.exit_code != 0

    def test_resume_calls_pipeline(self, runner, db_path, project_dir):
        session = self._prepare_session(runner, db_path, project_dir)
        from squad.recovery import ResumePoint

        with (
            patch("squad.cli.get_global_db_path", return_value=db_path),
            patch(
                "squad.cli.resume_pipeline",
                return_value=ResumePoint(
                    session_id=session.id,
                    phase="etat_des_lieux",
                    reason="test",
                ),
            ) as mock_resume,
        ):
            result = runner.invoke(cli, ["resume", session.id], catch_exceptions=False)
        assert result.exit_code == 0
        mock_resume.assert_called_once()
        assert "Resumed at phase etat_des_lieux" in result.output

    def test_resume_terminal_says_nothing_to_resume(self, runner, db_path, project_dir):
        session = self._prepare_session(runner, db_path, project_dir)
        from squad.db import update_session_status

        update_session_status(session.id, "done", db_path=db_path)
        with (
            patch("squad.cli.get_global_db_path", return_value=db_path),
            patch("squad.cli.resume_pipeline", return_value=None),
        ):
            result = runner.invoke(cli, ["resume", session.id], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Nothing to resume" in result.output


# ── squad review / squad approve (LOT 7) ───────────────────────────────────────


from squad.db import create_plan, update_session_status  # noqa: E402
from squad.forge_bridge import (  # noqa: E402
    ForgeQueueBusy,
    ForgeUnavailable,
    SubmitOutcome,
)


def _prepare_session_with_plans(runner, db_path, project_dir, plan_count=1):
    _run(runner, db_path, "start", str(project_dir), "Build CRM")
    from squad.db import list_active_sessions

    session = list_active_sessions(db_path=db_path)[0]
    update_session_status(session.id, "review", db_path=db_path)
    for i in range(plan_count):
        create_plan(
            session.id,
            f"plan-{i + 1}",
            f"/tmp/plan-{i + 1}.md",
            f"# plan {i + 1}",
            db_path=db_path,
        )
    return session


class TestReviewCommand:
    def test_unknown_session_errors(self, runner, db_path):
        with patch("squad.cli.get_global_db_path", return_value=db_path):
            result = runner.invoke(cli, ["review", "ghost"], catch_exceptions=False)
        assert result.exit_code != 0

    def test_show_prints_plans(self, runner, db_path, project_dir):
        session = _prepare_session_with_plans(runner, db_path, project_dir)
        with patch("squad.cli.get_global_db_path", return_value=db_path):
            result = runner.invoke(cli, ["review", session.id], catch_exceptions=False)
        assert result.exit_code == 0
        assert "plan-1" in result.output
        assert "# plan 1" in result.output

    def test_approve_transitions_status(self, runner, db_path, project_dir):
        session = _prepare_session_with_plans(runner, db_path, project_dir)
        with patch("squad.cli.get_global_db_path", return_value=db_path):
            runner.invoke(
                cli,
                ["review", session.id, "--action", "approve"],
                catch_exceptions=False,
            )
        from squad.db import get_session as _get

        assert _get(session.id, db_path=db_path).status == "approved"

    def test_reject_sets_failed(self, runner, db_path, project_dir):
        session = _prepare_session_with_plans(runner, db_path, project_dir)
        with patch("squad.cli.get_global_db_path", return_value=db_path):
            runner.invoke(
                cli,
                ["review", session.id, "--action", "reject"],
                catch_exceptions=False,
            )
        from squad.db import get_session as _get

        assert _get(session.id, db_path=db_path).status == "failed"

    def test_edit_valid_persists_changes(self, runner, db_path, project_dir):
        session = _prepare_session_with_plans(runner, db_path, project_dir)
        valid_plan = "# proj — Plan 1/1: t\n\n> d\n> Prérequis : aucun.\n\n---\n\n" + "\n\n".join(
            f"## LOT {i} — t\n\nbody\n\n**Files**: `f.py`" for i in range(1, 6)
        )
        with (
            patch("squad.cli.get_global_db_path", return_value=db_path),
            patch("squad.cli.click.edit", return_value=valid_plan),
        ):
            result = runner.invoke(
                cli,
                ["review", session.id, "--action", "edit"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0

    def test_edit_invalid_raises(self, runner, db_path, project_dir):
        session = _prepare_session_with_plans(runner, db_path, project_dir)
        with (
            patch("squad.cli.get_global_db_path", return_value=db_path),
            patch("squad.cli.click.edit", return_value="# broken\n\nno lots"),
        ):
            result = runner.invoke(
                cli,
                ["review", session.id, "--action", "edit"],
                catch_exceptions=False,
            )
        assert result.exit_code != 0

    def test_no_plans_errors(self, runner, db_path, project_dir):
        _run(runner, db_path, "start", str(project_dir), "idea")
        from squad.db import list_active_sessions

        session = list_active_sessions(db_path=db_path)[0]
        update_session_status(session.id, "review", db_path=db_path)
        with patch("squad.cli.get_global_db_path", return_value=db_path):
            result = runner.invoke(cli, ["review", session.id], catch_exceptions=False)
        assert result.exit_code != 0


class TestApproveCommand:
    def test_unknown_session_errors(self, runner, db_path):
        with patch("squad.cli.get_global_db_path", return_value=db_path):
            result = runner.invoke(cli, ["approve", "ghost"], catch_exceptions=False)
        assert result.exit_code != 0

    def test_happy_path_submits(self, runner, db_path, project_dir):
        session = _prepare_session_with_plans(runner, db_path, project_dir)
        with (
            patch("squad.cli.get_global_db_path", return_value=db_path),
            patch(
                "squad.cli.submit_session_to_forge",
                return_value=SubmitOutcome(plans_sent=1, queue_started=True),
            ) as m_submit,
            patch("squad.cli.notify_queued") as m_notify,
        ):
            result = runner.invoke(cli, ["approve", session.id], catch_exceptions=False)
        assert result.exit_code == 0
        m_submit.assert_called_once()
        m_notify.assert_called_once()
        assert "queued" in result.output.lower() or "approved" in result.output.lower()

    def test_wrong_status_errors(self, runner, db_path, project_dir):
        _run(runner, db_path, "start", str(project_dir), "idea")
        from squad.db import list_active_sessions

        session = list_active_sessions(db_path=db_path)[0]
        # Session is 'draft' — approve should refuse
        with patch("squad.cli.get_global_db_path", return_value=db_path):
            result = runner.invoke(cli, ["approve", session.id], catch_exceptions=False)
        assert result.exit_code != 0

    def test_forge_unavailable_falls_back_to_review(self, runner, db_path, project_dir):
        session = _prepare_session_with_plans(runner, db_path, project_dir)
        with (
            patch("squad.cli.get_global_db_path", return_value=db_path),
            patch(
                "squad.cli.submit_session_to_forge",
                side_effect=ForgeUnavailable("down"),
            ),
            patch("squad.cli.notify_fallback_review") as m_fallback,
        ):
            result = runner.invoke(cli, ["approve", session.id], catch_exceptions=False)
        assert result.exit_code != 0
        m_fallback.assert_called_once()
        from squad.db import get_session as _get

        assert _get(session.id, db_path=db_path).status == "review"

    def test_queue_busy_falls_back_to_review(self, runner, db_path, project_dir):
        session = _prepare_session_with_plans(runner, db_path, project_dir)
        with (
            patch("squad.cli.get_global_db_path", return_value=db_path),
            patch(
                "squad.cli.submit_session_to_forge",
                side_effect=ForgeQueueBusy("busy"),
            ),
            patch("squad.cli.notify_fallback_review") as m_fallback,
        ):
            runner.invoke(cli, ["approve", session.id], catch_exceptions=False)
        m_fallback.assert_called_once()


class TestAutonomousStartDispatch:
    def test_autonomous_submits_on_review(self, runner, db_path, project_dir):
        """After pipeline finishes in review, autonomous mode submits to Forge."""

        def _fake_pipeline(session_id, db_path=None):
            update_session_status(session_id, "review", db_path=db_path)
            create_plan(session_id, "p", "/tmp/p.md", "# p", db_path=db_path)

        with (
            patch("squad.cli.get_global_db_path", return_value=db_path),
            patch("squad.cli.run_pipeline", side_effect=_fake_pipeline),
            patch(
                "squad.cli.submit_session_to_forge",
                return_value=SubmitOutcome(plans_sent=1, queue_started=True),
            ) as m_submit,
            patch("squad.cli.notify_queued") as m_notify,
        ):
            result = runner.invoke(
                cli,
                ["start", str(project_dir), "Build CRM", "--mode", "autonomous"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        m_submit.assert_called_once()
        m_notify.assert_called_once()

    def test_autonomous_falls_back_to_review_on_forge_error(self, runner, db_path, project_dir):
        def _fake_pipeline(session_id, db_path=None):
            update_session_status(session_id, "review", db_path=db_path)
            create_plan(session_id, "p", "/tmp/p.md", "# p", db_path=db_path)

        with (
            patch("squad.cli.get_global_db_path", return_value=db_path),
            patch("squad.cli.run_pipeline", side_effect=_fake_pipeline),
            patch(
                "squad.cli.submit_session_to_forge",
                side_effect=ForgeUnavailable("offline"),
            ),
            patch("squad.cli.notify_fallback_review") as m_fallback,
        ):
            result = runner.invoke(
                cli,
                ["start", str(project_dir), "Build CRM", "--mode", "autonomous"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0  # fallback is graceful
        m_fallback.assert_called_once()
        from squad.db import list_active_sessions

        sessions = list_active_sessions(db_path=db_path)
        assert sessions[0].status == "review"

    def test_approval_mode_does_not_submit(self, runner, db_path, project_dir):
        def _fake_pipeline(session_id, db_path=None):
            update_session_status(session_id, "review", db_path=db_path)
            create_plan(session_id, "p", "/tmp/p.md", "# p", db_path=db_path)

        with (
            patch("squad.cli.get_global_db_path", return_value=db_path),
            patch("squad.cli.run_pipeline", side_effect=_fake_pipeline),
            patch("squad.cli.submit_session_to_forge") as m_submit,
        ):
            runner.invoke(
                cli,
                ["start", str(project_dir), "Build CRM"],
                catch_exceptions=False,
            )
        m_submit.assert_not_called()
