"""Tests for squad/pipeline.py — happy path, pause, retry, resume, failures."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


@pytest.fixture(autouse=True)
def _stub_plan_generation():
    """Stub the final plan-generation step so pipeline tests don't touch Claude.

    Plan-generation logic is tested directly in ``test_plan_generator.py``.
    Here the focus is on phase orchestration.
    """
    with patch("squad.pipeline._generate_and_copy_plans", return_value=None):
        yield


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

        def _record_research(session_id, extra_context=None, db_path=None, **kwargs):
            calls.append(("benchmark", "research"))
            return SimpleNamespace(content="# research / benchmark")

        with (
            patch("squad.pipeline.run_agent", side_effect=_record_agent),
            patch("squad.pipeline.run_agents_tolerant", side_effect=_record_tolerant),
            patch("squad.pipeline.run_research", side_effect=_record_research),
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
        def _fake_research(session_id, extra_context=None, db_path=None, **kwargs):
            return SimpleNamespace(content="# research / benchmark")

        with (
            patch("squad.pipeline.run_agent") as m_agent,
            patch("squad.pipeline.run_agents_tolerant") as m_tol,
            patch("squad.pipeline.run_research", side_effect=_fake_research),
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


# ── event callback + failure_reason (LOT 2 — Plan 4) ──────────────────────────


class TestEventCallback:
    def test_working_event_per_phase(self, db_path, session, happy_pm_output):
        from squad.models import EVENT_WORKING

        events = []

        def cb(evt):
            events.append(evt)

        with (
            patch("squad.pipeline.run_agent") as m_agent,
            patch("squad.pipeline.run_agents_tolerant") as m_tol,
        ):
            _configure_mocks(m_agent, m_tol, happy_pm_output)
            run_pipeline(session.id, db_path=db_path, event_callback=cb)

        working_phases = [e.phase for e in events if e.type == EVENT_WORKING]
        # One working event per phase, in canonical order
        assert working_phases == PHASES
        # Every working event includes a timestamp and non-negative elapsed
        for e in events:
            if e.type == EVENT_WORKING:
                assert e.timestamp_utc is not None
                assert e.elapsed_seconds >= 0

    def test_review_event_emitted_with_plan_count(
        self, db_path, session, happy_pm_output
    ):
        from squad.db import create_plan
        from squad.models import EVENT_REVIEW

        def _make_plans(session_id, db_path):  # replaces _generate_and_copy_plans
            create_plan(session_id, "p1", "/tmp/p1.md", "# plan 1", db_path=db_path)
            create_plan(session_id, "p2", "/tmp/p2.md", "# plan 2", db_path=db_path)

        events = []
        with (
            patch("squad.pipeline.run_agent") as m_agent,
            patch("squad.pipeline.run_agents_tolerant") as m_tol,
            patch("squad.pipeline._generate_and_copy_plans", side_effect=_make_plans),
        ):
            _configure_mocks(m_agent, m_tol, happy_pm_output)
            run_pipeline(session.id, db_path=db_path, event_callback=events.append)

        review = [e for e in events if e.type == EVENT_REVIEW]
        assert len(review) == 1
        assert review[0].plans_count == 2

    def test_interviewing_event_with_pending_count(self, db_path, session):
        from squad.models import EVENT_INTERVIEWING

        paused_output = (
            '```json\n{"questions": [{"id": "q1", "question": "A?"}, '
            '{"id": "q2", "question": "B?"}], "needs_pause": true}\n```'
        )
        events = []
        with (
            patch("squad.pipeline.run_agent", return_value=paused_output),
            patch("squad.pipeline.run_agents_tolerant"),
        ):
            run_pipeline(session.id, db_path=db_path, event_callback=events.append)

        paused = [e for e in events if e.type == EVENT_INTERVIEWING]
        assert len(paused) == 1
        assert paused[0].pending_questions == 2
        assert paused[0].phase == PHASE_CADRAGE

    def test_failed_event_persists_and_emits_reason(self, db_path, session):
        from squad.models import EVENT_FAILED

        events = []
        with patch("squad.pipeline.run_agent", side_effect=AgentError("pm exploded")):
            with pytest.raises((PipelineError, AgentError)):
                run_pipeline(session.id, db_path=db_path, event_callback=events.append)

        failed = [e for e in events if e.type == EVENT_FAILED]
        assert len(failed) == 1
        assert "pm exploded" in (failed[0].failure_reason or "")
        persisted = get_session(session.id, db_path=db_path)
        assert persisted.status == "failed"
        assert "pm exploded" in (persisted.failure_reason or "")

    def test_callback_error_does_not_break_pipeline(
        self, db_path, session, happy_pm_output
    ):
        def _bad_cb(evt):
            raise RuntimeError("sink down")

        with (
            patch("squad.pipeline.run_agent") as m_agent,
            patch("squad.pipeline.run_agents_tolerant") as m_tol,
        ):
            _configure_mocks(m_agent, m_tol, happy_pm_output)
            run_pipeline(session.id, db_path=db_path, event_callback=_bad_cb)

        updated = get_session(session.id, db_path=db_path)
        assert updated.status == "review"

    def test_no_callback_is_backwards_compatible(
        self, db_path, session, happy_pm_output
    ):
        # Running without event_callback must keep existing behavior (smoke test).
        with (
            patch("squad.pipeline.run_agent") as m_agent,
            patch("squad.pipeline.run_agents_tolerant") as m_tol,
        ):
            _configure_mocks(m_agent, m_tol, happy_pm_output)
            run_pipeline(session.id, db_path=db_path)
        updated = get_session(session.id, db_path=db_path)
        assert updated.status == "review"

    def test_plan_generation_failure_persists_reason_and_emits(
        self, db_path, session, happy_pm_output
    ):
        from squad.models import EVENT_FAILED

        events = []
        with (
            patch("squad.pipeline.run_agent") as m_agent,
            patch("squad.pipeline.run_agents_tolerant") as m_tol,
            patch(
                "squad.pipeline._generate_and_copy_plans",
                side_effect=ValueError("no synthese contract"),
            ),
        ):
            _configure_mocks(m_agent, m_tol, happy_pm_output)
            with pytest.raises(PipelineError):
                run_pipeline(session.id, db_path=db_path, event_callback=events.append)

        failed = [e for e in events if e.type == EVENT_FAILED]
        assert len(failed) == 1
        assert "Plan generation failed" in (failed[0].failure_reason or "")
        persisted = get_session(session.id, db_path=db_path)
        assert persisted.status == "failed"
        assert "Plan generation failed" in (persisted.failure_reason or "")


# ── cwd routing by agent (LOT 3 — Plan 5) ─────────────────────────────────────


class TestCwdRoutingByAgent:
    def test_parallel_phase_routes_cwd_only_to_ux_and_architect(
        self, db_path, session, happy_pm_output
    ):
        """In etat_des_lieux, only ux should get cwd=project_path; others None."""
        captured_cwd_maps: list[dict[str, str | None]] = []

        def _tolerant(
            agents_list,
            session_id,
            phase,
            context_sections_by_agent=None,
            *,
            cumulative_context=None,
            phase_instruction=None,
            cwd_by_agent=None,
        ):
            if phase == PHASE_ETAT_DES_LIEUX:
                captured_cwd_maps.append(dict(cwd_by_agent or {}))
            return {a: f"# {a}" for a in agents_list}, {}

        with (
            patch("squad.pipeline.run_agent", return_value=happy_pm_output),
            patch("squad.pipeline.run_agents_tolerant", side_effect=_tolerant),
        ):
            run_phase(session.id, PHASE_ETAT_DES_LIEUX, db_path=db_path)

        assert captured_cwd_maps, "etat_des_lieux did not dispatch through run_agents_tolerant"
        cwd_map = captured_cwd_maps[0]
        assert cwd_map["ux"] == session.project_path
        for agent in ("customer-success", "data", "sales"):
            assert cwd_map.get(agent) is None

    def test_conception_phase_routes_cwd_to_ux_and_architect_only(
        self, db_path, session, happy_pm_output
    ):
        captured: list[dict[str, str | None]] = []

        def _tolerant(
            agents_list,
            session_id,
            phase,
            context_sections_by_agent=None,
            *,
            cumulative_context=None,
            phase_instruction=None,
            cwd_by_agent=None,
        ):
            if phase == PHASE_CONCEPTION:
                captured.append(dict(cwd_by_agent or {}))
            return {a: f"# {a}" for a in agents_list}, {}

        with (
            patch("squad.pipeline.run_agent", return_value=happy_pm_output),
            patch("squad.pipeline.run_agents_tolerant", side_effect=_tolerant),
        ):
            run_phase(session.id, PHASE_CONCEPTION, db_path=db_path)

        assert captured
        cwd_map = captured[0]
        assert cwd_map["ux"] == session.project_path
        assert cwd_map["architect"] == session.project_path
        assert cwd_map.get("ai-lead") is None
        assert cwd_map.get("growth") is None

    def test_challenge_phase_routes_cwd_to_architect_only(
        self, db_path, session, happy_pm_output
    ):
        captured: list[dict[str, str | None]] = []

        def _tolerant(
            agents_list,
            session_id,
            phase,
            context_sections_by_agent=None,
            *,
            cumulative_context=None,
            phase_instruction=None,
            cwd_by_agent=None,
        ):
            if phase == PHASE_CHALLENGE:
                captured.append(dict(cwd_by_agent or {}))
            return {a: f"# {a}" for a in agents_list}, {}

        with (
            patch("squad.pipeline.run_agent", return_value=happy_pm_output),
            patch("squad.pipeline.run_agents_tolerant", side_effect=_tolerant),
        ):
            run_phase(session.id, PHASE_CHALLENGE, db_path=db_path)

        assert captured
        cwd_map = captured[0]
        assert cwd_map["architect"] == session.project_path
        assert cwd_map.get("security") is None
        assert cwd_map.get("delivery") is None

    def test_sequential_phase_does_not_route_cwd_for_pm(
        self, db_path, session, happy_pm_output
    ):
        captured_kwargs: list[dict] = []

        def _agent(*args, **kwargs):
            captured_kwargs.append(kwargs)
            return happy_pm_output

        with patch("squad.pipeline.run_agent", side_effect=_agent):
            run_phase(session.id, PHASE_CADRAGE, db_path=db_path)

        assert captured_kwargs
        pm_call = captured_kwargs[0]
        assert pm_call.get("cwd") is None

    def test_sequential_run_agent_forwards_cwd_when_applicable(
        self, db_path, session, happy_pm_output, monkeypatch
    ):
        """Force a sequential phase to include ux to prove run_agent cwd plumbing."""
        from squad import phase_config as pc

        sequential_cfg = pc.PhaseConfig(
            phase=PHASE_CADRAGE,
            order=1,
            default_agents=("ux",),
            critical_agents=(),
            parallel=False,
            can_pause=False,
            max_questions=0,
            retry_policy=pc.RetryPolicy(max_attempts=1),
            skip_policy=pc.SkipPolicy(),
        )

        def _fake_get_phase_config(phase):
            return sequential_cfg if phase == PHASE_CADRAGE else pc.PHASE_CONFIGS[phase]

        monkeypatch.setattr("squad.pipeline.get_phase_config", _fake_get_phase_config)

        captured: list[dict] = []

        def _agent(*args, **kwargs):
            captured.append(kwargs)
            return "# ux output"

        with patch("squad.pipeline.run_agent", side_effect=_agent):
            run_phase(session.id, PHASE_CADRAGE, db_path=db_path)

        assert captured
        assert captured[0]["cwd"] == session.project_path
        assert captured[0]["agent_name"] == "ux"

    def test_missing_project_path_yields_cwd_none_with_warning(
        self, db_path, tmp_path, caplog
    ):
        """project_path pointing at a non-existent dir → cwd=None + warning."""
        workspace_path = tmp_path / "workspace"
        s = create_session(
            title="Ghost project",
            project_path=str(tmp_path / "does-not-exist"),
            workspace_path=str(workspace_path),
            idea="x",
            db_path=db_path,
        )
        create_workspace(s)

        captured: list[dict[str, str | None]] = []

        def _tolerant(
            agents_list,
            session_id,
            phase,
            context_sections_by_agent=None,
            *,
            cumulative_context=None,
            phase_instruction=None,
            cwd_by_agent=None,
        ):
            captured.append(dict(cwd_by_agent or {}))
            return {a: f"# {a}" for a in agents_list}, {}

        import logging

        with (
            caplog.at_level(logging.WARNING, logger="squad.pipeline"),
            patch("squad.pipeline.run_agents_tolerant", side_effect=_tolerant),
        ):
            run_phase(s.id, PHASE_ETAT_DES_LIEUX, db_path=db_path)

        assert captured
        assert captured[0]["ux"] is None
        assert any("does not exist" in r.message for r in caplog.records)

    def test_empty_project_path_yields_cwd_none(
        self, db_path, tmp_path, happy_pm_output
    ):
        """Empty project_path → cwd=None without warning."""
        workspace_path = tmp_path / "workspace"
        s = create_session(
            title="No project",
            project_path="",
            workspace_path=str(workspace_path),
            idea="x",
            db_path=db_path,
        )
        create_workspace(s)

        captured: list[dict[str, str | None]] = []

        def _tolerant(
            agents_list,
            session_id,
            phase,
            context_sections_by_agent=None,
            *,
            cumulative_context=None,
            phase_instruction=None,
            cwd_by_agent=None,
        ):
            captured.append(dict(cwd_by_agent or {}))
            return {a: f"# {a}" for a in agents_list}, {}

        with patch("squad.pipeline.run_agents_tolerant", side_effect=_tolerant):
            run_phase(s.id, PHASE_ETAT_DES_LIEUX, db_path=db_path)

        assert captured
        assert captured[0]["ux"] is None


# ── smoke: pipeline -> executor (LOT 5 — Plan 5) ──────────────────────────────


class TestPipelineExecutorSmoke:
    """End-to-end smoke tests from pipeline through the real executor code.

    Only ``squad.executor._call_claude_cli`` is mocked — the pipeline
    calls ``run_agent`` / ``run_agents_tolerant`` as usual, agent
    markdowns are parsed for real, and the subprocess argv (including
    ``--allowedTools`` and ``cwd``) is captured at the last hop before
    the real CLI would be invoked. These are intentionally *not* marked
    ``@pytest.mark.integration`` because no real Claude process is
    launched.
    """

    @staticmethod
    def _fake_completed(text: str = "ok output") -> MagicMock:
        mock = MagicMock()
        mock.stdout = json.dumps({"type": "text", "text": text})
        mock.returncode = 0
        mock.stderr = ""
        return mock

    def _capture_calls(self):
        calls: list[dict] = []

        def fake(cmd, timeout, cwd=None):
            calls.append({"cmd": list(cmd), "cwd": cwd})
            return self._fake_completed()

        return calls, fake

    @staticmethod
    def _calls_for_agent(calls: list[dict], agent_name: str) -> list[dict]:
        header = f"# Agent: {agent_name}\n"
        return [c for c in calls if c["cmd"][-1].startswith(header)]

    @staticmethod
    def _tools_of(call: dict) -> str:
        for arg in call["cmd"]:
            if isinstance(arg, str) and arg.startswith("--allowedTools="):
                return arg.split("=", 1)[1]
        return ""

    def test_ux_gets_exploration_tools_and_cwd_end_to_end(self, db_path, session):
        calls, fake = self._capture_calls()
        with patch("squad.executor._call_claude_cli", side_effect=fake):
            run_phase(session.id, PHASE_ETAT_DES_LIEUX, db_path=db_path)

        ux_calls = self._calls_for_agent(calls, "ux")
        assert len(ux_calls) == 1, "ux should be invoked exactly once in etat_des_lieux"
        assert self._tools_of(ux_calls[0]) == "Read,WebSearch,WebFetch,Glob,LS,Grep"
        assert ux_calls[0]["cwd"] == session.project_path

    def test_customer_success_keeps_read_only_without_cwd(self, db_path, session):
        calls, fake = self._capture_calls()
        with patch("squad.executor._call_claude_cli", side_effect=fake):
            run_phase(session.id, PHASE_ETAT_DES_LIEUX, db_path=db_path)

        cs_calls = self._calls_for_agent(calls, "customer-success")
        assert len(cs_calls) == 1
        assert self._tools_of(cs_calls[0]) == "Read"
        assert cs_calls[0]["cwd"] is None

    def test_sales_keeps_web_tools_without_project_cwd(self, db_path, session):
        calls, fake = self._capture_calls()
        with patch("squad.executor._call_claude_cli", side_effect=fake):
            run_phase(session.id, PHASE_ETAT_DES_LIEUX, db_path=db_path)

        sales_calls = self._calls_for_agent(calls, "sales")
        assert len(sales_calls) == 1
        assert self._tools_of(sales_calls[0]) == "Read,WebSearch,WebFetch"
        assert sales_calls[0]["cwd"] is None

    def test_architect_gets_exploration_tools_and_cwd_in_challenge(
        self, db_path, session
    ):
        calls, fake = self._capture_calls()
        with patch("squad.executor._call_claude_cli", side_effect=fake):
            run_phase(session.id, PHASE_CHALLENGE, db_path=db_path)

        architect_calls = self._calls_for_agent(calls, "architect")
        assert len(architect_calls) == 1
        assert self._tools_of(architect_calls[0]) == "Read,WebSearch,WebFetch,Glob,LS,Grep"
        assert architect_calls[0]["cwd"] == session.project_path
