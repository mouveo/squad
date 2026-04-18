"""Ideation service — runs the `ideation` agent, parses angles & strategy, persists them.

This module owns the ``ideation`` phase end-to-end from the executor's
point of view:

* ``run_ideation(session_id, ...)`` loads the session, dispatches the
  ``ideation`` Claude agent via ``squad.executor.run_agent`` (with
  ``cwd=session.project_path`` when that path exists on disk), parses
  the agent's markdown into typed angles and a 4-key strategy dict,
  persists the angles in ``ideation_angles`` and returns an
  ``IdeationResult``.
* ``parse_angles`` is tolerant: it normalises ``idx`` based on the
  order angles appear in the document rather than on any number shown
  in the heading.
* ``parse_strategy`` validates the four keys of the strategy JSON and
  falls back on a safe default when parsing fails or keys are missing.

The ``ideation`` phase is non-critical: if the agent output is empty,
unparseable, or yields zero angles, ``run_ideation`` still emits a
single synthetic angle seeded on ``session.idea`` so the downstream
phases never see an empty ``ideation_angles`` set for the session.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from squad.db import (
    get_session,
    list_ideation_angles,
    persist_ideation_angle,
)
from squad.executor import run_agent
from squad.models import IdeationAngle
from squad.phase_contracts import ContractError, extract_json_block

logger = logging.getLogger(__name__)


# ── public dataclass ──────────────────────────────────────────────────────────


@dataclass
class IdeationResult:
    """Outcome of a successful (or fallback) ideation run."""

    content: str
    angles: list[IdeationAngle] = field(default_factory=list)
    strategy: dict = field(default_factory=dict)


# ── strategy ──────────────────────────────────────────────────────────────────


_STRATEGY_KEYS: tuple[str, ...] = (
    "strategy",
    "best_angle_idx",
    "rationale",
    "divergence_score",
)

_VALID_STRATEGIES: tuple[str, ...] = ("auto_pick", "ask_user")
_VALID_DIVERGENCE: tuple[str, ...] = ("low", "medium", "high")

_STRATEGY_FALLBACK: dict = {
    "strategy": "auto_pick",
    "best_angle_idx": 0,
    "divergence_score": "medium",
}


def parse_strategy(markdown: str) -> dict:
    """Parse the 4-key strategy JSON from an ideation agent output.

    Returns a dict with at least the keys ``strategy``, ``best_angle_idx``
    and ``divergence_score``. A ``rationale`` is preserved when present.
    Any parse failure (no JSON block, invalid values, missing keys) falls
    back on ``_STRATEGY_FALLBACK`` so callers never crash on a bad agent
    output — the phase is non-critical by design.
    """
    try:
        data = extract_json_block(markdown)
    except ContractError:
        return dict(_STRATEGY_FALLBACK)

    strategy = data.get("strategy")
    idx_raw = data.get("best_angle_idx")
    divergence = data.get("divergence_score")

    if strategy not in _VALID_STRATEGIES:
        return dict(_STRATEGY_FALLBACK)
    try:
        idx = int(idx_raw)
    except (TypeError, ValueError):
        return dict(_STRATEGY_FALLBACK)
    if idx < 0:
        return dict(_STRATEGY_FALLBACK)
    if divergence not in _VALID_DIVERGENCE:
        return dict(_STRATEGY_FALLBACK)

    result: dict = {
        "strategy": strategy,
        "best_angle_idx": idx,
        "divergence_score": divergence,
    }
    rationale = data.get("rationale")
    if isinstance(rationale, str) and rationale.strip():
        result["rationale"] = rationale.strip()
    return result


# ── angles ────────────────────────────────────────────────────────────────────


# Matches an "Angle" header at any ## / ### level. Captures the trailing
# header text so we can derive a title. The header text is everything
# after ``Angle`` on the same line and excludes the ``##``/``###`` prefix.
_ANGLE_HEADER_RE = re.compile(
    r"^(?P<hashes>#{2,3})\s+Angle\b[^\n]*$",
    re.MULTILINE | re.IGNORECASE,
)

# Field labels accepted inside an angle body. Keys are the normalised
# fields of ``IdeationAngle``; values are the case-insensitive label
# prefixes (French + light English tolerance) we accept from the agent.
_FIELD_LABELS: dict[str, tuple[str, ...]] = {
    "segment": ("segment",),
    "value_prop": (
        "value prop",
        "value proposition",
        "proposition de valeur",
        "valeur",
    ),
    "approach": (
        "approche technique",
        "approche",
        "approach",
    ),
    "divergence_note": (
        "note de divergence",
        "divergence note",
        "divergence",
    ),
}


def _extract_title(header_line: str) -> str:
    """Return the short title portion of an Angle header line.

    Strips the leading ``##`` and the leading ``Angle`` keyword. If the
    header uses an em-dash / hyphen separator (``Angle 2 — Foo``), returns
    the right-hand side; otherwise returns whatever follows ``Angle``.
    The result is stripped and never empty (falls back to ``"Angle"``).
    """
    raw = header_line.lstrip("#").strip()
    # Drop the leading "Angle" keyword (case-insensitive).
    raw = re.sub(r"^Angle\b", "", raw, flags=re.IGNORECASE).strip()
    for sep in ("—", "–", " - ", ":"):
        if sep in raw:
            _prefix, _, title = raw.partition(sep)
            title = title.strip()
            if title:
                return title
    # No separator → fall back to the remaining text (typically a number).
    return raw or "Angle"


def _parse_angle_fields(body: str) -> dict[str, str]:
    """Extract ``segment/value_prop/approach/divergence_note`` from a body.

    Accepts bullet (``- Segment:``), bold (``**Segment**:``) and plain
    (``Segment:``) prefixes. Values are single-line; anything after a
    newline is ignored for that field. Missing fields default to an empty
    string rather than raising — tolerance is intentional.
    """
    out: dict[str, str] = {
        "segment": "",
        "value_prop": "",
        "approach": "",
        "divergence_note": "",
    }
    for line in body.splitlines():
        clean = line.strip()
        if not clean:
            continue
        # Strip common bullet / emphasis markers
        clean = clean.lstrip("-*• ").strip()
        clean = re.sub(r"^\*+|\*+$", "", clean).strip()
        for field_name, labels in _FIELD_LABELS.items():
            if out[field_name]:
                continue
            for label in labels:
                pattern = rf"^{re.escape(label)}\s*[:\-–—]\s*(.+)$"
                match = re.match(pattern, clean, flags=re.IGNORECASE)
                if match:
                    out[field_name] = match.group(1).strip()
                    break
    return out


def parse_angles(markdown: str, session_id: str) -> list[IdeationAngle]:
    """Parse every angle found in an ideation agent output, in document order.

    ``idx`` is assigned strictly by the order angles appear in the
    markdown (0-based), never from any number shown in the heading —
    agents sometimes shift numbering, and the DB primary key must stay
    deterministic. Empty input, no matching headers, or angles without
    any field value yield an empty list; callers handle the fallback.
    """
    if not markdown or not markdown.strip():
        return []

    matches = list(_ANGLE_HEADER_RE.finditer(markdown))
    if not matches:
        return []

    angles: list[IdeationAngle] = []
    now = datetime.utcnow().isoformat()
    for i, match in enumerate(matches):
        header_line = match.group(0)
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(markdown)
        body = markdown[body_start:body_end]

        fields = _parse_angle_fields(body)
        # Skip completely empty sections — an angle header with no field at
        # all is useless downstream and would clutter the table.
        if not any(fields.values()):
            continue

        title = _extract_title(header_line)
        angles.append(
            IdeationAngle(
                session_id=session_id,
                idx=len(angles),
                title=title,
                segment=fields["segment"],
                value_prop=fields["value_prop"],
                approach=fields["approach"],
                divergence_note=fields["divergence_note"],
                created_at=now,
            )
        )
    return angles


# ── orchestration ─────────────────────────────────────────────────────────────


def _resolve_project_cwd(project_path: str | None) -> str | None:
    """Return ``project_path`` only when it is a non-empty, existing directory.

    Mirrors the safe fallback practised in ``pipeline._resolve_agent_cwd``
    so agents with active-exploration capabilities never inherit a cwd
    that points at a missing directory.
    """
    if not project_path:
        return None
    if not Path(project_path).exists():
        logger.warning(
            "Ideation: project_path %r does not exist; falling back to cwd=None",
            project_path,
        )
        return None
    return project_path


def _fallback_angle(session_id: str, idea: str) -> IdeationAngle:
    """Build a single synthetic angle seeded on ``session.idea``.

    Used when the agent output is empty or unparseable. ``idx=0`` keeps
    the DB primary key deterministic; downstream code treats the
    ``(session_id, 0)`` row as the baseline angle when no richer set
    exists.
    """
    value_prop = (idea or "").strip()
    if len(value_prop) > 300:
        value_prop = value_prop[:297] + "…"
    return IdeationAngle(
        session_id=session_id,
        idx=0,
        title="Fallback — baseline",
        segment="TBD",
        value_prop=value_prop or "TBD",
        approach="Implement the submitted idea as stated",
        divergence_note="Fallback angle — ideation service produced nothing exploitable",
        created_at=datetime.utcnow().isoformat(),
    )


def _fallback_content(idea: str) -> str:
    """Minimal markdown body returned when the agent produced nothing usable."""
    return (
        "# Ideation — fallback\n\n"
        "Aucun angle exploitable n'a pu être généré par l'agent. "
        "Un angle synthétique a été persisté à partir de l'idée soumise.\n\n"
        "## Angle 0 — Fallback\n"
        "- Segment: TBD\n"
        f"- Value prop: {(idea or 'TBD')[:200]}\n"
        "- Approche: implémentation directe de l'idée soumise\n"
        "- Note de divergence: aucun (angle de repli)\n"
    )


def run_ideation(
    session_id: str,
    db_path: Path | None = None,
    extra_context: str | None = None,
) -> IdeationResult:
    """Run the ideation phase for a session and persist the produced angles.

    Loads the session, dispatches the ``ideation`` agent through
    ``squad.executor.run_agent`` (with ``cwd=session.project_path`` when
    the directory exists), parses the output into angles + strategy,
    persists the angles and returns an ``IdeationResult``. When the
    agent output is empty/invalid or no angles can be parsed, a single
    synthetic angle is persisted and ``strategy`` is forced to
    ``auto_pick`` — the phase is non-critical and must never leave the
    session without at least one angle to work from.
    """
    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise ValueError(f"Session not found: {session_id!r}")

    cwd = _resolve_project_cwd(session.project_path)

    try:
        content = run_agent(
            agent_name="ideation",
            session_id=session_id,
            phase="ideation",
            cumulative_context=extra_context,
            cwd=cwd,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Ideation agent failed for session %s: %s — using fallback",
            session_id,
            exc,
        )
        content = ""

    angles = parse_angles(content, session_id)
    if not angles:
        logger.info(
            "Ideation produced no parseable angles for session %s — using fallback",
            session_id,
        )
        fallback = _fallback_angle(session_id, session.idea)
        persist_ideation_angle(db_path, fallback)
        if not (content or "").strip():
            content = _fallback_content(session.idea)
        return IdeationResult(
            content=content,
            angles=[fallback],
            strategy={
                **_STRATEGY_FALLBACK,
                "rationale": "Agent output unusable — fallback angle seeded from session.idea",
            },
        )

    for angle in angles:
        persist_ideation_angle(db_path, angle)

    strategy = parse_strategy(content)
    # Clamp best_angle_idx to the actual set persisted so downstream
    # consumers never dereference a missing angle.
    if strategy["best_angle_idx"] >= len(angles):
        strategy["best_angle_idx"] = 0

    # Reload from DB so the returned angles carry the persisted shape
    # (same semantics as plan_generator reloading plans by session).
    persisted = list_ideation_angles(db_path, session_id)
    return IdeationResult(content=content, angles=persisted, strategy=strategy)
