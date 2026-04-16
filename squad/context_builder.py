"""Assembles cumulative prompt context from session state for phase injection.

The context passed to each agent includes: the project idea, the project context
(CLAUDE.md or a minimal stub), answered Q&A from the interviewing phase, and the
text outputs from all phases that have completed before the current one.

Research/benchmark output is capped deterministically before injection so that
the total context stays within the ~15 000-token target.
"""

import logging
from pathlib import Path

from sqlite_utils import Database

from squad.config import get_global_db_path
from squad.constants import PHASE_BENCHMARK, PHASE_LABELS, PHASES
from squad.db import get_session, list_phase_outputs
from squad.workspace import get_context

logger = logging.getLogger(__name__)

# Soft ceiling: ~15 000 tokens at 4 chars / token
_TARGET_CHARS = 60_000
# Budget reserved for research/benchmark text within the cumulative context
_RESEARCH_MAX_CHARS = 16_000


# ── research summariser ────────────────────────────────────────────────────────


def summarize_research(research_text: str, max_chars: int = _RESEARCH_MAX_CHARS) -> str:
    """Deterministically truncate a benchmark text to fit within max_chars.

    Tries to cut at a paragraph boundary to avoid mid-sentence breaks.
    Appends a truncation marker so downstream agents know the content was capped.

    Args:
        research_text: Raw benchmark / research output.
        max_chars: Maximum character budget (default ~4 000 tokens).

    Returns:
        Original text if it fits, otherwise a truncated version with a marker.
    """
    if len(research_text) <= max_chars:
        return research_text

    cutoff = max_chars - 120
    # Prefer a paragraph boundary in the second half of the allowed budget
    boundary = research_text.rfind("\n\n", cutoff // 2, cutoff)
    if boundary > 0:
        cutoff = boundary

    return (
        research_text[:cutoff]
        + "\n\n*[Résumé tronqué — contenu complet disponible dans le workspace.]*"
    )


# ── private DB helper ──────────────────────────────────────────────────────────


def _get_answered_questions(session_id: str, db_path: Path | None) -> list[dict]:
    """Return answered Q&A rows for a session, ordered by creation date."""
    path = db_path or get_global_db_path()
    db = Database(path)
    if "questions" not in db.table_names():
        return []
    return list(
        db["questions"].rows_where(
            "session_id = ? AND answer IS NOT NULL",
            [session_id],
            order_by="created_at ASC",
        )
    )


# ── public API ─────────────────────────────────────────────────────────────────


def build_cumulative_context(
    session_id: str,
    current_phase: str,
    db_path: Path | None = None,
) -> str:
    """Assemble the cumulative context string to inject into a phase prompt.

    Sections included (in order):
    1. Project idea (from session record).
    2. Project context (CLAUDE.md if present, else a minimal stub).
    3. Answered Q&A from the interviewing step (if any).
    4. Text outputs from all phases that completed before current_phase.
       Benchmark output is capped via summarize_research before inclusion.

    The total character length is logged as a warning when it exceeds
    _TARGET_CHARS (~15 000 tokens). Content is never hard-truncated at the
    context level — only benchmark text is bounded individually.

    Args:
        session_id: ID of the active session.
        current_phase: Phase identifier about to run (must be in constants.PHASES).
        db_path: Optional path to the SQLite DB; uses the global path if None.

    Returns:
        Assembled context as a markdown string, sections separated by "---".

    Raises:
        ValueError: If the session is not found.
    """
    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise ValueError(f"Session not found: {session_id!r}")

    parts: list[str] = []

    # 1. Project idea
    parts.append(f"## Idée du projet\n\n{session.idea}")

    # 2. Project context (CLAUDE.md or minimal stub)
    project_context = get_context(session.project_path)
    parts.append(f"## Contexte projet\n\n{project_context}")

    # 3. Answered Q&A
    answered = _get_answered_questions(session_id, db_path)
    if answered:
        qa_lines = [
            f"**Q ({row['agent']}/{row['phase']}):** {row['question']}\n"
            f"**R:** {row['answer']}"
            for row in answered
        ]
        parts.append("## Q&A\n\n" + "\n\n".join(qa_lines))

    # 4. Phase outputs from phases preceding current_phase
    if current_phase in PHASES:
        preceding_phases = PHASES[: PHASES.index(current_phase)]
    else:
        preceding_phases = []

    if preceding_phases:
        all_outputs = list_phase_outputs(session_id, db_path=db_path)
        by_phase: dict[str, list] = {}
        for po in all_outputs:
            if po.phase in preceding_phases:
                by_phase.setdefault(po.phase, []).append(po)

        for phase_id in preceding_phases:
            phase_outputs = by_phase.get(phase_id, [])
            if not phase_outputs:
                continue
            label = PHASE_LABELS.get(phase_id, phase_id)
            agent_sections: list[str] = []
            for po in phase_outputs:
                content = po.output
                if phase_id == PHASE_BENCHMARK:
                    content = summarize_research(content)
                agent_sections.append(f"### {po.agent}\n\n{content}")
            parts.append(f"## Phase : {label}\n\n" + "\n\n".join(agent_sections))

    context = "\n\n---\n\n".join(parts)

    if len(context) > _TARGET_CHARS:
        logger.warning(
            "Cumulative context for session %r / phase %r exceeds target (%d > %d chars)",
            session_id,
            current_phase,
            len(context),
            _TARGET_CHARS,
        )

    return context
