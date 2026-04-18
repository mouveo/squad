"""Slack business helpers — channel → project mapping, session creation.

These functions are deliberately Slack-client agnostic: they take raw
identifiers (channel, user, idea) and the config dict, and return plain
Python values or raise ``SlackResolutionError``. The Bolt handlers in
``squad.slack_handlers`` are responsible for turning those into Slack
responses.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from squad.config import get_project_state_dir, load_config
from squad.constants import MODE_APPROVAL, SESSION_MODES
from squad.db import create_session, update_session_slack_thread
from squad.models import Session
from squad.workspace import create_workspace, get_context, write_context, write_idea

logger = logging.getLogger(__name__)

# Maximum title length used when deriving a session title from a Slack idea.
_TITLE_MAX_LEN = 60


class SlackResolutionError(Exception):
    """Raised when a Slack command cannot be mapped to a valid project.

    The message is user-facing and posted back to Slack verbatim, so it
    must stay short and actionable.
    """


def _derive_title(idea: str, max_len: int = _TITLE_MAX_LEN) -> str:
    """Truncate an idea to a concise session title (mirrors squad.cli)."""
    idea = idea.strip()
    if len(idea) <= max_len:
        return idea
    return idea[:max_len].rstrip() + "…"


def resolve_project_path(channel_id: str, config: dict) -> str:
    """Return the project path mapped to ``channel_id``.

    Raises :class:`SlackResolutionError` when no mapping exists or the
    configured directory is missing on disk.
    """
    channels = (config.get("slack") or {}).get("channels") or {}
    entry = channels.get(channel_id)
    if not entry:
        raise SlackResolutionError(
            f"Channel `{channel_id}` n'est mappé à aucun projet Squad. "
            f"Ajoutez `slack.channels.{channel_id}.project_path` dans votre config."
        )
    project_path = entry.get("project_path") if isinstance(entry, dict) else None
    if not project_path:
        raise SlackResolutionError(
            f"Channel `{channel_id}` n'a pas de `project_path` configuré."
        )
    path = Path(project_path)
    if not path.is_dir():
        raise SlackResolutionError(
            f"Le `project_path` configuré pour `{channel_id}` n'existe pas : {project_path}"
        )
    return str(path.resolve())


def assert_user_allowed(user_id: str, config: dict) -> None:
    """Raise ``SlackResolutionError`` if ``user_id`` is not in the allowlist.

    An empty or missing ``allowed_user_ids`` list disables the check
    (useful for private single-user installations).
    """
    allowed = (config.get("slack") or {}).get("allowed_user_ids") or []
    if allowed and user_id not in allowed:
        raise SlackResolutionError(
            f"User `{user_id}` n'est pas autorisé à utiliser Squad depuis ce Slack."
        )


def create_session_from_slack(
    *,
    idea: str,
    channel_id: str,
    user_id: str,
    db_path: Path,
    config: dict | None = None,
    mode: str = MODE_APPROVAL,
) -> Session:
    """Create a Squad session from a Slack slash command.

    Order of operations mirrors ``squad.cli._create_and_init_session`` so
    the session is indistinguishable from a CLI-started one except for
    the ``slack_channel`` / ``slack_user_id`` columns. ``slack_thread_ts``
    is populated later by the handler once the root message has been
    posted.
    """
    if not idea or not idea.strip():
        raise SlackResolutionError("Idée vide — utilisez `/squad new <idée>`.")

    if mode not in SESSION_MODES:
        raise SlackResolutionError(f"Mode inconnu : {mode!r}")

    cfg = config if config is not None else load_config()
    assert_user_allowed(user_id, cfg)
    project_path = resolve_project_path(channel_id, cfg)

    session_id = str(uuid.uuid4())
    workspace_path = get_project_state_dir(project_path) / "sessions" / session_id
    title = _derive_title(idea)

    session = create_session(
        title=title,
        project_path=project_path,
        workspace_path=str(workspace_path),
        idea=idea,
        mode=mode,
        db_path=db_path,
        session_id=session_id,
        slack_channel=channel_id,
        slack_user_id=user_id,
    )
    create_workspace(session)
    write_idea(session.id, idea, db_path=db_path)
    context = get_context(project_path)
    write_context(session.id, context, db_path=db_path)
    return session


def record_thread_ts(session_id: str, thread_ts: str, db_path: Path) -> None:
    """Persist the Slack thread timestamp after the root message has been posted."""
    update_session_slack_thread(session_id, thread_ts, db_path=db_path)


def format_root_message(session: Session) -> str:
    """Return the markdown body of the root session thread message."""
    short_id = session.id[:8]
    return (
        f"*[Squad]* Session créée — `{short_id}`\n"
        f"*Titre* : {session.title}\n"
        f"*Projet* : `{session.project_path}`\n"
        f"_Suivi et questions arrivent dans ce thread._"
    )
