"""Shared business services for the human review step.

The rejection path used to live inline inside the Slack submission
handler and the ``squad review --action reject`` CLI branch. Each call
site persisted ``failure_reason`` and flipped the session to
``failed`` on its own, which made it too easy to drift between
entry points. LOT 2 of plan 8 consolidates that logic here so Slack,
the CLI and the upcoming dashboard action share one transition.

The approval path keeps its own single entry point in
``squad.forge_bridge.approve_and_submit`` and is intentionally not
duplicated here.
"""

from pathlib import Path

from squad.constants import STATUS_FAILED
from squad.db import update_session_failure_reason, update_session_status


def reject_session(
    session_id: str,
    reason: str,
    db_path: Path | None = None,
) -> None:
    """Persist a rejection reason and flip the session to ``failed``.

    Writes ``failure_reason`` first so any observer that reads the
    status transition on ``failed`` sees the explanation already in
    place.
    """
    update_session_failure_reason(session_id, reason, db_path=db_path)
    update_session_status(session_id, STATUS_FAILED, db_path=db_path)
