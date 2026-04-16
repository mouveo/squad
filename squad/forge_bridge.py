"""Forge CLI bridge — availability, queue status, plan submission.

The bridge is purely technical: it never prompts the user and never
changes session status for anything other than a successful submission
(``queued``). CLI-facing validation (approve / reject / edit) lives in
``squad.cli``. When Forge is absent, inaccessible, or busy, the bridge
raises a dedicated exception; callers decide how to fall back (the
pipeline and the ``squad approve`` CLI command both flip the session to
``review`` and notify Slack).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from squad.constants import STATUS_QUEUED
from squad.db import get_session, list_plans, update_session_status

logger = logging.getLogger(__name__)

# Name of the Forge CLI on PATH
FORGE_CMD = "forge"
# Default timeout for forge subprocess calls
_FORGE_TIMEOUT = 60


class ForgeUnavailable(RuntimeError):
    """Raised when the Forge CLI is missing, failing, or produces an error."""


class ForgeQueueBusy(RuntimeError):
    """Raised when the queue is already running and cannot accept a new run."""


@dataclass(frozen=True)
class QueueStatus:
    """Snapshot of the Forge queue for a project."""

    available: bool
    busy: bool
    reason: str | None = None


# ── availability ───────────────────────────────────────────────────────────────


def is_forge_available() -> bool:
    """Return True when the Forge CLI binary is on PATH."""
    return shutil.which(FORGE_CMD) is not None


def _run_forge(args: list[str], timeout: int = _FORGE_TIMEOUT) -> subprocess.CompletedProcess:
    """Invoke ``forge`` with the given args. Isolated so tests can mock it."""
    try:
        return subprocess.run(
            [FORGE_CMD, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise ForgeUnavailable(f"forge CLI not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ForgeUnavailable(f"forge CLI timed out: {exc}") from exc


# ── queue operations ───────────────────────────────────────────────────────────


def get_queue_status(project_path: str) -> QueueStatus:
    """Return a ``QueueStatus`` snapshot for ``project_path``.

    A missing Forge CLI is reported as unavailable rather than raised, so
    callers can choose to fall back gracefully. The ``busy`` flag is a
    conservative heuristic on the output of ``forge queue status``.
    """
    if not is_forge_available():
        return QueueStatus(available=False, busy=False, reason="forge CLI not installed")
    try:
        result = _run_forge(["queue", "status", project_path])
    except ForgeUnavailable as exc:
        return QueueStatus(available=False, busy=False, reason=str(exc))
    if result.returncode != 0:
        return QueueStatus(
            available=False,
            busy=False,
            reason=(result.stderr or result.stdout or "forge queue status failed")[:200],
        )
    stdout_low = result.stdout.lower()
    busy = "running" in stdout_low or "in progress" in stdout_low or "busy" in stdout_low
    return QueueStatus(available=True, busy=busy)


def add_plan_to_queue(project_path: str, plan_file: Path) -> None:
    """Add a single plan file to the project's Forge queue."""
    result = _run_forge(["queue", "add", project_path, str(plan_file)])
    if result.returncode != 0:
        raise ForgeUnavailable(
            f"forge queue add failed for {plan_file.name}: {(result.stderr or result.stdout)[:200]}"
        )


def run_queue(project_path: str) -> None:
    """Start the Forge queue run for ``project_path``."""
    result = _run_forge(["queue", "run", project_path])
    if result.returncode != 0:
        raise ForgeUnavailable(f"forge queue run failed: {(result.stderr or result.stdout)[:200]}")


# ── high-level entry ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SubmitOutcome:
    """Outcome of ``submit_session_to_forge``.

    ``plans_sent`` is the number of plan files added to the queue.
    ``queue_started`` reflects whether ``forge queue run`` was invoked.
    """

    plans_sent: int
    queue_started: bool


def submit_session_to_forge(
    session_id: str,
    db_path: Path | None = None,
    start_queue: bool = True,
) -> SubmitOutcome:
    """Add every generated plan of a session to Forge and (optionally) start the queue.

    On success, the session status is transitioned to ``queued``. On any
    error, the caller receives a ``ForgeUnavailable`` or ``ForgeQueueBusy``
    exception and the session status is left untouched — the caller decides
    whether to fall back to ``review``.
    """
    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise ValueError(f"Session not found: {session_id!r}")

    status = get_queue_status(session.project_path)
    if not status.available:
        raise ForgeUnavailable(status.reason or "forge queue is not available")
    if status.busy:
        raise ForgeQueueBusy("forge queue is currently running")

    plans = list_plans(session.id, db_path=db_path)
    if not plans:
        raise ValueError(f"No plans found for session {session_id!r}")

    for plan in plans:
        add_plan_to_queue(session.project_path, Path(plan.file_path))

    queue_started = False
    if start_queue:
        run_queue(session.project_path)
        queue_started = True

    update_session_status(session_id, STATUS_QUEUED, db_path=db_path)
    logger.info(
        "Submitted %d plan(s) to Forge for session %s (queue_started=%s)",
        len(plans),
        session_id,
        queue_started,
    )
    return SubmitOutcome(plans_sent=len(plans), queue_started=queue_started)
