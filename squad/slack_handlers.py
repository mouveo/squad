"""Slack Bolt event handlers — slash commands and (later) actions.

The handlers are intentionally thin: they delegate all business logic to
:mod:`squad.slack_service` and only bridge Bolt's ``ack`` / ``respond`` /
``client`` primitives to those helpers. Pipeline runs are dispatched on
the shared executor created by ``squad serve`` so the Socket Mode thread
never blocks on an agent subprocess.
"""

from __future__ import annotations

import logging
from concurrent.futures import Executor
from pathlib import Path

from squad.pipeline import PipelineError, run_pipeline
from squad.slack_service import (
    SlackResolutionError,
    create_session_from_slack,
    format_root_message,
    record_thread_ts,
)

logger = logging.getLogger(__name__)


def _run_pipeline_bg(session_id: str, db_path: Path) -> None:
    """Run the pipeline and swallow expected errors (background executor)."""
    try:
        run_pipeline(session_id, db_path=db_path)
    except PipelineError as exc:
        logger.warning("Pipeline failed for session %s: %s", session_id, exc)
    except Exception:
        # A crash here must not take down the executor worker; log and move on.
        logger.exception("Unexpected pipeline crash for session %s", session_id)


def register_handlers(
    app,
    *,
    db_path: Path,
    executor: Executor,
    config: dict,
) -> None:
    """Register all Slack command / action handlers on the Bolt ``app``.

    Separated from ``slack_app.build_app`` so tests can register handlers
    against a lightweight fake app. The ``executor`` is the shared pool
    owned by ``squad serve`` (long-running pipeline runs dispatched off
    the Socket Mode thread).
    """

    @app.command("/squad")
    def _handle_squad_command(ack, respond, command, client):
        ack()
        handle_squad_command(
            command=command,
            respond=respond,
            client=client,
            db_path=db_path,
            executor=executor,
            config=config,
        )


def handle_squad_command(
    *,
    command: dict,
    respond,
    client,
    db_path: Path,
    executor: Executor,
    config: dict,
) -> None:
    """Dispatch the raw ``/squad <sub>`` slash command.

    Sub-commands supported in LOT 1:

    * ``new <idée>`` — create a Squad session from the current channel.

    Unknown subcommands respond with a short usage hint; neither the
    allowlist nor the project-mapping guard is enforced in that early
    path so listing help is always free.
    """
    text = (command.get("text") or "").strip()
    if not text:
        respond("Usage : `/squad new <idée>`")
        return

    parts = text.split(None, 1)
    subcommand = parts[0].lower()
    remainder = parts[1] if len(parts) > 1 else ""

    if subcommand == "new":
        _handle_new(
            idea=remainder,
            command=command,
            respond=respond,
            client=client,
            db_path=db_path,
            executor=executor,
            config=config,
        )
        return

    respond(f"Sous-commande inconnue : `{subcommand}`. Usage : `/squad new <idée>`")


def _handle_new(
    *,
    idea: str,
    command: dict,
    respond,
    client,
    db_path: Path,
    executor: Executor,
    config: dict,
) -> None:
    channel_id = command.get("channel_id") or ""
    user_id = command.get("user_id") or ""

    if not idea.strip():
        respond("Idée vide — utilisez `/squad new <idée>`.")
        return

    try:
        session = create_session_from_slack(
            idea=idea,
            channel_id=channel_id,
            user_id=user_id,
            db_path=db_path,
            config=config,
        )
    except SlackResolutionError as exc:
        respond(str(exc))
        return
    except Exception as exc:
        logger.exception("Failed to create session from Slack")
        respond(f"Erreur interne : {exc}")
        return

    # Post the root thread message so future pipeline events can reply in-thread.
    try:
        message = client.chat_postMessage(
            channel=channel_id,
            text=format_root_message(session),
        )
        thread_ts = message.get("ts") if isinstance(message, dict) else getattr(message, "get", lambda _k: None)("ts")
        if thread_ts:
            record_thread_ts(session.id, thread_ts, db_path=db_path)
    except Exception:
        logger.exception("Failed to post root thread message for session %s", session.id)

    executor.submit(_run_pipeline_bg, session.id, db_path)

    respond(
        f"Session `{session.id[:8]}` créée — _{session.title}_. "
        f"Suivez la progression dans le thread."
    )
