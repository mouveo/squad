"""Session workspace — filesystem operations for Squad session artifacts."""

import json
import logging
import re
import subprocess
from pathlib import Path

from squad.constants import PHASE_DIRS
from squad.db import get_session, list_pending_questions
from squad.models import Session

logger = logging.getLogger(__name__)

# Dirs/files that add noise without context. Excluded from the tree scan.
_TREE_EXCLUDES: set[str] = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".turbo",
    ".forge",
    ".claude",
    ".DS_Store",
    "target",
    "vendor",
    "coverage",
    ".cache",
}

# Manifests scanned for stack detection, in priority order.
_MANIFEST_FILES: tuple[str, ...] = (
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "Pipfile",
    "composer.json",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
)

# Soft truncation budgets per scanned artefact (in characters).
_BUDGET_CLAUDE_MD: int = 4000
_BUDGET_README: int = 2500
_BUDGET_MANIFEST: int = 1200
_BUDGET_TREE_ENTRIES: int = 80
_BUDGET_GIT_LOG_LINES: int = 20


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

    for subdir in ("questions", "plans", "research", "attachments"):
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


def _truncate(text: str, budget: int, marker: str = "\n\n[… truncated]") -> str:
    """Cut ``text`` to at most ``budget`` chars, appending a marker if clipped."""
    if len(text) <= budget:
        return text
    return text[:budget].rstrip() + marker


def _read_text_if_present(path: Path, budget: int) -> str | None:
    """Read ``path`` as UTF-8 text, truncated. Return None if missing or unreadable."""
    if not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return None
    if not content:
        return None
    return _truncate(content, budget)


def _project_tree(root: Path, max_entries: int = _BUDGET_TREE_ENTRIES) -> str:
    """Produce a shallow (2-level) tree listing, skipping noisy dirs."""
    lines: list[str] = []
    try:
        top = sorted(
            p for p in root.iterdir() if p.name not in _TREE_EXCLUDES and not p.name.startswith(".")
        )
    except OSError as exc:
        logger.warning("Could not list %s: %s", root, exc)
        return ""

    for entry in top:
        if len(lines) >= max_entries:
            lines.append("…")
            break
        suffix = "/" if entry.is_dir() else ""
        lines.append(f"{entry.name}{suffix}")
        if entry.is_dir():
            try:
                children = sorted(
                    c
                    for c in entry.iterdir()
                    if c.name not in _TREE_EXCLUDES and not c.name.startswith(".")
                )[:8]
            except OSError:
                continue
            for child in children:
                if len(lines) >= max_entries:
                    break
                child_suffix = "/" if child.is_dir() else ""
                lines.append(f"  {child.name}{child_suffix}")
    return "\n".join(lines)


def _recent_git_log(project_path: Path, max_lines: int = _BUDGET_GIT_LOG_LINES) -> str:
    """Return the last ``max_lines`` git commits, or empty string if not a repo."""
    if not (project_path / ".git").exists():
        return ""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(project_path),
                "log",
                f"-{max_lines}",
                "--oneline",
                "--no-decorate",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("git log failed for %s: %s", project_path, exc)
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def get_context(project_path: str) -> str:
    """Return a rich project context scan.

    Scans the target project directory and assembles a markdown block
    fed to every agent via ``squad.context_builder``. Coverage:

    * ``CLAUDE.md`` — authoritative project doc (if present).
    * ``README.md`` — human-facing overview (top ~2500 chars).
    * Manifests — ``package.json``, ``pyproject.toml``, ``composer.json``…
      for stack detection.
    * Directory tree — 2-level listing of top-level folders (noise dirs
      like ``node_modules``, ``.venv``, ``.git`` are filtered out).
    * Recent git log — last 20 commits (``--oneline``) when the project
      is a git repo.

    All artefacts are truncated under soft budgets so the cumulative
    context stays well under the 15k-token ceiling documented in
    ``CLAUDE.md``. Missing files are simply omitted.
    """
    root = Path(project_path)
    name = root.name
    parts: list[str] = [f"Project: {name}", f"Path: {project_path}"]

    claude_md = _read_text_if_present(root / "CLAUDE.md", _BUDGET_CLAUDE_MD)
    if claude_md:
        parts.append(f"### CLAUDE.md\n\n{claude_md}")

    readme = _read_text_if_present(root / "README.md", _BUDGET_README)
    if readme:
        parts.append(f"### README.md\n\n{readme}")

    manifests: list[str] = []
    for name_ in _MANIFEST_FILES:
        content = _read_text_if_present(root / name_, _BUDGET_MANIFEST)
        if content:
            manifests.append(f"**{name_}**\n```\n{content}\n```")
    if manifests:
        parts.append("### Manifests\n\n" + "\n\n".join(manifests))

    tree = _project_tree(root)
    if tree:
        parts.append(f"### Top-level tree\n\n```\n{tree}\n```")

    git_log = _recent_git_log(root)
    if git_log:
        parts.append(f"### Recent commits (last {_BUDGET_GIT_LOG_LINES})\n\n```\n{git_log}\n```")

    if len(parts) == 2:  # only Project/Path headers → nothing found
        parts.append(
            "No CLAUDE.md, README, manifests or git history found. "
            "Agents should ask clarifying questions before inferring stack or architecture."
        )
    return "\n\n".join(parts)


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


def copy_plans_to_project(
    session_id: str,
    db_path: Path | None = None,
) -> list[Path]:
    """Copy every ``*.md`` in the workspace ``plans/`` dir to ``{project_path}/plans/``.

    The target directory is created if missing. Existing files with the
    same name are overwritten so re-runs stay predictable. Returns the
    list of written target paths (sorted).
    """
    import shutil

    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise ValueError(f"Session not found: {session_id!r}")

    workspace = _ws(session_id, db_path)
    source_dir = workspace / "plans"
    if not source_dir.exists():
        return []

    target_dir = Path(session.project_path) / "plans"
    target_dir.mkdir(parents=True, exist_ok=True)

    copied: list[Path] = []
    for source in sorted(source_dir.glob("*.md")):
        target = target_dir / source.name
        shutil.copy2(source, target)
        copied.append(target)
    return copied


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
