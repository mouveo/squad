"""Research / benchmark service for the Squad pipeline.

This module owns the benchmark phase end-to-end:

* It declares a deterministic research budget per depth profile
  (``normal`` vs ``deep``) — max axes, max prompt chars, max output
  chars, and executor timeout.
* It prepares 3-5 research axes from the subject type.
* It builds a structured prompt instructing Claude to produce a
  sourced markdown report (executive summary, competitor table, one
  section per axis, sources list).
* It runs the task via ``squad.executor.run_task_text`` with the three
  tools the benchmark actually needs (``Read``, ``WebSearch``,
  ``WebFetch``). All Claude invocations stay centralised in the
  executor.
* It persists the report into ``research/benchmark-{slug}.md`` via
  ``squad.workspace`` and registers an entry under agent ``research``
  in ``phase_outputs`` with the correct attempt number.

Light-depth sessions never reach this service: the pipeline marks the
benchmark phase as skipped at profile time (see
``squad.subject_detector.detect_and_persist``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from squad.constants import PHASE_BENCHMARK
from squad.db import create_phase_output, get_session
from squad.executor import run_task_text
from squad.models import (
    RESEARCH_DEPTH_DEEP,
    RESEARCH_DEPTH_LIGHT,
    RESEARCH_DEPTH_NORMAL,
)
from squad.workspace import write_benchmark

logger = logging.getLogger(__name__)

# Allowed tools for the research task. The executor translates these
# strings directly into Claude CLI ``--allowedTools`` identifiers.
RESEARCH_TOOLS: tuple[str, ...] = ("Read", "WebSearch", "WebFetch")

# Repo-local source of truth for the deep-research skill protocol. The
# user-side install (`scripts/install-skills.sh`) syncs the same file to
# the Claude CLI skills directory; we still load the repo copy here so
# the benchmark stays deterministic and testable when the skill is not
# installed globally.
REPO_SKILL_PATH: Path = (
    Path(__file__).resolve().parent.parent / "skills" / "deep-research" / "SKILL.md"
)


# ── budget ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResearchBudget:
    """Deterministic limits applied to a research run.

    ``max_axes`` caps how many axes are prepared upstream. ``max_prompt_chars``
    caps the total prompt length (context is truncated deterministically).
    ``max_output_chars`` caps the persisted report size. ``timeout_seconds``
    is forwarded to the Claude CLI subprocess call.
    """

    max_axes: int
    max_prompt_chars: int
    max_output_chars: int
    timeout_seconds: int


NORMAL_BUDGET = ResearchBudget(
    max_axes=3,
    max_prompt_chars=4_000,
    max_output_chars=16_000,
    timeout_seconds=600,
)

DEEP_BUDGET = ResearchBudget(
    max_axes=5,
    max_prompt_chars=8_000,
    max_output_chars=32_000,
    timeout_seconds=900,
)


def budget_for_depth(depth: str) -> ResearchBudget:
    """Return the research budget matching a depth profile.

    Raises ``ValueError`` for ``light`` (the pipeline must skip instead
    of running) and for unknown depth values.
    """
    if depth == RESEARCH_DEPTH_NORMAL:
        return NORMAL_BUDGET
    if depth == RESEARCH_DEPTH_DEEP:
        return DEEP_BUDGET
    if depth == RESEARCH_DEPTH_LIGHT:
        raise ValueError("light depth: benchmark must be skipped, not run")
    raise ValueError(f"Unknown research depth: {depth!r}")


# ── axes ───────────────────────────────────────────────────────────────────────


_BASE_AXES: tuple[str, ...] = (
    "Competitive landscape and positioning",
    "User pain points and expectations",
    "Proven patterns and technical references",
)

_DEEP_EXTRA_AXES: tuple[str, ...] = (
    "Pricing and monetization benchmarks",
    "Regulatory and compliance constraints",
)


def prepare_research_axes(subject_type: str | None, depth: str) -> list[str]:
    """Return 3 axes for ``normal`` and up to 5 for ``deep``.

    The subject type is passed through so future revisions can specialise
    axes (e.g. AI-specific benchmarks). The base triplet is already
    representative across subject types — this function stays
    deterministic and side-effect-free.
    """
    budget = budget_for_depth(depth)
    axes: list[str] = list(_BASE_AXES)
    if depth == RESEARCH_DEPTH_DEEP:
        axes.extend(_DEEP_EXTRA_AXES)
    axes = axes[: budget.max_axes]
    return axes


# ── skill loading ──────────────────────────────────────────────────────────────


def load_research_skill(path: Path | None = None) -> str | None:
    """Return the deep-research skill body, or ``None`` when unavailable.

    Reads ``REPO_SKILL_PATH`` by default (the repo-local copy), strips
    any leading YAML frontmatter so only the protocol body is injected
    into the research prompt. Returning ``None`` when the file is missing
    lets the caller fall back to the bare prompt without crashing.
    """
    target = path or REPO_SKILL_PATH
    if not target.exists():
        return None
    text = target.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            text = text[end + len("\n---") :].lstrip("\n")
    text = text.strip()
    return text or None


# ── prompt ─────────────────────────────────────────────────────────────────────


def build_research_prompt(
    idea: str,
    axes: list[str],
    budget: ResearchBudget,
    extra_context: str | None = None,
    protocol: str | None = None,
    *,
    input_richness: str | None = None,
) -> str:
    """Build the single research prompt sent to Claude.

    The prompt is capped at ``budget.max_prompt_chars`` by truncating
    ``extra_context`` first, then the protocol, then the full prompt as
    a last resort. The structure of the requested output is fixed so
    downstream summarisation in the context builder can rely on it.

    ``input_richness="rich"`` flips the prompt into "cover the gaps"
    mode (directive forbidding generic research restart).
    """
    axes_block = "\n".join(f"{idx + 1}. {axis}" for idx, axis in enumerate(axes))
    context_block = (extra_context or "").strip()
    protocol_block = (protocol or "").strip()

    header = (
        "You are the Research/Benchmark agent in a multi-agent design "
        "pipeline. Produce ONE markdown report that is structured, "
        "concise, and fully sourced.\n\n"
    )
    directives: list[str] = []
    if (input_richness or "").lower() == "rich":
        directives.append(
            "## Posture\n"
            "L'utilisateur a déjà fourni un contexte riche. Ton job n'est "
            "pas de refaire une recherche généraliste mais de combler les "
            "angles morts : vérifier les points non couverts, challenger "
            "les chiffres, et identifier les alternatives absentes.\n"
        )
    directives_block = "\n".join(directives)
    body = (
        f"## Idea\n{idea}\n\n"
        f"## Research axes (cover each one)\n{axes_block}\n\n"
        "## Required report structure (in French or English, keep the headings)\n"
        "# Benchmark\n\n"
        "## Résumé exécutif\n"
        "Three to five bullet points summarising the key findings.\n\n"
        "## Concurrents\n"
        "A markdown table with columns: Acteur | Positionnement | Forces | Limites.\n\n"
        "## Analyse par axe\n"
        "One `### {axis}` subsection per axis above, each with findings and "
        "at least one source URL.\n\n"
        "## Sources\n"
        "A consolidated bullet list of every source URL used, with a one-line label.\n\n"
        "Constraints:\n"
        f"- Keep the full report under ~{budget.max_output_chars} characters.\n"
        "- Cite real, reachable URLs. If you cannot verify a source, drop it.\n"
        "- Do not invent numbers or product names.\n"
        "- Use the tools provided (Read, WebSearch, WebFetch) only when relevant.\n"
    )

    def _assemble(ctx: str, proto: str) -> str:
        protocol_section = f"## Research protocol\n{proto}\n\n" if proto else ""
        context_section = f"## Additional context\n{ctx}\n\n" if ctx else ""
        directives_section = f"{directives_block}\n" if directives_block else ""
        return header + directives_section + protocol_section + context_section + body

    prompt = _assemble(context_block, protocol_block)
    if len(prompt) <= budget.max_prompt_chars:
        return prompt

    # 1. Trim the additional context first.
    if context_block:
        overflow = len(prompt) - budget.max_prompt_chars
        keep = max(0, len(context_block) - overflow - 40)
        context_block = context_block[:keep] + "\n…[context truncated]"
        prompt = _assemble(context_block, protocol_block)

    # 2. Then trim the protocol section if still over.
    if len(prompt) > budget.max_prompt_chars and protocol_block:
        overflow = len(prompt) - budget.max_prompt_chars
        keep = max(0, len(protocol_block) - overflow - 40)
        protocol_block = protocol_block[:keep] + "\n…[protocol truncated]"
        prompt = _assemble(context_block, protocol_block)

    # 3. Last resort: hard truncate the assembled prompt.
    if len(prompt) > budget.max_prompt_chars:
        prompt = prompt[: budget.max_prompt_chars - 40] + "\n…[prompt truncated]"
    return prompt


# ── report container ───────────────────────────────────────────────────────────


@dataclass
class BenchmarkReport:
    """Outcome of a successful research run."""

    slug: str
    content: str
    axes: list[str] = field(default_factory=list)
    file_path: Path | None = None
    attempt: int = 1


# ── persistence helpers ────────────────────────────────────────────────────────


def _truncate_output(text: str, budget: ResearchBudget) -> str:
    if len(text) <= budget.max_output_chars:
        return text
    cutoff = budget.max_output_chars - 80
    boundary = text.rfind("\n\n", cutoff // 2, cutoff)
    if boundary > 0:
        cutoff = boundary
    return text[:cutoff] + "\n\n*[Report truncated to fit the research budget.]*"


def persist_benchmark(
    session_id: str,
    slug: str,
    content: str,
    axes: list[str],
    attempt: int = 1,
    db_path: Path | None = None,
) -> BenchmarkReport:
    """Write the benchmark to the workspace and register it in phase_outputs.

    The phase_outputs row is stored under ``phase=benchmark`` and
    ``agent=research`` with the given ``attempt``; the context builder uses
    that attempt marker to ignore stale retries.
    """
    file_path = write_benchmark(session_id, slug, content, db_path=db_path)
    create_phase_output(
        session_id=session_id,
        phase=PHASE_BENCHMARK,
        agent="research",
        output=content,
        file_path=str(file_path),
        attempt=attempt,
        db_path=db_path,
    )
    return BenchmarkReport(
        slug=slug,
        content=content,
        axes=list(axes),
        file_path=file_path,
        attempt=attempt,
    )


# ── public entry point ─────────────────────────────────────────────────────────


def run_research(
    session_id: str,
    extra_context: str | None = None,
    slug: str | None = None,
    attempt: int = 1,
    db_path: Path | None = None,
) -> BenchmarkReport:
    """Run the benchmark phase for a session and persist the report.

    Reads the session's ``research_depth`` and ``idea`` from the DB,
    prepares the axes, builds the prompt under budget, invokes Claude
    via the executor with ``Read``/``WebSearch``/``WebFetch`` enabled,
    truncates the output if needed, and persists the result.

    ``light`` depth must not reach this function: callers are expected
    to skip the benchmark upstream.
    """
    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise ValueError(f"Session not found: {session_id!r}")
    if session.research_depth is None:
        raise ValueError(
            f"Session {session_id!r} has no research_depth; run the subject detector first"
        )

    budget = budget_for_depth(session.research_depth)
    axes = prepare_research_axes(session.subject_type, session.research_depth)
    protocol = load_research_skill()
    prompt = build_research_prompt(
        idea=session.idea,
        axes=axes,
        budget=budget,
        extra_context=extra_context,
        protocol=protocol,
        input_richness=session.input_richness,
    )

    logger.info(
        "Running research for session %s (depth=%s, axes=%d, budget=%s chars)",
        session_id,
        session.research_depth,
        len(axes),
        budget.max_output_chars,
    )
    cwd = _resolve_project_cwd(session)
    raw = run_task_text(
        prompt,
        timeout=budget.timeout_seconds,
        allowed_tools=list(RESEARCH_TOOLS),
        cwd=cwd,
    )
    content = _truncate_output(raw, budget)

    effective_slug = slug or _derive_slug(session.idea)
    return persist_benchmark(
        session_id=session_id,
        slug=effective_slug,
        content=content,
        axes=axes,
        attempt=attempt,
        db_path=db_path,
    )


# ── helpers ────────────────────────────────────────────────────────────────────


def _resolve_project_cwd(session) -> str | None:
    """Return ``session.project_path`` as the Claude subprocess cwd, if safe.

    Mirrors ``pipeline._resolve_agent_cwd``: only returns a path when it
    exists on disk, otherwise logs a warning and falls back to ``None``
    so the benchmark still runs (just without active-exploration tools
    resolving against the real project).
    """
    project_path = getattr(session, "project_path", None)
    if not project_path:
        return None
    if not Path(project_path).exists():
        logger.warning(
            "run_research: session.project_path %r does not exist; falling back to cwd=None",
            project_path,
        )
        return None
    return project_path


def _derive_slug(idea: str, max_len: int = 40) -> str:
    """Derive a short, filename-safe slug from the session idea."""
    slug = "".join(c if c.isalnum() or c in "-_ " else " " for c in idea.lower())
    slug = "-".join(part for part in slug.split() if part)
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug or "benchmark"
