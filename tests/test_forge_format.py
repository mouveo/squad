"""Tests for squad/forge_format.py — extraction, validation, split."""

import pytest

from squad.forge_format import (
    MAX_LOTS,
    MIN_LOTS,
    ForgeFormatError,
    extract_header,
    extract_lots,
    split_plan,
    validate_or_split,
    validate_plan,
)


def _lot(n: int, with_files: bool = True) -> str:
    body = f"## LOT {n} — Title {n}\n\nBody for lot {n}.\n\n**Success criteria**:\n- does X\n"
    if with_files:
        body += f"\n**Files**: `file_{n}.py`\n"
    return body


def _plan(n_lots: int, with_files: bool = True) -> str:
    header = "# Project — Plan 1/1: Test"
    lots = "\n\n".join(_lot(i, with_files=with_files) for i in range(1, n_lots + 1))
    return header + "\n\n" + lots


# ── extract_lots / extract_header ──────────────────────────────────────────────


class TestExtractLots:
    def test_basic(self):
        plan = _plan(3)
        lots = extract_lots(plan)
        assert [lot.number for lot in lots] == [1, 2, 3]
        assert lots[0].title == "Title 1"
        assert "Files" in lots[0].body

    def test_no_lots(self):
        assert extract_lots("# Only a header") == []

    def test_sub_headings_stay_in_body(self):
        plan = (
            "# X\n\n## LOT 1 — a\n\n### subhead\n\nbody\n\n**Files**: x\n\n"
            "## LOT 2 — b\n\nbody\n\n**Files**: y"
        )
        lots = extract_lots(plan)
        assert len(lots) == 2
        assert "subhead" in lots[0].body


class TestExtractHeader:
    def test_finds_first_h1(self):
        assert extract_header("# Hello\n\n## LOT 1 — a") == "# Hello"

    def test_none_when_missing(self):
        assert extract_header("## LOT 1 — a") is None


# ── validate_plan ──────────────────────────────────────────────────────────────


class TestValidatePlan:
    def test_valid_plan(self):
        result = validate_plan(_plan(5))
        assert result.valid
        assert result.errors == []
        assert len(result.lots) == 5

    def test_missing_header(self):
        plan = _plan(5).split("\n", 2)[2]  # drop the header line
        result = validate_plan(plan)
        assert not result.valid
        assert any("header" in e.lower() for e in result.errors)

    def test_no_lots(self):
        result = validate_plan("# Header only\n\nSome prose")
        assert not result.valid
        assert any("LOT" in e for e in result.errors)

    def test_below_minimum(self):
        result = validate_plan(_plan(3))
        assert not result.valid
        assert any("minimum" in e for e in result.errors)

    def test_above_maximum(self):
        result = validate_plan(_plan(MAX_LOTS + 1))
        assert not result.valid
        assert any("maximum" in e for e in result.errors)

    def test_non_sequential(self):
        plan = (
            "# X\n\n"
            + _lot(1)
            + "\n\n"
            + _lot(3)
            + "\n\n"  # skips 2
            + _lot(4)
            + "\n\n"
            + _lot(5)
            + "\n\n"
            + _lot(6)
        )
        result = validate_plan(plan)
        assert not result.valid
        assert any("sequential" in e for e in result.errors)

    def test_missing_files_line(self):
        # LOT 3 has no Files line
        parts = [_lot(i, with_files=(i != 3)) for i in range(1, 6)]
        plan = "# X\n\n" + "\n\n".join(parts)
        result = validate_plan(plan)
        assert not result.valid
        assert any("Files" in e for e in result.errors)

    def test_duplicate_lot_numbers(self):
        plan = (
            "# X\n\n"
            + _lot(1)
            + "\n\n"
            + _lot(1)
            + "\n\n"  # duplicate
            + _lot(2)
            + "\n\n"
            + _lot(3)
            + "\n\n"
            + _lot(4)
            + "\n\n"
            + _lot(5)
        )
        result = validate_plan(plan)
        assert not result.valid
        # At least one error about numbering or duplicates
        assert any("Duplicate" in e or "sequential" in e for e in result.errors)


# ── split_plan ─────────────────────────────────────────────────────────────────


class TestSplitPlan:
    def test_single_chunk_returns_one_part(self):
        parts = split_plan(_plan(10))
        assert len(parts) == 1
        assert "Plan 1/1" in parts[0]

    def test_splits_into_multiple_parts(self):
        plan = _plan(20)
        parts = split_plan(plan, max_lots=10)
        assert len(parts) == 2
        assert "Plan 1/2" in parts[0]
        assert "Plan 2/2" in parts[1]

    def test_each_part_renumbers_from_one(self):
        plan = _plan(20)
        parts = split_plan(plan, max_lots=10)
        for part in parts:
            lots = extract_lots(part)
            assert [lot.number for lot in lots] == list(range(1, len(lots) + 1))

    def test_refuses_zero_max_lots(self):
        with pytest.raises(ValueError):
            split_plan(_plan(5), max_lots=0)

    def test_refuses_plan_with_no_lots(self):
        with pytest.raises(ForgeFormatError):
            split_plan("# header only")

    def test_preserves_preamble_in_first_part_only(self):
        plan = "# X\n\n> A description\n> Prérequis : aucun.\n\n---\n\n" + "\n\n".join(
            _lot(i) for i in range(1, 21)
        )
        parts = split_plan(plan, max_lots=10)
        assert "A description" in parts[0]
        assert "A description" not in parts[1]


# ── validate_or_split ──────────────────────────────────────────────────────────


class TestValidateOrSplit:
    def test_valid_plan_passes(self):
        parts = validate_or_split(_plan(10))
        assert len(parts) == 1

    def test_too_few_lots_raises(self):
        with pytest.raises(ForgeFormatError, match="minimum"):
            validate_or_split(_plan(MIN_LOTS - 1))

    def test_too_many_splits(self):
        parts = validate_or_split(_plan(20))
        assert len(parts) >= 2
        for part in parts:
            assert validate_plan(part).valid

    def test_invalid_structure_raises(self):
        with pytest.raises(ForgeFormatError):
            validate_or_split("# Header\n\nNo lots here.")

    def test_split_parts_all_pass_validate(self):
        plan = _plan(30)
        parts = validate_or_split(plan)
        for part in parts:
            assert validate_plan(part).valid
