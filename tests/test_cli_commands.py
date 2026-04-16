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
