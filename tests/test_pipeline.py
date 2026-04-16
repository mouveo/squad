"""Tests for squad/pipeline.py — happy path, pause, critical failure, persistence."""

from pathlib import Path
from unittest.mock import patch

import pytest

from squad.constants import (
    PHASE_CADRAGE,
    PHASE_CONCEPTION,
    PHASE_ETAT_DES_LIEUX,
    PHASES,
)
from squad.db import (
    create_session,
    ensure_schema,
    get_session,
    list_pending_questions,
    list_phase_outputs,
)
from squad.executor import AgentError
from squad.pipeline import (
    PhaseResult,
    PipelineError,
    run_phase,
    run_pipeline,
)
from squad.workspace import create_workspace

# ── fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / ".squad" / "squad.db"
    ensure_schema(path)
    return path


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    project = tmp_path / "target-project"
    project.mkdir()
    return project


@pytest.fixture
def session(db_path: Path, tmp_path: Path, project_dir: Path):
    workspace_path = tmp_path / "workspace"
    s = create_session(
        title="Test",
        project_path=str(project_dir),
        workspace_path=str(workspace_path),
        idea="Build something",
        db_path=db_path,
    )
    create_workspace(s)
    return s


@pytest.fixture
def happy_pm_output() -> str:
    """A cadrage pm output without any pause request."""
    return '# Cadrage\n\nreformulation\n```json\n{"questions": [], "needs_pause": false}\n```'


def _configure_mocks(run_agent_mock, run_parallel_mock, pm_output: str) -> None:
    """Seed executor mocks with deterministic outputs for all phases."""
    run_agent_mock.side_effect = lambda agent_name, session_id, phase, context_sections=None: (
        pm_output if agent_name == "pm" else f"# {agent_name} output for {phase}"
    )

    def _parallel(agents_list, session_id, phase, context_sections_by_agent=None):
        return {a: f"# {a} output for {phase}" for a in agents_list}

    run_parallel_mock.side_effect = _parallel


# ── happy path ─────────────────────────────────────────────────────────────────


class TestHappyPath:
    def test_runs_all_six_phases_in_order(self, db_path, session, happy_pm_output):
        calls: list[tuple[str, str]] = []

        def _record_agent(agent_name, session_id, phase, context_sections=None):
            calls.append((phase, agent_name))
            return happy_pm_output if agent_name == "pm" else f"# {agent_name} / {phase}"

        def _record_parallel(agents_list, session_id, phase, context_sections_by_agent=None):
            results = {}
            for a in agents_list:
                calls.append((phase, a))
                results[a] = f"# {a} / {phase}"
            return results

        with (
            patch("squad.pipeline.run_agent", side_effect=_record_agent),
            patch("squad.pipeline.run_agents_parallel", side_effect=_record_parallel),
        ):
            run_pipeline(session.id, db_path=db_path)

        phases_run = [p for p, _ in calls]
        # Each of the 6 phases should appear at least once, in canonical order
        first_indexes = [phases_run.index(p) for p in PHASES]
        assert first_indexes == sorted(first_indexes)

    def test_status_is_review_after_pipeline(self, db_path, session, happy_pm_output):
        with (
            patch("squad.pipeline.run_agent") as m_agent,
            patch("squad.pipeline.run_agents_parallel") as m_par,
        ):
            _configure_mocks(m_agent, m_par, happy_pm_output)
            run_pipeline(session.id, db_path=db_path)

        updated = get_session(session.id, db_path=db_path)
        assert updated.status == "review"
        assert updated.current_phase == PHASES[-1]

    def test_phase_outputs_persisted(self, db_path, session, happy_pm_output):
        with (
            patch("squad.pipeline.run_agent") as m_agent,
            patch("squad.pipeline.run_agents_parallel") as m_par,
        ):
            _configure_mocks(m_agent, m_par, happy_pm_output)
            run_pipeline(session.id, db_path=db_path)

        outputs = list_phase_outputs(session.id, db_path=db_path)
        phases_with_output = {po.phase for po in outputs}
        # All 6 phases should have persisted at least one deliverable
        assert set(PHASES).issubset(phases_with_output)

    def test_parallel_phase_uses_parallel_executor(self, db_path, session, happy_pm_output):
        with (
            patch("squad.pipeline.run_agent") as m_agent,
            patch("squad.pipeline.run_agents_parallel") as m_par,
        ):
            _configure_mocks(m_agent, m_par, happy_pm_output)
            run_pipeline(session.id, db_path=db_path)

        phases_dispatched = [
            call.kwargs.get("phase", call.args[2] if len(call.args) > 2 else None)
            for call in m_par.call_args_list
        ]
        assert PHASE_ETAT_DES_LIEUX in phases_dispatched
        assert PHASE_CONCEPTION in phases_dispatched


# ── run_phase ──────────────────────────────────────────────────────────────────


class TestRunPhase:
    def test_marks_session_working_and_current_phase(self, db_path, session, happy_pm_output):
        with patch("squad.pipeline.run_agent", return_value=happy_pm_output):
            run_phase(session.id, PHASE_CADRAGE, db_path=db_path)

        updated = get_session(session.id, db_path=db_path)
        assert updated.current_phase == PHASE_CADRAGE
        # Cadrage without pause ends 'working', not 'review'
        assert updated.status == "working"

    def test_returns_phase_result(self, db_path, session, happy_pm_output):
        with patch("squad.pipeline.run_agent", return_value=happy_pm_output):
            result = run_phase(session.id, PHASE_CADRAGE, db_path=db_path)
        assert isinstance(result, PhaseResult)
        assert result.phase == PHASE_CADRAGE
        assert "pm" in result.outputs
        assert result.paused is False


# ── pause ──────────────────────────────────────────────────────────────────────


class TestPause:
    def test_cadrage_with_questions_pauses_pipeline(self, db_path, session):
        paused_output = (
            "# Cadrage\n\nneed answers\n"
            '```json\n{"questions": [{"id": "q1", "question": "Scope?"}], '
            '"needs_pause": true}\n```'
        )

        with (
            patch("squad.pipeline.run_agent", return_value=paused_output),
            patch("squad.pipeline.run_agents_parallel") as m_par,
        ):
            run_pipeline(session.id, db_path=db_path)

        updated = get_session(session.id, db_path=db_path)
        assert updated.status == "interviewing"
        assert updated.current_phase == PHASE_CADRAGE
        # No later phase should have been dispatched
        m_par.assert_not_called()

    def test_questions_persisted_on_pause(self, db_path, session):
        paused_output = (
            '```json\n{"questions": [{"id": "q1", "question": "Why?"}, '
            '{"id": "q2", "question": "Who?"}], "needs_pause": true}\n```'
        )

        with (
            patch("squad.pipeline.run_agent", return_value=paused_output),
            patch("squad.pipeline.run_agents_parallel"),
        ):
            run_pipeline(session.id, db_path=db_path)

        pending = list_pending_questions(session.id, db_path=db_path)
        assert len(pending) == 2


# ── failures ───────────────────────────────────────────────────────────────────


class TestFailures:
    def test_critical_pm_failure_fails_session(self, db_path, session):
        with patch(
            "squad.pipeline.run_agent",
            side_effect=AgentError("pm exploded"),
        ):
            with pytest.raises((PipelineError, AgentError)):
                run_pipeline(session.id, db_path=db_path)

        updated = get_session(session.id, db_path=db_path)
        assert updated.status == "failed"

    def test_session_not_found_raises(self, db_path):
        with pytest.raises(PipelineError):
            run_pipeline("ghost-id", db_path=db_path)

    def test_parallel_total_failure_fails_session(self, db_path, session, happy_pm_output):
        def _parallel_fails(agents_list, session_id, phase, context_sections_by_agent=None):
            raise AgentError("all down")

        with (
            patch("squad.pipeline.run_agent", return_value=happy_pm_output),
            patch("squad.pipeline.run_agents_parallel", side_effect=_parallel_fails),
        ):
            with pytest.raises(PipelineError):
                run_pipeline(session.id, db_path=db_path)

        updated = get_session(session.id, db_path=db_path)
        assert updated.status == "failed"
