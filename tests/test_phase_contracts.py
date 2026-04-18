"""Tests for squad/phase_contracts.py — JSON extraction and contract parsing."""

import pytest

from squad.phase_contracts import (
    Blocker,
    BlockersContract,
    ContractError,
    Question,
    QuestionsContract,
    SynthesisContract,
    extract_json_block,
    parse_blockers_contract,
    parse_questions_contract,
    parse_synthesis_contract,
)

# ── extract_json_block ─────────────────────────────────────────────────────────


class TestExtractJsonBlock:
    def test_parses_fenced_json_block(self):
        text = 'Some prose\n```json\n{"a": 1}\n```\nmore prose'
        assert extract_json_block(text) == {"a": 1}

    def test_parses_fenced_block_without_json_tag(self):
        text = '```\n{"x": true}\n```'
        assert extract_json_block(text) == {"x": True}

    def test_parses_bare_object_in_text(self):
        text = 'preamble {"k": "v"} trailer'
        assert extract_json_block(text) == {"k": "v"}

    def test_skips_non_json_fence_before_valid_one(self):
        text = '```\nnot json at all\n```\n```json\n{"ok": 1}\n```'
        assert extract_json_block(text) == {"ok": 1}

    def test_raises_on_missing_json(self):
        with pytest.raises(ContractError):
            extract_json_block("no json at all here")

    def test_raises_on_top_level_array(self):
        with pytest.raises(ContractError):
            extract_json_block("```json\n[1, 2, 3]\n```")

    def test_handles_nested_braces(self):
        text = '```json\n{"outer": {"inner": 42}}\n```'
        assert extract_json_block(text) == {"outer": {"inner": 42}}

    def test_parses_uppercase_json_fence(self):
        """Language tag is case-insensitive — ```JSON``` must also work."""
        text = '```JSON\n{"a": 1}\n```'
        assert extract_json_block(text) == {"a": 1}

    def test_parses_mixed_case_json_fence(self):
        text = '```Json\n{"a": 2}\n```'
        assert extract_json_block(text) == {"a": 2}

    def test_parses_raw_json_object_response(self):
        """Some agents return a bare JSON object with no markdown at all."""
        text = '{"decision_summary": "ship", "open_questions": [], "plan_inputs": []}'
        assert extract_json_block(text) == {
            "decision_summary": "ship",
            "open_questions": [],
            "plan_inputs": [],
        }

    def test_parses_raw_json_with_surrounding_whitespace(self):
        text = '   \n  {"k": "v"}  \n\n  '
        assert extract_json_block(text) == {"k": "v"}


# ── parse_questions_contract ───────────────────────────────────────────────────


class TestParseQuestionsContract:
    def test_parses_single_question_with_pause(self):
        text = (
            "# Cadrage\n\nmarkdown here\n"
            '```json\n{"questions": [{"id": "q1", "question": "Scope?"}], '
            '"needs_pause": true}\n```'
        )
        result = parse_questions_contract(text)
        assert isinstance(result, QuestionsContract)
        assert result.needs_pause is True
        assert result.questions == (Question(id="q1", question="Scope?"),)

    def test_empty_questions_list_no_pause(self):
        text = '```json\n{"questions": [], "needs_pause": false}\n```'
        result = parse_questions_contract(text)
        assert result.questions == ()
        assert result.needs_pause is False

    def test_needs_pause_defaults_to_true_with_questions(self):
        text = '```json\n{"questions": [{"id": "q", "question": "?"}]}\n```'
        result = parse_questions_contract(text)
        assert result.needs_pause is True

    def test_raises_on_missing_key(self):
        with pytest.raises(ContractError):
            parse_questions_contract('```json\n{"other": 1}\n```')

    def test_raises_on_malformed_question(self):
        with pytest.raises(ContractError):
            parse_questions_contract('```json\n{"questions": [{"id": "q"}]}\n```')


# ── parse_blockers_contract ────────────────────────────────────────────────────


class TestParseBlockersContract:
    def test_parses_single_blocking_blocker(self):
        text = (
            '```json\n{"blockers": [{"id": "b1", "severity": "blocking", '
            '"constraint": "authn gap"}]}\n```'
        )
        result = parse_blockers_contract(text)
        assert isinstance(result, BlockersContract)
        assert result.blockers == (Blocker(id="b1", severity="blocking", constraint="authn gap"),)
        assert result.has_blocking is True

    def test_empty_blockers_no_blocking(self):
        text = '```json\n{"blockers": []}\n```'
        result = parse_blockers_contract(text)
        assert result.blockers == ()
        assert result.has_blocking is False

    def test_minor_severity_is_not_blocking(self):
        text = (
            '```json\n{"blockers": [{"id": "b", "severity": "minor", '
            '"constraint": "cost overrun"}]}\n```'
        )
        assert parse_blockers_contract(text).has_blocking is False

    def test_raises_on_unknown_severity(self):
        text = (
            '```json\n{"blockers": [{"id": "b", "severity": "catastrophic", '
            '"constraint": "x"}]}\n```'
        )
        with pytest.raises(ContractError):
            parse_blockers_contract(text)

    def test_raises_on_missing_blockers(self):
        with pytest.raises(ContractError):
            parse_blockers_contract('```json\n{"other": []}\n```')


# ── parse_synthesis_contract ───────────────────────────────────────────────────


class TestParseSynthesisContract:
    def test_parses_full_contract(self):
        text = (
            '```json\n{"decision_summary": "ship it", '
            '"open_questions": ["pricing"], '
            '"plan_inputs": ["Plan A", "Plan B"]}\n```'
        )
        result = parse_synthesis_contract(text)
        assert isinstance(result, SynthesisContract)
        assert result.decision_summary == "ship it"
        assert result.open_questions == ("pricing",)
        assert result.plan_inputs == ("Plan A", "Plan B")

    def test_allows_empty_lists(self):
        text = '```json\n{"decision_summary": "s", "open_questions": [], "plan_inputs": []}\n```'
        result = parse_synthesis_contract(text)
        assert result.open_questions == ()
        assert result.plan_inputs == ()

    def test_raises_on_missing_field(self):
        text = '```json\n{"decision_summary": "s", "open_questions": []}\n```'
        with pytest.raises(ContractError):
            parse_synthesis_contract(text)

    def test_raises_on_non_list_plan_inputs(self):
        text = (
            '```json\n{"decision_summary": "s", "open_questions": [], "plan_inputs": "oops"}\n```'
        )
        with pytest.raises(ContractError):
            parse_synthesis_contract(text)

    def test_accepts_uppercase_fence(self):
        text = (
            '```JSON\n{"decision_summary": "s", "open_questions": [], "plan_inputs": []}\n```'
        )
        result = parse_synthesis_contract(text)
        assert result.decision_summary == "s"

    def test_accepts_neutral_fence(self):
        text = (
            '```\n{"decision_summary": "s", "open_questions": [], "plan_inputs": []}\n```'
        )
        result = parse_synthesis_contract(text)
        assert result.decision_summary == "s"

    def test_accepts_raw_json_object_response(self):
        text = '{"decision_summary": "raw", "open_questions": [], "plan_inputs": ["P1"]}'
        result = parse_synthesis_contract(text)
        assert result.decision_summary == "raw"
        assert result.plan_inputs == ("P1",)
