"""Squad pipeline orchestrator — happy path 1→6 and phase transitions.

The pipeline exposes a small functional API:

* ``run_phase(session_id, phase)`` runs a single phase end-to-end: builds
  the cumulative context, executes the phase agents (serial or parallel),
  persists each deliverable (workspace + ``phase_outputs``), and parses the
  structured contract of flow-driving phases (cadrage pause, synthese).
* ``run_pipeline(session_id)`` iterates the 6 phases in canonical order,
  updating ``sessions.status`` and ``sessions.current_phase`` at each step.

Workflow events (pauses, retries) are driven by the structured contracts
declared in ``squad.phase_contracts`` and by the ``sessions`` row, never by
records written to ``phase_outputs`` (which stays reserved for agent
deliverables).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from squad.constants import (
    PHASE_CADRAGE,
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
    update_session_status,
)
from squad.executor import AgentError, run_agent, run_agents_parallel
from squad.phase_config import PhaseConfig, get_phase_config, iter_phases
from squad.phase_contracts import (
    ContractError,
    QuestionsContract,
    parse_questions_contract,
)
from squad.workspace import write_pending_questions, write_phase_output

logger = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    """Raised when the pipeline cannot continue (critical agent or system failure)."""


class PipelinePaused(Exception):
    """Raised internally when a phase decides to pause the pipeline."""

    def __init__(self, phase: str, reason: str) -> None:
        super().__init__(f"Pipeline paused at {phase}: {reason}")
        self.phase = phase
        self.reason = reason


@dataclass
class PhaseResult:
    """Outcome of a single phase run."""

    phase: str
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
    db_path: Path | None,
) -> None:
    file_path = write_phase_output(session_id, phase, agent, output, db_path=db_path)
    create_phase_output(
        session_id=session_id,
        phase=phase,
        agent=agent,
        output=output,
        file_path=str(file_path),
        db_path=db_path,
    )


def _run_agents(
    cfg: PhaseConfig,
    session_id: str,
    context_sections: list[str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Execute the configured agents for a phase and return (results, errors)."""
    agents = list(cfg.default_agents)
    if not agents:
        return {}, {}

    if cfg.parallel and len(agents) > 1:
        context_map = {agent: context_sections for agent in agents}
        try:
            results = run_agents_parallel(
                agents_list=agents,
                session_id=session_id,
                phase=cfg.phase,
                context_sections_by_agent=context_map,
            )
            return results, {}
        except AgentError as exc:
            # LOT 1 treats any parallel failure as a phase failure; LOT 5
            # introduces per-agent tolerance via is_critical_agent.
            return {}, {"*": str(exc)}

    results: dict[str, str] = {}
    errors: dict[str, str] = {}
    for agent in agents:
        try:
            results[agent] = run_agent(
                agent_name=agent,
                session_id=session_id,
                phase=cfg.phase,
                context_sections=context_sections,
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
) -> PhaseResult:
    """Execute a single phase end-to-end and return its structured result.

    The phase is marked as the current phase on the session row before
    agent execution. Each successful agent output is persisted to the
    workspace and to ``phase_outputs``. For flow-driving phases the
    relevant structured contract is parsed to decide whether the pipeline
    should pause.
    """
    cfg = get_phase_config(phase)

    update_session_status(
        session_id=session_id,
        status=STATUS_WORKING,
        current_phase=phase,
        db_path=db_path,
    )

    context = build_cumulative_context(session_id, phase, db_path=db_path)
    results, errors = _run_agents(cfg, session_id, [context])

    # Fail fast if a critical agent failed
    for agent in cfg.critical_agents:
        if agent in errors or (agent not in results and agent in cfg.default_agents):
            reason = errors.get(agent, errors.get("*", f"agent {agent!r} did not run"))
            raise PipelineError(f"Critical agent {agent!r} failed in phase {phase!r}: {reason}")

    # Persist every successful deliverable before any flow-control decision.
    for agent, output in results.items():
        _persist_output(session_id, phase, agent, output, db_path)

    # LOT 5 will lift parallel failures to per-agent tolerance; here, if a
    # parallel batch failed entirely, surface it as a pipeline error.
    if "*" in errors and not results:
        raise PipelineError(f"Phase {phase!r} failed: {errors['*']}")

    result = PhaseResult(phase=phase, outputs=results, errors=errors)

    # Pause detection: only cadrage drives user-facing questions in LOT 1.
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


def run_pipeline(session_id: str, db_path: Path | None = None) -> None:
    """Execute the 6-phase happy path for a session.

    Drives ``sessions.status`` through ``working`` during phase execution,
    ``interviewing`` when a pause is triggered, ``review`` on successful
    completion, and ``failed`` on any unrecoverable error.
    """
    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise PipelineError(f"Session not found: {session_id!r}")

    try:
        for cfg in iter_phases():
            logger.info("Running phase %s (order %d)", cfg.phase, cfg.order)
            result = run_phase(session_id, cfg.phase, db_path=db_path)
            if result.paused:
                logger.info("Pipeline paused at %s: %s", cfg.phase, result.pause_reason)
                return
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
