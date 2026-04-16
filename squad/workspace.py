"""Session workspace — filesystem operations for Squad session artifacts."""

import json
import re
from pathlib import Path

from squad.constants import PHASE_DIRS
from squad.db import get_session, list_pending_questions
from squad.models import Session

_MINIMAL_CONTEXT_TEMPLATE = """\
# Project context

Project: {name}
Path: {path}

No CLAUDE.md found in this project. Agents should infer context from the idea provided.
"""


# ── helpers ────────────────────────────────────────────────────────────────────


def _slugify(text: str) -> str:
    """Convert a string to a filename-safe slug."""
    slug = re.sub(r"[^\w]+", "-", text.lower()).strip("-")
    return slug or "plan"


def _ws(session_id: str, db_path: Path | None) -> Path:
    """Resolve workspace path from DB for a session."""
    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise ValueError(f"Session not found: {session_id!r}")
    return Path(session.workspace_path)


# ── workspace creation ─────────────────────────────────────────────────────────


def create_workspace(session: Session) -> Path:
    """Create the workspace directory tree for a session and return its path."""
    workspace = Path(session.workspace_path)

    for phase_dir_name in PHASE_DIRS.values():
        (workspace / "phases" / phase_dir_name).mkdir(parents=True, exist_ok=True)

    for subdir in ("questions", "plans", "research"):
        (workspace / subdir).mkdir(parents=True, exist_ok=True)

    return workspace


def get_session_workspace(session_id: str, db_path: Path | None = None) -> Path:
    """Return the workspace Path for a session, resolved from the DB."""
    return _ws(session_id, db_path)


# ── idea and context ───────────────────────────────────────────────────────────


def write_idea(session_id: str, idea: str, db_path: Path | None = None) -> Path:
    """Write idea.md to the session workspace and return its path."""
    workspace = _ws(session_id, db_path)
    idea_file = workspace / "idea.md"
    idea_file.write_text(idea, encoding="utf-8")
    return idea_file


def write_context(session_id: str, content: str, db_path: Path | None = None) -> Path:
    """Write context.md to the session workspace and return its path."""
    workspace = _ws(session_id, db_path)
    context_file = workspace / "context.md"
    context_file.write_text(content, encoding="utf-8")
    return context_file


def get_context(project_path: str) -> str:
    """Return the project context: CLAUDE.md content if present, else a minimal stub."""
    claude_md = Path(project_path) / "CLAUDE.md"
    if claude_md.exists():
        return claude_md.read_text(encoding="utf-8")
    name = Path(project_path).name
    return _MINIMAL_CONTEXT_TEMPLATE.format(name=name, path=project_path)


# ── phase outputs ──────────────────────────────────────────────────────────────


def write_phase_output(
    session_id: str,
    phase: str,
    agent: str,
    content: str,
    db_path: Path | None = None,
) -> Path:
    """Write {agent}.md into the correct phase directory and return the path."""
    workspace = _ws(session_id, db_path)
    phase_dir_name = PHASE_DIRS[phase]
    output_file = workspace / "phases" / phase_dir_name / f"{agent}.md"
    output_file.write_text(content, encoding="utf-8")
    return output_file


def read_phase_outputs(
    session_id: str,
    phase: str | None = None,
    db_path: Path | None = None,
) -> dict:
    """Return phase outputs as {phase_id: {agent: content}}.

    If phase is given, only that phase is returned.
    """
    workspace = _ws(session_id, db_path)
    result: dict = {}

    phases_to_read = {phase: PHASE_DIRS[phase]} if phase else PHASE_DIRS

    for phase_id, phase_dir_name in phases_to_read.items():
        phase_dir = workspace / "phases" / phase_dir_name
        if not phase_dir.exists():
            continue
        files = list(phase_dir.glob("*.md"))
        if files:
            result[phase_id] = {f.stem: f.read_text(encoding="utf-8") for f in files}

    return result


# ── plans ──────────────────────────────────────────────────────────────────────


def write_plan(
    session_id: str,
    plan_title: str,
    plan_content: str,
    db_path: Path | None = None,
) -> Path:
    """Write a plan markdown file into the plans/ directory and return its path."""
    workspace = _ws(session_id, db_path)
    slug = _slugify(plan_title)
    plan_file = workspace / "plans" / f"{slug}.md"
    plan_file.write_text(plan_content, encoding="utf-8")
    return plan_file


def list_plans(session_id: str, db_path: Path | None = None) -> list[Path]:
    """Return sorted list of plan file paths for a session."""
    workspace = _ws(session_id, db_path)
    return sorted((workspace / "plans").glob("*.md"))


# ── research / benchmark ───────────────────────────────────────────────────────


def _benchmark_path(workspace: Path, slug: str) -> Path:
    return workspace / "research" / f"benchmark-{_slugify(slug)}.md"


def write_benchmark(
    session_id: str,
    slug: str,
    content: str,
    db_path: Path | None = None,
) -> Path:
    """Write a benchmark report under ``research/benchmark-{slug}.md``.

    The slug is filename-safe; long or accented slugs are normalised. The
    parent directory is created on demand (it already exists for sessions
    created via ``create_workspace``).
    """
    workspace = _ws(session_id, db_path)
    path = _benchmark_path(workspace, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def read_benchmark(
    session_id: str,
    slug: str,
    db_path: Path | None = None,
) -> str | None:
    """Return the benchmark content for ``slug``, or None if missing."""
    workspace = _ws(session_id, db_path)
    path = _benchmark_path(workspace, slug)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def list_benchmarks(
    session_id: str,
    db_path: Path | None = None,
) -> list[Path]:
    """Return all benchmark report paths for a session, sorted by name."""
    workspace = _ws(session_id, db_path)
    return sorted((workspace / "research").glob("benchmark-*.md"))


# ── questions ──────────────────────────────────────────────────────────────────


def write_pending_questions(
    session_id: str,
    questions: list[dict],
    db_path: Path | None = None,
) -> Path:
    """Persist pending questions as JSON in the questions/ directory."""
    workspace = _ws(session_id, db_path)
    questions_file = workspace / "questions" / "pending.json"
    questions_file.write_text(json.dumps(questions, ensure_ascii=False, indent=2), encoding="utf-8")
    return questions_file


def read_pending_questions(session_id: str, db_path: Path | None = None) -> list[dict]:
    """Return pending questions list, or [] if no file exists."""
    workspace = _ws(session_id, db_path)
    questions_file = workspace / "questions" / "pending.json"
    if not questions_file.exists():
        return []
    return json.loads(questions_file.read_text(encoding="utf-8"))


def sync_pending_questions(session_id: str, db_path: Path | None = None) -> Path:
    """Rewrite ``questions/pending.json`` to match the DB's pending questions.

    Called after ``squad answer`` records an answer so the filesystem stays
    in sync with the DB. Returns the path to the (rewritten) file.
    """
    pending = list_pending_questions(session_id, db_path=db_path)
    rows = [
        {
            "id": q.id,
            "agent": q.agent,
            "phase": q.phase,
            "question": q.question,
        }
        for q in pending
    ]
    return write_pending_questions(session_id, rows, db_path=db_path)
