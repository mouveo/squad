"""Plan generator — turns the PM synthesis into one or more Forge plans.

The generator takes the structured outputs of the synthese phase and the
unresolved constraints from the challenge phase, asks Claude (via
``squad.executor.run_task_text``) to produce a Forge-formatted plan, then
validates it with ``squad.forge_format``. Over-large drafts are split
deterministically. All persistence goes through ``squad.db`` and
``squad.workspace``; no skill or asset outside the repo is required.

Contract consumed:

* ``decision_summary``, ``plan_inputs``, ``open_questions`` from
  ``squad.phase_contracts.SynthesisContract``.
* ``unresolved_blockers`` — ``list[str]`` produced by
  ``squad.recovery.collect_blocker_constraints``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from squad.constants import PHASE_SYNTHESE
from squad.db import create_plan as db_create_plan
from squad.db import get_session, list_phase_outputs
from squad.executor import AgentError, run_task_text
from squad.forge_format import ForgeFormatError, validate_or_split
from squad.phase_contracts import ContractError, parse_synthesis_contract
from squad.recovery import collect_blocker_constraints
from squad.workspace import copy_plans_to_project as _ws_copy_plans_to_project
from squad.workspace import write_plan

logger = logging.getLogger(__name__)

# Default Claude timeout for plan generation (plan drafts are compact)
_PROMPT_TIMEOUT = 600

# Characters reserved per prompt section
_MAX_SECTION_CHARS = 4_000

# Repo-local Forge plan template — single source of truth for the format
# shown to Claude. Validation rules live in ``squad.forge_format``.
TEMPLATE_PATH: Path = Path(__file__).resolve().parent.parent / "templates" / "forge-plan.md"


def load_plan_template(path: Path | None = None) -> str:
    """Return the Forge plan template body used in the generation prompt."""
    return (path or TEMPLATE_PATH).read_text(encoding="utf-8")


@dataclass
class GeneratedPlanDraft:
    """One plan draft after validation + persistence."""

    title: str
    content: str
    workspace_path: Path
    db_id: str | None = None
    project_path: Path | None = None


# ── prompt ─────────────────────────────────────────────────────────────────────


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n…[truncated]"


def build_plan_prompt(
    project_name: str,
    project_path: str,
    idea: str,
    decision_summary: str,
    plan_inputs: list[str],
    open_questions: list[str],
    unresolved_blockers: list[str],
    project_context: str = "",
) -> str:
    """Build the Claude prompt that asks for a Forge-formatted plan."""
    inputs_bullets = "\n".join(f"- {p}" for p in plan_inputs) or "(none)"
    questions_bullets = "\n".join(f"- {q}" for q in open_questions) or "(none)"
    blockers_bullets = "\n".join(f"- {b}" for b in unresolved_blockers) or "(none)"
    context_block = _truncate(project_context.strip(), _MAX_SECTION_CHARS)
    template = _truncate(load_plan_template(), _MAX_SECTION_CHARS)

    return (
        "You are producing a Forge-executable plan from a PM synthesis.\n\n"
        f"## Target project\n{project_name} — {project_path}\n\n"
        f"## Idea\n{idea}\n\n"
        f"## Project context\n{context_block or '(no CLAUDE.md)'}\n\n"
        f"## Decision summary\n{decision_summary.strip() or '(missing)'}\n\n"
        f"## Plan inputs\n{inputs_bullets}\n\n"
        f"## Open questions (already resolved)\n{questions_bullets}\n\n"
        f"## Unresolved blockers (must be accounted for)\n{blockers_bullets}\n\n"
        "## Output format\n"
        "Use exactly the structure of this template. Replace placeholders "
        f"(`{{Project name}}`, `{{Plan title}}`, `{{Lot title}}`, ...) with "
        f"real values for `{project_name}`. Keep section headings verbatim.\n\n"
        f"```markdown\n{template}\n```\n\n"
        "Constraints:\n"
        "- Between 5 and 15 lots.\n"
        "- Lots numbered sequentially starting at 1.\n"
        "- Each lot must include a `**Success criteria**:` bullet list and a `**Files**:` line.\n"
        "- Lots must be concrete and match the target project's stack.\n"
        "- Address every unresolved blocker explicitly in one or more lots.\n"
        "- Output the markdown directly — do not wrap the whole response in a code fence."
    )


# ── public API ─────────────────────────────────────────────────────────────────


def generate_plans(
    session_id: str,
    decision_summary: str,
    plan_inputs: list[str],
    open_questions: list[str],
    unresolved_blockers: list[str],
    project_context: str = "",
    timeout: int = _PROMPT_TIMEOUT,
    db_path: Path | None = None,
) -> list[GeneratedPlanDraft]:
    """Generate one or more Forge plans for a session and persist them.

    Steps: build prompt → call Claude → strip a surrounding code fence if
    present → validate via ``forge_format.validate_or_split`` → write each
    plan into the workspace → record each plan in the DB. Copying to the
    target project is handled separately by ``copy_plans_to_project`` so
    the pipeline can decide when the files should leave the workspace.
    """
    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise ValueError(f"Session not found: {session_id!r}")

    project_name = Path(session.project_path).name
    prompt = build_plan_prompt(
        project_name=project_name,
        project_path=session.project_path,
        idea=session.idea,
        decision_summary=decision_summary,
        plan_inputs=plan_inputs,
        open_questions=open_questions,
        unresolved_blockers=unresolved_blockers,
        project_context=project_context,
    )

    try:
        raw = run_task_text(prompt, timeout=timeout)
    except AgentError as exc:
        raise RuntimeError(f"Claude plan generation failed: {exc}") from exc

    content = _strip_outer_fence(raw)
    plans = validate_or_split(content)  # raises ForgeFormatError on invalid

    drafts: list[GeneratedPlanDraft] = []
    for idx, plan_content in enumerate(plans, start=1):
        title = _extract_plan_title(plan_content) or f"plan-{idx}"
        ws_path = write_plan(session_id, title, plan_content, db_path=db_path)
        row = db_create_plan(
            session_id=session_id,
            title=title,
            file_path=str(ws_path),
            content=plan_content,
            db_path=db_path,
        )
        drafts.append(
            GeneratedPlanDraft(
                title=title,
                content=plan_content,
                workspace_path=ws_path,
                db_id=row.id,
            )
        )
    return drafts


def copy_plans_to_project(
    session_id: str,
    db_path: Path | None = None,
) -> list[Path]:
    """Copy the workspace plans into the target project's ``plans/`` directory.

    Thin wrapper over ``squad.workspace.copy_plans_to_project`` — exposed
    here so callers have a single ``plan_generator`` entry point for the
    full generate-then-copy flow.
    """
    return _ws_copy_plans_to_project(session_id, db_path=db_path)


def generate_plans_from_session(
    session_id: str,
    db_path: Path | None = None,
) -> list[GeneratedPlanDraft]:
    """Read the synthese output + blockers for a session and generate plans.

    This is the high-level entry point called by the pipeline once the
    six phases have completed. It parses the synthesis contract from the
    latest synthese attempt, collects unresolved blockers from the
    challenge phase, and delegates to ``generate_plans``.
    """
    outputs = list_phase_outputs(session_id, phase=PHASE_SYNTHESE, db_path=db_path)
    if not outputs:
        raise ValueError(f"No synthese output found for session {session_id!r}")

    max_attempt = max(po.attempt for po in outputs)
    synthese_outputs = [po for po in outputs if po.attempt == max_attempt]

    contract = None
    last_error: Exception | None = None
    for po in synthese_outputs:
        try:
            contract = parse_synthesis_contract(po.output)
            break
        except ContractError as exc:
            last_error = exc
            continue
    if contract is None:
        raise ValueError(
            "Could not parse a synthesis contract from the synthese outputs"
            + (f": {last_error}" if last_error else "")
        )

    blockers = collect_blocker_constraints(session_id, db_path=db_path)
    session = get_session(session_id, db_path=db_path)
    project_context = ""
    if session is not None:
        claude_md = Path(session.project_path) / "CLAUDE.md"
        if claude_md.exists():
            try:
                project_context = claude_md.read_text(encoding="utf-8")
            except OSError:
                project_context = ""

    return generate_plans(
        session_id=session_id,
        decision_summary=contract.decision_summary,
        plan_inputs=list(contract.plan_inputs),
        open_questions=list(contract.open_questions),
        unresolved_blockers=blockers,
        project_context=project_context,
        db_path=db_path,
    )


# ── helpers ────────────────────────────────────────────────────────────────────


def _strip_outer_fence(text: str) -> str:
    """Drop a surrounding ```` ``` ```` code fence if Claude wrapped everything in one."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()
    if len(lines) < 2:
        return text
    # Drop the opening fence (``` or ```markdown …) and the closing fence
    opening = lines[0].strip()
    if not opening.startswith("```"):
        return text
    # Find matching closing fence
    body_lines = lines[1:]
    if body_lines and body_lines[-1].strip() == "```":
        body_lines = body_lines[:-1]
    return "\n".join(body_lines).strip() + "\n"


def _extract_plan_title(content: str) -> str | None:
    """Return a short slug derived from the first ``# ...`` header of ``content``."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            header = stripped[2:].strip()
            # Prefer the part after a ':' when the header is "X — Plan 1/M: Title"
            if ":" in header:
                return header.split(":", 1)[1].strip()
            return header
    return None


# Re-export for convenience
__all__ = [
    "GeneratedPlanDraft",
    "build_plan_prompt",
    "copy_plans_to_project",
    "generate_plans",
    "generate_plans_from_session",
]

# Keep ForgeFormatError importable from plan_generator so callers can catch
# format errors without having to depend on forge_format directly.
ForgeFormatError = ForgeFormatError
