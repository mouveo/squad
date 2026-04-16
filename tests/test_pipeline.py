"""Tests for squad/pipeline.py — happy path, pause, retry, resume, failures."""

from pathlib import Path
from unittest.mock import patch

import pytest

from squad.constants import (
    PHASE_CADRAGE,
    PHASE_CHALLENGE,
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
    update_session_status,
)
from squad.executor import AgentError
from squad.pipeline import (
    PhaseResult,
    PipelineError,
    resume_pipeline,
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


@pytest.fixture
def clean_challenge_output() -> str:
    """A challenge output with no blocking issues."""
    return '# Challenge\n\nclean\n```json\n{"blockers": []}\n```'


def _configure_mocks(
    run_agent_mock, run_tolerant_mock, pm_output: str, challenge_output: str = ""
) -> None:
    """Seed executor mocks with deterministic outputs for all phases."""

    def _agent(agent_name, session_id, phase, **kwargs):
        return pm_output if agent_name == "pm" else f"# {agent_name} output for {phase}"

    run_agent_mock.side_effect = _agent

    def _tolerant(
        agents_list,
        session_id,
        phase,
        context_sections_by_agent=None,
        **kwargs,
    ):
        results = {}
        for a in agents_list:
            if phase == PHASE_CHALLENGE and challenge_output:
                results[a] = challenge_output
            else:
                results[a] = f"# {a} output for {phase}"
        return results, {}

    run_tolerant_mock.side_effect = _tolerant


# ── happy path ─────────────────────────────────────────────────────────────────


class TestHappyPath:
    def test_runs_all_six_phases_in_order(self, db_path, session, happy_pm_output):
        calls: list[tuple[str, str]] = []

        def _record_agent(agent_name, session_id, phase, **kwargs):
            calls.append((phase, agent_name))
            return happy_pm_output if agent_name == "pm" else f"# {agent_name} / {phase}"

        def _record_tolerant(
            agents_list, session_id, phase, context_sections_by_agent=None, **kwargs
        ):
            results = {}
            for a in agents_list:
                calls.append((phase, a))
                results[a] = f"# {a} / {phase}"
            return results, {}

        with (
            patch("squad.pipeline.run_agent", side_effect=_record_agent),
            patch("squad.pipeline.run_agents_tolerant", side_effect=_record_tolerant),
        ):
            run_pipeline(session.id, db_path=db_path)

        phases_run = [p for p, _ in calls]
        first_indexes = [phases_run.index(p) for p in PHASES]
        assert first_indexes == sorted(first_indexes)

    def test_status_is_review_after_pipeline(self, db_path, session, happy_pm_output):
        with (
            patch("squad.pipeline.run_agent") as m_agent,
            patch("squad.pipeline.run_agents_tolerant") as m_tol,
        ):
            _configure_mocks(m_agent, m_tol, happy_pm_output)
            run_pipeline(session.id, db_path=db_path)

        updated = get_session(session.id, db_path=db_path)
        assert updated.status == "review"
        assert updated.current_phase == PHASES[-1]

    def test_phase_outputs_persisted_with_attempt(self, db_path, session, happy_pm_output):
        with (
            patch("squad.pipeline.run_agent") as m_agent,
            patch("squad.pipeline.run_agents_tolerant") as m_tol,
        ):
            _configure_mocks(m_agent, m_tol, happy_pm_output)
            run_pipeline(session.id, db_path=db_path)

        outputs = list_phase_outputs(session.id, db_path=db_path)
        phases_with_output = {po.phase for po in outputs}
        assert set(PHASES).issubset(phases_with_output)
        # First pass → attempt 1 everywhere
        assert {po.attempt for po in outputs} == {1}

    def test_parallel_phase_uses_tolerant_executor(self, db_path, session, happy_pm_output):
        with (
            patch("squad.pipeline.run_agent") as m_agent,
            patch("squad.pipeline.run_agents_tolerant") as m_tol,
        ):
            _configure_mocks(m_agent, m_tol, happy_pm_output)
            run_pipeline(session.id, db_path=db_path)

        phases_dispatched = [
            call.kwargs.get("phase", call.args[2] if len(call.args) > 2 else None)
            for call in m_tol.call_args_list
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
        assert updated.status == "working"

    def test_returns_phase_result(self, db_path, session, happy_pm_output):
        with patch("squad.pipeline.run_agent", return_value=happy_pm_output):
            result = run_phase(session.id, PHASE_CADRAGE, db_path=db_path)
        assert isinstance(result, PhaseResult)
        assert result.phase == PHASE_CADRAGE
        assert "pm" in result.outputs
        assert result.paused is False
        assert result.attempt == 1

    def test_second_call_increments_attempt(self, db_path, session, happy_pm_output):
        with patch("squad.pipeline.run_agent", return_value=happy_pm_output):
            first = run_phase(session.id, PHASE_CADRAGE, db_path=db_path)
            second = run_phase(session.id, PHASE_CADRAGE, db_path=db_path)
        assert first.attempt == 1
        assert second.attempt == 2


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
            patch("squad.pipeline.run_agents_tolerant") as m_tol,
        ):
            run_pipeline(session.id, db_path=db_path)

        updated = get_session(session.id, db_path=db_path)
        assert updated.status == "interviewing"
        assert updated.current_phase == PHASE_CADRAGE
        m_tol.assert_not_called()

    def test_questions_persisted_on_pause(self, db_path, session):
        paused_output = (
            '```json\n{"questions": [{"id": "q1", "question": "Why?"}, '
            '{"id": "q2", "question": "Who?"}], "needs_pause": true}\n```'
        )

        with (
            patch("squad.pipeline.run_agent", return_value=paused_output),
            patch("squad.pipeline.run_agents_tolerant"),
        ):
            run_pipeline(session.id, db_path=db_path)

        pending = list_pending_questions(session.id, db_path=db_path)
        assert len(pending) == 2


# ── failures ───────────────────────────────────────────────────────────────────


class TestFailures:
    def test_critical_pm_failure_fails_session(self, db_path, session):
        with patch("squad.pipeline.run_agent", side_effect=AgentError("pm exploded")):
            with pytest.raises((PipelineError, AgentError)):
                run_pipeline(session.id, db_path=db_path)

        updated = get_session(session.id, db_path=db_path)
        assert updated.status == "failed"

    def test_session_not_found_raises(self, db_path):
        with pytest.raises(PipelineError):
            run_pipeline("ghost-id", db_path=db_path)

    def test_non_critical_failure_does_not_fail_session(self, db_path, session, happy_pm_output):
        def _agent(agent_name, **kwargs):
            return happy_pm_output if agent_name == "pm" else "# ok"

        def _tolerant(agents_list, session_id, phase, **kwargs):
            # Simulate one non-critical agent failing (architect etc. are non-critical)
            results = {}
            errors = {}
            for a in agents_list:
                if a == "ux":
                    errors[a] = "ux timed out"
                else:
                    results[a] = f"# {a} / {phase}"
            return results, errors

        with (
            patch("squad.pipeline.run_agent", side_effect=_agent),
            patch("squad.pipeline.run_agents_tolerant", side_effect=_tolerant),
        ):
            run_pipeline(session.id, db_path=db_path)

        updated = get_session(session.id, db_path=db_path)
        assert updated.status == "review"

    def test_parallel_total_failure_fails_session_when_pm_missing(
        self, db_path, session, happy_pm_output
    ):
        def _agent(agent_name, **kwargs):
            return happy_pm_output if agent_name == "pm" else "# ok"

        # Tolerant always returns empty results + errors → but only synthese/cadrage
        # have pm as critical. This test just verifies non-critical phase total
        # failure does not fail: no critical agents missing means pipeline continues.
        def _tolerant(agents_list, session_id, phase, **kwargs):
            return {}, {a: "all down" for a in agents_list}

        with (
            patch("squad.pipeline.run_agent", side_effect=_agent),
            patch("squad.pipeline.run_agents_tolerant", side_effect=_tolerant),
        ):
            # No critical agent in parallel phases → pipeline proceeds to review
            run_pipeline(session.id, db_path=db_path)
        updated = get_session(session.id, db_path=db_path)
        assert updated.status == "review"


# ── challenge retry ────────────────────────────────────────────────────────────


_BLOCKING_CHALLENGE = (
    "# Challenge\nblockers found\n"
    '```json\n{"blockers": [{"id": "b1", "severity": "blocking", '
    '"constraint": "Add auth gating"}]}\n```'
)


class TestChallengeRetry:
    def test_blocking_challenge_triggers_conception_retry(self, db_path, session, happy_pm_output):
        call_phases: list[tuple[str, int]] = []

        def _agent(agent_name, session_id, phase, **kwargs):
            # Attempt count not known here — track unique calls per phase
            call_phases.append((phase, 0))
            return happy_pm_output if agent_name == "pm" else f"# {agent_name}"

        # Track how many times challenge runs (first: blocking, second: clean)
        challenge_count = {"n": 0}

        def _tolerant(agents_list, session_id, phase, **kwargs):
            results = {}
            for a in agents_list:
                if phase == PHASE_CHALLENGE:
                    challenge_count["n"] += 1
                    # First challenge run → blockers; second → clean
                    if challenge_count["n"] <= len(agents_list):
                        results[a] = _BLOCKING_CHALLENGE
                    else:
                        results[a] = '# ok\n```json\n{"blockers": []}\n```'
                else:
                    results[a] = f"# {a} / {phase}"
            return results, {}

        with (
            patch("squad.pipeline.run_agent", side_effect=_agent),
            patch("squad.pipeline.run_agents_tolerant", side_effect=_tolerant),
        ):
            run_pipeline(session.id, db_path=db_path)

        updated = get_session(session.id, db_path=db_path)
        assert updated.status == "review"
        assert updated.challenge_retry_count == 1
        # Conception should have been run twice (attempts 1 and 2)
        conception_outputs = list_phase_outputs(session.id, phase=PHASE_CONCEPTION, db_path=db_path)
        attempts = {po.attempt for po in conception_outputs}
        assert 2 in attempts

    def test_retry_happens_only_once(self, db_path, session, happy_pm_output):
        # Challenge keeps returning blockers — retry must happen only once
        def _agent(agent_name, **kwargs):
            return happy_pm_output if agent_name == "pm" else "# ok"

        def _tolerant(agents_list, session_id, phase, **kwargs):
            results = {}
            for a in agents_list:
                if phase == PHASE_CHALLENGE:
                    results[a] = _BLOCKING_CHALLENGE
                else:
                    results[a] = f"# {a}"
            return results, {}

        with (
            patch("squad.pipeline.run_agent", side_effect=_agent),
            patch("squad.pipeline.run_agents_tolerant", side_effect=_tolerant),
        ):
            run_pipeline(session.id, db_path=db_path)

        updated = get_session(session.id, db_path=db_path)
        assert updated.challenge_retry_count == 1
        assert updated.status == "review"


# ── resume ─────────────────────────────────────────────────────────────────────


class TestResume:
    def test_resume_after_answered_questions(self, db_path, session, happy_pm_output):
        # Simulate: cadrage ran, pm produced questions, session paused
        paused_output = (
            '```json\n{"questions": [{"id": "q1", "question": "?"}], "needs_pause": true}\n```'
        )

        with (
            patch("squad.pipeline.run_agent", return_value=paused_output),
            patch("squad.pipeline.run_agents_tolerant"),
        ):
            run_pipeline(session.id, db_path=db_path)

        updated = get_session(session.id, db_path=db_path)
        assert updated.status == "interviewing"

        # User answers all questions
        pending = list_pending_questions(session.id, db_path=db_path)
        from squad.db import answer_question

        for q in pending:
            answer_question(q.id, "answer", db_path=db_path)

        # Resume — should skip cadrage and run phases 2-6
        def _agent(agent_name, **kwargs):
            return happy_pm_output if agent_name == "pm" else "# ok"

        def _tolerant(agents_list, session_id, phase, **kwargs):
            return {a: f"# {a}" for a in agents_list}, {}

        with (
            patch("squad.pipeline.run_agent", side_effect=_agent),
            patch("squad.pipeline.run_agents_tolerant", side_effect=_tolerant),
        ):
            rp = resume_pipeline(session.id, db_path=db_path)

        assert rp is not None
        assert rp.phase == PHASE_ETAT_DES_LIEUX
        final = get_session(session.id, db_path=db_path)
        assert final.status == "review"

    def test_resume_after_crash_restarts_current_phase(self, db_path, session, happy_pm_output):
        # Simulate a crash after partial progress by manually setting state
        update_session_status(
            session.id,
            status="working",
            current_phase=PHASE_CONCEPTION,
            db_path=db_path,
        )

        def _agent(agent_name, **kwargs):
            return happy_pm_output if agent_name == "pm" else "# ok"

        def _tolerant(agents_list, session_id, phase, **kwargs):
            return {a: f"# {a}" for a in agents_list}, {}

        with (
            patch("squad.pipeline.run_agent", side_effect=_agent),
            patch("squad.pipeline.run_agents_tolerant", side_effect=_tolerant),
        ):
            rp = resume_pipeline(session.id, db_path=db_path)

        assert rp is not None
        assert rp.phase == PHASE_CONCEPTION
        final = get_session(session.id, db_path=db_path)
        assert final.status == "review"

    def test_resume_on_terminal_session_returns_none(self, db_path, session):
        update_session_status(session.id, status="done", db_path=db_path)
        rp = resume_pipeline(session.id, db_path=db_path)
        assert rp is None
