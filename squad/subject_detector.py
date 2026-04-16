"""Subject detection — classifies the session subject and selects agents.

Combines two inputs:

* **Local inspection** of the target project (``CLAUDE.md``, ``pyproject.toml``,
  ``requirements*.txt``, ``package.json``, ``composer.json``) — gives the
  detector deterministic signals (B2B, AI, onboarding, pricing, integration,
  data, security, growth, sales, customer-success).
* **Claude classification** via ``run_task_json`` with the light model
  already declared in ``squad.executor``. Claude returns a JSON object
  validated against the ``SubjectProfile`` contract; on failure the
  detector falls back to the deterministic heuristic so the pipeline can
  always proceed.

The detector persists the result once on the ``sessions`` row. Resume
paths must call ``detect_and_persist(..., force=False)`` so the subject
is never reclassified mid-flight.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from squad.constants import (
    PHASE_BENCHMARK,
    PHASE_CHALLENGE,
    PHASE_CONCEPTION,
    PHASE_ETAT_DES_LIEUX,
)
from squad.db import (
    get_session,
    mark_phase_skipped,
    update_session_profile,
)

# ``_MODEL_LIGHT`` is the single declaration of the light model id;
# importing it keeps the detector aligned with executor's source of truth.
from squad.executor import _MODEL_LIGHT, AgentError, run_task_json
from squad.models import (
    RESEARCH_DEPTH_DEEP,
    RESEARCH_DEPTH_LIGHT,
    RESEARCH_DEPTH_NORMAL,
    RESEARCH_DEPTHS,
    SubjectProfile,
)

logger = logging.getLogger(__name__)

# Public alias for the light model id (tests and downstream code use this).
MODEL_LIGHT = _MODEL_LIGHT

# Maximum characters kept per manifest when building the classification prompt
_MANIFEST_SNIPPET_CHARS = 2_000

# Manifests scanned in order — stop missing ones silently.
_MANIFESTS: tuple[str, ...] = (
    "CLAUDE.md",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "package.json",
    "composer.json",
)

# Canonical signal identifiers
SIGNAL_B2B = "b2b"
SIGNAL_AI = "ai"
SIGNAL_ONBOARDING = "onboarding"
SIGNAL_PRICING = "pricing"
SIGNAL_INTEGRATION = "integration"
SIGNAL_DATA = "data"
SIGNAL_SECURITY = "security"
SIGNAL_GROWTH = "growth"
SIGNAL_SALES = "sales"
SIGNAL_CUSTOMER_SUCCESS = "customer_success"

# Keyword map used by derive_signals. Case-insensitive substring matching.
_KEYWORDS: dict[str, tuple[str, ...]] = {
    SIGNAL_B2B: ("b2b", "saas", "enterprise", "compliance", "multi-tenant", "tenant"),
    SIGNAL_AI: (
        "ai",
        "llm",
        "ml",
        "gpt",
        "claude",
        "openai",
        "anthropic",
        "embedding",
        "rag",
        "agent",
        "prompt",
    ),
    SIGNAL_ONBOARDING: ("onboarding", "signup", "sign-up", "activation", "first-run"),
    SIGNAL_PRICING: (
        "pricing",
        "billing",
        "checkout",
        "stripe",
        "paywall",
        "monetization",
        "subscription",
        "payment",
    ),
    SIGNAL_INTEGRATION: (
        "integration",
        "webhook",
        "oauth",
        "connector",
        "api key",
        "third-party",
        "third party",
    ),
    SIGNAL_DATA: (
        "data",
        "analytics",
        "etl",
        "pipeline",
        "metrics",
        "dashboard",
        "warehouse",
        "kpi",
    ),
    SIGNAL_SECURITY: (
        "security",
        "gdpr",
        "rgpd",
        "encryption",
        "pii",
        "auth",
        "authentication",
        "authorization",
        "sso",
    ),
    SIGNAL_GROWTH: (
        "growth",
        "acquisition",
        "retention",
        "funnel",
        "conversion",
        "marketing",
        "referral",
        "virality",
    ),
    SIGNAL_SALES: ("sales", "lead", "crm", "prospect", "pipeline sales", "outbound"),
    SIGNAL_CUSTOMER_SUCCESS: ("support", "success", "churn", "ticket", "helpdesk", "nps"),
}


# ── local inspection ───────────────────────────────────────────────────────────


def inspect_project(project_path: str | Path) -> dict[str, str]:
    """Read relevant manifests from ``project_path`` and return {name: snippet}.

    Files that do not exist are silently skipped. Long files are truncated
    to ``_MANIFEST_SNIPPET_CHARS`` so the concatenation stays compact enough
    to embed in the classification prompt.
    """
    root = Path(project_path)
    snippets: dict[str, str] = {}
    if not root.exists() or not root.is_dir():
        return snippets

    for name in _MANIFESTS:
        path = root / name
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(text) > _MANIFEST_SNIPPET_CHARS:
            text = text[:_MANIFEST_SNIPPET_CHARS] + "\n…[truncated]"
        snippets[name] = text

    # Also catch requirements-*.txt that aren't exactly requirements-dev.txt
    for extra in root.glob("requirements-*.txt"):
        if extra.name in snippets:
            continue
        try:
            text = extra.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if len(text) > _MANIFEST_SNIPPET_CHARS:
            text = text[:_MANIFEST_SNIPPET_CHARS] + "\n…[truncated]"
        snippets[extra.name] = text

    return snippets


# ── deterministic signals ──────────────────────────────────────────────────────


def derive_signals(idea: str, inspection: dict[str, str]) -> set[str]:
    """Return the set of signals detected in idea + manifest snippets."""
    haystack = (idea + "\n" + "\n".join(inspection.values())).lower()
    signals: set[str] = set()
    for signal, keywords in _KEYWORDS.items():
        for kw in keywords:
            if kw in haystack:
                signals.add(signal)
                break
    return signals


def default_agents_for_signals(signals: set[str]) -> dict[str, list[str]]:
    """Pick the variable-agent sets per phase from the detected signals.

    The detector only overrides phases where signals matter. Phases whose
    agents are fixed (cadrage/pm, synthese/pm, benchmark/research) are not
    returned here — the pipeline falls back to ``phase_config`` defaults.
    Challenge is always ``security + delivery + architect`` and is returned
    explicitly to document the choice.
    """
    etat: list[str] = ["ux"]
    if {SIGNAL_SALES, SIGNAL_B2B} & signals:
        etat.append("sales")
    if {SIGNAL_DATA} & signals:
        etat.append("data")
    if {SIGNAL_CUSTOMER_SUCCESS, SIGNAL_ONBOARDING} & signals:
        etat.append("customer-success")

    conception: list[str] = ["ux", "architect"]
    if SIGNAL_AI in signals:
        conception.append("ai-lead")
    if {SIGNAL_GROWTH, SIGNAL_PRICING, SIGNAL_ONBOARDING} & signals:
        conception.append("growth")

    challenge = ["security", "delivery", "architect"]

    return {
        PHASE_ETAT_DES_LIEUX: _dedupe(etat),
        PHASE_CONCEPTION: _dedupe(conception),
        PHASE_CHALLENGE: challenge,
    }


def default_depth_for_signals(signals: set[str]) -> str:
    """Pick the research depth from signals.

    - ``deep`` when the subject spans several strategic signals (b2b + ai,
      integration + pricing, etc.) — broad positioning work warranted.
    - ``light`` when the signals are internal-tooling-only (none of the
      market-facing ones).
    - ``normal`` otherwise.
    """
    market_signals = {
        SIGNAL_B2B,
        SIGNAL_AI,
        SIGNAL_PRICING,
        SIGNAL_GROWTH,
        SIGNAL_INTEGRATION,
        SIGNAL_SALES,
    }
    hits = signals & market_signals
    if len(hits) >= 3:
        return RESEARCH_DEPTH_DEEP
    if not hits:
        return RESEARCH_DEPTH_LIGHT
    return RESEARCH_DEPTH_NORMAL


def default_subject_type(signals: set[str]) -> str:
    """Label the session for downstream humans — not used for flow control."""
    if SIGNAL_AI in signals and SIGNAL_B2B in signals:
        return "b2b_ai_product"
    if SIGNAL_AI in signals:
        return "ai_product"
    if SIGNAL_B2B in signals:
        return "b2b_saas"
    if SIGNAL_PRICING in signals or SIGNAL_GROWTH in signals:
        return "consumer_product"
    return "generic"


def heuristic_profile(idea: str, inspection: dict[str, str]) -> SubjectProfile:
    """Return a deterministic profile from idea + inspection, without Claude."""
    signals = derive_signals(idea, inspection)
    return SubjectProfile(
        subject_type=default_subject_type(signals),
        research_depth=default_depth_for_signals(signals),
        agents_by_phase=default_agents_for_signals(signals),
        rationale=f"heuristic: signals={sorted(signals)}",
    )


# ── Claude classification ──────────────────────────────────────────────────────


def _build_classification_prompt(idea: str, inspection: dict[str, str], signals: set[str]) -> str:
    manifests = (
        "\n\n".join(f"### {name}\n```\n{content}\n```" for name, content in inspection.items())
        or "(no manifests found)"
    )
    return (
        "You are classifying a product idea to pick research depth and agent "
        "composition for a multi-agent design pipeline.\n\n"
        f"## Idea\n{idea}\n\n"
        f"## Deterministic signals\n{sorted(signals) or '(none)'}\n\n"
        f"## Local manifests\n{manifests}\n\n"
        "Return a single JSON object with exactly these fields:\n"
        '- "subject_type": short label (snake_case, e.g. b2b_saas, ai_product, '
        "consumer_product, internal_tool).\n"
        '- "research_depth": one of "light", "normal", "deep".\n'
        '- "agents_by_phase": object mapping phase id to an array of agent '
        "slugs. Phases: etat_des_lieux (customer-success, data, sales, ux), "
        "conception (ai-lead, architect, growth, ux), challenge "
        "(security, delivery, architect). Only list agents that are truly "
        "relevant to the idea.\n"
        "Respond with JSON only. No prose, no markdown fence."
    )


def classify_with_claude(
    idea: str,
    inspection: dict[str, str],
    signals: set[str],
    model: str = _MODEL_LIGHT,
) -> dict:
    """Call Claude for a subject classification and return the raw dict.

    Raises ``AgentError`` on Claude failure and ``ValueError`` on invalid
    JSON. Validation against ``SubjectProfile`` happens in ``detect_subject``.
    """
    prompt = _build_classification_prompt(idea, inspection, signals)
    return run_task_json(prompt, model=model)


# ── public entry points ────────────────────────────────────────────────────────


def _dedupe(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _coerce_profile(
    data: dict,
    fallback: SubjectProfile,
) -> SubjectProfile:
    """Validate and convert a raw dict into a SubjectProfile.

    Missing or invalid fields fall back to the deterministic heuristic
    result so the pipeline never stalls on a malformed Claude response.
    """
    subject_type = str(data.get("subject_type") or fallback.subject_type)
    depth = str(data.get("research_depth") or fallback.research_depth)
    if depth not in RESEARCH_DEPTHS:
        depth = fallback.research_depth

    raw_agents = data.get("agents_by_phase") or {}
    agents: dict[str, list[str]] = {}
    if isinstance(raw_agents, dict):
        for phase, value in raw_agents.items():
            if not isinstance(value, list):
                continue
            agents[str(phase)] = _dedupe([str(a) for a in value if isinstance(a, str)])
    if not agents:
        agents = dict(fallback.agents_by_phase)

    return SubjectProfile(
        subject_type=subject_type,
        research_depth=depth,
        agents_by_phase=agents,
        rationale=str(data.get("rationale") or fallback.rationale or ""),
    )


def detect_subject(
    idea: str,
    project_path: str | Path,
    use_llm: bool = True,
) -> SubjectProfile:
    """Detect a deterministic subject profile for an idea and a project.

    When ``use_llm`` is True (default), the detector queries Claude via the
    light model. On any Claude or parsing failure, the deterministic
    heuristic profile is returned instead — the pipeline always gets a
    usable profile.
    """
    inspection = inspect_project(project_path)
    signals = derive_signals(idea, inspection)
    fallback = heuristic_profile(idea, inspection)
    if not use_llm:
        return fallback
    try:
        raw = classify_with_claude(idea, inspection, signals)
    except (AgentError, ValueError) as exc:
        logger.warning("Claude classification failed, falling back to heuristic: %s", exc)
        return fallback
    if not isinstance(raw, dict):
        logger.warning("Claude classification returned non-dict payload; falling back.")
        return fallback
    profile = _coerce_profile(raw, fallback)
    return replace(profile, rationale=profile.rationale or fallback.rationale)


def detect_and_persist(
    session_id: str,
    use_llm: bool = True,
    force: bool = False,
    db_path: Path | None = None,
) -> SubjectProfile:
    """Detect the subject once and persist it on the session row.

    Resume paths call this with ``force=False`` (default): if a profile
    already exists on the session, it is returned as-is and the subject
    is never reclassified. When the detected depth is ``light``, the
    benchmark phase is marked as skipped with a persisted reason.
    """
    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise ValueError(f"Session not found: {session_id!r}")

    if not force and session.subject_type and session.research_depth:
        return SubjectProfile(
            subject_type=session.subject_type,
            research_depth=session.research_depth,
            agents_by_phase=dict(session.agents_by_phase or {}),
            rationale="loaded from existing session profile",
        )

    profile = detect_subject(session.idea, session.project_path, use_llm=use_llm)
    update_session_profile(
        session_id=session_id,
        subject_type=profile.subject_type,
        research_depth=profile.research_depth,
        agents_by_phase=profile.agents_by_phase,
        db_path=db_path,
    )

    if profile.research_depth == RESEARCH_DEPTH_LIGHT:
        mark_phase_skipped(
            session_id=session_id,
            phase=PHASE_BENCHMARK,
            reason="research_depth=light",
            db_path=db_path,
        )

    return profile


__all__ = [
    "MODEL_LIGHT",
    "SubjectProfile",
    "classify_with_claude",
    "default_agents_for_signals",
    "default_depth_for_signals",
    "default_subject_type",
    "derive_signals",
    "detect_and_persist",
    "detect_subject",
    "heuristic_profile",
    "inspect_project",
]
