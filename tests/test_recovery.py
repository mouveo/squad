"""Tests for squad/recovery.py — resume points, blockers, conception retry."""

from pathlib import Path

import pytest

from squad.constants import (
    PHASE_BENCHMARK,
    PHASE_CADRAGE,
    PHASE_CHALLENGE,
    PHASE_CONCEPTION,
    PHASE_ETAT_DES_LIEUX,
    PHASE_IDEATION,
    STATUS_DONE,
    STATUS_DRAFT,
    STATUS_FAILED,
    STATUS_INTERVIEWING,
    STATUS_REVIEW,
    STATUS_WORKING,
)
from squad.db import (
    create_phase_output,
    create_question,
    create_session,
    ensure_schema,
    update_session_status,
)
from squad.recovery import (
    MAX_CHALLENGE_RETRIES,
    ResumePoint,
    build_retry_instruction,
    can_retry_conception,
    collect_blocker_constraints,
    determine_resume_point,
    has_blocking_constraints,
    has_pending_questions,
    record_conception_retry,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "s.db"
    ensure_schema(path)
    return path


def _session(db_path: Path, **kwargs):
    defaults = dict(
        title="t",
        project_path="/tmp/p",
        workspace_path="/tmp/ws",
        idea="idea",
    )
    return create_session(**{**defaults, **kwargs}, db_path=db_path)


_BLOCKING_CONTENT = (
    "# Challenge\n"
    '```json\n{"blockers": [{"id": "b1", "severity": "blocking", '
    '"constraint": "Missing auth gating"}]}\n```'
)

_MINOR_CONTENT = (
    '```json\n{"blockers": [{"id": "b1", "severity": "minor", "constraint": "naming"}]}\n```'
)


# ── pending questions ──────────────────────────────────────────────────────────


class TestHasPendingQuestions:
    def test_true_when_unanswered(self, db_path):
        s = _session(db_path)
        create_question(s.id, "pm", PHASE_CADRAGE, "Q?", db_path=db_path)
        assert has_pending_questions(s.id, db_path=db_path) is True

    def test_false_when_all_answered(self, db_path):
        s = _session(db_path)
        from squad.db import answer_question

        q = create_question(s.id, "pm", PHASE_CADRAGE, "Q?", db_path=db_path)
        answer_question(q.id, "A", db_path=db_path)
        assert has_pending_questions(s.id, db_path=db_path) is False

    def test_false_when_no_questions(self, db_path):
        s = _session(db_path)
        assert has_pending_questions(s.id, db_path=db_path) is False


# ── collect_blocker_constraints ────────────────────────────────────────────────


class TestCollectBlockerConstraints:
    def test_extracts_blocking_only(self, db_path):
        s = _session(db_path)
        create_phase_output(
            s.id,
            PHASE_CHALLENGE,
            "security",
            _BLOCKING_CONTENT,
            "/f1.md",
            attempt=1,
            db_path=db_path,
        )
        constraints = collect_blocker_constraints(s.id, db_path=db_path)
        assert len(constraints) == 1
        assert "Missing auth gating" in constraints[0]
        assert "security" in constraints[0]

    def test_ignores_minor_blockers(self, db_path):
        s = _session(db_path)
        create_phase_output(
            s.id,
            PHASE_CHALLENGE,
            "security",
            _MINOR_CONTENT,
            "/f.md",
            attempt=1,
            db_path=db_path,
        )
        assert collect_blocker_constraints(s.id, db_path=db_path) == []

    def test_only_latest_attempt_considered(self, db_path):
        s = _session(db_path)
        create_phase_output(
            s.id,
            PHASE_CHALLENGE,
            "security",
            _BLOCKING_CONTENT,
            "/f1.md",
            attempt=1,
            db_path=db_path,
        )
        create_phase_output(
            s.id,
            PHASE_CHALLENGE,
            "security",
            _MINOR_CONTENT,
            "/f2.md",
            attempt=2,
            db_path=db_path,
        )
        # Latest attempt (2) has only minor → no constraints returned
        assert collect_blocker_constraints(s.id, db_path=db_path) == []

    def test_has_blocking_constraints_flag(self, db_path):
        s = _session(db_path)
        assert has_blocking_constraints(s.id, db_path=db_path) is False
        create_phase_output(
            s.id,
            PHASE_CHALLENGE,
            "security",
            _BLOCKING_CONTENT,
            "/f.md",
            attempt=1,
            db_path=db_path,
        )
        assert has_blocking_constraints(s.id, db_path=db_path) is True


# ── conception retry ───────────────────────────────────────────────────────────


class TestCanRetryConception:
    def test_no_retry_without_blockers(self, db_path):
        s = _session(db_path)
        assert can_retry_conception(s.id, db_path=db_path) is False

    def test_retry_allowed_once(self, db_path):
        s = _session(db_path)
        create_phase_output(
            s.id,
            PHASE_CHALLENGE,
            "security",
            _BLOCKING_CONTENT,
            "/f.md",
            attempt=1,
            db_path=db_path,
        )
        assert can_retry_conception(s.id, db_path=db_path) is True
        record_conception_retry(s.id, db_path=db_path)
        assert can_retry_conception(s.id, db_path=db_path) is False

    def test_record_increments_counter(self, db_path):
        s = _session(db_path)
        assert record_conception_retry(s.id, db_path=db_path) == 1
        assert record_conception_retry(s.id, db_path=db_path) == 2

    def test_max_retries_constant(self):
        assert MAX_CHALLENGE_RETRIES == 1


# ── build_retry_instruction ────────────────────────────────────────────────────


class TestBuildRetryInstruction:
    def test_empty_constraints(self):
        out = build_retry_instruction([])
        assert "retry" in out.lower()

    def test_mentions_constraints(self):
        out = build_retry_instruction(["[b1] must add rate limiting (source: security)"])
        assert "rate limiting" in out
        assert "hard constraints" in out.lower()


# ── determine_resume_point ─────────────────────────────────────────────────────


class TestDetermineResumePoint:
    def test_unknown_session_raises(self, db_path):
        with pytest.raises(ValueError):
            determine_resume_point("ghost", db_path=db_path)

    def test_done_returns_none(self, db_path):
        s = _session(db_path)
        update_session_status(s.id, STATUS_DONE, db_path=db_path)
        assert determine_resume_point(s.id, db_path=db_path) is None

    def test_failed_returns_none(self, db_path):
        s = _session(db_path)
        update_session_status(s.id, STATUS_FAILED, db_path=db_path)
        assert determine_resume_point(s.id, db_path=db_path) is None

    def test_review_returns_none(self, db_path):
        s = _session(db_path)
        update_session_status(s.id, STATUS_REVIEW, db_path=db_path)
        assert determine_resume_point(s.id, db_path=db_path) is None

    def test_draft_starts_at_cadrage(self, db_path):
        s = _session(db_path)
        assert s.status == STATUS_DRAFT
        rp = determine_resume_point(s.id, db_path=db_path)
        assert isinstance(rp, ResumePoint)
        assert rp.phase == PHASE_CADRAGE

    def test_interviewing_with_pending_raises(self, db_path):
        s = _session(db_path)
        update_session_status(s.id, STATUS_INTERVIEWING, db_path=db_path)
        create_question(s.id, "pm", PHASE_CADRAGE, "Q?", db_path=db_path)
        with pytest.raises(RuntimeError, match="unanswered"):
            determine_resume_point(s.id, db_path=db_path)

    def test_interviewing_all_answered_goes_to_next_phase(self, db_path):
        from squad.db import answer_question

        s = _session(db_path)
        update_session_status(s.id, STATUS_INTERVIEWING, db_path=db_path)
        q = create_question(s.id, "pm", PHASE_CADRAGE, "Q?", db_path=db_path)
        answer_question(q.id, "A", db_path=db_path)
        rp = determine_resume_point(s.id, db_path=db_path)
        assert rp.phase == PHASE_ETAT_DES_LIEUX

    def test_working_resumes_at_current_phase(self, db_path):
        s = _session(db_path)
        update_session_status(s.id, STATUS_WORKING, current_phase=PHASE_BENCHMARK, db_path=db_path)
        rp = determine_resume_point(s.id, db_path=db_path)
        assert rp.phase == PHASE_BENCHMARK

    def test_working_on_challenge_with_blockers_triggers_conception_retry(self, db_path):
        s = _session(db_path)
        create_phase_output(
            s.id,
            PHASE_CHALLENGE,
            "security",
            _BLOCKING_CONTENT,
            "/f.md",
            attempt=1,
            db_path=db_path,
        )
        update_session_status(s.id, STATUS_WORKING, current_phase=PHASE_CHALLENGE, db_path=db_path)
        rp = determine_resume_point(s.id, db_path=db_path)
        assert rp.phase == PHASE_CONCEPTION
        assert rp.blocker_constraints

    def test_working_on_challenge_after_retry_used(self, db_path):
        s = _session(db_path)
        create_phase_output(
            s.id,
            PHASE_CHALLENGE,
            "security",
            _BLOCKING_CONTENT,
            "/f.md",
            attempt=1,
            db_path=db_path,
        )
        record_conception_retry(s.id, db_path=db_path)
        update_session_status(s.id, STATUS_WORKING, current_phase=PHASE_CHALLENGE, db_path=db_path)
        rp = determine_resume_point(s.id, db_path=db_path)
        # Retry budget used; resume at challenge (not conception)
        assert rp.phase == PHASE_CHALLENGE


# ── ideation pause / resume (LOT 5) ────────────────────────────────────────────


class TestDetermineResumePointIdeationPause:
    def test_ideation_interviewing_without_selection_raises(self, db_path):
        """A paused ideation session with no angle picked yet cannot resume."""
        s = _session(db_path)
        update_session_status(
            s.id, STATUS_INTERVIEWING, current_phase=PHASE_IDEATION, db_path=db_path
        )
        with pytest.raises(RuntimeError, match="angle"):
            determine_resume_point(s.id, db_path=db_path)

    def test_ideation_interviewing_with_selection_resumes_at_benchmark(self, db_path):
        from squad.db import set_selected_angle

        s = _session(db_path)
        update_session_status(
            s.id, STATUS_INTERVIEWING, current_phase=PHASE_IDEATION, db_path=db_path
        )
        set_selected_angle(db_path, s.id, 2)
        rp = determine_resume_point(s.id, db_path=db_path)
        assert rp.phase == PHASE_BENCHMARK

    def test_cadrage_interviewing_unaffected_by_ideation_branch(self, db_path):
        """Existing cadrage path keeps its current behaviour (after answers)."""
        from squad.db import answer_question

        s = _session(db_path)
        update_session_status(
            s.id, STATUS_INTERVIEWING, current_phase=PHASE_CADRAGE, db_path=db_path
        )
        q = create_question(s.id, "pm", PHASE_CADRAGE, "Q?", db_path=db_path)
        answer_question(q.id, "A", db_path=db_path)
        rp = determine_resume_point(s.id, db_path=db_path)
        assert rp.phase == PHASE_ETAT_DES_LIEUX
