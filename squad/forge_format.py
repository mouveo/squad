"""Forge plan format — validation, numbering checks, and deterministic split.

A Forge plan is a markdown document containing a series of ``## LOT N —
{title}`` sections. Squad's pipeline generates such plans and must validate
them before persisting. This module owns the format rules and lives in the
repo so no external skill or asset is required to enforce them.

Rules enforced here:

* The document must start with a top-level ``#`` heading.
* Each plan must contain between ``MIN_LOTS`` (5) and ``MAX_LOTS`` (15) lots.
* Lots are numbered starting at 1 and must be sequential with no gaps.
* Each lot body must mention a ``**Success criteria**:`` line and a
  ``**Files**:`` line (no specific shape otherwise — free-form prose is
  allowed).
* When a draft exceeds ``MAX_LOTS``, ``split_plan`` cuts it into several
  plans of up to ``MAX_LOTS`` lots each, re-numbering each part from 1
  and renaming the ``# ...`` header as ``Plan N/M``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Inclusive bounds
MIN_LOTS = 5
MAX_LOTS = 15

# Defensive ceiling — protects split_plan against runaway outputs.
_HARD_MAX_LOTS = 100

_HEADER_RE = re.compile(r"^#\s+.+$", re.MULTILINE)
_LOT_HEADING_RE = re.compile(r"^##\s+LOT\s+(\d+)\s+—\s+(.+?)\s*$", re.MULTILINE)
_FILES_LINE_RE = re.compile(r"^\*\*Files\*\*:\s*.+$", re.MULTILINE)
_SUCCESS_LINE_RE = re.compile(r"^\*\*Success\s+criteria\*\*:", re.MULTILINE)


# ── dataclasses ────────────────────────────────────────────────────────────────


@dataclass
class Lot:
    """A single ``## LOT N — title`` section extracted from a plan."""

    number: int
    title: str
    body: str  # full markdown including the heading line


@dataclass
class ValidationResult:
    """Outcome of validating a plan. ``valid`` is True only when ``errors`` is empty."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    lots: list[Lot] = field(default_factory=list)


class ForgeFormatError(ValueError):
    """Raised when a plan cannot be parsed or validated."""


# ── extraction ─────────────────────────────────────────────────────────────────


def extract_lots(content: str) -> list[Lot]:
    """Return the list of ``## LOT N — title`` sections found in ``content``.

    The returned bodies include the heading line and run up to the next
    top-level ``## `` heading (``### `` sub-headings stay inside the body).
    """
    matches = list(_LOT_HEADING_RE.finditer(content))
    lots: list[Lot] = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
        number = int(match.group(1))
        title = match.group(2).strip()
        body = content[start:end].rstrip() + "\n"
        lots.append(Lot(number=number, title=title, body=body))
    return lots


def extract_header(content: str) -> str | None:
    """Return the first top-level ``# ...`` line (without the newline), or None."""
    match = _HEADER_RE.search(content)
    return match.group(0) if match else None


# ── validation ─────────────────────────────────────────────────────────────────


def _check_numbering(lots: list[Lot]) -> list[str]:
    errors: list[str] = []
    if not lots:
        return errors
    expected = 1
    for lot in lots:
        if lot.number != expected:
            errors.append(
                f"Lot numbering is not sequential: expected LOT {expected}, got LOT {lot.number}"
            )
            break
        expected += 1
    seen: set[int] = set()
    for lot in lots:
        if lot.number in seen:
            errors.append(f"Duplicate lot number: LOT {lot.number}")
        seen.add(lot.number)
    return errors


def _check_bounds(lots: list[Lot]) -> list[str]:
    errors: list[str] = []
    count = len(lots)
    if count < MIN_LOTS:
        errors.append(f"Plan has {count} lot(s); minimum is {MIN_LOTS}")
    if count > MAX_LOTS:
        errors.append(f"Plan has {count} lot(s); maximum is {MAX_LOTS}")
    return errors


def _check_lot_bodies(lots: list[Lot]) -> list[str]:
    errors: list[str] = []
    for lot in lots:
        if not _SUCCESS_LINE_RE.search(lot.body):
            errors.append(f"LOT {lot.number} is missing a '**Success criteria**:' section")
        if not _FILES_LINE_RE.search(lot.body):
            errors.append(f"LOT {lot.number} is missing a '**Files**:' line")
    return errors


def validate_plan(content: str) -> ValidationResult:
    """Return a ``ValidationResult`` describing whether ``content`` is a valid plan."""
    errors: list[str] = []
    if not extract_header(content):
        errors.append("Plan is missing a top-level '# ...' header")

    lots = extract_lots(content)
    if not lots:
        errors.append("Plan contains no '## LOT N — ...' sections")
        return ValidationResult(valid=False, errors=errors, lots=lots)

    errors.extend(_check_bounds(lots))
    errors.extend(_check_numbering(lots))
    errors.extend(_check_lot_bodies(lots))

    return ValidationResult(valid=not errors, errors=errors, lots=lots)


# ── split ──────────────────────────────────────────────────────────────────────


def _rewrite_header(header: str | None, part_index: int, total_parts: int) -> str:
    """Return a header line for split part ``part_index`` of ``total_parts``."""
    base = header or "# Plan"
    base = base.rstrip()
    # Strip trailing "Plan N/M" patterns from the original header
    base = re.sub(r"\s*[—-]\s*Plan\s+\d+/\d+.*$", "", base)
    return f"{base} — Plan {part_index}/{total_parts}"


def _renumber_lot_body(body: str, new_number: int) -> str:
    """Rewrite the first ``## LOT X — ...`` line of ``body`` to use ``new_number``."""
    return _LOT_HEADING_RE.sub(
        lambda m: f"## LOT {new_number} — {m.group(2)}",
        body,
        count=1,
    )


def split_plan(content: str, max_lots: int = MAX_LOTS) -> list[str]:
    """Split ``content`` into one or more plans of at most ``max_lots`` lots.

    The returned plans each carry a rewritten header (``Plan N/M``), a
    fresh 1..K numbering for their lots, and preserve any preamble text
    between the header and the first lot — but only in the first part
    (subsequent parts start directly with the renumbered lots).
    """
    if max_lots < 1:
        raise ValueError("max_lots must be >= 1")
    lots = extract_lots(content)
    if not lots:
        raise ForgeFormatError("Cannot split a plan with no lots")
    if len(lots) > _HARD_MAX_LOTS:
        raise ForgeFormatError(
            f"Plan has {len(lots)} lots; refusing to split beyond {_HARD_MAX_LOTS}"
        )

    header = extract_header(content)
    # Preamble = everything between the header and the first lot
    first_lot_start = _LOT_HEADING_RE.search(content)
    preamble = ""
    if header and first_lot_start:
        header_end = content.find(header) + len(header)
        preamble = content[header_end : first_lot_start.start()].strip("\n")

    chunks: list[list[Lot]] = [lots[i : i + max_lots] for i in range(0, len(lots), max_lots)]
    total = len(chunks)

    if total == 1:
        # Still renumber defensively and rewrite the header as Plan 1/1
        return [_assemble_plan(header, preamble, chunks[0], 1, 1)]

    return [
        _assemble_plan(header, preamble if idx == 0 else "", chunk, idx + 1, total)
        for idx, chunk in enumerate(chunks)
    ]


def _assemble_plan(
    header: str | None,
    preamble: str,
    lots: list[Lot],
    part_index: int,
    total_parts: int,
) -> str:
    new_header = _rewrite_header(header, part_index, total_parts)
    renumbered = "\n".join(
        _renumber_lot_body(lot.body, new_number) for new_number, lot in enumerate(lots, start=1)
    )
    sections = [new_header]
    if preamble:
        sections.append(preamble)
    sections.append(renumbered)
    return "\n\n".join(section.strip("\n") for section in sections) + "\n"


# ── high-level entry ───────────────────────────────────────────────────────────


def validate_or_split(content: str) -> list[str]:
    """Return a list of plans that all pass ``validate_plan``.

    * If ``content`` is already within bounds and valid, returns ``[content]``.
    * If ``content`` has more than ``MAX_LOTS`` lots, splits deterministically.
    * If the split still produces an invalid plan (e.g. too few lots in the
      last part), raises ``ForgeFormatError``.
    * If ``content`` has fewer than ``MIN_LOTS`` lots, raises
      ``ForgeFormatError`` — callers should regenerate rather than ship a
      plan that is too thin.
    """
    lots = extract_lots(content)
    if not lots:
        raise ForgeFormatError("Plan contains no '## LOT N — ...' sections")
    if len(lots) < MIN_LOTS:
        raise ForgeFormatError(f"Plan has {len(lots)} lot(s); minimum is {MIN_LOTS}")

    if len(lots) <= MAX_LOTS:
        result = validate_plan(content)
        if not result.valid:
            raise ForgeFormatError("; ".join(result.errors))
        return [content]

    parts = split_plan(content)
    for idx, part in enumerate(parts, start=1):
        part_result = validate_plan(part)
        if not part_result.valid:
            raise ForgeFormatError(
                f"Split part {idx}/{len(parts)} is invalid: " + "; ".join(part_result.errors)
            )
    return parts
