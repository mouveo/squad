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

from squad.attachment_service import AttachmentError, download_file, store_attachment
from squad.pipeline import PipelineError, run_pipeline
from squad.slack_service import (
    SlackResolutionError,
    create_session_from_slack,
    find_session_by_thread,
    format_root_message,
    post_thread_message,
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

    @app.event("file_shared")
    def _handle_file_shared(event, client):
        handle_file_shared(
            event=event,
            client=client,
            db_path=db_path,
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


# ── file_shared (LOT 3) ───────────────────────────────────────────────────────


def _resolve_thread_ts_from_shares(file_info: dict) -> tuple[str | None, str | None]:
    """Pick the first ``(channel_id, thread_ts)`` pair from a file's ``shares``.

    Slack reports shares under ``shares.public`` and ``shares.private``;
    each entry is a ``{channel_id: [{ts, thread_ts?}, ...]}`` mapping.
    Returns ``(None, None)`` when no thread context is found — the file
    was dropped at top level of a channel and Squad cannot attach it.
    """
    shares = (file_info or {}).get("shares") or {}
    for visibility in ("public", "private"):
        bucket = shares.get(visibility) or {}
        for channel_id, entries in bucket.items():
            for entry in entries or []:
                thread_ts = entry.get("thread_ts") or entry.get("ts")
                if thread_ts:
                    return channel_id, thread_ts
    return None, None


def handle_file_shared(
    *,
    event: dict,
    client,
    db_path: Path,
    config: dict,
) -> None:
    """Process a Slack ``file_shared`` event.

    Looks up the file via ``files.info``, resolves the originating
    thread, attaches the file to the matching session if any. Drops
    targeting unrelated threads are silently ignored. Validation /
    network failures post a short error message in the session thread
    so the PO knows the upload was rejected, then return without
    raising.
    """
    file_id = (event or {}).get("file_id") or ((event or {}).get("file") or {}).get("id")
    if not file_id:
        logger.debug("file_shared event without file id: %r", event)
        return

    try:
        info_response = client.files_info(file=file_id)
    except Exception:
        logger.exception("files.info failed for file %s", file_id)
        return
    file_info = (
        info_response.get("file")
        if isinstance(info_response, dict)
        else getattr(info_response, "get", lambda _k: None)("file")
    )
    if not file_info:
        return

    channel_id, thread_ts = _resolve_thread_ts_from_shares(file_info)
    if not channel_id or not thread_ts:
        logger.debug("file %s has no thread share — ignoring", file_id)
        return

    session = find_session_by_thread(channel_id, thread_ts, db_path=db_path)
    if session is None:
        logger.debug(
            "file %s shared in %s/%s but no Squad session matches", file_id, channel_id, thread_ts
        )
        return

    filename = file_info.get("name") or file_id
    size = int(file_info.get("size") or 0)
    mime_type = file_info.get("mimetype")
    download_url = file_info.get("url_private_download") or file_info.get("url_private")
    bot_token = (config.get("slack") or {}).get("bot_token")

    try:
        from squad.attachment_service import validate_attachment

        validate_attachment(
            filename, size, session_id=session.id, config=config, db_path=db_path
        )
    except AttachmentError as exc:
        post_thread_message(client, session, f":warning: Pièce jointe rejetée : {exc}")
        return

    if not download_url or not bot_token:
        post_thread_message(
            client, session, ":warning: Téléchargement Slack indisponible (URL ou token manquant)."
        )
        return

    try:
        content = download_file(download_url, bot_token)
        meta = store_attachment(
            session.id,
            filename,
            content,
            mime_type=mime_type,
            slack_file_id=file_id,
            config=config,
            db_path=db_path,
        )
    except AttachmentError as exc:
        post_thread_message(client, session, f":warning: Pièce jointe rejetée : {exc}")
        return
    except Exception as exc:
        logger.exception("Unexpected attachment error for file %s", file_id)
        post_thread_message(client, session, f":warning: Erreur interne pendant l'attachement : {exc}")
        return

    post_thread_message(
        client,
        session,
        f":paperclip: Fichier `{meta.filename}` attaché ({meta.size_bytes} octets).",
    )
