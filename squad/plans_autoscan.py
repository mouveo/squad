"""Explicit-path auto-scan for ``{project}/plans/…`` mentions in the idea.

Design principle : zero guesswork. The user writes the path of the
folder or file they want Squad to import as part of their idea, and
the parser extracts exactly what is mentioned — nothing else. This
replaces the earlier token-matching behaviour which could collide with
any word of the idea that happened to match a folder name under
``plans/``.

Accepted patterns in the idea :

* ``plans/<name>`` → imports every eligible direct file from that
  folder (``.md`` / ``.txt`` / ``.csv``, non-recursive, capped).
* ``plans/<name>/`` → same thing; trailing slash tolerated.
* ``plans/<name>/<file>.ext`` → imports just that single file (if its
  extension is in :data:`SCOPED_EXTENSIONS`).

Punctuation that normally terminates a sentence (``,;.:)!?"'``) is
stripped from the end of each match so the path doesn't eat the
punctuation that follows it in French prose.

Shared orchestration lives in :func:`autoscan_and_import_plans` so the
Slack handler and the CLI entry point call the exact same code path
and deliver the same messages to the user.
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

# Capture any ``plans/…`` path mentioned in the idea. Boundary on the
# left: not preceded by a word char or another slash so we don't catch
# ``some/other/plans/...`` nested paths or stick to the previous word.
_PATH_RE = re.compile(r"(?<![\w/])(plans/[A-Za-z0-9._\-/]+)")

# Trailing punctuation to strip off each match.
_TRAILING_PUNCT = ".,;:)!?\"'"


@dataclass
class PlanFolderInventory:
    """Non-recursive snapshot of a ``plans/<subject>/`` directory."""

    folder: Path
    files: list[Path] = field(default_factory=list)
    ignored_count: int = 0


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


def extract_plan_paths_from_idea(idea: str, project_path: Path) -> list[Path]:
    """Extract every ``plans/…`` path mentioned in ``idea`` as an absolute path.

    Each match is resolved against ``project_path``. Directories are
    kept as directories (the caller inventories them); single files are
    kept only when their extension is in :data:`SCOPED_EXTENSIONS`.
    Duplicates are collapsed. Paths that do not resolve to an existing
    file or directory are silently dropped — they are typos the user
    can fix without triggering a noisy error.
    """
    seen: set[str] = set()
    resolved: list[Path] = []
    for raw in _PATH_RE.findall(idea):
        cleaned = raw.rstrip(_TRAILING_PUNCT).rstrip("/")
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        candidate = (project_path / cleaned).resolve()
        if candidate.is_dir():
            resolved.append(candidate)
            continue
        if candidate.is_file():
            ext = candidate.suffix.lstrip(".").lower()
            if ext in SCOPED_EXTENSIONS:
                resolved.append(candidate)
    return resolved


# ── shared orchestration ──────────────────────────────────────────────────────


@dataclass
class AutoScanResult:
    """Outcome of a ``plans/…`` auto-scan pass.

    ``enabled`` reflects the effective decision after considering the
    explicit override and the ``pipeline.project_plans_autoscan`` config
    key. ``folders`` lists the folders that were inventoried (empty
    when only single files were mentioned). ``imported`` holds the
    :class:`AttachmentMeta` rows produced by successful imports.
    """

    enabled: bool
    folders: list[Path] = field(default_factory=list)
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
    """Extract explicit ``plans/…`` paths from ``idea`` and import them.

    Loads the merged project config, resolves the effective enabled
    flag (explicit override wins, otherwise reads
    ``pipeline.project_plans_autoscan``, defaulting to ``True`` when
    the key is absent), then delegates per-file import to
    :func:`import_local_attachment` so the attachment policy (allowed
    extensions, per-file size, cumulative quota) is applied from the
    project config.

    Single-file :class:`AttachmentError` are logged and counted as
    ``rejected_count`` ; the scan continues with the remaining files.
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
    resolved = extract_plan_paths_from_idea(idea, project_path)
    if not resolved:
        logger.info(
            "plans auto-scan: no explicit plans/… path found in idea "
            "(project=%s)",
            project_path,
        )
        return AutoScanResult(enabled=True)

    # Turn every entry into a concrete list of files to import, logging
    # each source as we go so the serve.log reads like a narrative.
    files_to_import: list[Path] = []
    folders_matched: list[Path] = []
    ignored = 0
    for entry in resolved:
        if entry.is_dir():
            inv = inventory_plan_folder(entry, max_files=max_files)
            folders_matched.append(entry)
            logger.info(
                "plans auto-scan matched folder %s (%d eligible file(s), %d ignored)",
                entry,
                len(inv.files),
                inv.ignored_count,
            )
            ignored += inv.ignored_count
            files_to_import.extend(inv.files)
        else:
            logger.info("plans auto-scan matched file %s", entry)
            files_to_import.append(entry)

    imported: list[AttachmentMeta] = []
    rejected = 0
    for src in files_to_import:
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
        "plans auto-scan done: imported=%d rejected=%d ignored=%d (folders=%d)",
        len(imported),
        rejected,
        ignored,
        len(folders_matched),
    )

    return AutoScanResult(
        enabled=True,
        folders=folders_matched,
        imported_count=len(imported),
        rejected_count=rejected,
        ignored_count=ignored,
        imported=imported,
    )
