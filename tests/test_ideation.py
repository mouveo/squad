"""Tests for squad/ideation.py — parse_angles, parse_strategy, run_ideation."""

from pathlib import Path
from unittest.mock import patch

import pytest

from squad.db import (
    create_session,
    ensure_schema,
    list_ideation_angles,
)
from squad.ideation import (
    IdeationResult,
    parse_angles,
    parse_strategy,
    run_ideation,
)
from squad.models import IdeationAngle


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
    return create_session(
        title="Test session",
        project_path=str(project_dir),
        workspace_path=str(workspace_path),
        idea="Reduce onboarding time for new SaaS customers",
        db_path=db_path,
    )


# ── parse_angles ───────────────────────────────────────────────────────────────


_NOMINAL_ANGLES_MD = """# Ideation

Intro line.

## Angle 0 — Focus B2B ops
- Segment: SMB operations leads
- Value prop: Save 2h/week on manual steps
- Approche: Lightweight automation over existing CRM
- Note de divergence: segment (ops vs founders)

## Angle 1 — Enterprise compliance
- Segment: Enterprise IT
- Value prop: Audit-ready integration
- Approche technique: SSO + audit log ingestion
- Note de divergence: approche (governance-first)

## Angle 2 — Self-serve founders
- Segment: early-stage founders
- Value prop: Ship in 10 minutes
- Approche: Single-binary CLI, no backend
- Note de divergence: value prop (speed vs thoroughness)

```json
{"strategy": "auto_pick", "best_angle_idx": 0, "rationale": "B2B ops has the clearest signal in cadrage.", "divergence_score": "high"}
```
"""


class TestParseAnglesNominal:
    def test_parses_three_angles(self):
        angles = parse_angles(_NOMINAL_ANGLES_MD, "sess-1")
        assert len(angles) == 3

    def test_idx_is_order_based_zero_indexed(self):
        angles = parse_angles(_NOMINAL_ANGLES_MD, "sess-1")
        assert [a.idx for a in angles] == [0, 1, 2]

    def test_session_id_forwarded(self):
        angles = parse_angles(_NOMINAL_ANGLES_MD, "sess-X")
        assert all(a.session_id == "sess-X" for a in angles)

    def test_titles_extracted_after_dash(self):
        angles = parse_angles(_NOMINAL_ANGLES_MD, "sess-1")
        assert angles[0].title == "Focus B2B ops"
        assert angles[1].title == "Enterprise compliance"
        assert angles[2].title == "Self-serve founders"

    def test_fields_populated(self):
        angles = parse_angles(_NOMINAL_ANGLES_MD, "sess-1")
        a0 = angles[0]
        assert a0.segment == "SMB operations leads"
        assert a0.value_prop == "Save 2h/week on manual steps"
        assert a0.approach == "Lightweight automation over existing CRM"
        assert a0.divergence_note == "segment (ops vs founders)"

    def test_alternate_field_labels_accepted(self):
        """'Approche technique' / 'Note de divergence' are tolerated."""
        angles = parse_angles(_NOMINAL_ANGLES_MD, "sess-1")
        assert angles[1].approach == "SSO + audit log ingestion"
        assert angles[2].divergence_note == "value prop (speed vs thoroughness)"


class TestParseAnglesEmpty:
    def test_empty_string(self):
        assert parse_angles("", "sess-1") == []

    def test_only_whitespace(self):
        assert parse_angles("   \n\n  ", "sess-1") == []

    def test_no_angle_headers(self):
        md = "# Title\n\nSome prose without any angle.\n\n## Some other section\n- bullet\n"
        assert parse_angles(md, "sess-1") == []

    def test_angle_header_without_any_field_is_skipped(self):
        md = "## Angle 0 — Empty\n\n## Angle 1 — With fields\n- Segment: X\n"
        angles = parse_angles(md, "sess-1")
        assert len(angles) == 1
        assert angles[0].title == "With fields"
        # Order-based idx → first surviving angle becomes 0
        assert angles[0].idx == 0


class TestParseAnglesSpecialTitles:
    def test_title_ignored_number_is_normalised_to_order(self):
        """Even if the agent prints Angle 7 / Angle 3, idx follows document order."""
        md = (
            "## Angle 7 — First in doc\n"
            "- Segment: A\n\n"
            "## Angle 3 — Second in doc\n"
            "- Segment: B\n\n"
            "## Angle 99 — Third\n"
            "- Segment: C\n"
        )
        angles = parse_angles(md, "sess-1")
        assert [a.idx for a in angles] == [0, 1, 2]
        assert angles[0].title == "First in doc"
        assert angles[1].title == "Second in doc"
        assert angles[2].title == "Third"

    def test_title_with_unicode_and_punctuation(self):
        md = (
            "## Angle 0 — Éco-système low-code / no-code\n"
            "- Segment: Product ops\n"
        )
        angles = parse_angles(md, "sess-1")
        assert angles[0].title == "Éco-système low-code / no-code"

    def test_title_without_separator_falls_back_to_remainder(self):
        md = "## Angle 0\n- Segment: A\n"
        angles = parse_angles(md, "sess-1")
        # No separator → title becomes whatever follows "Angle" (just "0")
        assert angles[0].title == "0"

    def test_accepts_level_three_headers(self):
        md = "### Angle 0 — H3 variant\n- Segment: Z\n"
        angles = parse_angles(md, "sess-1")
        assert len(angles) == 1
        assert angles[0].title == "H3 variant"

    def test_is_case_insensitive_on_angle_keyword(self):
        md = "## ANGLE 0 — caps\n- Segment: Z\n"
        angles = parse_angles(md, "sess-1")
        assert len(angles) == 1


# ── parse_strategy ─────────────────────────────────────────────────────────────


class TestParseStrategyNominal:
    def test_complete_block(self):
        md = (
            "intro\n\n"
            '```json\n{"strategy": "auto_pick", "best_angle_idx": 2, '
            '"rationale": "Angle 2 dominates.", "divergence_score": "high"}\n```'
        )
        result = parse_strategy(md)
        assert result["strategy"] == "auto_pick"
        assert result["best_angle_idx"] == 2
        assert result["rationale"] == "Angle 2 dominates."
        assert result["divergence_score"] == "high"

    def test_ask_user_strategy(self):
        md = (
            '```json\n{"strategy": "ask_user", "best_angle_idx": 0, '
            '"rationale": "No clear winner.", "divergence_score": "low"}\n```'
        )
        result = parse_strategy(md)
        assert result["strategy"] == "ask_user"


class TestParseStrategyFallback:
    def test_no_json_block_returns_fallback(self):
        assert parse_strategy("pure markdown, no JSON here") == {
            "strategy": "auto_pick",
            "best_angle_idx": 0,
            "divergence_score": "medium",
        }

    def test_malformed_json_returns_fallback(self):
        md = "```json\n{not valid json at all}\n```"
        result = parse_strategy(md)
        assert result == {
            "strategy": "auto_pick",
            "best_angle_idx": 0,
            "divergence_score": "medium",
        }

    def test_unknown_strategy_value_returns_fallback(self):
        md = (
            '```json\n{"strategy": "magic", "best_angle_idx": 0, '
            '"divergence_score": "medium"}\n```'
        )
        assert parse_strategy(md)["strategy"] == "auto_pick"

    def test_missing_best_angle_idx_returns_fallback(self):
        md = (
            '```json\n{"strategy": "auto_pick", '
            '"divergence_score": "high"}\n```'
        )
        result = parse_strategy(md)
        # Fallback kicks in wholesale when a required key is missing
        assert result["divergence_score"] == "medium"

    def test_missing_divergence_score_returns_fallback(self):
        md = '```json\n{"strategy": "auto_pick", "best_angle_idx": 0}\n```'
        assert parse_strategy(md)["divergence_score"] == "medium"

    def test_negative_best_angle_idx_returns_fallback(self):
        md = (
            '```json\n{"strategy": "auto_pick", "best_angle_idx": -1, '
            '"divergence_score": "low"}\n```'
        )
        assert parse_strategy(md)["best_angle_idx"] == 0

    def test_unknown_divergence_value_returns_fallback(self):
        md = (
            '```json\n{"strategy": "auto_pick", "best_angle_idx": 1, '
            '"divergence_score": "extreme"}\n```'
        )
        assert parse_strategy(md)["divergence_score"] == "medium"

    def test_rationale_optional_absent(self):
        md = (
            '```json\n{"strategy": "auto_pick", "best_angle_idx": 0, '
            '"divergence_score": "medium"}\n```'
        )
        result = parse_strategy(md)
        assert "rationale" not in result


# ── run_ideation ───────────────────────────────────────────────────────────────


class TestRunIdeationNominal:
    def test_passes_cwd_when_project_path_exists(self, db_path, session):
        captured: dict = {}

        def _fake(**kwargs):
            captured.update(kwargs)
            return _NOMINAL_ANGLES_MD

        with patch("squad.ideation.run_agent", side_effect=_fake):
            run_ideation(session.id, db_path=db_path)

        assert captured["agent_name"] == "ideation"
        assert captured["cwd"] == session.project_path

    def test_persists_angles(self, db_path, session):
        with patch("squad.ideation.run_agent", return_value=_NOMINAL_ANGLES_MD):
            result = run_ideation(session.id, db_path=db_path)

        assert isinstance(result, IdeationResult)
        persisted = list_ideation_angles(db_path, session.id)
        assert [a.idx for a in persisted] == [0, 1, 2]
        # Returned angles reflect the persisted set
        assert len(result.angles) == 3

    def test_returns_parsed_strategy(self, db_path, session):
        with patch("squad.ideation.run_agent", return_value=_NOMINAL_ANGLES_MD):
            result = run_ideation(session.id, db_path=db_path)
        assert result.strategy["strategy"] == "auto_pick"
        assert result.strategy["best_angle_idx"] == 0
        assert result.strategy["divergence_score"] == "high"

    def test_content_is_raw_agent_output(self, db_path, session):
        with patch("squad.ideation.run_agent", return_value=_NOMINAL_ANGLES_MD):
            result = run_ideation(session.id, db_path=db_path)
        assert result.content == _NOMINAL_ANGLES_MD


class TestRunIdeationFallback:
    def test_empty_output_produces_synthetic_angle(self, db_path, session):
        with patch("squad.ideation.run_agent", return_value=""):
            result = run_ideation(session.id, db_path=db_path)
        assert len(result.angles) == 1
        assert result.angles[0].idx == 0
        assert result.strategy["strategy"] == "auto_pick"

    def test_unparseable_output_produces_synthetic_angle(self, db_path, session):
        with patch(
            "squad.ideation.run_agent",
            return_value="# Random\nno angle header here at all",
        ):
            result = run_ideation(session.id, db_path=db_path)
        assert len(result.angles) == 1
        assert result.angles[0].idx == 0
        persisted = list_ideation_angles(db_path, session.id)
        assert len(persisted) == 1

    def test_agent_error_does_not_crash(self, db_path, session):
        def _boom(**kwargs):
            raise RuntimeError("agent exploded")

        with patch("squad.ideation.run_agent", side_effect=_boom):
            result = run_ideation(session.id, db_path=db_path)
        # Pipeline stays non-critical — a synthetic angle is returned
        assert len(result.angles) == 1
        assert result.angles[0].session_id == session.id

    def test_fallback_angle_seeds_from_session_idea(self, db_path, session):
        with patch("squad.ideation.run_agent", return_value=""):
            result = run_ideation(session.id, db_path=db_path)
        assert session.idea in result.angles[0].value_prop

    def test_missing_project_path_falls_back_to_cwd_none(self, db_path, tmp_path):
        """A session whose project_path doesn't exist must still run."""
        s = create_session(
            title="Ghost project",
            project_path=str(tmp_path / "does-not-exist"),
            workspace_path=str(tmp_path / "ws"),
            idea="idea",
            db_path=db_path,
        )
        captured: dict = {}

        def _fake(**kwargs):
            captured.update(kwargs)
            return _NOMINAL_ANGLES_MD

        with patch("squad.ideation.run_agent", side_effect=_fake):
            run_ideation(s.id, db_path=db_path)

        assert captured["cwd"] is None


class TestRunIdeationSessionLookup:
    def test_unknown_session_raises(self, db_path):
        with pytest.raises(ValueError, match="Session not found"):
            run_ideation("ghost-session", db_path=db_path)


class TestRunIdeationBestAngleClamping:
    def test_best_angle_idx_is_clamped_when_out_of_range(self, db_path, session):
        md = (
            "## Angle 0 — A\n- Segment: X\n\n"
            "## Angle 1 — B\n- Segment: Y\n\n"
            '```json\n{"strategy": "auto_pick", "best_angle_idx": 99, '
            '"divergence_score": "low"}\n```'
        )
        with patch("squad.ideation.run_agent", return_value=md):
            result = run_ideation(session.id, db_path=db_path)
        assert result.strategy["best_angle_idx"] == 0


class TestIdeationResult:
    def test_fields_carry_typed_angles(self):
        angles = [
            IdeationAngle(
                session_id="s",
                idx=0,
                title="t",
                segment="seg",
                value_prop="vp",
                approach="ap",
                divergence_note="div",
            )
        ]
        result = IdeationResult(content="# md", angles=angles, strategy={"k": "v"})
        assert result.content == "# md"
        assert result.angles == angles
        assert result.strategy == {"k": "v"}
