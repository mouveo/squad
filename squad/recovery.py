"""Recovery helpers — compute a safe resume point and drive challenge retries.

This module is the single source of truth for:

* **Resume points** — which phase a session should re-enter after a pause
  (questions waiting for an answer) or a crash. Decisions are derived from
  the persisted session state (``status``, ``current_phase``, pending
  questions in DB) — never from filesystem heuristics.
* **Challenge retries** — the pipeline may re-run the conception phase
  once when the challenge produced blocking constraints. The decision
  is driven by ``phase_attempts`` and ``challenge_retry_count`` on the
  session row plus the structured ``blockers`` contract; the second
  conception pass receives a ``phase_instruction`` with the additional
  constraints.

Keeping this logic here lets ``pipeline.py`` stay a thin scheduler and
makes the recovery rules unit-testable on their own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from squad.constants import (
    PHASE_CADRAGE,
    PHASE_CHALLENGE,
    PHASE_CONCEPTION,
    PHASES,
    STATUS_APPROVED,
    STATUS_DONE,
    STATUS_DRAFT,
    STATUS_FAILED,
    STATUS_INTERVIEWING,
    STATUS_QUEUED,
    STATUS_REVIEW,
    STATUS_WORKING,
)
from squad.db import (
    get_session,
    increment_challenge_retry_count,
    list_pending_questions,
    list_phase_outputs,
)
from squad.phase_contracts import ContractError, parse_blockers_contract

logger = logging.getLogger(__name__)

# Maximum number of conception retries allowed after a challenge produces
# blocking constraints. The plan mandates a single retry.
MAX_CHALLENGE_RETRIES = 1


# ── dataclasses ────────────────────────────────────────────────────────────────


@dataclass
class ResumePoint:
    """Where to re-enter a session's pipeline.

    ``phase`` is the next phase that should be run. ``reason`` is a short
    label used in CLI/log output. ``blocker_constraints`` is non-empty
    only when the resume point is a conception retry triggered by
    challenge blockers.
    """

    session_id: str
    phase: str
    reason: str
    blocker_constraints: list[str] = field(default_factory=list)


# ── pending questions ──────────────────────────────────────────────────────────


def has_pending_questions(session_id: str, db_path: Path | None = None) -> bool:
    """Return True when the session still has unanswered questions in the DB."""
    return len(list_pending_questions(session_id, db_path=db_path)) > 0


# ── blockers from the challenge phase ──────────────────────────────────────────


def _latest_challenge_outputs(session_id: str, db_path: Path | None):
    """Return the phase_outputs of the latest challenge attempt."""
    outputs = list_phase_outputs(session_id, phase=PHASE_CHALLENGE, db_path=db_path)
    if not outputs:
        return []
    max_attempt = max(po.attempt for po in outputs)
    return [po for po in outputs if po.attempt == max_attempt]


def collect_blocker_constraints(session_id: str, db_path: Path | None = None) -> list[str]:
    """Return the deduped list of constraint strings from the latest challenge.

    Only ``blocking`` severities are surfaced as hard constraints. Lower
    severities stay informational and are not forwarded to the retry
    instruction.
    """
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for po in _latest_challenge_outputs(session_id, db_path):
        try:
            contract = parse_blockers_contract(po.output)
        except ContractError:
            continue
        for blocker in contract.blockers:
            if blocker.severity != "blocking":
                continue
            key = (blocker.id, blocker.constraint)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"[{blocker.id}] {blocker.constraint} (source: {po.agent})")
    return lines


def has_blocking_constraints(session_id: str, db_path: Path | None = None) -> bool:
    """True when the latest challenge attempt contains at least one blocking item."""
    return bool(collect_blocker_constraints(session_id, db_path=db_path))


# ── conception retry ───────────────────────────────────────────────────────────


def can_retry_conception(session_id: str, db_path: Path | None = None) -> bool:
    """Return True when a conception retry is both needed and allowed.

    Needed: the latest challenge produced at least one ``blocking`` item.
    Allowed: the session has not yet consumed its single retry budget
    (``challenge_retry_count < MAX_CHALLENGE_RETRIES``).
    """
    session = get_session(session_id, db_path=db_path)
    if session is None:
        return False
    if session.challenge_retry_count >= MAX_CHALLENGE_RETRIES:
        return False
    return has_blocking_constraints(session_id, db_path=db_path)


def record_conception_retry(session_id: str, db_path: Path | None = None) -> int:
    """Increment the challenge-driven retry counter and return the new value."""
    return increment_challenge_retry_count(session_id, db_path=db_path)


def build_retry_instruction(constraints: list[str]) -> str:
    """Build the ``phase_instruction`` passed to the retry conception run."""
    if not constraints:
        return (
            "This is a retry after the challenge phase raised blocking issues. "
            "Revisit the design and address the concerns surfaced by security, "
            "delivery and architect."
        )
    bullets = "\n".join(f"- {c}" for c in constraints)
    return (
        "This is a retry of the conception phase triggered by blocking issues "
        "raised during the challenge phase. The previous proposal must be "
        "reworked to satisfy the following hard constraints:\n\n"
        f"{bullets}\n\n"
        "Produce a new deliverable that explicitly addresses each constraint "
        "and explains the mitigation chosen."
    )


# ── resume point ───────────────────────────────────────────────────────────────


_NO_RESUME_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_APPROVED, STATUS_QUEUED)


def determine_resume_point(session_id: str, db_path: Path | None = None) -> ResumePoint | None:
    """Return the ``ResumePoint`` for a session, or None when there is nothing to do.

    Rules:

    * Terminal statuses (``done``, ``failed``, ``approved``, ``queued``) →
      nothing to resume.
    * ``review`` → the pipeline finished; human-in-the-loop owns the next
      move. Return None.
    * ``interviewing`` → wait for every pending question to be answered;
      once none remain, the next phase is ``etat_des_lieux`` (cadrage is
      already persisted).
    * ``working`` → re-enter ``current_phase``. If that phase is challenge
      and the latest run surfaced blocking constraints, point to a
      conception retry with the associated instruction.
    * ``draft`` → start from phase 1 (``cadrage``).
    """
    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise ValueError(f"Session not found: {session_id!r}")

    if session.status in _NO_RESUME_STATUSES:
        return None
    if session.status == STATUS_REVIEW:
        return None

    if session.status == STATUS_INTERVIEWING:
        if has_pending_questions(session_id, db_path=db_path):
            raise RuntimeError(
                f"Session {session_id!r} still has unanswered questions. "
                "Answer them via `squad answer` before resuming."
            )
        return ResumePoint(
            session_id=session_id,
            phase=_phase_after(PHASE_CADRAGE),
            reason="questions answered — resuming after cadrage",
        )

    if session.status == STATUS_DRAFT:
        return ResumePoint(
            session_id=session_id,
            phase=PHASE_CADRAGE,
            reason="new session — starting at cadrage",
        )

    if session.status == STATUS_WORKING:
        current = session.current_phase or PHASE_CADRAGE
        # If we crashed mid-challenge and blockers are already persisted,
        # prefer a conception retry when the budget allows it.
        if current == PHASE_CHALLENGE and can_retry_conception(session_id, db_path):
            return ResumePoint(
                session_id=session_id,
                phase=PHASE_CONCEPTION,
                reason="blockers surfaced — retry conception",
                blocker_constraints=collect_blocker_constraints(session_id, db_path),
            )
        return ResumePoint(
            session_id=session_id,
            phase=current,
            reason=f"resuming incomplete phase {current!r}",
        )

    logger.warning(
        "Unexpected session status %r for session %s — nothing to resume",
        session.status,
        session_id,
    )
    return None


def _phase_after(phase: str) -> str:
    """Return the phase immediately after ``phase`` in the canonical order.

    Raises ``ValueError`` when ``phase`` is the last one — callers should
    avoid calling this at the end of the pipeline.
    """
    idx = PHASES.index(phase)
    if idx + 1 >= len(PHASES):
        raise ValueError(f"No phase after {phase!r}")
    return PHASES[idx + 1]
