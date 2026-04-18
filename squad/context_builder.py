"""Assembles cumulative prompt context from session state for phase injection.

The context passed to each agent includes:

1. The project idea (from the session record).
2. The project context (``CLAUDE.md`` if present, else a minimal stub).
3. Answered Q&A from the cadrage interview (see ``format_qa``).
4. Structured constraints extracted from the challenge phase outputs, when
   they carry a blockers contract (see ``squad.phase_contracts``).
5. Text outputs from every phase that completed before ``current_phase``.

Phase outputs are filtered by attempt: only the deliverables of the
latest attempt of each phase are re-injected, so a retry after a
challenge does not leak the previous conception output alongside the
new one.

The research/benchmark phase is summarised before re-injection. The
summariser prefers the structured sections of the agreed report layout
(executive summary, competitor table, decision-relevant sections) and
falls back to a deterministic truncation when the report does not have
the expected headings.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from sqlite_utils import Database

from squad.attachment_service import (
    INLINE_TEXT_EXTENSIONS,
    list_attachments,
)
from squad.config import get_config_value, get_global_db_path
from squad.constants import (
    PHASE_BENCHMARK,
    PHASE_CHALLENGE,
    PHASE_CONCEPTION,
    PHASE_LABELS,
    PHASE_SYNTHESE,
    PHASES,
)
from squad.db import get_session, list_ideation_angles, list_phase_outputs
from squad.models import AttachmentMeta, IdeationAngle, PhaseOutput
from squad.phase_contracts import ContractError, parse_blockers_contract
from squad.workspace import get_context

logger = logging.getLogger(__name__)

# Soft ceiling: ~15 000 tokens at 4 chars / token. Kept as a module-level
# fallback for callers that pre-date the config-driven budget; the live
# value is resolved through ``get_config_value("pipeline.context_budget_chars")``
# in ``build_cumulative_context``.
_TARGET_CHARS = 60_000
_CONTEXT_BUDGET_KEY = "pipeline.context_budget_chars"
# Budget reserved for research/benchmark text within the cumulative context
_RESEARCH_MAX_CHARS = 16_000

# Markers used by the budget-enforcement layer.
_COMPRESSED_PHASE_MARKER = (
    "*[Phase résumée pour tenir le budget — contenu complet dans le workspace.]*"
)
_OMITTED_PHASE_MARKER = (
    "*[Historique omis pour tenir le budget — voir workspace.]*"
)
# Cap applied to the first paragraph when summarising a phase section so
# a single verbose block cannot defeat the compression step.
_COMPRESSED_FIRST_PARAGRAPH_CHARS = 800

# Emitted only in the pathological case where the protected payload alone
# (idea, context, Q&A, attachments, angle, constraints, last two phases)
# already exceeds the budget.
FINAL_TRUNCATION_MARKER = "[… contexte tronqué au-delà du budget]"

# Per-attachment inlining budget (text files only) and per-session cap.
_ATTACHMENT_INLINE_PER_FILE = 8_000
_ATTACHMENT_INLINE_TOTAL = 24_000

# Headings (lowercased) that the structured benchmark summariser prioritises.
# Ordered groups: each tuple lists equivalent French/English variants and
# groups them by their business-decision importance.
_BENCHMARK_PRIORITY_HEADINGS: tuple[tuple[str, ...], ...] = (
    ("résumé exécutif", "resume executif", "executive summary"),
    ("concurrents", "competitors", "competitive landscape"),
    ("décisions", "decisions", "décision", "decision"),
    ("analyse par axe", "analysis", "analyses"),
)


# ── research summariser (deterministic fallback) ───────────────────────────────


def summarize_research(research_text: str, max_chars: int = _RESEARCH_MAX_CHARS) -> str:
    """Deterministically truncate a benchmark text to fit within ``max_chars``.

    Tries to cut at a paragraph boundary to avoid mid-sentence breaks.
    Appends a truncation marker so downstream agents know the content was
    capped. Safe for any markdown shape — this is the fallback path used
    when ``summarize_benchmark_structured`` cannot find the expected headings.
    """
    if len(research_text) <= max_chars:
        return research_text

    cutoff = max_chars - 120
    boundary = research_text.rfind("\n\n", cutoff // 2, cutoff)
    if boundary > 0:
        cutoff = boundary

    return (
        research_text[:cutoff]
        + "\n\n*[Résumé tronqué — contenu complet disponible dans le workspace.]*"
    )


# ── structured benchmark summariser ────────────────────────────────────────────


_SECTION_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _split_top_level_sections(md: str) -> list[tuple[str, str]]:
    """Return ``[(heading, body), ...]`` for every top-level ``##`` section.

    ``###`` sub-headings stay inside the body of their parent ``##``.
    """
    matches = list(_SECTION_HEADING_RE.finditer(md))
    if not matches:
        return []
    sections: list[tuple[str, str]] = []
    for i, match in enumerate(matches):
        heading = match.group(1).strip()
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        body = md[body_start:body_end].strip("\n")
        sections.append((heading, body))
    return sections


def _matches_priority(heading: str, priorities: tuple[tuple[str, ...], ...]) -> int | None:
    """Return the index of the priority group the heading matches, or None."""
    low = heading.strip().lower()
    for idx, group in enumerate(priorities):
        for candidate in group:
            if candidate in low:
                return idx
    return None


def summarize_benchmark_structured(text: str, max_chars: int = _RESEARCH_MAX_CHARS) -> str:
    """Summarise a benchmark report by keeping its decision-relevant sections.

    The agreed layout is:

    * ``## Résumé exécutif`` — highest priority (always kept when present).
    * ``## Concurrents`` — kept next.
    * Decision-oriented sections (``## Décisions``, ``## Analyse par axe``) — kept
      while budget remains.

    Sections that do not fit the budget are dropped whole (not cut in the
    middle) so the re-injected context stays readable. When the report has
    no recognisable section (prose-only, wrong headings), the function falls
    back to ``summarize_research`` which applies a deterministic truncation.
    """
    if len(text) <= max_chars:
        return text

    sections = _split_top_level_sections(text)
    if not sections:
        return summarize_research(text, max_chars)

    ranked: list[tuple[int, int, str, str]] = []
    for order_idx, (heading, body) in enumerate(sections):
        priority = _matches_priority(heading, _BENCHMARK_PRIORITY_HEADINGS)
        if priority is None:
            continue
        ranked.append((priority, order_idx, heading, body))
    if not ranked:
        return summarize_research(text, max_chars)

    # Sort by priority group first, then by original order (stable)
    ranked.sort(key=lambda r: (r[0], r[1]))

    budget = max_chars - 120  # reserve room for the truncation marker
    kept: list[str] = []
    used = 0
    dropped_any = False
    for _, _, heading, body in ranked:
        chunk = f"## {heading}\n\n{body}".rstrip()
        if used + len(chunk) + 4 > budget:
            dropped_any = True
            continue
        kept.append(chunk)
        used += len(chunk) + 4  # account for the "\n\n" join

    if not kept:
        return summarize_research(text, max_chars)

    summary = "\n\n".join(kept)
    if dropped_any or len(text) > len(summary):
        summary += (
            "\n\n*[Sections secondaires omises pour tenir le budget — "
            "rapport complet dans research/benchmark-*.md]*"
        )
    return summary


# ── Q&A formatter ──────────────────────────────────────────────────────────────


def format_qa(questions_and_answers: list[dict]) -> str:
    """Return a markdown ``## Q&A`` block, or an empty string when nothing to show.

    Each entry is expected to be a mapping with ``agent``, ``phase``,
    ``question`` and ``answer`` keys (string values). Entries without an
    answer are skipped.
    """
    if not questions_and_answers:
        return ""
    lines: list[str] = []
    for row in questions_and_answers:
        answer = row.get("answer")
        if not answer:
            continue
        agent = row.get("agent", "?")
        phase = row.get("phase", "?")
        question = row.get("question", "")
        lines.append(f"**Q ({agent}/{phase}):** {question}\n**R:** {answer}")
    if not lines:
        return ""
    return "## Q&A\n\n" + "\n\n".join(lines)


# ── attachments formatter ──────────────────────────────────────────────────────


def _format_size(size_bytes: int) -> str:
    """Return a short human-readable size string (KB/MB)."""
    if size_bytes < 1024:
        return f"{size_bytes} o"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} Ko"
    return f"{size_bytes / (1024 * 1024):.1f} Mo"


def _read_text_attachment(meta: AttachmentMeta, max_chars: int) -> str:
    """Best-effort UTF-8 read of an inlinable text attachment, capped to ``max_chars``."""
    try:
        text = Path(meta.path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Could not read attachment %s: %s", meta.path, exc)
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n\n*[Tronqué pour tenir le budget de contexte.]*"


def format_attachments(attachments: list[AttachmentMeta]) -> str:
    """Return the ``## Fichiers joints`` section, or an empty string when none.

    Text files (``md``, ``txt``, ``csv``) are inlined under their own
    ``###`` sub-heading, capped to a per-file budget; the cumulative
    inlined text is also bounded so a single PO drop cannot blow up the
    prompt. Binary files (``pdf``, ``png``, ``jpg``, ``jpeg``) are
    listed by name, size and mime type without inline content.
    """
    if not attachments:
        return ""

    listing_lines: list[str] = []
    for meta in attachments:
        kind = meta.mime_type or f".{meta.extension}" if meta.extension else "fichier"
        listing_lines.append(
            f"- `{meta.filename}` — {_format_size(meta.size_bytes)} ({kind})"
        )

    inline_blocks: list[str] = []
    used = 0
    for meta in attachments:
        if meta.extension not in INLINE_TEXT_EXTENSIONS:
            continue
        remaining = _ATTACHMENT_INLINE_TOTAL - used
        if remaining <= 0:
            break
        budget = min(_ATTACHMENT_INLINE_PER_FILE, remaining)
        body = _read_text_attachment(meta, budget)
        if not body:
            continue
        block = f"### {meta.filename}\n\n```\n{body}\n```"
        inline_blocks.append(block)
        used += len(body)

    parts = ["## Fichiers joints", "", *listing_lines]
    if inline_blocks:
        parts.append("")
        parts.extend(inline_blocks)
    return "\n".join(parts)


# ── ideation angle injection ───────────────────────────────────────────────────


# Phases that can see a single selected angle re-injected in their context.
# Benchmark additionally supports ``## Angles à benchmarker`` when the
# reviewer picked "benchmark all" — downstream phases stay mono-angle
# so conception/challenge/synthese aren't contaminated by competing
# directions.
_ANGLE_AWARE_PHASES: frozenset[str] = frozenset(
    {PHASE_BENCHMARK, PHASE_CONCEPTION, PHASE_CHALLENGE, PHASE_SYNTHESE}
)


def _format_angle_entry(angle: IdeationAngle) -> str:
    """Render one ideation angle as a markdown sub-block for context injection."""
    return (
        f"### Angle {angle.idx} — {angle.title}\n"
        f"- Segment : {angle.segment}\n"
        f"- Proposition de valeur : {angle.value_prop}\n"
        f"- Approche : {angle.approach}\n"
        f"- Divergence : {angle.divergence_note}"
    )


def format_selected_angle(angles: list[IdeationAngle], idx: int) -> str:
    """Return a ``## Angle choisi`` block for the ``idx``-th angle, or empty.

    An out-of-range ``idx`` or an empty angle list yields an empty string
    so the caller can append unconditionally. Downstream prompts treat the
    absence of this section as "angle fallback to the ideation markdown".
    """
    match = next((a for a in angles if a.idx == idx), None)
    if match is None:
        return ""
    return "## Angle choisi\n\n" + _format_angle_entry(match)


def format_all_angles(angles: list[IdeationAngle]) -> str:
    """Return a ``## Angles à benchmarker`` block listing every angle, or empty."""
    if not angles:
        return ""
    blocks = [_format_angle_entry(a) for a in angles]
    return "## Angles à benchmarker\n\n" + "\n\n".join(blocks)


def _build_angle_section(
    session,
    current_phase: str,
    angles: list[IdeationAngle],
) -> str:
    """Resolve which angle block (if any) should be injected for ``current_phase``.

    Rules (keep in sync with the LOT 7 contract):

    * benchmark + ``benchmark_all_angles=True`` → ``## Angles à benchmarker``.
    * benchmark/conception/challenge/synthese + ``selected_angle_idx`` set
      → ``## Angle choisi`` (single angle only, even if the session was
      flagged ``benchmark_all_angles``).
    * Everything else → empty string.
    """
    if current_phase not in _ANGLE_AWARE_PHASES or not angles:
        return ""

    benchmark_all = bool(getattr(session, "benchmark_all_angles", False))
    selected_idx = getattr(session, "selected_angle_idx", None)

    if current_phase == PHASE_BENCHMARK and benchmark_all:
        return format_all_angles(angles)

    if selected_idx is not None:
        return format_selected_angle(angles, int(selected_idx))

    return ""


# ── challenge constraints ──────────────────────────────────────────────────────


def extract_challenge_constraints(outputs: list[PhaseOutput]) -> list[str]:
    """Extract structured constraints from challenge-phase outputs.

    Parses each challenge output for a blockers contract (see
    ``squad.phase_contracts``) and returns a flat list of formatted lines.
    Outputs without a parseable contract are silently ignored; the
    free-form markdown is still included elsewhere by the caller.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for po in outputs:
        if po.phase != PHASE_CHALLENGE:
            continue
        try:
            contract = parse_blockers_contract(po.output)
        except ContractError:
            continue
        for blocker in contract.blockers:
            key = (blocker.id, blocker.constraint)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"- [{blocker.severity}] ({po.agent}/{blocker.id}) {blocker.constraint}")
    return lines


# ── attempt filtering ──────────────────────────────────────────────────────────


def _filter_latest_attempt(outputs: list[PhaseOutput]) -> list[PhaseOutput]:
    """Keep only the outputs that belong to the latest attempt of each phase.

    Preserves input order so downstream formatting stays deterministic.
    """
    max_attempt: dict[str, int] = {}
    for po in outputs:
        max_attempt[po.phase] = max(max_attempt.get(po.phase, 0), po.attempt)
    return [po for po in outputs if po.attempt == max_attempt.get(po.phase, 0)]


# ── private DB helper ──────────────────────────────────────────────────────────


def compress_phase_section(section_text: str) -> str:
    """Return a deterministic, LLM-free summary of a ``## Phase : ...`` block.

    The summary keeps the phase header, extracts the first non-empty
    paragraph of the body, and lists the ``##`` / ``###`` headings that
    appear inside the agent deliverables. A closing marker signals that
    the section was compressed so downstream agents know the content was
    intentionally reduced.
    """
    lines = section_text.splitlines()
    if not lines:
        return section_text
    header = lines[0]
    body = "\n".join(lines[1:])

    # First useful paragraph: skip the ones that are only markdown
    # headings, so a body starting with ``### agent\n\nreal prose``
    # returns the prose rather than the agent header. Cap the paragraph
    # so a single verbose block cannot blow the compressed summary.
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    first_para = next(
        (
            p
            for p in paragraphs
            if not all(line.strip().startswith("#") for line in p.splitlines() if line.strip())
        ),
        "",
    )
    if len(first_para) > _COMPRESSED_FIRST_PARAGRAPH_CHARS:
        first_para = (
            first_para[:_COMPRESSED_FIRST_PARAGRAPH_CHARS].rstrip() + "…"
        )

    # Detected sub-headings, excluding the phase header itself.
    titles = [
        match.strip()
        for match in re.findall(r"^#{2,3}\s+[^\n]+$", body, re.MULTILINE)
    ]

    out: list[str] = [header, ""]
    if first_para:
        out.append(first_para)
        out.append("")
    if titles:
        out.append("Titres détectés :")
        for title in titles:
            out.append(f"- {title}")
        out.append("")
    out.append(_COMPRESSED_PHASE_MARKER)
    return "\n".join(out).rstrip() + "\n"


def _enforce_context_budget(
    protected_parts: list[str],
    phase_sections: list[tuple[str, str]],
    budget: int,
    *,
    session_id: str,
    current_phase: str,
) -> str:
    """Join protected parts + phase sections under ``budget`` characters.

    Compression is staged: (1) summarise the oldest phase sections while
    keeping the two most recent phases untouched, logging the char gain
    per phase at INFO level; (2) if still over budget, drop the already
    compressed summaries from oldest to newest, replacing each one with
    an explicit omission marker; (3) if the protected payload alone (the
    parts that are never compressed plus the two newest phases) is still
    over budget, log a WARNING and truncate the final string, appending
    :data:`FINAL_TRUNCATION_MARKER`.
    """
    separator = "\n\n---\n\n"

    def _join(sections: list[tuple[str, str]]) -> str:
        return separator.join(protected_parts + [body for _, body in sections])

    context = _join(phase_sections)
    if len(context) <= budget:
        return context

    # Step 1 — compress oldest phase sections first, protecting the two
    # most recent phases. ``compressible_max`` is the index range [0,
    # len-2) so the last two phase sections stay intact.
    compressible_max = max(0, len(phase_sections) - 2)
    for idx in range(compressible_max):
        phase_id, original = phase_sections[idx]
        compressed = compress_phase_section(original)
        gain = len(original) - len(compressed)
        if gain <= 0:
            continue
        phase_sections[idx] = (phase_id, compressed)
        logger.info(
            "Context compression: phase %r saved %d chars (session=%s, current_phase=%s)",
            phase_id,
            gain,
            session_id,
            current_phase,
        )
        context = _join(phase_sections)
        if len(context) <= budget:
            return context

    # Step 2 — drop the oldest compressed summaries outright, leaving
    # only an explicit omission marker so downstream agents still see the
    # phase happened.
    for idx in range(compressible_max):
        phase_id, _ = phase_sections[idx]
        label = PHASE_LABELS.get(phase_id, phase_id)
        phase_sections[idx] = (
            phase_id,
            f"## Phase : {label}\n\n{_OMITTED_PHASE_MARKER}\n",
        )
        context = _join(phase_sections)
        if len(context) <= budget:
            return context

    # Step 3 — the protected payload itself is too big. Log a warning
    # and apply a visible final truncation so the budget overflow is not
    # silent.
    logger.warning(
        "Cumulative context for session %r / phase %r still exceeds budget "
        "after compression and history omission (%d > %d chars) — applying "
        "final truncation",
        session_id,
        current_phase,
        len(context),
        budget,
    )
    marker = "\n\n" + FINAL_TRUNCATION_MARKER
    cutoff = max(0, budget - len(marker))
    return context[:cutoff].rstrip() + marker


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
    2. Project context (``CLAUDE.md`` if present, else a minimal stub).
    3. Answered Q&A (from the cadrage interview).
    4. Constraints extracted from challenge blockers contracts, when any.
    5. Text outputs from every phase preceding ``current_phase``, filtered
       to the latest attempt of each phase, with the benchmark section
       summarised via ``summarize_benchmark_structured``.

    The total character length is logged as a warning when it exceeds
    ``_TARGET_CHARS`` (~15 000 tokens). Content is never hard-truncated at
    the context level — only benchmark text is bounded individually.
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
    qa_block = format_qa(_get_answered_questions(session_id, db_path))
    if qa_block:
        parts.append(qa_block)

    # 3b. Slack-attached files (LOT 3 — Plan 4)
    attachments_block = format_attachments(list_attachments(session_id, db_path=db_path))
    if attachments_block:
        parts.append(attachments_block)

    # 3c. Ideation angle(s) for the phases that consume them. Benchmark is
    # the only phase that can see ``## Angles à benchmarker`` (multi-angle
    # mode); every downstream phase receives at most a single
    # ``## Angle choisi`` block so conception/challenge/synthese never
    # diverge on competing directions.
    if current_phase in _ANGLE_AWARE_PHASES:
        angles = list_ideation_angles(db_path, session_id)
        angle_block = _build_angle_section(session, current_phase, angles)
        if angle_block:
            parts.append(angle_block)

    # 4 & 5. Preceding phase outputs + challenge constraints
    if current_phase in PHASES:
        preceding_phases = PHASES[: PHASES.index(current_phase)]
    else:
        preceding_phases = []

    all_outputs: list[PhaseOutput] = []
    if preceding_phases:
        all_outputs = list_phase_outputs(session_id, db_path=db_path)
        all_outputs = [po for po in all_outputs if po.phase in preceding_phases]
        all_outputs = _filter_latest_attempt(all_outputs)

    # Challenge constraints (parsed from blockers contracts). These are
    # curated, compact summaries derived from the blockers contract and
    # must never be compressed away: downstream phases rely on them to
    # honour unresolved blockers.
    constraints = extract_challenge_constraints(all_outputs)
    if constraints:
        parts.append("## Contraintes issues du challenge\n\n" + "\n".join(constraints))

    # Group phase outputs for injection
    by_phase: dict[str, list[PhaseOutput]] = {}
    for po in all_outputs:
        by_phase.setdefault(po.phase, []).append(po)

    phase_sections: list[tuple[str, str]] = []
    for phase_id in preceding_phases:
        phase_outputs = by_phase.get(phase_id, [])
        if not phase_outputs:
            continue
        label = PHASE_LABELS.get(phase_id, phase_id)
        agent_sections: list[str] = []
        for po in phase_outputs:
            content = po.output
            if phase_id == PHASE_BENCHMARK:
                content = summarize_benchmark_structured(content)
            agent_sections.append(f"### {po.agent}\n\n{content}")
        section = f"## Phase : {label}\n\n" + "\n\n".join(agent_sections)
        phase_sections.append((phase_id, section))

    budget = int(
        get_config_value(
            _CONTEXT_BUDGET_KEY,
            project_path=session.project_path,
            default=_TARGET_CHARS,
        )
        or _TARGET_CHARS
    )
    return _enforce_context_budget(
        protected_parts=parts,
        phase_sections=phase_sections,
        budget=budget,
        session_id=session_id,
        current_phase=current_phase,
    )
