"""Tests for constants, enums and domain models."""

import pytest

from squad.constants import (
    AGENT_CAPABILITIES,
    CAP_EXECUTE_COMMANDS,
    CAP_READ_FILES,
    CAP_WEB_FETCH,
    CAP_WEB_SEARCH,
    CAP_WRITE_FILES,
    MODE_APPROVAL,
    MODE_AUTONOMOUS,
    PHASE_CADRAGE,
    PHASE_DIRS,
    PHASE_ETAT_DES_LIEUX,
    PHASE_LABELS,
    PHASE_SYNTHESE,
    PHASES,
    SESSION_MODES,
    SESSION_STATUSES,
    STATUS_APPROVED,
    STATUS_DONE,
    STATUS_DRAFT,
    STATUS_FAILED,
    STATUS_INTERVIEWING,
    STATUS_QUEUED,
    STATUS_REVIEW,
    STATUS_WORKING,
)
from squad.models import (
    EVENT_FAILED,
    EVENT_INTERVIEWING,
    EVENT_REVIEW,
    EVENT_WORKING,
    PIPELINE_EVENT_TYPES,
    RESEARCH_DEPTH_DEEP,
    RESEARCH_DEPTH_LIGHT,
    RESEARCH_DEPTH_NORMAL,
    RESEARCH_DEPTHS,
    AttachmentMeta,
    GeneratedPlan,
    PhaseOutput,
    PipelineEvent,
    Question,
    Session,
    SessionMode,
    SessionStatus,
    SubjectProfile,
)

# ── constants ──────────────────────────────────────────────────────────────────


class TestPhaseConstants:
    def test_phases_order(self):
        assert PHASES == [
            "cadrage",
            "etat_des_lieux",
            "benchmark",
            "conception",
            "challenge",
            "synthese",
        ]

    def test_phases_are_ascii(self):
        for phase in PHASES:
            assert phase.isascii()
            assert phase == phase.lower()

    def test_phase_labels_cover_all_phases(self):
        assert set(PHASE_LABELS.keys()) == set(PHASES)

    def test_phase_dirs_cover_all_phases(self):
        assert set(PHASE_DIRS.keys()) == set(PHASES)

    def test_phase_dir_numbering(self):
        dirs = list(PHASE_DIRS.values())
        for i, d in enumerate(dirs, start=1):
            assert d.startswith(str(i) + "-")

    def test_phase_labels_content(self):
        assert PHASE_LABELS[PHASE_CADRAGE] == "Cadrage"
        assert PHASE_LABELS[PHASE_ETAT_DES_LIEUX] == "État des lieux"
        assert PHASE_LABELS[PHASE_SYNTHESE] == "Synthèse"

    def test_phase_dirs_content(self):
        assert PHASE_DIRS[PHASE_CADRAGE] == "1-cadrage"
        assert PHASE_DIRS[PHASE_ETAT_DES_LIEUX] == "2-etat-des-lieux"
        assert PHASE_DIRS[PHASE_SYNTHESE] == "6-synthese"


class TestSessionStatusConstants:
    def test_all_statuses_present(self):
        assert set(SESSION_STATUSES) == {
            "draft",
            "interviewing",
            "working",
            "review",
            "approved",
            "queued",
            "done",
            "failed",
        }

    def test_status_values(self):
        assert STATUS_DRAFT == "draft"
        assert STATUS_INTERVIEWING == "interviewing"
        assert STATUS_WORKING == "working"
        assert STATUS_REVIEW == "review"
        assert STATUS_APPROVED == "approved"
        assert STATUS_QUEUED == "queued"
        assert STATUS_DONE == "done"
        assert STATUS_FAILED == "failed"


class TestSessionModeConstants:
    def test_all_modes_present(self):
        assert set(SESSION_MODES) == {"approval", "autonomous"}

    def test_mode_values(self):
        assert MODE_APPROVAL == "approval"
        assert MODE_AUTONOMOUS == "autonomous"


class TestAgentCapabilityConstants:
    def test_all_capabilities_present(self):
        assert set(AGENT_CAPABILITIES) == {
            "web_search",
            "web_fetch",
            "read_files",
            "write_files",
            "execute_commands",
        }

    def test_capability_values(self):
        assert CAP_WEB_SEARCH == "web_search"
        assert CAP_WEB_FETCH == "web_fetch"
        assert CAP_READ_FILES == "read_files"
        assert CAP_WRITE_FILES == "write_files"
        assert CAP_EXECUTE_COMMANDS == "execute_commands"


# ── enums ──────────────────────────────────────────────────────────────────────


class TestSessionStatusEnum:
    def test_values_match_constants(self):
        assert set(SessionStatus.values()) == set(SESSION_STATUSES)

    def test_string_coercion(self):
        assert SessionStatus.DRAFT == "draft"
        assert SessionStatus("working") == SessionStatus.WORKING


class TestSessionModeEnum:
    def test_values_match_constants(self):
        assert set(SessionMode.values()) == set(SESSION_MODES)

    def test_string_coercion(self):
        assert SessionMode.APPROVAL == "approval"


# ── models ─────────────────────────────────────────────────────────────────────


class TestSession:
    def _make(self, **kwargs) -> Session:
        defaults = dict(
            id="sess-1",
            title="Test session",
            project_path="/tmp/proj",
            workspace_path="/tmp/proj/.squad/sessions/sess-1",
            idea="improve the CRM",
        )
        return Session(**{**defaults, **kwargs})

    def test_default_status(self):
        s = self._make()
        assert s.status == STATUS_DRAFT

    def test_default_mode(self):
        s = self._make()
        assert s.mode == MODE_APPROVAL

    def test_default_current_phase_is_none(self):
        s = self._make()
        assert s.current_phase is None

    def test_created_at_set(self):
        s = self._make()
        assert s.created_at is not None

    def test_invalid_status_raises(self):
        with pytest.raises(ValueError, match="Invalid status"):
            self._make(status="nonexistent")

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="Invalid mode"):
            self._make(mode="nonexistent")

    def test_valid_mode_autonomous(self):
        s = self._make(mode=MODE_AUTONOMOUS)
        assert s.mode == MODE_AUTONOMOUS


class TestPhaseOutput:
    def test_instantiation(self):
        po = PhaseOutput(
            id="po-1",
            session_id="sess-1",
            phase=PHASE_CADRAGE,
            agent="pm",
            output="output text",
            file_path="/tmp/proj/.squad/sessions/sess-1/phases/1-cadrage/pm.md",
        )
        assert po.duration_seconds is None
        assert po.tokens_used is None
        assert po.attempt == 1
        assert po.created_at is not None

    def test_attempt_tracking(self):
        po = PhaseOutput(
            id="po-2",
            session_id="sess-1",
            phase=PHASE_CADRAGE,
            agent="pm",
            output="retry output",
            file_path="/f.md",
            attempt=2,
        )
        assert po.attempt == 2


class TestQuestion:
    def test_instantiation(self):
        q = Question(
            id="q-1",
            session_id="sess-1",
            agent="pm",
            phase=PHASE_CADRAGE,
            question="What is the target segment?",
        )
        assert q.answer is None
        assert q.answered_at is None


class TestGeneratedPlan:
    def test_instantiation(self):
        p = GeneratedPlan(
            id="plan-1",
            session_id="sess-1",
            title="Plan 1 — CRM improvements",
            file_path="/tmp/proj/plans/plan-1.md",
            content="## LOT 1 — ...",
        )
        assert p.forge_status is None
        assert p.created_at is not None


# ── session profile (LOT 2) ────────────────────────────────────────────────────


class TestSessionProfileFields:
    def _make(self, **kwargs) -> Session:
        defaults = dict(
            id="sess-1",
            title="Test",
            project_path="/tmp/proj",
            workspace_path="/tmp/proj/.squad/sessions/sess-1",
            idea="x",
        )
        return Session(**{**defaults, **kwargs})

    def test_profile_defaults_are_empty(self):
        s = self._make()
        assert s.subject_type is None
        assert s.research_depth is None
        assert s.agents_by_phase == {}
        assert s.phase_attempts == {}
        assert s.challenge_retry_count == 0
        assert s.skipped_phases == {}

    def test_profile_fields_round_trip(self):
        s = self._make(
            subject_type="b2b_saas",
            research_depth=RESEARCH_DEPTH_DEEP,
            agents_by_phase={"etat_des_lieux": ["sales", "ux"]},
            phase_attempts={"cadrage": 2},
            challenge_retry_count=1,
            skipped_phases={"benchmark": "light"},
        )
        assert s.subject_type == "b2b_saas"
        assert s.research_depth == RESEARCH_DEPTH_DEEP
        assert s.agents_by_phase == {"etat_des_lieux": ["sales", "ux"]}
        assert s.phase_attempts == {"cadrage": 2}
        assert s.challenge_retry_count == 1
        assert s.skipped_phases == {"benchmark": "light"}

    def test_invalid_depth_rejected(self):
        with pytest.raises(ValueError):
            self._make(research_depth="extreme")


class TestSubjectProfile:
    def test_valid_profile(self):
        p = SubjectProfile(
            subject_type="ai_product",
            research_depth=RESEARCH_DEPTH_NORMAL,
            agents_by_phase={"conception": ["ai-lead", "architect"]},
        )
        assert p.subject_type == "ai_product"
        assert p.research_depth == RESEARCH_DEPTH_NORMAL

    def test_empty_agents_by_phase_default(self):
        p = SubjectProfile(subject_type="x", research_depth=RESEARCH_DEPTH_LIGHT)
        assert p.agents_by_phase == {}

    def test_invalid_depth_rejected(self):
        with pytest.raises(ValueError):
            SubjectProfile(subject_type="x", research_depth="magic")


class TestResearchDepthConstants:
    def test_all_depths_present(self):
        assert set(RESEARCH_DEPTHS) == {"light", "normal", "deep"}


# ── Slack origin fields (LOT 1 — Plan 4) ──────────────────────────────────────


class TestSessionSlackFields:
    def _make(self, **kwargs) -> Session:
        defaults = dict(
            id="sess-1",
            title="Test",
            project_path="/tmp/proj",
            workspace_path="/tmp/proj/.squad/sessions/sess-1",
            idea="x",
        )
        return Session(**{**defaults, **kwargs})

    def test_slack_defaults_are_none(self):
        s = self._make()
        assert s.slack_channel is None
        assert s.slack_thread_ts is None
        assert s.slack_user_id is None

    def test_slack_fields_roundtrip(self):
        s = self._make(
            slack_channel="C999",
            slack_thread_ts="1700000000.000100",
            slack_user_id="U123",
        )
        assert s.slack_channel == "C999"
        assert s.slack_thread_ts == "1700000000.000100"
        assert s.slack_user_id == "U123"

    def test_failure_reason_default_none(self):
        s = self._make()
        assert s.failure_reason is None

    def test_failure_reason_roundtrip(self):
        s = self._make(failure_reason="pm exploded")
        assert s.failure_reason == "pm exploded"


# ── Pipeline events (LOT 2 — Plan 4) ──────────────────────────────────────────


class TestPipelineEvent:
    def test_all_event_types_present(self):
        assert set(PIPELINE_EVENT_TYPES) == {
            EVENT_WORKING,
            EVENT_INTERVIEWING,
            EVENT_REVIEW,
            EVENT_FAILED,
        }

    def test_instantiation_with_defaults(self):
        from datetime import datetime

        e = PipelineEvent(
            type=EVENT_WORKING,
            session_id="s1",
            timestamp_utc=datetime.utcnow(),
            elapsed_seconds=42.0,
            phase="cadrage",
        )
        assert e.type == EVENT_WORKING
        assert e.phase == "cadrage"
        assert e.pending_questions == 0
        assert e.plans_count == 0
        assert e.failure_reason is None

    def test_invalid_type_rejected(self):
        from datetime import datetime

        with pytest.raises(ValueError, match="Invalid pipeline event type"):
            PipelineEvent(
                type="nope",
                session_id="s1",
                timestamp_utc=datetime.utcnow(),
                elapsed_seconds=0.0,
            )

    def test_review_event_carries_plan_count(self):
        from datetime import datetime

        e = PipelineEvent(
            type=EVENT_REVIEW,
            session_id="s1",
            timestamp_utc=datetime.utcnow(),
            elapsed_seconds=600.0,
            plans_count=3,
        )
        assert e.plans_count == 3

    def test_failed_event_carries_reason(self):
        from datetime import datetime

        e = PipelineEvent(
            type=EVENT_FAILED,
            session_id="s1",
            timestamp_utc=datetime.utcnow(),
            elapsed_seconds=600.0,
            failure_reason="critical agent pm failed",
        )
        assert e.failure_reason == "critical agent pm failed"


# ── AttachmentMeta (LOT 3 — Plan 4) ───────────────────────────────────────────


class TestAttachmentMeta:
    def test_extension_inferred_from_filename(self):
        m = AttachmentMeta(
            session_id="s1",
            filename="brief.md",
            path="/tmp/attachments/brief.md",
            size_bytes=120,
        )
        assert m.extension == "md"

    def test_explicit_extension_preserved(self):
        m = AttachmentMeta(
            session_id="s1",
            filename="weird",
            path="/tmp/weird",
            size_bytes=10,
            extension="txt",
        )
        assert m.extension == "txt"

    def test_extension_lowercased(self):
        m = AttachmentMeta(
            session_id="s1",
            filename="REPORT.PDF",
            path="/tmp/REPORT.PDF",
            size_bytes=2048,
        )
        assert m.extension == "pdf"

    def test_optional_fields(self):
        m = AttachmentMeta(
            session_id="s1",
            filename="brief.md",
            path="/tmp/brief.md",
            size_bytes=10,
        )
        assert m.mime_type is None
        assert m.slack_file_id is None
        assert m.uploaded_at is not None
