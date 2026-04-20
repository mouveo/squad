"""Helpers backing the ``{project}/plans/<subject>/`` auto-scan.

Pure building blocks:

* :func:`discover_plans_subfolder` — picks a `plans/<token>/` directory
  under ``project_path`` based on tokens extracted from the idea, reusing
  exactly the tokenisation logic of ``slack_service.discover_project_path``.
* :func:`inventory_plan_folder` — a non-recursive listing of the direct
  files in that folder, filtering to the text extensions eligible for the
  cumulative context (``.md``, ``.txt``, ``.csv``) and capping the set.

Shared orchestration:

* :func:`autoscan_and_import_plans` — composes the two helpers above with
  :func:`squad.attachment_service.import_local_attachment` so both the
  Slack handler and the CLI entrypoint can pre-load locally prepared
  briefs into the session just before ``run_pipeline(...)``. The helper
  deliberately owns no user-facing output (no Slack post, no
  ``click.echo``) — it logs and returns a structured result.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from squad.attachment_service import AttachmentError, import_local_attachment
from squad.config import load_config
from squad.models import AttachmentMeta, Session

logger = logging.getLogger(__name__)

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


# ── shared orchestration ──────────────────────────────────────────────────────


@dataclass
class AutoScanResult:
    """Outcome of a ``plans/<subject>/`` auto-scan pass.

    ``enabled`` reflects the effective decision after considering the
    explicit override and the ``pipeline.project_plans_autoscan`` config
    key. ``folder`` is the matched subfolder (or ``None`` when the scan
    was disabled or nothing matched). ``imported`` holds the
    :class:`AttachmentMeta` rows produced by successful imports.
    """

    enabled: bool
    folder: Path | None = None
    imported_count: int = 0
    rejected_count: int = 0
    ignored_count: int = 0
    imported: list[AttachmentMeta] = field(default_factory=list)


def autoscan_and_import_plans(
    session: Session,
    idea: str,
    *,
    db_path: Path | None = None,
    enabled: bool | None = None,
    max_files: int = 10,
) -> AutoScanResult:
    """Discover, inventory and import files from ``{project}/plans/<subject>/``.

    Loads the merged project config, resolves the effective enabled flag
    (explicit override wins, otherwise reads
    ``pipeline.project_plans_autoscan``, defaulting to ``True`` when the
    key is absent), then delegates per-file import to
    :func:`import_local_attachment` so the attachment policy (allowed
    extensions, per-file size, cumulative quota) is applied exactly once,
    from the project config.

    A single-file :class:`AttachmentError` is logged and counted as
    ``rejected_count``; the scan continues with the remaining files.
    """
    project_config = load_config(session.project_path)

    if enabled is None:
        pipeline_cfg = project_config.get("pipeline") or {}
        enabled = bool(pipeline_cfg.get("project_plans_autoscan", True))

    if not enabled:
        logger.info(
            "plans auto-scan disabled for session %s (project=%s)",
            session.id,
            session.project_path,
        )
        return AutoScanResult(enabled=False)

    project_path = Path(session.project_path)
    folder = discover_plans_subfolder(idea, project_path)
    if folder is None:
        logger.info(
            "plans auto-scan: no matching plans/<subject>/ folder under %s",
            project_path,
        )
        return AutoScanResult(enabled=True)

    inventory = inventory_plan_folder(folder, max_files=max_files)
    logger.info(
        "plans auto-scan matched %s (%d eligible file(s), %d ignored)",
        folder,
        len(inventory.files),
        inventory.ignored_count,
    )

    imported: list[AttachmentMeta] = []
    rejected = 0
    for src in inventory.files:
        try:
            meta = import_local_attachment(
                session.id,
                src,
                config=project_config,
                db_path=db_path,
            )
            imported.append(meta)
        except AttachmentError as exc:
            rejected += 1
            logger.warning("plans auto-scan rejected %s: %s", src, exc)

    logger.info(
        "plans auto-scan done: imported=%d rejected=%d ignored=%d from %s",
        len(imported),
        rejected,
        inventory.ignored_count,
        folder,
    )

    return AutoScanResult(
        enabled=True,
        folder=folder,
        imported_count=len(imported),
        rejected_count=rejected,
        ignored_count=inventory.ignored_count,
        imported=imported,
    )
