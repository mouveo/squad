"""Declarative configuration for the 6 Squad phases.

Each phase has a fixed identity (see ``squad.constants``) and a set of
policies used by the pipeline: ordering, default agents, critical agents,
parallelism, pause support, question budget, retry and skip policies.

This module is intentionally free of side effects and dependencies on the
runtime (DB, filesystem, executor) so it can be imported cheaply by any
layer that needs to reason about phase structure.
"""

from dataclasses import dataclass

from squad.constants import (
    PHASE_BENCHMARK,
    PHASE_CADRAGE,
    PHASE_CHALLENGE,
    PHASE_CONCEPTION,
    PHASE_ETAT_DES_LIEUX,
    PHASE_SYNTHESE,
    PHASES,
)


@dataclass(frozen=True)
class RetryPolicy:
    """Retry policy for a phase.

    ``max_attempts`` counts the initial run as attempt 1. A value of 2 means
    the phase may be executed at most twice. ``retry_on_contract_field`` is
    the structured-contract field whose non-empty presence triggers a retry
    (e.g. ``"blockers"`` on the challenge phase pushing a second conception
    attempt). The pipeline owns the cross-phase wiring; the config only
    declares the policy.
    """

    max_attempts: int = 1
    retry_on_contract_field: str | None = None


@dataclass(frozen=True)
class SkipPolicy:
    """Skip policy for a phase.

    ``skippable`` flags whether the phase may be skipped at all.
    ``skip_when_depth`` lists research-depth values that trigger a skip
    (used by the benchmark phase when the subject profile is ``light``).
    A skip must be explicit and persisted, not silent.
    """

    skippable: bool = False
    skip_when_depth: tuple[str, ...] = ()


@dataclass(frozen=True)
class PhaseConfig:
    """Static configuration for a single phase."""

    phase: str
    order: int
    default_agents: tuple[str, ...]
    critical_agents: tuple[str, ...]
    parallel: bool
    can_pause: bool
    max_questions: int
    retry_policy: RetryPolicy
    skip_policy: SkipPolicy


PHASE_CONFIGS: dict[str, PhaseConfig] = {
    PHASE_CADRAGE: PhaseConfig(
        phase=PHASE_CADRAGE,
        order=1,
        default_agents=("pm",),
        critical_agents=("pm",),
        parallel=False,
        can_pause=True,
        max_questions=5,
        retry_policy=RetryPolicy(max_attempts=1),
        skip_policy=SkipPolicy(),
    ),
    PHASE_ETAT_DES_LIEUX: PhaseConfig(
        phase=PHASE_ETAT_DES_LIEUX,
        order=2,
        default_agents=("ux",),
        critical_agents=(),
        parallel=False,
        can_pause=False,
        max_questions=0,
        retry_policy=RetryPolicy(max_attempts=1),
        skip_policy=SkipPolicy(),
    ),
    PHASE_BENCHMARK: PhaseConfig(
        phase=PHASE_BENCHMARK,
        order=3,
        default_agents=("research",),
        critical_agents=(),
        parallel=False,
        can_pause=False,
        max_questions=0,
        retry_policy=RetryPolicy(max_attempts=1),
        skip_policy=SkipPolicy(skippable=True, skip_when_depth=("light",)),
    ),
    PHASE_CONCEPTION: PhaseConfig(
        phase=PHASE_CONCEPTION,
        order=4,
        default_agents=("ux", "architect"),
        critical_agents=(),
        parallel=True,
        can_pause=False,
        max_questions=0,
        retry_policy=RetryPolicy(max_attempts=2, retry_on_contract_field="blockers"),
        skip_policy=SkipPolicy(),
    ),
    PHASE_CHALLENGE: PhaseConfig(
        phase=PHASE_CHALLENGE,
        # TODO(squad-v2-lot-2): convert security challenge to checklist
        # — once the security/delivery checklist is extracted into the
        # architect prompt, this stays a single-agent phase.
        order=5,
        default_agents=("architect",),
        critical_agents=(),
        parallel=False,
        can_pause=False,
        max_questions=0,
        retry_policy=RetryPolicy(max_attempts=1),
        skip_policy=SkipPolicy(),
    ),
    PHASE_SYNTHESE: PhaseConfig(
        phase=PHASE_SYNTHESE,
        order=6,
        default_agents=("pm",),
        critical_agents=("pm",),
        parallel=False,
        can_pause=False,
        max_questions=0,
        retry_policy=RetryPolicy(max_attempts=1),
        skip_policy=SkipPolicy(),
    ),
}


def get_phase_config(phase: str) -> PhaseConfig:
    """Return the config for a known phase identifier."""
    if phase not in PHASE_CONFIGS:
        raise KeyError(f"Unknown phase: {phase!r}")
    return PHASE_CONFIGS[phase]


def iter_phases() -> list[PhaseConfig]:
    """Return phase configs in canonical order (as in ``constants.PHASES``)."""
    return [PHASE_CONFIGS[p] for p in PHASES]


def is_critical_agent(agent: str, phase: str) -> bool:
    """Return True when an agent failure must fail the phase."""
    cfg = get_phase_config(phase)
    return agent in cfg.critical_agents


def should_skip_phase(phase: str, research_depth: str | None) -> bool:
    """Return True when the phase must be skipped for the given depth profile."""
    cfg = get_phase_config(phase)
    if not cfg.skip_policy.skippable or research_depth is None:
        return False
    return research_depth in cfg.skip_policy.skip_when_depth
