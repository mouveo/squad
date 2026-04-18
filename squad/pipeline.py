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
from datetime import datetime
from pathlib import Path
from typing import Callable

from types import SimpleNamespace

from squad.constants import (
    PHASE_BENCHMARK,
    PHASE_CADRAGE,
    PHASE_CHALLENGE,
    PHASE_CONCEPTION,
    PHASE_IDEATION,
    PHASE_SYNTHESE,
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
    list_pending_questions,
    list_plans,
    set_selected_angle,
    update_input_richness,
    update_session_failure_reason,
    update_session_status,
)
from squad.input_richness import score_input_richness
from squad.executor import AgentError, run_agent, run_agents_tolerant
from squad.forge_format import ForgeFormatError
from squad.models import (
    EVENT_FAILED,
    EVENT_INTERVIEWING,
    EVENT_REVIEW,
    EVENT_WORKING,
    PipelineEvent,
)
from squad.phase_config import (
    PhaseConfig,
    get_phase_config,
    is_critical_agent,
    iter_phases,
    should_skip_phase,
)
from squad.research import run_research
from squad.subject_detector import detect_and_persist

try:  # LOT 3 ships squad.ideation; until then the pipeline falls back gracefully.
    from squad.ideation import run_ideation as _run_ideation
except ImportError:  # pragma: no cover — exercised indirectly in tests
    _run_ideation = None
from squad.phase_contracts import (
    ContractError,
    QuestionsContract,
    parse_questions_contract,
)
from squad.plan_generator import (
    InvalidSynthesisContractError,
    copy_plans_to_project,
    generate_plans_from_session,
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

# Type alias for the optional Slack/observer callback
EventCallback = Callable[[PipelineEvent], None]

logger = logging.getLogger(__name__)

# Agents that run in the target project's filesystem (active exploration).
# Their Claude subprocess cwd is routed to ``session.project_path`` so
# Glob/LS/Grep/Read resolve against the real project rather than the
# Squad workspace.
_AGENTS_WITH_PROJECT_CWD: set[str] = {"ux", "architect"}

# Phases whose agents reason on the persisted subject profile
# (research depth for the benchmark, agent composition for the rest).
# Entering any of these requires a classified session so the skip
# policy has been populated and the research budget is known.
_PROFILE_DEPENDENT_PHASES: set[str] = {
    PHASE_BENCHMARK,
    PHASE_CONCEPTION,
    PHASE_CHALLENGE,
    PHASE_SYNTHESE,
}

# Phase instruction sent when the synthese phase must be rerun because
# its first attempt produced an unparseable synthesis contract. The
# message is explicit about the only acceptable shape so the agent
# re-emits the structured block the plan generator needs.
_SYNTHESE_CONTRACT_RETRY_INSTRUCTION = (
    "Ta première synthèse n'a pas produit de contrat JSON exploitable. "
    "Reformate STRICTEMENT ta réponse : le document markdown reste le "
    "même (résumés, décisions, plans), mais il doit se terminer par UN "
    "seul bloc ```json``` contenant exactement les trois clés "
    "`decision_summary` (string), `open_questions` (array of strings) "
    "et `plan_inputs` (array of strings). Pas de texte après le bloc "
    "JSON. Sans ce bloc, la génération des plans Forge échoue."
)


def _ensure_subject_profile(session, db_path: Path | None):
    """Guarantee that ``session`` has ``subject_type`` and ``research_depth`` set.

    When either field is missing, delegate to
    :func:`squad.subject_detector.detect_and_persist` (which also marks
    the benchmark as skipped when the detected depth is ``light``) and
    reload the session so callers see the persisted profile and the
    populated ``skipped_phases`` mapping.
    """
    if session.subject_type and session.research_depth:
        return session
    logger.info(
        "Session %s entering pipeline without subject profile — classifying",
        session.id,
    )
    detect_and_persist(session.id, use_llm=True, db_path=db_path)
    refreshed = get_session(session.id, db_path=db_path)
    return refreshed or session


def _should_skip_phase(session, phase: str) -> bool:
    """True when ``phase`` must be skipped for ``session``.

    Honors the persisted ``skipped_phases`` mapping (set explicitly by
    ``detect_and_persist`` for ``light`` sessions) AND the declarative
    ``phase_config.should_skip_phase`` policy so manual test setups
    without a persisted skip still land on the right branch.
    """
    if phase in (session.skipped_phases or {}):
        return True
    return should_skip_phase(phase, session.research_depth)


def _resolve_agent_cwd(session, agent_name: str) -> str | None:
    """Return ``session.project_path`` when the agent needs active exploration.

    Returns ``None`` when the agent is not in ``_AGENTS_WITH_PROJECT_CWD``,
    when ``session.project_path`` is unset, or when the path does not
    exist on disk (logged as a warning so operators can notice the drift).
    """
    if agent_name not in _AGENTS_WITH_PROJECT_CWD:
        return None
    project_path = getattr(session, "project_path", None)
    if not project_path:
        return None
    if not Path(project_path).exists():
        logger.warning(
            "Agent %r requested active exploration but project_path %r does not exist; "
            "falling back to cwd=None",
            agent_name,
            project_path,
        )
        return None
    return project_path


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
    session,
    cumulative_context: str,
    phase_instruction: str | None,
    db_path: Path | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Execute the configured agents for a phase and return (results, errors).

    Never raises on a partial failure; orchestration decides based on
    critical vs. non-critical agents. A per-agent ``cwd`` is routed
    through ``_resolve_agent_cwd`` so active-exploration agents run
    inside ``session.project_path``.
    """
    agents = list(cfg.default_agents)
    if not agents:
        return {}, {}

    session_id = session.id

    if cfg.parallel and len(agents) > 1:
        cwd_by_agent = {agent: _resolve_agent_cwd(session, agent) for agent in agents}
        return run_agents_tolerant(
            agents_list=agents,
            session_id=session_id,
            phase=cfg.phase,
            cumulative_context=cumulative_context,
            phase_instruction=phase_instruction,
            cwd_by_agent=cwd_by_agent,
        )

    results: dict[str, str] = {}
    errors: dict[str, str] = {}
    for agent in agents:
        try:
            # The benchmark phase uses the dedicated research service rather
            # than a generic .md agent definition (there is no agents/research.md).
            if cfg.phase == PHASE_BENCHMARK and agent == "research":
                report = run_research(
                    session_id=session_id,
                    extra_context=cumulative_context,
                    db_path=db_path,
                )
                results[agent] = report.content
                continue

            # The ideation phase is handled directly in ``run_phase`` so
            # the strategy resolution can consult the typed
            # ``IdeationResult`` (angles + strategy block). ``_run_agents``
            # therefore never sees the ``ideation`` agent.
            if cfg.phase == PHASE_IDEATION and agent == "ideation":
                continue

            results[agent] = run_agent(
                agent_name=agent,
                session_id=session_id,
                phase=cfg.phase,
                cumulative_context=cumulative_context,
                phase_instruction=phase_instruction,
                cwd=_resolve_agent_cwd(session, agent),
            )
        except AgentError as exc:
            errors[agent] = str(exc)
        except Exception as exc:  # noqa: BLE001
            errors[agent] = f"{type(exc).__name__}: {exc}"
    return results, errors


def _run_ideation_step(
    *,
    session,
    cumulative_context: str,
    db_path: Path | None,
):
    """Dispatch the ideation phase to ``squad.ideation.run_ideation``.

    Returns the full ``IdeationResult`` (or a SimpleNamespace mirroring
    its shape when the service is unavailable / raises). Always carries
    ``content``, ``angles`` and ``strategy`` so the strategy resolver
    downstream can decide whether to ``auto_pick`` or pause for review.
    The ideation phase is non-critical: any failure falls back to a
    trivial synthetic result so the pipeline still proceeds.
    """
    if _run_ideation is None:
        logger.warning("squad.ideation not available — using trivial ideation fallback")
        return _trivial_ideation_result(session)
    try:
        return _run_ideation(
            session_id=session.id,
            extra_context=cumulative_context,
            db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "run_ideation failed for session %s: %s — using trivial fallback",
            session.id,
            exc,
        )
        return _trivial_ideation_result(session)


def _trivial_ideation_output(session) -> str:
    """Markdown body of the synthetic fallback when ideation can't run."""
    idea = getattr(session, "idea", "") or ""
    return (
        "# Ideation — fallback\n\n"
        "## Angle 1 — baseline\n"
        f"- Segment: TBD\n"
        f"- Value prop: {idea[:200]}\n"
        "- Approche: direct implementation of the submitted idea\n"
        "- Divergence: none (fallback angle)\n"
    )


def _trivial_ideation_result(session):
    """Synthetic IdeationResult-shaped namespace used as a fallback.

    Carries an empty ``angles`` list (downstream resolver will treat as
    "fallback to angle 0") and a strategy block forcing ``auto_pick`` so
    the pipeline never stalls on a missing ideation service.
    """
    return SimpleNamespace(
        content=_trivial_ideation_output(session),
        angles=[],
        strategy={
            "strategy": "auto_pick",
            "best_angle_idx": 0,
            "divergence_score": "medium",
            "rationale": "ideation fallback (service unavailable)",
        },
    )


# ── ideation strategy resolution (LOT 5) ──────────────────────────────────────


@dataclass
class IdeationDecision:
    """Outcome of ``_resolve_ideation_strategy``.

    ``auto_pick`` True means the pipeline should persist
    ``selected_angle_idx`` and continue to ``benchmark``. False means
    the session must pause (``status=interviewing``,
    ``current_phase=ideation``) and wait for the reviewer to pick an
    angle through the LOT 6 review flow.
    """

    auto_pick: bool
    selected_idx: int | None
    reason: str


_VALID_IDEATION_STRATEGIES: tuple[str, ...] = ("auto_pick", "ask_user")


def _resolve_ideation_strategy(session, ideation_result, db_path: Path | None) -> IdeationDecision:
    """Decide what the pipeline should do after ``run_ideation`` returned.

    Rules, in order:

    1. If a Slack thread is not exploitable for this session, force
       ``auto_pick`` — there is no channel to ask the user on.
    2. If ``session.input_richness == "rich"``, force ``auto_pick`` —
       the agent already has plenty to work with, save the round-trip.
    3. If ``session.selected_angle_idx`` is already set, skip the
       decision and continue (keep the previous selection).
    4. If the agent recommended ``ask_user`` AND the session is sparse
       AND a Slack thread exists, pause for human review.
    5. On any malformed strategy / out-of-range index / missing angles,
       log a warning and fall back to angle 0 with ``auto_pick``.
    """
    angles = list(getattr(ideation_result, "angles", None) or [])
    strategy = getattr(ideation_result, "strategy", None) or {}

    # Rule 3 — already chosen: skip the decision entirely.
    if session.selected_angle_idx is not None:
        return IdeationDecision(
            auto_pick=True,
            selected_idx=int(session.selected_angle_idx),
            reason="angle already selected — continuing to benchmark",
        )

    # Rule 5 — guard on the structural sanity of the strategy block.
    requested_strategy = strategy.get("strategy")
    requested_idx = strategy.get("best_angle_idx")
    n_angles = len(angles)
    valid_strategy = requested_strategy in _VALID_IDEATION_STRATEGIES
    valid_idx = (
        isinstance(requested_idx, int)
        and not isinstance(requested_idx, bool)
        and 0 <= requested_idx < n_angles
    ) if n_angles > 0 else False
    if not valid_strategy or not valid_idx:
        logger.warning(
            "Ideation strategy malformed or idx out of range for session %s "
            "(strategy=%r, idx=%r, n_angles=%d) — falling back to angle 0",
            session.id,
            requested_strategy,
            requested_idx,
            n_angles,
        )
        return IdeationDecision(
            auto_pick=True,
            selected_idx=0,
            reason="strategy malformed — fallback to angle 0",
        )

    has_slack_thread = bool(session.slack_channel and session.slack_thread_ts)

    # Rule 1 — no Slack thread to ask on: force auto_pick.
    if not has_slack_thread:
        return IdeationDecision(
            auto_pick=True,
            selected_idx=requested_idx,
            reason="no Slack thread — auto_pick forced",
        )

    # Rule 2 — input is rich: skip the user-review round-trip.
    if session.input_richness == "rich":
        return IdeationDecision(
            auto_pick=True,
            selected_idx=requested_idx,
            reason="input_richness=rich — auto_pick forced",
        )

    # Rule 4 — ask_user + sparse + Slack thread → pause.
    if requested_strategy == "ask_user":
        return IdeationDecision(
            auto_pick=False,
            selected_idx=None,
            reason="ask_user + sparse + Slack thread — pausing for angle selection",
        )

    # Default (auto_pick from agent).
    return IdeationDecision(
        auto_pick=True,
        selected_idx=requested_idx,
        reason="auto_pick from agent strategy",
    )


def _run_ideation_phase(
    *,
    session,
    attempt: int,
    cumulative_context: str,
    db_path: Path | None,
) -> "PhaseResult":
    """Run the ideation phase end-to-end and return a ``PhaseResult``.

    Persists the agent markdown, applies ``_resolve_ideation_strategy``
    to the typed ``IdeationResult`` and either persists
    ``selected_angle_idx`` (auto_pick) or pauses the session
    (``status=interviewing``, ``current_phase=ideation``) so the LOT 6
    review flow can collect the angle pick from the user.
    """
    ideation_result = _run_ideation_step(
        session=session,
        cumulative_context=cumulative_context,
        db_path=db_path,
    )
    content = getattr(ideation_result, "content", None) or _trivial_ideation_output(session)
    _persist_output(session.id, PHASE_IDEATION, "ideation", content, attempt, db_path)

    decision = _resolve_ideation_strategy(session, ideation_result, db_path)
    logger.info(
        "Ideation strategy for session %s: auto_pick=%s, idx=%s, reason=%s",
        session.id,
        decision.auto_pick,
        decision.selected_idx,
        decision.reason,
    )

    result = PhaseResult(
        phase=PHASE_IDEATION,
        attempt=attempt,
        outputs={"ideation": content},
        errors={},
    )
    if decision.auto_pick:
        if decision.selected_idx is not None:
            set_selected_angle(db_path, session.id, decision.selected_idx)
        return result

    # Pause for user-driven angle selection.
    update_session_status(
        session_id=session.id,
        status=STATUS_INTERVIEWING,
        current_phase=PHASE_IDEATION,
        db_path=db_path,
    )
    result.paused = True
    result.pause_reason = decision.reason
    return result


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

    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise PipelineError(f"Session not found: {session_id!r}")

    # Ensure the subject profile is known before any phase that reasons
    # on it, so downstream branches (research budget, agent composition)
    # never see a missing ``research_depth``.
    if phase in _PROFILE_DEPENDENT_PHASES:
        session = _ensure_subject_profile(session, db_path)

    # Honor the skip policy before mutating session state: this mirrors
    # what ``run_pipeline`` does in its loop so a direct ``run_phase``
    # call on a ``light`` benchmark behaves the same way.
    if _should_skip_phase(session, phase):
        logger.info(
            "Skipping phase %s for session %s (skip policy)", phase, session_id
        )
        return PhaseResult(phase=phase, attempt=0, outputs={}, errors={})

    attempt = increment_phase_attempt(session_id, phase, db_path=db_path)

    update_session_status(
        session_id=session_id,
        status=STATUS_WORKING,
        current_phase=phase,
        db_path=db_path,
    )

    # Refresh after status updates so downstream code sees the latest row.
    session = get_session(session_id, db_path=db_path) or session

    # Recompute input richness on every entry into ideation so that
    # attachments dropped after session creation but before this phase
    # are taken into account. The persisted value gates the strategy
    # decision below and the benchmark prompt in later lots.
    if phase == PHASE_IDEATION:
        try:
            label = score_input_richness(session_id, db_path=db_path)
            update_input_richness(db_path, session_id, label)
            session = get_session(session_id, db_path=db_path) or session
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Input-richness scoring failed for session %s: %s — phase continues",
                session_id,
                exc,
            )

    context = build_cumulative_context(session_id, phase, db_path=db_path)

    # Ideation runs through a dedicated path: the strategy block returned
    # by ``run_ideation`` drives the auto_pick / pause decision below,
    # so we cannot fold it into the generic ``_run_agents`` loop.
    if phase == PHASE_IDEATION:
        return _run_ideation_phase(
            session=session,
            attempt=attempt,
            cumulative_context=context,
            db_path=db_path,
        )

    results, errors = _run_agents(
        cfg, session, context, phase_instruction, db_path=db_path
    )

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


def _emit_event(
    callback: EventCallback | None,
    *,
    session_id: str,
    started_at: datetime,
    type: str,
    phase: str | None = None,
    pending_questions: int = 0,
    plans_count: int = 0,
    failure_reason: str | None = None,
) -> None:
    """Send a pipeline event to ``callback`` and swallow observer errors.

    Observer exceptions must never take down the pipeline — they are
    logged and then suppressed so a misbehaving Slack sink cannot turn
    a successful session into a failed one.
    """
    if callback is None:
        return
    now = datetime.utcnow()
    elapsed = max(0.0, (now - started_at).total_seconds())
    event = PipelineEvent(
        type=type,
        session_id=session_id,
        timestamp_utc=now,
        elapsed_seconds=elapsed,
        phase=phase,
        pending_questions=pending_questions,
        plans_count=plans_count,
        failure_reason=failure_reason,
    )
    try:
        callback(event)
    except Exception:  # noqa: BLE001
        logger.exception("Pipeline event callback raised for event %r", type)


def run_pipeline(
    session_id: str,
    db_path: Path | None = None,
    start_phase: str | None = None,
    *,
    phase_instruction: str | None = None,
    event_callback: EventCallback | None = None,
) -> None:
    """Execute the phases from ``start_phase`` onward.

    Drives ``sessions.status`` through ``working`` during phase execution,
    ``interviewing`` when a pause is triggered, ``review`` on successful
    completion, and ``failed`` on any unrecoverable error.

    After the challenge phase completes, a single conception retry may be
    triggered when ``squad.recovery.can_retry_conception`` returns True.
    ``phase_instruction`` applies only to the first phase executed — it
    lets callers inject constraints on a resumed retry run.

    ``event_callback`` is an optional observer invoked on every
    ``working/<phase>``, ``interviewing``, ``review`` and ``failed``
    transition. Observer errors are logged and suppressed so a faulty
    sink never affects pipeline outcome.
    """
    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise PipelineError(f"Session not found: {session_id!r}")

    started_at = session.created_at or datetime.utcnow()

    # Classify once at pipeline entry so the skip policy (e.g. light →
    # skip benchmark) is persisted on the session row before the loop
    # looks at it. Sessions that already carry a profile (CLI, Slack,
    # resume) are a no-op here.
    session = _ensure_subject_profile(session, db_path)

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

            # Refresh the session so skips persisted mid-pipeline
            # (e.g. by a previous classification) are visible here.
            session = get_session(session_id, db_path=db_path) or session
            if _should_skip_phase(session, cfg.phase):
                logger.info(
                    "Skipping phase %s for session %s (skip policy)",
                    cfg.phase,
                    session_id,
                )
                phase_idx += 1
                continue

            instruction = phase_instruction if first_iteration else None
            first_iteration = False

            logger.info(
                "Running phase %s (order %d, idx=%d)",
                cfg.phase,
                cfg.order,
                phase_idx,
            )
            _emit_event(
                event_callback,
                session_id=session_id,
                started_at=started_at,
                type=EVENT_WORKING,
                phase=cfg.phase,
            )
            result = run_phase(
                session_id,
                cfg.phase,
                db_path=db_path,
                phase_instruction=instruction,
            )
            if result.paused:
                logger.info("Pipeline paused at %s: %s", cfg.phase, result.pause_reason)
                pending = list_pending_questions(session_id, db_path=db_path)
                _emit_event(
                    event_callback,
                    session_id=session_id,
                    started_at=started_at,
                    type=EVENT_INTERVIEWING,
                    phase=cfg.phase,
                    pending_questions=len(pending),
                )
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
    except (AgentError, PipelineError) as exc:
        reason = str(exc) or type(exc).__name__
        update_session_failure_reason(session_id, reason, db_path=db_path)
        update_session_status(
            session_id=session_id,
            status=STATUS_FAILED,
            db_path=db_path,
        )
        _emit_event(
            event_callback,
            session_id=session_id,
            started_at=started_at,
            type=EVENT_FAILED,
            failure_reason=reason,
        )
        raise

    # After the six phases complete, generate Forge plans from the
    # synthesis contract, validate them and copy them to the target
    # project. A malformed synthesis contract is recoverable: we rerun
    # the synthese phase exactly once with a strict reformat instruction
    # before giving up. Any other failure is terminal.
    try:
        _generate_and_copy_plans(session_id, db_path=db_path)
    except InvalidSynthesisContractError as exc:
        logger.warning(
            "Synthesis contract invalid for session %s (%s) — retrying synthese once",
            session_id,
            exc,
        )
        try:
            run_phase(
                session_id,
                PHASE_SYNTHESE,
                db_path=db_path,
                phase_instruction=_SYNTHESE_CONTRACT_RETRY_INSTRUCTION,
            )
            _generate_and_copy_plans(session_id, db_path=db_path)
        except InvalidSynthesisContractError as exc2:
            path = exc2.last_output_path or exc.last_output_path or "(unknown)"
            reason = (
                f"Plan generation failed after synthese retry: {exc2} "
                f"(last synthese file: {path})"
            )
            logger.error(
                "Plan generation failed after synthese retry for session %s: %s",
                session_id,
                exc2,
            )
            update_session_failure_reason(session_id, reason, db_path=db_path)
            update_session_status(
                session_id=session_id,
                status=STATUS_FAILED,
                db_path=db_path,
            )
            _emit_event(
                event_callback,
                session_id=session_id,
                started_at=started_at,
                type=EVENT_FAILED,
                failure_reason=reason,
            )
            raise PipelineError(reason) from exc2
        except (AgentError, PipelineError, ValueError, RuntimeError, ForgeFormatError) as exc2:
            reason = f"Plan generation failed after synthese retry: {exc2}"
            logger.error(
                "Plan generation failed after synthese retry for session %s: %s",
                session_id,
                exc2,
            )
            update_session_failure_reason(session_id, reason, db_path=db_path)
            update_session_status(
                session_id=session_id,
                status=STATUS_FAILED,
                db_path=db_path,
            )
            _emit_event(
                event_callback,
                session_id=session_id,
                started_at=started_at,
                type=EVENT_FAILED,
                failure_reason=reason,
            )
            raise PipelineError(reason) from exc2
    except (ValueError, RuntimeError, ForgeFormatError) as exc:
        reason = f"Plan generation failed: {exc}"
        logger.error("Plan generation failed for session %s: %s", session_id, exc)
        update_session_failure_reason(session_id, reason, db_path=db_path)
        update_session_status(
            session_id=session_id,
            status=STATUS_FAILED,
            db_path=db_path,
        )
        _emit_event(
            event_callback,
            session_id=session_id,
            started_at=started_at,
            type=EVENT_FAILED,
            failure_reason=reason,
        )
        raise PipelineError(reason) from exc

    update_session_status(
        session_id=session_id,
        status=STATUS_REVIEW,
        current_phase=PHASES[-1],
        db_path=db_path,
    )
    plans = list_plans(session_id, db_path=db_path)
    _emit_event(
        event_callback,
        session_id=session_id,
        started_at=started_at,
        type=EVENT_REVIEW,
        plans_count=len(plans),
    )


def _generate_and_copy_plans(session_id: str, db_path: Path | None = None) -> None:
    """Run the plan generator and copy the resulting files into the project.

    Split from ``run_pipeline`` so tests can patch a single symbol
    (``squad.pipeline._generate_and_copy_plans``) when the LLM is not
    available, and so the failure path stays localised.
    """
    drafts = generate_plans_from_session(session_id, db_path=db_path)
    logger.info("Plan generation produced %d plan(s) for session %s", len(drafts), session_id)
    copied = copy_plans_to_project(session_id, db_path=db_path)
    logger.info("Copied %d plan file(s) into the target project", len(copied))


def resume_pipeline(
    session_id: str,
    db_path: Path | None = None,
    *,
    event_callback: EventCallback | None = None,
) -> ResumePoint | None:
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
        event_callback=event_callback,
    )
    return resume_point
