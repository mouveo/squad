"""Squad pipeline orchestrator — happy path, pause, retry and resume.

The pipeline exposes a small functional API:

* ``run_phase(session_id, phase)`` runs a single phase end-to-end: builds
  the cumulative context, executes the phase agents (serial or parallel),
  persists each deliverable (workspace + ``phase_outputs`` with the
  current attempt number), and parses the structured contract of
  flow-driving phases (cadrage pause, synthese).
* ``run_pipeline(session_id, start_phase=None)`` iterates the phases in
  canonical order from ``start_phase`` onward. After the challenge phase
  it may trigger a single conception retry when ``squad.recovery`` says
  blocking constraints were raised.
* ``resume_pipeline(session_id)`` determines a safe resume point via
  ``squad.recovery.determine_resume_point`` and continues from there.

Workflow events (pauses, retries) are driven by the structured contracts
declared in ``squad.phase_contracts`` and by the ``sessions`` row
(``phase_attempts``, ``challenge_retry_count``, ``status``) — never by
records written to ``phase_outputs`` (reserved for agent deliverables).
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
    STATUS_FAILED,
    STATUS_INTERVIEWING,
    STATUS_REVIEW,
    STATUS_WORKING,
)
from squad.context_builder import build_cumulative_context
from squad.db import (
    create_phase_output,
    create_question,
    get_session,
    increment_phase_attempt,
    update_session_status,
)
from squad.executor import AgentError, run_agent, run_agents_tolerant
from squad.phase_config import (
    PhaseConfig,
    get_phase_config,
    is_critical_agent,
    iter_phases,
)
from squad.phase_contracts import (
    ContractError,
    QuestionsContract,
    parse_questions_contract,
)
from squad.recovery import (
    ResumePoint,
    build_retry_instruction,
    can_retry_conception,
    collect_blocker_constraints,
    determine_resume_point,
    record_conception_retry,
)
from squad.workspace import write_pending_questions, write_phase_output

logger = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    """Raised when the pipeline cannot continue (critical agent or system failure)."""


@dataclass
class PhaseResult:
    """Outcome of a single phase run."""

    phase: str
    attempt: int = 1
    outputs: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    paused: bool = False
    pause_reason: str | None = None


# ── internal helpers ───────────────────────────────────────────────────────────


def _persist_output(
    session_id: str,
    phase: str,
    agent: str,
    output: str,
    attempt: int,
    db_path: Path | None,
) -> None:
    file_path = write_phase_output(session_id, phase, agent, output, db_path=db_path)
    create_phase_output(
        session_id=session_id,
        phase=phase,
        agent=agent,
        output=output,
        file_path=str(file_path),
        attempt=attempt,
        db_path=db_path,
    )


def _run_agents(
    cfg: PhaseConfig,
    session_id: str,
    cumulative_context: str,
    phase_instruction: str | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Execute the configured agents for a phase and return (results, errors).

    Never raises on a partial failure; orchestration decides based on
    critical vs. non-critical agents.
    """
    agents = list(cfg.default_agents)
    if not agents:
        return {}, {}

    if cfg.parallel and len(agents) > 1:
        return run_agents_tolerant(
            agents_list=agents,
            session_id=session_id,
            phase=cfg.phase,
            cumulative_context=cumulative_context,
            phase_instruction=phase_instruction,
        )

    results: dict[str, str] = {}
    errors: dict[str, str] = {}
    for agent in agents:
        try:
            results[agent] = run_agent(
                agent_name=agent,
                session_id=session_id,
                phase=cfg.phase,
                cumulative_context=cumulative_context,
                phase_instruction=phase_instruction,
            )
        except AgentError as exc:
            errors[agent] = str(exc)
    return results, errors


def _handle_cadrage_pause(
    session_id: str,
    pm_output: str,
    db_path: Path | None,
) -> QuestionsContract | None:
    """If the pm output carries a paused questions contract, persist it.

    Returns the parsed contract when the phase must pause, None otherwise.
    A missing/invalid contract block is treated as "no pause" (happy path):
    the free-form markdown deliverable is still valid and already persisted.
    """
    try:
        contract = parse_questions_contract(pm_output)
    except ContractError:
        logger.debug("No questions contract found in pm output (happy path).")
        return None

    if not contract.needs_pause or not contract.questions:
        return None

    persisted: list[dict[str, str]] = []
    for q in contract.questions:
        row = create_question(
            session_id=session_id,
            agent="pm",
            phase=PHASE_CADRAGE,
            question=q.question,
            db_path=db_path,
        )
        persisted.append({"id": row.id, "external_id": q.id, "question": q.question})

    write_pending_questions(session_id, persisted, db_path=db_path)
    return contract


# ── public API ─────────────────────────────────────────────────────────────────


def run_phase(
    session_id: str,
    phase: str,
    db_path: Path | None = None,
    *,
    phase_instruction: str | None = None,
) -> PhaseResult:
    """Execute a single phase end-to-end and return its structured result.

    The phase is marked as the current phase on the session row before
    agent execution, and the phase attempt counter is incremented so the
    context builder can later re-inject only the outputs of this attempt.
    Each successful agent output is persisted to the workspace and to
    ``phase_outputs`` with the attempt number. Critical-agent failures
    raise ``PipelineError``; non-critical failures are logged and the
    phase continues with the available results.

    ``phase_instruction`` is forwarded to the executor so orchestration
    code (retry after challenge blockers) can inject additional
    constraints without moving logic into the executor.
    """
    cfg = get_phase_config(phase)
    attempt = increment_phase_attempt(session_id, phase, db_path=db_path)

    update_session_status(
        session_id=session_id,
        status=STATUS_WORKING,
        current_phase=phase,
        db_path=db_path,
    )

    context = build_cumulative_context(session_id, phase, db_path=db_path)
    results, errors = _run_agents(cfg, session_id, context, phase_instruction)

    # Persist every successful deliverable before any flow-control decision.
    for agent, output in results.items():
        _persist_output(session_id, phase, agent, output, attempt, db_path)

    # Critical-agent failures stop the pipeline. Non-critical failures
    # are logged; the phase continues with the partial results.
    for agent, reason in errors.items():
        if is_critical_agent(agent, phase):
            raise PipelineError(f"Critical agent {agent!r} failed in phase {phase!r}: {reason}")
        logger.warning(
            "Non-critical agent %r failed in phase %r (attempt %d): %s",
            agent,
            phase,
            attempt,
            reason,
        )

    # If no critical agent ran at all (e.g. empty results), bail out.
    for crit in cfg.critical_agents:
        if crit not in results:
            raise PipelineError(f"Critical agent {crit!r} missing from phase {phase!r} outputs")

    result = PhaseResult(phase=phase, attempt=attempt, outputs=results, errors=errors)

    # Pause detection: only cadrage drives user-facing questions.
    if cfg.can_pause and "pm" in results:
        contract = _handle_cadrage_pause(session_id, results["pm"], db_path)
        if contract is not None:
            update_session_status(
                session_id=session_id,
                status=STATUS_INTERVIEWING,
                current_phase=phase,
                db_path=db_path,
            )
            result.paused = True
            result.pause_reason = f"{len(contract.questions)} question(s) pending"

    return result


def run_pipeline(
    session_id: str,
    db_path: Path | None = None,
    start_phase: str | None = None,
    *,
    phase_instruction: str | None = None,
) -> None:
    """Execute the phases from ``start_phase`` onward.

    Drives ``sessions.status`` through ``working`` during phase execution,
    ``interviewing`` when a pause is triggered, ``review`` on successful
    completion, and ``failed`` on any unrecoverable error.

    After the challenge phase completes, a single conception retry may be
    triggered when ``squad.recovery.can_retry_conception`` returns True.
    ``phase_instruction`` applies only to the first phase executed — it
    lets callers inject constraints on a resumed retry run.
    """
    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise PipelineError(f"Session not found: {session_id!r}")

    start_idx = 0
    if start_phase is not None:
        if start_phase not in PHASES:
            raise PipelineError(f"Unknown start phase: {start_phase!r}")
        start_idx = PHASES.index(start_phase)

    phase_idx = start_idx
    first_iteration = True
    try:
        while phase_idx < len(PHASES):
            cfg = iter_phases()[phase_idx]
            instruction = phase_instruction if first_iteration else None
            first_iteration = False

            logger.info(
                "Running phase %s (order %d, idx=%d)",
                cfg.phase,
                cfg.order,
                phase_idx,
            )
            result = run_phase(
                session_id,
                cfg.phase,
                db_path=db_path,
                phase_instruction=instruction,
            )
            if result.paused:
                logger.info("Pipeline paused at %s: %s", cfg.phase, result.pause_reason)
                return

            if cfg.phase == PHASE_CHALLENGE and can_retry_conception(session_id, db_path=db_path):
                record_conception_retry(session_id, db_path=db_path)
                constraints = collect_blocker_constraints(session_id, db_path=db_path)
                phase_instruction = build_retry_instruction(constraints)
                first_iteration = True  # next iteration is the retry entry
                phase_idx = PHASES.index(PHASE_CONCEPTION)
                logger.info(
                    "Challenge produced blockers — retrying conception with %d constraint(s)",
                    len(constraints),
                )
                continue

            phase_idx += 1
    except (AgentError, PipelineError):
        update_session_status(
            session_id=session_id,
            status=STATUS_FAILED,
            db_path=db_path,
        )
        raise

    update_session_status(
        session_id=session_id,
        status=STATUS_REVIEW,
        current_phase=PHASES[-1],
        db_path=db_path,
    )


def resume_pipeline(session_id: str, db_path: Path | None = None) -> ResumePoint | None:
    """Resume a session from the point computed by ``determine_resume_point``.

    Returns the ``ResumePoint`` actually used, or None when the session is
    already finished / in a state that does not warrant a resume.
    """
    resume_point = determine_resume_point(session_id, db_path=db_path)
    if resume_point is None:
        return None

    instruction: str | None = None
    if resume_point.phase == PHASE_CONCEPTION and resume_point.blocker_constraints:
        record_conception_retry(session_id, db_path=db_path)
        instruction = build_retry_instruction(resume_point.blocker_constraints)

    run_pipeline(
        session_id,
        db_path=db_path,
        start_phase=resume_point.phase,
        phase_instruction=instruction,
    )
    return resume_point
