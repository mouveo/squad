"""Tests for squad/context_builder.py — context assembly and research summarisation."""

from unittest.mock import patch

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
    _filter_latest_attempt,
    _get_answered_questions,
    build_cumulative_context,
    extract_challenge_constraints,
    format_qa,
    summarize_benchmark_structured,
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


def _make_phase_output(phase: str, agent: str, output: str, attempt: int = 1) -> PhaseOutput:
    return PhaseOutput(
        id=f"{phase}-{agent}-{attempt}",
        session_id="sess-test",
        phase=phase,
        agent=agent,
        output=output,
        file_path=f"/tmp/ws/{phase}/{agent}.md",
        attempt=attempt,
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


# ── format_qa ──────────────────────────────────────────────────────────────────


class TestFormatQA:
    def test_empty_list_returns_empty_string(self):
        assert format_qa([]) == ""

    def test_single_entry(self):
        out = format_qa(
            [
                {
                    "agent": "pm",
                    "phase": PHASE_CADRAGE,
                    "question": "Who is the target?",
                    "answer": "SMBs",
                }
            ]
        )
        assert "## Q&A" in out
        assert "Who is the target?" in out
        assert "SMBs" in out
        assert "pm/cadrage" in out

    def test_skips_unanswered(self):
        out = format_qa(
            [
                {"agent": "pm", "phase": PHASE_CADRAGE, "question": "?", "answer": None},
            ]
        )
        assert out == ""

    def test_multiple_entries_separated(self):
        out = format_qa(
            [
                {"agent": "pm", "phase": PHASE_CADRAGE, "question": "q1", "answer": "a1"},
                {"agent": "pm", "phase": PHASE_CADRAGE, "question": "q2", "answer": "a2"},
            ]
        )
        assert "q1" in out and "q2" in out


# ── summarize_benchmark_structured ─────────────────────────────────────────────


_STRUCTURED_REPORT = """# Benchmark

## Résumé exécutif

- point A
- point B

## Concurrents

| Acteur | Positionnement |
| --- | --- |
| X | leader |

## Analyse par axe

### Axis 1

Long detail here.

## Sources

- https://example.com
"""


class TestSummarizeBenchmarkStructured:
    def test_short_text_returned_unchanged(self):
        text = "short benchmark"
        assert summarize_benchmark_structured(text, max_chars=500) == text

    def test_keeps_resume_executif_and_concurrents(self):
        # Build a long but structured report
        long_axis = "word " * 3000
        text = _STRUCTURED_REPORT.replace("Long detail here.", long_axis)
        out = summarize_benchmark_structured(text, max_chars=2000)
        assert "Résumé exécutif" in out
        assert "Concurrents" in out

    def test_drops_secondary_sections_to_fit_budget(self):
        long_axis = "word " * 3000
        text = _STRUCTURED_REPORT.replace("Long detail here.", long_axis)
        out = summarize_benchmark_structured(text, max_chars=800)
        assert len(out) <= 1200  # modest overhead for omission marker
        assert "Sections secondaires omises" in out or "tronqué" in out

    def test_fallback_to_deterministic_truncation_when_no_structure(self):
        prose = "x" * 5000
        out = summarize_benchmark_structured(prose, max_chars=1000)
        assert "tronqué" in out

    def test_fallback_when_no_priority_heading(self):
        # Structured but headings not recognised
        text = "## Random\n\n" + ("y" * 5000) + "\n\n## Another\n\nmore"
        out = summarize_benchmark_structured(text, max_chars=500)
        assert "tronqué" in out


# ── extract_challenge_constraints ──────────────────────────────────────────────


class TestExtractChallengeConstraints:
    def test_returns_empty_when_no_challenge_output(self):
        outputs = [_make_phase_output(PHASE_CADRAGE, "pm", "some text")]
        assert extract_challenge_constraints(outputs) == []

    def test_parses_blockers_from_challenge_output(self):
        content = (
            "# Security challenge\n\n"
            '```json\n{"blockers": [{"id": "b1", "severity": "blocking", '
            '"constraint": "No auth wall"}]}\n```'
        )
        outputs = [_make_phase_output(PHASE_CHALLENGE, "security", content)]
        lines = extract_challenge_constraints(outputs)
        assert len(lines) == 1
        assert "blocking" in lines[0]
        assert "No auth wall" in lines[0]
        assert "security" in lines[0]

    def test_ignores_unparseable_output(self):
        outputs = [_make_phase_output(PHASE_CHALLENGE, "security", "no json here")]
        assert extract_challenge_constraints(outputs) == []

    def test_dedupes_identical_constraints(self):
        body = (
            '```json\n{"blockers": [{"id": "b1", "severity": "major", "constraint": "same"}]}\n```'
        )
        outputs = [
            _make_phase_output(PHASE_CHALLENGE, "security", body),
            _make_phase_output(PHASE_CHALLENGE, "delivery", body),
        ]
        assert len(extract_challenge_constraints(outputs)) == 1


# ── _filter_latest_attempt ─────────────────────────────────────────────────────


class TestFilterLatestAttempt:
    def test_keeps_only_latest_per_phase(self):
        outputs = [
            _make_phase_output(PHASE_CONCEPTION, "architect", "v1", attempt=1),
            _make_phase_output(PHASE_CONCEPTION, "architect", "v2", attempt=2),
            _make_phase_output(PHASE_CADRAGE, "pm", "cadrage", attempt=1),
        ]
        filtered = _filter_latest_attempt(outputs)
        assert len(filtered) == 2
        texts = {po.output for po in filtered}
        assert texts == {"v2", "cadrage"}

    def test_preserves_order(self):
        outputs = [
            _make_phase_output(PHASE_CADRAGE, "pm", "a", attempt=1),
            _make_phase_output(PHASE_CONCEPTION, "ux", "b", attempt=1),
            _make_phase_output(PHASE_CONCEPTION, "ux", "c", attempt=2),
        ]
        filtered = _filter_latest_attempt(outputs)
        assert [po.output for po in filtered] == ["a", "c"]


# ── attempt-aware build_cumulative_context ─────────────────────────────────────


class TestBuildCumulativeContextAttemptAware:
    def test_only_latest_attempt_reinjected(
        self,
        patch_get_session,
        patch_get_context,
        patch_answered_questions,
    ):
        outputs = [
            _make_phase_output(PHASE_CONCEPTION, "architect", "stale attempt", attempt=1),
            _make_phase_output(PHASE_CONCEPTION, "architect", "fresh attempt", attempt=2),
        ]
        with patch("squad.context_builder.list_phase_outputs", return_value=outputs):
            ctx = build_cumulative_context("sess-test", PHASE_CHALLENGE)
        assert "fresh attempt" in ctx
        assert "stale attempt" not in ctx

    def test_challenge_constraints_section_included(
        self,
        patch_get_session,
        patch_get_context,
        patch_answered_questions,
    ):
        challenge_body = (
            "# Security challenge\n"
            '```json\n{"blockers": [{"id": "b1", "severity": "blocking", '
            '"constraint": "Missing rate limiting"}]}\n```'
        )
        outputs = [_make_phase_output(PHASE_CHALLENGE, "security", challenge_body)]
        with patch("squad.context_builder.list_phase_outputs", return_value=outputs):
            ctx = build_cumulative_context("sess-test", PHASE_SYNTHESE)
        assert "Contraintes issues du challenge" in ctx
        assert "Missing rate limiting" in ctx

    def test_structured_benchmark_summarised_before_injection(
        self,
        patch_get_session,
        patch_get_context,
        patch_answered_questions,
    ):
        axis_body = "detail " * 4000  # much larger than budget
        report = (
            "# Benchmark\n\n"
            "## Résumé exécutif\n\n- p1\n\n"
            "## Concurrents\n\n| X | Y |\n\n"
            f"## Analyse par axe\n\n{axis_body}\n\n"
            "## Sources\n\n- https://x"
        )
        outputs = [_make_phase_output(PHASE_BENCHMARK, "research", report)]
        with patch("squad.context_builder.list_phase_outputs", return_value=outputs):
            ctx = build_cumulative_context("sess-test", PHASE_CONCEPTION)
        assert "Résumé exécutif" in ctx
        assert "Concurrents" in ctx
        # Budget cap respected
        assert len(ctx) < _TARGET_CHARS


# ── attachments section (LOT 3 — Plan 4) ──────────────────────────────────────


from squad.context_builder import format_attachments  # noqa: E402
from squad.models import AttachmentMeta as _AttachmentMeta  # noqa: E402


def _attach(filename, *, size=100, mime=None, ext=None) -> _AttachmentMeta:
    return _AttachmentMeta(
        session_id="sess-test",
        filename=filename,
        path=f"/tmp/attachments/{filename}",
        size_bytes=size,
        mime_type=mime,
        extension=ext or "",
    )


class TestFormatAttachments:
    def test_returns_empty_when_no_attachments(self):
        assert format_attachments([]) == ""

    def test_lists_each_attachment(self):
        out = format_attachments(
            [
                _attach("brief.md", size=120),
                _attach("photo.png", size=2048, mime="image/png"),
            ]
        )
        assert "## Fichiers joints" in out
        assert "`brief.md`" in out
        assert "`photo.png`" in out

    def test_inlines_text_files_only(self, tmp_path):
        # Real file on disk so the inliner can read it
        p = tmp_path / "brief.md"
        p.write_text("# brief\n\nimportant detail", encoding="utf-8")
        out = format_attachments(
            [
                _AttachmentMeta(
                    session_id="s",
                    filename="brief.md",
                    path=str(p),
                    size_bytes=p.stat().st_size,
                ),
                _attach("photo.png", size=2048, mime="image/png"),
            ]
        )
        assert "important detail" in out
        # Binary file is referenced but never inlined
        assert "photo.png" in out
        assert "image/png" in out

    def test_truncates_large_text_attachment(self, tmp_path):
        p = tmp_path / "huge.md"
        p.write_text("X" * 50_000, encoding="utf-8")
        out = format_attachments(
            [
                _AttachmentMeta(
                    session_id="s",
                    filename="huge.md",
                    path=str(p),
                    size_bytes=p.stat().st_size,
                )
            ]
        )
        assert "Tronqué" in out

    def test_binary_extensions_not_inlined(self, tmp_path):
        p = tmp_path / "doc.pdf"
        p.write_bytes(b"%PDF-1.4\n%fake")
        out = format_attachments(
            [
                _AttachmentMeta(
                    session_id="s",
                    filename="doc.pdf",
                    path=str(p),
                    size_bytes=p.stat().st_size,
                    mime_type="application/pdf",
                )
            ]
        )
        # Only listed, never inlined
        assert "doc.pdf" in out
        assert "%PDF" not in out


class TestBuildCumulativeContextWithAttachments:
    def test_attachments_section_appears_after_qa(
        self,
        patch_get_session,
        patch_get_context,
        patch_list_phase_outputs,
        tmp_path,
    ):
        attached = tmp_path / "brief.md"
        attached.write_text("Persona: SMB ops manager", encoding="utf-8")

        meta = _AttachmentMeta(
            session_id="sess-test",
            filename="brief.md",
            path=str(attached),
            size_bytes=attached.stat().st_size,
        )
        with (
            patch(
                "squad.context_builder._get_answered_questions",
                return_value=[
                    {"agent": "pm", "phase": "cadrage", "question": "Q?", "answer": "A"}
                ],
            ),
            patch("squad.context_builder.list_attachments", return_value=[meta]),
        ):
            ctx = build_cumulative_context("sess-test", PHASE_CADRAGE)

        assert "## Q&A" in ctx
        assert "## Fichiers joints" in ctx
        # Attachments come after Q&A
        assert ctx.index("## Q&A") < ctx.index("## Fichiers joints")
        assert "Persona: SMB ops manager" in ctx

    def test_no_attachments_section_when_none(
        self,
        patch_get_session,
        patch_get_context,
        patch_answered_questions,
        patch_list_phase_outputs,
    ):
        with patch("squad.context_builder.list_attachments", return_value=[]):
            ctx = build_cumulative_context("sess-test", PHASE_CADRAGE)
        assert "## Fichiers joints" not in ctx


# ── LOT 7 — ideation angle injection ──────────────────────────────────────────


def _angle(idx: int, title: str = "T", segment: str = "S", vp: str = "VP"):
    from squad.models import IdeationAngle

    return IdeationAngle(
        session_id="sess-test",
        idx=idx,
        title=title,
        segment=segment,
        value_prop=vp,
        approach="Approach",
        divergence_note="Divergence",
    )


class TestBuildCumulativeContextAngleInjection:
    """Angle sections must appear only for the right phase + flag combo."""

    def _session(self, **overrides):
        return _make_session(**overrides)

    def test_benchmark_all_angles_injects_all(
        self,
        patch_get_context,
        patch_answered_questions,
    ):
        session = self._session(
            selected_angle_idx=0, benchmark_all_angles=True
        )
        angles = [_angle(i, title=f"A{i}") for i in range(3)]
        with (
            patch("squad.context_builder.get_session", return_value=session),
            patch("squad.context_builder.list_ideation_angles", return_value=angles),
            patch("squad.context_builder.list_phase_outputs", return_value=[]),
        ):
            ctx = build_cumulative_context("sess-test", PHASE_BENCHMARK)
        assert "## Angles à benchmarker" in ctx
        for i in range(3):
            assert f"### Angle {i} — A{i}" in ctx
        assert "## Angle choisi" not in ctx

    def test_benchmark_with_selected_idx_injects_single(
        self,
        patch_get_context,
        patch_answered_questions,
    ):
        session = self._session(
            selected_angle_idx=1, benchmark_all_angles=False
        )
        angles = [_angle(i, title=f"A{i}") for i in range(3)]
        with (
            patch("squad.context_builder.get_session", return_value=session),
            patch("squad.context_builder.list_ideation_angles", return_value=angles),
            patch("squad.context_builder.list_phase_outputs", return_value=[]),
        ):
            ctx = build_cumulative_context("sess-test", PHASE_BENCHMARK)
        assert "## Angle choisi" in ctx
        assert "Angle 1 — A1" in ctx
        assert "## Angles à benchmarker" not in ctx
        # Other angles must not leak
        assert "A0" not in ctx
        assert "A2" not in ctx

    def test_conception_never_sees_multiple_angles(
        self,
        patch_get_context,
        patch_answered_questions,
    ):
        """Even with benchmark_all_angles=True, conception stays mono-angle."""
        session = self._session(
            selected_angle_idx=2, benchmark_all_angles=True
        )
        angles = [_angle(i, title=f"A{i}") for i in range(3)]
        with (
            patch("squad.context_builder.get_session", return_value=session),
            patch("squad.context_builder.list_ideation_angles", return_value=angles),
            patch("squad.context_builder.list_phase_outputs", return_value=[]),
        ):
            ctx = build_cumulative_context("sess-test", PHASE_CONCEPTION)
        assert "## Angles à benchmarker" not in ctx
        assert "## Angle choisi" in ctx
        assert "Angle 2 — A2" in ctx
        assert "A0" not in ctx

    def test_challenge_uses_selected_angle(
        self,
        patch_get_context,
        patch_answered_questions,
    ):
        session = self._session(selected_angle_idx=0, benchmark_all_angles=False)
        angles = [_angle(0, title="only")]
        with (
            patch("squad.context_builder.get_session", return_value=session),
            patch("squad.context_builder.list_ideation_angles", return_value=angles),
            patch("squad.context_builder.list_phase_outputs", return_value=[]),
        ):
            ctx = build_cumulative_context("sess-test", PHASE_CHALLENGE)
        assert "## Angle choisi" in ctx

    def test_synthese_uses_selected_angle(
        self,
        patch_get_context,
        patch_answered_questions,
    ):
        session = self._session(selected_angle_idx=0, benchmark_all_angles=True)
        angles = [_angle(0, title="only")]
        with (
            patch("squad.context_builder.get_session", return_value=session),
            patch("squad.context_builder.list_ideation_angles", return_value=angles),
            patch("squad.context_builder.list_phase_outputs", return_value=[]),
        ):
            ctx = build_cumulative_context("sess-test", PHASE_SYNTHESE)
        # Mono-angle even though benchmark_all_angles=True
        assert "## Angle choisi" in ctx
        assert "## Angles à benchmarker" not in ctx

    def test_cadrage_gets_no_angle_section(
        self,
        patch_get_context,
        patch_answered_questions,
    ):
        session = self._session(selected_angle_idx=1, benchmark_all_angles=True)
        angles = [_angle(i) for i in range(2)]
        with (
            patch("squad.context_builder.get_session", return_value=session),
            patch("squad.context_builder.list_ideation_angles", return_value=angles),
            patch("squad.context_builder.list_phase_outputs", return_value=[]),
        ):
            ctx = build_cumulative_context("sess-test", PHASE_CADRAGE)
        assert "## Angles à benchmarker" not in ctx
        assert "## Angle choisi" not in ctx

    def test_benchmark_without_selection_skips_angle(
        self,
        patch_get_context,
        patch_answered_questions,
    ):
        """If ideation has not yet run (no angles, no selection), skip."""
        session = self._session(selected_angle_idx=None, benchmark_all_angles=False)
        with (
            patch("squad.context_builder.get_session", return_value=session),
            patch("squad.context_builder.list_ideation_angles", return_value=[]),
            patch("squad.context_builder.list_phase_outputs", return_value=[]),
        ):
            ctx = build_cumulative_context("sess-test", PHASE_BENCHMARK)
        assert "## Angle choisi" not in ctx
        assert "## Angles à benchmarker" not in ctx

    def test_out_of_range_selected_idx_emits_empty(
        self,
        patch_get_context,
        patch_answered_questions,
    ):
        """An invalid selected_angle_idx yields no angle section (graceful)."""
        session = self._session(selected_angle_idx=9, benchmark_all_angles=False)
        angles = [_angle(0, title="one")]
        with (
            patch("squad.context_builder.get_session", return_value=session),
            patch("squad.context_builder.list_ideation_angles", return_value=angles),
            patch("squad.context_builder.list_phase_outputs", return_value=[]),
        ):
            ctx = build_cumulative_context("sess-test", PHASE_BENCHMARK)
        assert "## Angle choisi" not in ctx
