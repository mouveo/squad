"""Tests for squad/ideation.py — parse_angles, parse_strategy, run_ideation."""

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from squad.db import (
    create_session,
    ensure_schema,
    list_ideation_angles,
)
from squad.ideation import (
    _RETRY_FORMAT_INSTRUCTION,
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


# ── parse_angles — non-regression on already-tolerated variants ───────────────


class TestParseAnglesToleratedVariants:
    """Freeze the variants the parser already accepts today.

    The parser intentionally tolerates ``##`` / ``###`` headers, bullet
    (``-``/``*``) and bold field prefixes, and mixed FR/EN labels. These
    tests pin that contract so a future refactor cannot silently shrink
    the set of tolerated outputs.
    """

    def test_star_bullets_accepted(self):
        md = (
            "## Angle 0 — Star bullets\n"
            "* Segment: ops leads\n"
            "* Value prop: save hours\n"
            "* Approche: automation\n"
            "* Note de divergence: segment\n"
        )
        angles = parse_angles(md, "sess-1")
        assert len(angles) == 1
        assert angles[0].segment == "ops leads"
        assert angles[0].value_prop == "save hours"
        assert angles[0].approach == "automation"
        assert angles[0].divergence_note == "segment"

    def test_english_label_variants_accepted(self):
        md = (
            "## Angle 0 — English labels\n"
            "- Segment: s\n"
            "- Value proposition: vp en\n"
            "- Approach: approach en\n"
            "- Divergence note: div en\n"
        )
        angles = parse_angles(md, "sess-1")
        a = angles[0]
        assert a.value_prop == "vp en"
        assert a.approach == "approach en"
        assert a.divergence_note == "div en"

    def test_french_alternate_labels_accepted(self):
        md = (
            "## Angle 0 — FR alt labels\n"
            "- Segment: seg fr\n"
            "- Proposition de valeur: vp fr\n"
            "- Approche technique: app tech\n"
            "- Divergence: div fr\n"
        )
        angles = parse_angles(md, "sess-1")
        a = angles[0]
        assert a.value_prop == "vp fr"
        assert a.approach == "app tech"
        assert a.divergence_note == "div fr"

    def test_h3_headers_alongside_h2_accepted(self):
        md = (
            "### Angle 0 — H3 first\n"
            "- Segment: a\n\n"
            "## Angle 1 — H2 second\n"
            "- Segment: b\n"
        )
        angles = parse_angles(md, "sess-1")
        assert [a.title for a in angles] == ["H3 first", "H2 second"]

    def test_en_dash_separator_accepted(self):
        md = "## Angle 0 – with en dash\n- Segment: s\n"
        angles = parse_angles(md, "sess-1")
        assert angles[0].title == "with en dash"

    def test_hyphen_separator_accepted(self):
        md = "## Angle 0 - with hyphen\n- Segment: s\n"
        angles = parse_angles(md, "sess-1")
        assert angles[0].title == "with hyphen"

    def test_colon_separator_accepted(self):
        md = "## Angle 0: with colon\n- Segment: s\n"
        angles = parse_angles(md, "sess-1")
        assert angles[0].title == "with colon"

    def test_bullet_label_value_with_en_dash_separator(self):
        md = "## Angle 0 — sep\n- Segment – value with en-dash sep\n"
        angles = parse_angles(md, "sess-1")
        assert angles[0].segment == "value with en-dash sep"


# ── run_ideation — retry-once + WARNING fallback log ──────────────────────────


_UNPARSEABLE_OUTPUT = "# Random output\nno angle header here at all, just prose\n"


class TestRunIdeationRetryOnce:
    def test_retries_once_when_no_angles_on_first_call(self, db_path, session):
        """First call returns unparseable output → retry must fire exactly once."""
        calls: list[dict] = []

        def _fake(**kwargs):
            calls.append(kwargs)
            # First call: unparseable; second call: nominal angles.
            if len(calls) == 1:
                return _UNPARSEABLE_OUTPUT
            return _NOMINAL_ANGLES_MD

        with patch("squad.ideation.run_agent", side_effect=_fake):
            result = run_ideation(session.id, db_path=db_path)

        assert len(calls) == 2, "run_agent must be called exactly twice"
        assert calls[0].get("phase_instruction") in (None, "")
        assert calls[1].get("phase_instruction") == _RETRY_FORMAT_INSTRUCTION
        # Second call yielded parseable angles → no fallback.
        assert len(result.angles) == 3

    def test_retry_phase_instruction_is_explicit(self, db_path, session):
        """The retry phase_instruction must reference the canonical example."""
        calls: list[dict] = []

        def _fake(**kwargs):
            calls.append(kwargs)
            return _UNPARSEABLE_OUTPUT if len(calls) == 1 else _NOMINAL_ANGLES_MD

        with patch("squad.ideation.run_agent", side_effect=_fake):
            run_ideation(session.id, db_path=db_path)

        instruction = calls[1].get("phase_instruction") or ""
        # Narrow checks: the instruction names the example, the angle
        # header, and the JSON contract keys.
        assert "Exemple d'output" in instruction
        assert "## Angle" in instruction
        assert "strategy" in instruction
        assert "best_angle_idx" in instruction
        assert "divergence_score" in instruction

    def test_no_retry_when_first_call_already_yields_angles(self, db_path, session):
        """Happy path: a single run_agent call, no retry."""
        calls: list[dict] = []

        def _fake(**kwargs):
            calls.append(kwargs)
            return _NOMINAL_ANGLES_MD

        with patch("squad.ideation.run_agent", side_effect=_fake):
            run_ideation(session.id, db_path=db_path)

        assert len(calls) == 1
        assert calls[0].get("phase_instruction") in (None, "")

    def test_retry_runs_even_when_first_call_raises(self, db_path, session):
        """A first-call exception must still trigger the single retry."""
        calls: list[dict] = []

        def _fake(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("first call exploded")
            return _NOMINAL_ANGLES_MD

        with patch("squad.ideation.run_agent", side_effect=_fake):
            result = run_ideation(session.id, db_path=db_path)

        assert len(calls) == 2
        assert calls[1].get("phase_instruction") == _RETRY_FORMAT_INSTRUCTION
        assert len(result.angles) == 3


class TestRunIdeationFallbackLog:
    def test_warning_logged_with_session_id_and_char_count(
        self, db_path, session, caplog
    ):
        """After two unparseable runs, the fallback log must be WARNING with size."""

        def _fake(**kwargs):
            return _UNPARSEABLE_OUTPUT

        with (
            caplog.at_level(logging.WARNING, logger="squad.ideation"),
            patch("squad.ideation.run_agent", side_effect=_fake),
        ):
            run_ideation(session.id, db_path=db_path)

        fallback_logs = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "retry produced no parseable angles" in r.getMessage()
        ]
        assert len(fallback_logs) == 1
        msg = fallback_logs[0].getMessage()
        assert session.id in msg
        # The exact character count of the (unparseable) retry output
        # must appear in the message so operators can tell "empty" from
        # "verbose but off-format".
        assert f"content={len(_UNPARSEABLE_OUTPUT)} chars" in msg

    def test_fallback_still_persists_synthetic_angle(self, db_path, session):
        def _fake(**kwargs):
            return _UNPARSEABLE_OUTPUT

        with patch("squad.ideation.run_agent", side_effect=_fake):
            result = run_ideation(session.id, db_path=db_path)

        assert len(result.angles) == 1
        assert result.angles[0].session_id == session.id
        persisted = list_ideation_angles(db_path, session.id)
        assert len(persisted) == 1
