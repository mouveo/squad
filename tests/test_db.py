"""Tests for squad/db.py — schema creation and CRUD for all four tables."""

from pathlib import Path

import pytest

from squad.constants import (
    MODE_AUTONOMOUS,
    PHASE_CADRAGE,
    PHASE_CONCEPTION,
    STATUS_DONE,
    STATUS_WORKING,
)
from squad.db import (
    answer_question,
    create_phase_output,
    create_plan,
    create_question,
    create_session,
    ensure_schema,
    get_phase_attempt,
    get_plan,
    get_question,
    get_session,
    increment_challenge_retry_count,
    increment_phase_attempt,
    list_active_sessions,
    list_ideation_angles,
    list_pending_questions,
    list_phase_outputs,
    list_plans,
    list_session_history,
    mark_phase_skipped,
    persist_ideation_angle,
    set_benchmark_all_angles,
    set_selected_angle,
    update_input_richness,
    update_plan_slack_message_ts,
    update_question_slack_message_ts,
    update_session_failure_reason,
    update_session_profile,
    update_session_status,
)
from squad.models import RESEARCH_DEPTH_DEEP, RESEARCH_DEPTH_LIGHT, IdeationAngle


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / ".squad" / "squad.db"
    ensure_schema(path)
    return path


def _session(db_path: Path, **kwargs):
    defaults = dict(
        title="CRM improvements",
        project_path="/tmp/myproject",
        workspace_path="/tmp/myproject/.squad/sessions/s1",
        idea="improve the CRM",
    )
    return create_session(**{**defaults, **kwargs}, db_path=db_path)


# ── schema ─────────────────────────────────────────────────────────────────────


class TestEnsureSchema:
    def test_creates_tables(self, db_path: Path):
        from sqlite_utils import Database

        db = Database(db_path)
        assert set(db.table_names()) >= {
            "sessions",
            "phase_outputs",
            "questions",
            "plans",
            "ideation_angles",
        }

    def test_idempotent(self, db_path: Path):
        ensure_schema(db_path)
        ensure_schema(db_path)  # second call must not raise

    def test_creates_indexes(self, db_path: Path):
        from sqlite_utils import Database

        db = Database(db_path)
        index_cols = {col for table in db.tables for idx in table.indexes for col in idx.columns}
        assert "status" in index_cols
        assert "project_path" in index_cols
        assert "session_id" in index_cols


# ── sessions ───────────────────────────────────────────────────────────────────


class TestCreateSession:
    def test_returns_session(self, db_path: Path):
        s = _session(db_path)
        assert s.id
        assert s.title == "CRM improvements"
        assert s.status == "draft"
        assert s.mode == "approval"
        assert s.current_phase is None

    def test_persists_workspace_path(self, db_path: Path):
        s = _session(db_path, workspace_path="/custom/ws")
        fetched = get_session(s.id, db_path)
        assert fetched.workspace_path == "/custom/ws"

    def test_persists_title(self, db_path: Path):
        s = _session(db_path, title="My title")
        fetched = get_session(s.id, db_path)
        assert fetched.title == "My title"

    def test_custom_mode(self, db_path: Path):
        s = _session(db_path, mode=MODE_AUTONOMOUS)
        assert s.mode == MODE_AUTONOMOUS


class TestGetSession:
    def test_returns_none_for_unknown(self, db_path: Path):
        assert get_session("nonexistent", db_path) is None

    def test_roundtrip(self, db_path: Path):
        s = _session(db_path)
        fetched = get_session(s.id, db_path)
        assert fetched.id == s.id
        assert fetched.idea == s.idea
        assert fetched.project_path == s.project_path


class TestUpdateSessionStatus:
    def test_updates_status(self, db_path: Path):
        s = _session(db_path)
        update_session_status(s.id, STATUS_WORKING, db_path=db_path)
        updated = get_session(s.id, db_path)
        assert updated.status == STATUS_WORKING

    def test_updates_current_phase(self, db_path: Path):
        s = _session(db_path)
        update_session_status(s.id, STATUS_WORKING, current_phase=PHASE_CADRAGE, db_path=db_path)
        updated = get_session(s.id, db_path)
        assert updated.current_phase == PHASE_CADRAGE

    def test_current_phase_unchanged_when_not_provided(self, db_path: Path):
        s = _session(db_path)
        update_session_status(s.id, STATUS_WORKING, current_phase=PHASE_CADRAGE, db_path=db_path)
        update_session_status(s.id, STATUS_WORKING, db_path=db_path)
        updated = get_session(s.id, db_path)
        assert updated.current_phase == PHASE_CADRAGE


class TestListActiveSessions:
    def test_excludes_terminal_sessions(self, db_path: Path):
        active = _session(db_path)
        done = _session(db_path, title="Done session")
        update_session_status(done.id, STATUS_DONE, db_path=db_path)

        results = list_active_sessions(db_path)
        ids = [s.id for s in results]
        assert active.id in ids
        assert done.id not in ids

    def test_empty_when_all_terminal(self, db_path: Path):
        s = _session(db_path)
        update_session_status(s.id, STATUS_DONE, db_path=db_path)
        assert list_active_sessions(db_path) == []


class TestListSessionHistory:
    def test_returns_all_sessions(self, db_path: Path):
        s1 = _session(db_path, title="s1")
        s2 = _session(db_path, title="s2")
        history = list_session_history(db_path=db_path)
        ids = [s.id for s in history]
        assert s1.id in ids
        assert s2.id in ids

    def test_filters_by_project(self, db_path: Path):
        s1 = _session(db_path, title="s1", project_path="/proj/a")
        s2 = _session(db_path, title="s2", project_path="/proj/b")
        results = list_session_history(project_path="/proj/a", db_path=db_path)
        ids = [s.id for s in results]
        assert s1.id in ids
        assert s2.id not in ids

    def test_respects_limit(self, db_path: Path):
        for i in range(5):
            _session(db_path, title=f"session {i}")
        results = list_session_history(limit=3, db_path=db_path)
        assert len(results) == 3


# ── phase outputs ──────────────────────────────────────────────────────────────


class TestPhaseOutputs:
    def test_create_and_retrieve(self, db_path: Path):
        s = _session(db_path)
        po = create_phase_output(
            session_id=s.id,
            phase=PHASE_CADRAGE,
            agent="pm",
            output="Cadrage output",
            file_path="/tmp/phases/1-cadrage/pm.md",
            db_path=db_path,
        )
        assert po.id
        assert po.session_id == s.id

    def test_list_all_for_session(self, db_path: Path):
        s = _session(db_path)
        create_phase_output(s.id, PHASE_CADRAGE, "pm", "out1", "/f1.md", db_path=db_path)
        create_phase_output(s.id, PHASE_CONCEPTION, "ux", "out2", "/f2.md", db_path=db_path)
        outputs = list_phase_outputs(s.id, db_path=db_path)
        assert len(outputs) == 2

    def test_filter_by_phase(self, db_path: Path):
        s = _session(db_path)
        create_phase_output(s.id, PHASE_CADRAGE, "pm", "out1", "/f1.md", db_path=db_path)
        create_phase_output(s.id, PHASE_CONCEPTION, "ux", "out2", "/f2.md", db_path=db_path)
        outputs = list_phase_outputs(s.id, phase=PHASE_CADRAGE, db_path=db_path)
        assert len(outputs) == 1
        assert outputs[0].phase == PHASE_CADRAGE

    def test_optional_fields(self, db_path: Path):
        s = _session(db_path)
        po = create_phase_output(
            s.id,
            PHASE_CADRAGE,
            "pm",
            "out",
            "/f.md",
            duration_seconds=12.5,
            tokens_used=800,
            db_path=db_path,
        )
        assert po.duration_seconds == 12.5
        assert po.tokens_used == 800

    def test_isolated_by_session(self, db_path: Path):
        s1 = _session(db_path)
        s2 = _session(db_path)
        create_phase_output(s1.id, PHASE_CADRAGE, "pm", "out", "/f.md", db_path=db_path)
        assert list_phase_outputs(s2.id, db_path=db_path) == []


# ── questions ──────────────────────────────────────────────────────────────────


class TestQuestions:
    def test_create_question(self, db_path: Path):
        s = _session(db_path)
        q = create_question(s.id, "pm", PHASE_CADRAGE, "Who is the target?", db_path=db_path)
        assert q.id
        assert q.answer is None
        assert q.answered_at is None

    def test_list_pending_questions(self, db_path: Path):
        s = _session(db_path)
        create_question(s.id, "pm", PHASE_CADRAGE, "Q1?", db_path=db_path)
        create_question(s.id, "pm", PHASE_CADRAGE, "Q2?", db_path=db_path)
        pending = list_pending_questions(s.id, db_path=db_path)
        assert len(pending) == 2

    def test_answer_question(self, db_path: Path):
        s = _session(db_path)
        q = create_question(s.id, "pm", PHASE_CADRAGE, "Who?", db_path=db_path)
        answer_question(q.id, "SMBs", db_path=db_path)
        pending = list_pending_questions(s.id, db_path=db_path)
        assert pending == []

    def test_answered_not_in_pending(self, db_path: Path):
        s = _session(db_path)
        q1 = create_question(s.id, "pm", PHASE_CADRAGE, "Q1?", db_path=db_path)
        create_question(s.id, "pm", PHASE_CADRAGE, "Q2?", db_path=db_path)
        answer_question(q1.id, "answer", db_path=db_path)
        pending = list_pending_questions(s.id, db_path=db_path)
        assert len(pending) == 1
        assert pending[0].question == "Q2?"

    def test_isolated_by_session(self, db_path: Path):
        s1 = _session(db_path)
        s2 = _session(db_path)
        create_question(s1.id, "pm", PHASE_CADRAGE, "Q?", db_path=db_path)
        assert list_pending_questions(s2.id, db_path=db_path) == []


# ── plans ──────────────────────────────────────────────────────────────────────


class TestPlans:
    def test_create_plan(self, db_path: Path):
        s = _session(db_path)
        p = create_plan(s.id, "Plan 1", "/plans/plan-1.md", "## LOT 1", db_path=db_path)
        assert p.id
        assert p.title == "Plan 1"
        assert p.forge_status is None

    def test_list_plans(self, db_path: Path):
        s = _session(db_path)
        create_plan(s.id, "Plan 1", "/p1.md", "content1", db_path=db_path)
        create_plan(s.id, "Plan 2", "/p2.md", "content2", db_path=db_path)
        plans = list_plans(s.id, db_path=db_path)
        assert len(plans) == 2

    def test_isolated_by_session(self, db_path: Path):
        s1 = _session(db_path)
        s2 = _session(db_path)
        create_plan(s1.id, "Plan 1", "/p1.md", "content", db_path=db_path)
        assert list_plans(s2.id, db_path=db_path) == []


# ── session profile (LOT 2) ────────────────────────────────────────────────────


class TestSessionProfile:
    def test_new_session_has_empty_profile(self, db_path: Path):
        s = _session(db_path)
        fetched = get_session(s.id, db_path)
        assert fetched.subject_type is None
        assert fetched.research_depth is None
        assert fetched.agents_by_phase == {}
        assert fetched.phase_attempts == {}
        assert fetched.challenge_retry_count == 0
        assert fetched.skipped_phases == {}

    def test_update_and_roundtrip_profile(self, db_path: Path):
        s = _session(db_path)
        update_session_profile(
            session_id=s.id,
            subject_type="ai_product",
            research_depth=RESEARCH_DEPTH_DEEP,
            agents_by_phase={
                "etat_des_lieux": ["ux"],
                "conception": ["ux", "architect"],
            },
            db_path=db_path,
        )
        fetched = get_session(s.id, db_path)
        assert fetched.subject_type == "ai_product"
        assert fetched.research_depth == RESEARCH_DEPTH_DEEP
        assert fetched.agents_by_phase["etat_des_lieux"] == ["ux"]
        assert fetched.agents_by_phase["conception"] == ["ux", "architect"]

    def test_mark_phase_skipped(self, db_path: Path):
        s = _session(db_path)
        mark_phase_skipped(s.id, "benchmark", "research_depth=light", db_path=db_path)
        fetched = get_session(s.id, db_path)
        assert fetched.skipped_phases == {"benchmark": "research_depth=light"}

    def test_mark_phase_skipped_accumulates(self, db_path: Path):
        s = _session(db_path)
        mark_phase_skipped(s.id, "benchmark", "light", db_path=db_path)
        mark_phase_skipped(s.id, "challenge", "out of scope", db_path=db_path)
        fetched = get_session(s.id, db_path)
        assert fetched.skipped_phases == {
            "benchmark": "light",
            "challenge": "out of scope",
        }

    def test_increment_phase_attempt(self, db_path: Path):
        s = _session(db_path)
        assert increment_phase_attempt(s.id, "conception", db_path=db_path) == 1
        assert increment_phase_attempt(s.id, "conception", db_path=db_path) == 2
        assert get_phase_attempt(s.id, "conception", db_path=db_path) == 2

    def test_phase_attempt_isolated_per_phase(self, db_path: Path):
        s = _session(db_path)
        increment_phase_attempt(s.id, "conception", db_path=db_path)
        increment_phase_attempt(s.id, "conception", db_path=db_path)
        increment_phase_attempt(s.id, "cadrage", db_path=db_path)
        assert get_phase_attempt(s.id, "conception", db_path=db_path) == 2
        assert get_phase_attempt(s.id, "cadrage", db_path=db_path) == 1
        assert get_phase_attempt(s.id, "benchmark", db_path=db_path) == 0

    def test_increment_challenge_retry_count(self, db_path: Path):
        s = _session(db_path)
        assert increment_challenge_retry_count(s.id, db_path=db_path) == 1
        assert increment_challenge_retry_count(s.id, db_path=db_path) == 2
        fetched = get_session(s.id, db_path)
        assert fetched.challenge_retry_count == 2

    def test_profile_survives_crash_simulation(self, db_path: Path):
        s = _session(db_path)
        update_session_profile(
            session_id=s.id,
            subject_type="b2b_saas",
            research_depth=RESEARCH_DEPTH_LIGHT,
            agents_by_phase={"conception": ["ux", "architect"]},
            db_path=db_path,
        )
        increment_phase_attempt(s.id, "cadrage", db_path=db_path)
        # Simulate process restart: reopen the same DB via a new call path
        fetched = get_session(s.id, db_path=db_path)
        assert fetched.subject_type == "b2b_saas"
        assert fetched.research_depth == RESEARCH_DEPTH_LIGHT
        assert fetched.phase_attempts == {"cadrage": 1}


class TestPhaseOutputAttempt:
    def test_default_attempt_is_one(self, db_path: Path):
        s = _session(db_path)
        po = create_phase_output(
            s.id, PHASE_CONCEPTION, "architect", "out", "/f.md", db_path=db_path
        )
        assert po.attempt == 1

    def test_second_attempt_persisted(self, db_path: Path):
        s = _session(db_path)
        create_phase_output(s.id, PHASE_CONCEPTION, "architect", "first", "/f1.md", db_path=db_path)
        create_phase_output(
            s.id,
            PHASE_CONCEPTION,
            "architect",
            "retry",
            "/f2.md",
            attempt=2,
            db_path=db_path,
        )
        outputs = list_phase_outputs(s.id, phase=PHASE_CONCEPTION, db_path=db_path)
        assert {po.attempt for po in outputs} == {1, 2}

    def test_list_filtered_by_attempt(self, db_path: Path):
        s = _session(db_path)
        create_phase_output(s.id, PHASE_CONCEPTION, "architect", "first", "/f1.md", db_path=db_path)
        create_phase_output(
            s.id,
            PHASE_CONCEPTION,
            "architect",
            "retry",
            "/f2.md",
            attempt=2,
            db_path=db_path,
        )
        latest = list_phase_outputs(s.id, phase=PHASE_CONCEPTION, attempt=2, db_path=db_path)
        assert len(latest) == 1
        assert latest[0].output == "retry"


# ── failure_reason (LOT 2 — Plan 4) ───────────────────────────────────────────


class TestFailureReason:
    def test_default_is_none(self, db_path: Path):
        s = _session(db_path)
        fetched = get_session(s.id, db_path=db_path)
        assert fetched.failure_reason is None

    def test_update_and_read_back(self, db_path: Path):
        s = _session(db_path)
        update_session_failure_reason(s.id, "pm exploded", db_path=db_path)
        fetched = get_session(s.id, db_path=db_path)
        assert fetched.failure_reason == "pm exploded"

    def test_update_bumps_updated_at(self, db_path: Path):
        s = _session(db_path)
        before = get_session(s.id, db_path=db_path).updated_at
        update_session_failure_reason(s.id, "boom", db_path=db_path)
        after = get_session(s.id, db_path=db_path).updated_at
        assert after >= before

    def test_failure_reason_survives_schema_reapply(self, db_path: Path):
        s = _session(db_path)
        update_session_failure_reason(s.id, "boom", db_path=db_path)
        # Idempotent schema migration must not overwrite the column.
        ensure_schema(db_path)
        fetched = get_session(s.id, db_path=db_path)
        assert fetched.failure_reason == "boom"


# ── question slack_message_ts (LOT 4 — Plan 4) ────────────────────────────────


class TestQuestionSlackMessageTs:
    def test_default_is_none(self, db_path: Path):
        s = _session(db_path)
        q = create_question(s.id, "pm", PHASE_CADRAGE, "Why?", db_path=db_path)
        fetched = get_question(q.id, db_path=db_path)
        assert fetched is not None
        assert fetched.slack_message_ts is None

    def test_update_and_read_back(self, db_path: Path):
        s = _session(db_path)
        q = create_question(s.id, "pm", PHASE_CADRAGE, "Why?", db_path=db_path)
        update_question_slack_message_ts(q.id, "1700000000.000100", db_path=db_path)
        fetched = get_question(q.id, db_path=db_path)
        assert fetched.slack_message_ts == "1700000000.000100"

    def test_get_question_unknown_returns_none(self, db_path: Path):
        assert get_question("ghost", db_path=db_path) is None

    def test_migration_keeps_existing_rows(self, db_path: Path):
        s = _session(db_path)
        q = create_question(s.id, "pm", PHASE_CADRAGE, "Why?", db_path=db_path)
        # Re-applying schema must not drop or reset the new column.
        ensure_schema(db_path)
        update_question_slack_message_ts(q.id, "1700000000.000200", db_path=db_path)
        ensure_schema(db_path)
        fetched = get_question(q.id, db_path=db_path)
        assert fetched.slack_message_ts == "1700000000.000200"


# ── plan slack_message_ts (LOT 5 — Plan 4) ────────────────────────────────────


class TestPlanSlackMessageTs:
    def test_default_is_none(self, db_path: Path):
        s = _session(db_path)
        plan = create_plan(s.id, "Plan 1", "/tmp/p1.md", "# plan", db_path=db_path)
        fetched = get_plan(plan.id, db_path=db_path)
        assert fetched is not None
        assert fetched.slack_message_ts is None

    def test_update_and_read_back(self, db_path: Path):
        s = _session(db_path)
        plan = create_plan(s.id, "Plan 1", "/tmp/p1.md", "# plan", db_path=db_path)
        update_plan_slack_message_ts(plan.id, "1700000000.000100", db_path=db_path)
        fetched = get_plan(plan.id, db_path=db_path)
        assert fetched.slack_message_ts == "1700000000.000100"

    def test_get_plan_unknown_returns_none(self, db_path: Path):
        assert get_plan("ghost", db_path=db_path) is None

    def test_migration_keeps_existing_rows(self, db_path: Path):
        s = _session(db_path)
        plan = create_plan(s.id, "Plan 1", "/tmp/p1.md", "# plan", db_path=db_path)
        update_plan_slack_message_ts(plan.id, "1700000000.000200", db_path=db_path)
        ensure_schema(db_path)  # must not reset
        fetched = get_plan(plan.id, db_path=db_path)
        assert fetched.slack_message_ts == "1700000000.000200"


# ── ideation (Plan 6 — LOT 1) ─────────────────────────────────────────────────


def _angle(session_id: str, idx: int, **overrides) -> IdeationAngle:
    defaults = dict(
        session_id=session_id,
        idx=idx,
        title=f"Angle {idx}",
        segment="SMB",
        value_prop="Save time",
        approach="Automation",
        divergence_note="unique",
    )
    return IdeationAngle(**{**defaults, **overrides})


class TestSessionIdeationState:
    def test_defaults_after_create(self, db_path: Path):
        s = _session(db_path)
        fetched = get_session(s.id, db_path=db_path)
        assert fetched.input_richness is None
        assert fetched.selected_angle_idx is None
        assert fetched.benchmark_all_angles is False

    def test_update_input_richness(self, db_path: Path):
        s = _session(db_path)
        update_input_richness(db_path, s.id, "rich")
        assert get_session(s.id, db_path=db_path).input_richness == "rich"

    def test_set_selected_angle(self, db_path: Path):
        s = _session(db_path)
        set_selected_angle(db_path, s.id, 2)
        assert get_session(s.id, db_path=db_path).selected_angle_idx == 2

    def test_set_selected_angle_supports_zero(self, db_path: Path):
        s = _session(db_path)
        set_selected_angle(db_path, s.id, 0)
        assert get_session(s.id, db_path=db_path).selected_angle_idx == 0

    def test_set_benchmark_all_angles_roundtrip(self, db_path: Path):
        s = _session(db_path)
        set_benchmark_all_angles(db_path, s.id, True)
        assert get_session(s.id, db_path=db_path).benchmark_all_angles is True
        set_benchmark_all_angles(db_path, s.id, False)
        assert get_session(s.id, db_path=db_path).benchmark_all_angles is False


class TestIdeationAnglesTable:
    def test_insert_and_list_roundtrip(self, db_path: Path):
        s = _session(db_path)
        a0 = persist_ideation_angle(db_path, _angle(s.id, 0, title="A"))
        a1 = persist_ideation_angle(db_path, _angle(s.id, 1, title="B"))
        angles = list_ideation_angles(db_path, s.id)
        assert [a.idx for a in angles] == [0, 1]
        assert angles[0].title == "A"
        assert angles[1].title == "B"
        assert a0.session_id == s.id and a1.session_id == s.id

    def test_upsert_replaces_existing(self, db_path: Path):
        s = _session(db_path)
        persist_ideation_angle(db_path, _angle(s.id, 0, title="Old"))
        persist_ideation_angle(db_path, _angle(s.id, 0, title="New"))
        angles = list_ideation_angles(db_path, s.id)
        assert len(angles) == 1
        assert angles[0].title == "New"

    def test_isolated_by_session(self, db_path: Path):
        s1 = _session(db_path)
        s2 = _session(db_path)
        persist_ideation_angle(db_path, _angle(s1.id, 0))
        assert list_ideation_angles(db_path, s2.id) == []

    def test_angles_survive_schema_reapply(self, db_path: Path):
        s = _session(db_path)
        persist_ideation_angle(db_path, _angle(s.id, 0, title="keep"))
        ensure_schema(db_path)
        angles = list_ideation_angles(db_path, s.id)
        assert len(angles) == 1
        assert angles[0].title == "keep"


class TestSchemaMigrationFromLegacyDb:
    def test_preexisting_db_gets_ideation_schema(self, tmp_path: Path):
        """A DB created with an older schema must upgrade cleanly."""
        from sqlite_utils import Database

        path = tmp_path / "legacy.db"
        legacy = Database(path)
        # Minimal legacy `sessions` table — no ideation columns.
        legacy["sessions"].create(
            {
                "id": str,
                "title": str,
                "project_path": str,
                "workspace_path": str,
                "idea": str,
                "status": str,
                "mode": str,
                "created_at": str,
                "updated_at": str,
            },
            pk="id",
            not_null={"title", "project_path", "workspace_path", "idea", "status"},
        )
        legacy["sessions"].insert(
            {
                "id": "legacy-1",
                "title": "Legacy",
                "project_path": "/tmp/proj",
                "workspace_path": "/tmp/proj/.squad/sessions/legacy-1",
                "idea": "old idea",
                "status": "draft",
                "mode": "approval",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        )

        ensure_schema(path)

        cols = set(Database(path)["sessions"].columns_dict)
        assert {"input_richness", "selected_angle_idx", "benchmark_all_angles"}.issubset(cols)
        assert "ideation_angles" in Database(path).table_names()

        fetched = get_session("legacy-1", db_path=path)
        assert fetched.input_richness is None
        assert fetched.selected_angle_idx is None
        assert fetched.benchmark_all_angles is False
