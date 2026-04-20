"""Pure helpers backing the ``{project}/plans/<subject>/`` auto-scan.

This module exposes side-effect-free building blocks that the pipeline
entrypoints (Slack handler and CLI) compose just before
``run_pipeline(...)`` to pre-load locally prepared briefs into the
session's attachment set.

Two pieces live here:

* :func:`discover_plans_subfolder` — picks a `plans/<token>/` directory
  under ``project_path`` based on tokens extracted from the idea, reusing
  exactly the tokenisation logic of ``slack_service.discover_project_path``.
* :func:`inventory_plan_folder` — a non-recursive listing of the direct
  files in that folder, filtering to the text extensions eligible for the
  cumulative context (``.md``, ``.txt``, ``.csv``) and capping the set.

Neither function reads configuration, imports attachments or logs —
orchestration and policy live in :mod:`squad.attachment_service` and (for
the glue) in LOT 3 of this plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Extensions eligible for the cumulative context at scoping time.
SCOPED_EXTENSIONS: tuple[str, ...] = ("md", "txt", "csv")

# Same token shape as ``slack_service.discover_project_path``: lowercase,
# starts with [a-z0-9], then [a-z0-9_-] of total length >= 3.
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-_]{2,}")


@dataclass
class PlanFolderInventory:
    """Non-recursive snapshot of a ``plans/<subject>/`` directory."""

    folder: Path
    files: list[Path] = field(default_factory=list)
    ignored_count: int = 0


def discover_plans_subfolder(idea: str, project_path: Path) -> Path | None:
    """Return ``project_path/plans/<token>/`` when a token from ``idea`` matches.

    Longest directory name wins; alphabetical order breaks ties so the
    result is deterministic. Returns ``None`` when ``plans/`` is missing,
    the idea yields no usable tokens, or no direct sub-directory of
    ``plans/`` matches one of them.
    """
    plans_dir = project_path / "plans"
    if not plans_dir.is_dir():
        return None
    try:
        candidates = [p for p in plans_dir.iterdir() if p.is_dir() and not p.name.startswith(".")]
    except OSError:
        return None

    tokens = {t for t in _TOKEN_RE.findall(idea.lower()) if len(t) >= 3}
    if not tokens:
        return None

    matches = [p for p in candidates if p.name.lower() in tokens]
    if not matches:
        return None

    matches.sort(key=lambda p: (-len(p.name), p.name))
    return matches[0]


def inventory_plan_folder(folder: Path, *, max_files: int = 10) -> PlanFolderInventory:
    """Return the direct, text-extension files in ``folder``, sorted + capped.

    Sub-directories are ignored entirely (no recursion). Files whose
    extension falls outside :data:`SCOPED_EXTENSIONS`, and direct files
    beyond the ``max_files`` cap, both contribute to ``ignored_count``.
    """
    inventory = PlanFolderInventory(folder=folder)
    if not folder.is_dir():
        return inventory

    try:
        direct_entries = list(folder.iterdir())
    except OSError:
        return inventory

    direct_files = sorted(
        (p for p in direct_entries if p.is_file()),
        key=lambda p: p.name,
    )

    eligible: list[Path] = []
    ignored = 0
    for path in direct_files:
        ext = path.suffix.lstrip(".").lower()
        if ext in SCOPED_EXTENSIONS:
            eligible.append(path)
        else:
            ignored += 1

    if len(eligible) > max_files:
        ignored += len(eligible) - max_files
        eligible = eligible[:max_files]

    inventory.files = eligible
    inventory.ignored_count = ignored
    return inventory
