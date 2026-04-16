"""Tests for squad/context_builder.py — context assembly and research summarisation."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from squad.constants import (
    PHASE_BENCHMARK,
    PHASE_CADRAGE,
    PHASE_CHALLENGE,
    PHASE_CONCEPTION,
    PHASE_ETAT_DES_LIEUX,
    PHASE_SYNTHESE,
)
from squad.context_builder import (
    _RESEARCH_MAX_CHARS,
    _TARGET_CHARS,
    _get_answered_questions,
    build_cumulative_context,
    summarize_research,
)
from squad.models import PhaseOutput, Session


# ── fixtures ───────────────────────────────────────────────────────────────────


def _make_session(**kwargs) -> Session:
    defaults = {
        "id": "sess-test",
        "title": "Test Session",
        "project_path": "/tmp/myproject",
        "workspace_path": "/tmp/ws/sess-test",
        "idea": "A SaaS tool for squad orchestration",
    }
    defaults.update(kwargs)
    return Session(**defaults)


def _make_phase_output(phase: str, agent: str, output: str) -> PhaseOutput:
    return PhaseOutput(
        id=f"{phase}-{agent}",
        session_id="sess-test",
        phase=phase,
        agent=agent,
        output=output,
        file_path=f"/tmp/ws/{phase}/{agent}.md",
    )


# ── summarize_research ─────────────────────────────────────────────────────────


class TestSummarizeResearch:
    def test_returns_unchanged_when_within_budget(self):
        text = "Short research report."
        assert summarize_research(text) == text

    def test_returns_unchanged_at_exact_budget(self):
        text = "x" * _RESEARCH_MAX_CHARS
        assert summarize_research(text) == text

    def test_truncates_long_text(self):
        text = "x" * (_RESEARCH_MAX_CHARS + 5000)
        result = summarize_research(text)
        assert len(result) <= _RESEARCH_MAX_CHARS + 200  # marker overhead

    def test_appends_truncation_marker(self):
        text = "y" * (_RESEARCH_MAX_CHARS * 2)
        result = summarize_research(text)
        assert "tronqué" in result

    def test_cuts_at_paragraph_boundary_when_possible(self):
        # Build text with a clear paragraph boundary well before the cutoff
        para_content = "A" * (_RESEARCH_MAX_CHARS // 2)
        filler = "B" * (_RESEARCH_MAX_CHARS * 2)
        text = para_content + "\n\n" + filler
        result = summarize_research(text)
        # Should end near the paragraph break, not mid-word
        without_marker = result.split("\n\n*[")[0]
        assert without_marker.endswith(para_content)

    def test_deterministic_for_same_input(self):
        text = "Z" * (_RESEARCH_MAX_CHARS * 3)
        assert summarize_research(text) == summarize_research(text)

    def test_custom_max_chars_respected(self):
        text = "W" * 2000
        result = summarize_research(text, max_chars=500)
        assert len(result) <= 700  # 500 + marker overhead


# ── _get_answered_questions ────────────────────────────────────────────────────


class TestGetAnsweredQuestions:
    def test_returns_empty_when_table_missing(self, tmp_path):
        from sqlite_utils import Database

        db_path = tmp_path / "empty.db"
        Database(db_path)  # creates DB with no tables
        result = _get_answered_questions("sess-x", db_path)
        assert result == []

    def test_returns_only_answered_rows(self, tmp_path):
        from sqlite_utils import Database

        db_path = tmp_path / "test.db"
        db = Database(db_path)
        db["questions"].insert_all(
            [
                {
                    "id": "q1",
                    "session_id": "sess-x",
                    "agent": "pm",
                    "phase": PHASE_CADRAGE,
                    "question": "What is the target?",
                    "answer": "SMBs",
                    "answered_at": "2026-01-01T00:00:00",
                    "created_at": "2026-01-01T00:00:00",
                },
                {
                    "id": "q2",
                    "session_id": "sess-x",
                    "agent": "pm",
                    "phase": PHASE_CADRAGE,
                    "question": "Budget?",
                    "answer": None,
                    "answered_at": None,
                    "created_at": "2026-01-01T00:01:00",
                },
            ]
        )
        result = _get_answered_questions("sess-x", db_path)
        assert len(result) == 1
        assert result[0]["id"] == "q1"

    def test_filters_by_session_id(self, tmp_path):
        from sqlite_utils import Database

        db_path = tmp_path / "multi.db"
        db = Database(db_path)
        db["questions"].insert_all(
            [
                {
                    "id": "qa",
                    "session_id": "sess-A",
                    "agent": "pm",
                    "phase": PHASE_CADRAGE,
                    "question": "Q?",
                    "answer": "A",
                    "answered_at": "2026-01-01T00:00:00",
                    "created_at": "2026-01-01T00:00:00",
                },
                {
                    "id": "qb",
                    "session_id": "sess-B",
                    "agent": "pm",
                    "phase": PHASE_CADRAGE,
                    "question": "Q?",
                    "answer": "B",
                    "answered_at": "2026-01-01T00:00:00",
                    "created_at": "2026-01-01T00:00:00",
                },
            ]
        )
        result = _get_answered_questions("sess-A", db_path)
        assert len(result) == 1
        assert result[0]["id"] == "qa"


# ── build_cumulative_context ───────────────────────────────────────────────────


@pytest.fixture()
def mock_session():
    return _make_session()


@pytest.fixture()
def patch_get_session(mock_session):
    with patch("squad.context_builder.get_session", return_value=mock_session) as m:
        yield m


@pytest.fixture()
def patch_get_context():
    with patch(
        "squad.context_builder.get_context",
        return_value="# Project context\n\nStack: Python + SQLite",
    ) as m:
        yield m


@pytest.fixture()
def patch_answered_questions():
    with patch("squad.context_builder._get_answered_questions", return_value=[]) as m:
        yield m


@pytest.fixture()
def patch_list_phase_outputs():
    with patch("squad.context_builder.list_phase_outputs", return_value=[]) as m:
        yield m


class TestBuildCumulativeContext:
    def test_raises_if_session_not_found(self):
        with patch("squad.context_builder.get_session", return_value=None):
            with pytest.raises(ValueError, match="Session not found"):
                build_cumulative_context("nonexistent", PHASE_CADRAGE)

    def test_includes_idea(
        self,
        patch_get_session,
        patch_get_context,
        patch_answered_questions,
        patch_list_phase_outputs,
    ):
        ctx = build_cumulative_context("sess-test", PHASE_CADRAGE)
        assert "A SaaS tool for squad orchestration" in ctx

    def test_includes_project_context(
        self,
        patch_get_session,
        patch_get_context,
        patch_answered_questions,
        patch_list_phase_outputs,
    ):
        ctx = build_cumulative_context("sess-test", PHASE_CADRAGE)
        assert "Stack: Python + SQLite" in ctx

    def test_no_phase_outputs_for_first_phase(
        self,
        patch_get_session,
        patch_get_context,
        patch_answered_questions,
        patch_list_phase_outputs,
    ):
        # Phase cadrage is first — no preceding phases
        ctx = build_cumulative_context("sess-test", PHASE_CADRAGE)
        assert "Phase :" not in ctx

    def test_includes_preceding_phase_output(
        self,
        patch_get_session,
        patch_get_context,
        patch_answered_questions,
    ):
        outputs = [
            _make_phase_output(PHASE_CADRAGE, "pm", "PM cadrage analysis here"),
        ]
        with patch("squad.context_builder.list_phase_outputs", return_value=outputs):
            ctx = build_cumulative_context("sess-test", PHASE_ETAT_DES_LIEUX)
        assert "PM cadrage analysis here" in ctx

    def test_excludes_current_phase_output(
        self,
        patch_get_session,
        patch_get_context,
        patch_answered_questions,
    ):
        outputs = [
            _make_phase_output(PHASE_ETAT_DES_LIEUX, "architect", "Arch output"),
        ]
        with patch("squad.context_builder.list_phase_outputs", return_value=outputs):
            # Current phase is etat_des_lieux — should NOT appear
            ctx = build_cumulative_context("sess-test", PHASE_ETAT_DES_LIEUX)
        assert "Arch output" not in ctx

    def test_includes_qa_when_answered(
        self,
        patch_get_session,
        patch_get_context,
        patch_list_phase_outputs,
    ):
        answered = [
            {
                "agent": "pm",
                "phase": PHASE_CADRAGE,
                "question": "Who is the target?",
                "answer": "SMBs",
            }
        ]
        with patch("squad.context_builder._get_answered_questions", return_value=answered):
            ctx = build_cumulative_context("sess-test", PHASE_CONCEPTION)
        assert "Who is the target?" in ctx
        assert "SMBs" in ctx

    def test_no_qa_section_when_empty(
        self,
        patch_get_session,
        patch_get_context,
        patch_answered_questions,
        patch_list_phase_outputs,
    ):
        ctx = build_cumulative_context("sess-test", PHASE_CADRAGE)
        assert "## Q&A" not in ctx

    def test_benchmark_phase_output_is_summarized(
        self,
        patch_get_session,
        patch_get_context,
        patch_answered_questions,
    ):
        long_benchmark = "B" * (_RESEARCH_MAX_CHARS * 3)
        outputs = [
            _make_phase_output(PHASE_CADRAGE, "pm", "Short cadrage"),
            _make_phase_output(PHASE_BENCHMARK, "research", long_benchmark),
        ]
        with patch("squad.context_builder.list_phase_outputs", return_value=outputs):
            ctx = build_cumulative_context("sess-test", PHASE_CONCEPTION)
        # benchmark must be capped
        assert "tronqué" in ctx
        # cadrage must be fully present
        assert "Short cadrage" in ctx

    def test_non_benchmark_output_not_truncated(
        self,
        patch_get_session,
        patch_get_context,
        patch_answered_questions,
    ):
        long_output = "C" * 5000
        outputs = [_make_phase_output(PHASE_CADRAGE, "pm", long_output)]
        with patch("squad.context_builder.list_phase_outputs", return_value=outputs):
            ctx = build_cumulative_context("sess-test", PHASE_ETAT_DES_LIEUX)
        assert long_output in ctx

    def test_phases_1_to_6_all_return_context(
        self,
        patch_get_session,
        patch_get_context,
        patch_answered_questions,
        patch_list_phase_outputs,
    ):
        from squad.constants import PHASES

        for phase in PHASES:
            ctx = build_cumulative_context("sess-test", phase)
            assert "Idée du projet" in ctx
            assert "Contexte projet" in ctx

    def test_unknown_phase_returns_base_context(
        self,
        patch_get_session,
        patch_get_context,
        patch_answered_questions,
        patch_list_phase_outputs,
    ):
        ctx = build_cumulative_context("sess-test", "unknown_phase_xyz")
        # No preceding phases for unknown phase — still returns idea + context
        assert "A SaaS tool" in ctx
        assert "Phase :" not in ctx

    def test_context_within_target_chars_for_typical_session(
        self,
        patch_get_session,
        patch_get_context,
        patch_answered_questions,
    ):
        # Simulate a realistic session with moderate-length outputs per phase
        outputs = [
            _make_phase_output(PHASE_CADRAGE, "pm", "A" * 3000),
            _make_phase_output(PHASE_ETAT_DES_LIEUX, "architect", "B" * 3000),
            _make_phase_output(PHASE_BENCHMARK, "research", "C" * (_RESEARCH_MAX_CHARS * 2)),
            _make_phase_output(PHASE_CONCEPTION, "pm", "D" * 3000),
            _make_phase_output(PHASE_CHALLENGE, "security", "E" * 3000),
        ]
        with patch("squad.context_builder.list_phase_outputs", return_value=outputs):
            ctx = build_cumulative_context("sess-test", PHASE_SYNTHESE)
        # Benchmark is capped; total should be well within target
        assert len(ctx) < _TARGET_CHARS
