"""Score session input richness as ``"sparse"`` or ``"rich"``.

Used by ``squad.research.build_research_prompt`` to flip the benchmark
into "cover the gaps" mode when the user already provided substantial
context (long idea, CLAUDE.md, deepsearch attachment) — saves a wasted
generic research pass.

Three signals feed the score:

* ``session.idea`` length — long text means the user already framed
  the problem in detail.
* ``CLAUDE.md`` size at the project root — a documented codebase makes
  the architect / ux phases much richer.
* Inline-text attachments (``.md``, ``.txt``, ``.csv``) dropped in the
  Slack thread — typically a deepsearch or a brief.

Binary or non-decodable files are silently ignored: the goal is to
detect signal, not parse every format. PDF attachments are intentionally
excluded — no PDF parsing is introduced here.

The scoring can be recomputed at any time and the latest value is
persisted via :func:`squad.db.update_input_richness`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from squad.attachment_service import INLINE_TEXT_EXTENSIONS, list_attachments
from squad.db import get_session

logger = logging.getLogger(__name__)


# ── thresholds & weights ──────────────────────────────────────────────────────
# Declared up-front so they are easy to tune from a single place. The
# rule below is intentionally readable as data: change a constant, not a
# control-flow branch.

IDEA_LONG_CHARS: int = 300
IDEA_VERY_LONG_CHARS: int = 500
CLAUDE_MD_RICH_CHARS: int = 1000
TEXT_ATTACHMENT_RICH_CHARS: int = 3000

IDEA_LONG_POINTS: int = 1
CLAUDE_MD_POINTS: int = 1
TEXT_ATTACHMENT_POINTS: int = 2

RICH_SCORE_THRESHOLD: int = 2


RichnessLabel = Literal["sparse", "rich"]


# ── signal extractors ─────────────────────────────────────────────────────────


def _idea_chars(idea: str | None) -> int:
    return len(idea) if idea else 0


def _claude_md_chars(project_path: str | None) -> int:
    """Return the size of ``project_path/CLAUDE.md`` in chars, or 0.

    A missing file, missing directory or undecodable bytes all yield 0 —
    the scorer never raises on environment issues.
    """
    if not project_path:
        return 0
    candidate = Path(project_path) / "CLAUDE.md"
    if not candidate.is_file():
        return 0
    try:
        return len(candidate.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return 0


def _largest_text_attachment_chars(
    session_id: str,
    db_path: Path | None,
) -> int:
    """Return the char length of the largest inline-text attachment.

    Iterates ``list_attachments`` (which mirrors what's currently on
    disk under ``{workspace}/attachments/``), keeps only the
    ``INLINE_TEXT_EXTENSIONS`` set (``md``, ``txt``, ``csv``) — the same
    set the context builder treats as inlinable — and returns the largest
    decodable size. Binary / undecodable files are skipped silently.
    No PDF parsing is performed.
    """
    largest = 0
    for meta in list_attachments(session_id, db_path=db_path):
        if meta.extension not in INLINE_TEXT_EXTENSIONS:
            continue
        try:
            chars = len(Path(meta.path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if chars > largest:
            largest = chars
    return largest


# ── public API ────────────────────────────────────────────────────────────────


def score_input_richness(
    session_id: str,
    db_path: Path | None = None,
) -> RichnessLabel:
    """Classify a session's input as ``"sparse"`` or ``"rich"``.

    The score is computed from three signals: idea length, project
    ``CLAUDE.md`` size, and the largest inline-text attachment.

    The session is ``rich`` when the score reaches ``RICH_SCORE_THRESHOLD``
    *and* at least one of the two "long-form" signals is present (a long
    text attachment, or an idea above ``IDEA_VERY_LONG_CHARS``). Without a
    long-form signal the pipeline falls back to ``sparse`` even at
    score ≥ 2 — two short signals are not enough to skip the human review.
    """
    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise ValueError(f"Session not found: {session_id!r}")

    idea_chars = _idea_chars(session.idea)
    claude_chars = _claude_md_chars(session.project_path)
    attachment_chars = _largest_text_attachment_chars(session_id, db_path=db_path)

    score = 0
    if idea_chars > IDEA_LONG_CHARS:
        score += IDEA_LONG_POINTS
    if claude_chars > CLAUDE_MD_RICH_CHARS:
        score += CLAUDE_MD_POINTS
    if attachment_chars > TEXT_ATTACHMENT_RICH_CHARS:
        score += TEXT_ATTACHMENT_POINTS

    has_long_attachment = attachment_chars > TEXT_ATTACHMENT_RICH_CHARS
    has_long_idea = idea_chars > IDEA_VERY_LONG_CHARS

    label: RichnessLabel = (
        "rich"
        if score >= RICH_SCORE_THRESHOLD and (has_long_attachment or has_long_idea)
        else "sparse"
    )
    logger.debug(
        "Input richness for session %s: idea=%d, CLAUDE.md=%d, attachment=%d, "
        "score=%d → %s",
        session_id,
        idea_chars,
        claude_chars,
        attachment_chars,
        score,
        label,
    )
    return label
