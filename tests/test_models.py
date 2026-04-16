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
    GeneratedPlan,
    PhaseOutput,
    Question,
    Session,
    SessionMode,
    SessionStatus,
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
            "draft", "interviewing", "working", "review",
            "approved", "queued", "done", "failed",
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
            "web_search", "web_fetch", "read_files", "write_files", "execute_commands",
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
        assert po.created_at is not None


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
