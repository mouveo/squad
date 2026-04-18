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
from squad.constants import PHASE_CADRAGE, STATUS_FAILED, STATUS_REVIEW
from squad.db import (
    answer_question,
    get_plan,
    get_question,
    get_session,
    list_pending_questions,
    update_session_failure_reason,
    update_session_status,
)
from squad.forge_bridge import (
    ForgeQueueBusy,
    ForgeUnavailable,
    approve_and_submit,
)
from squad.models import EVENT_INTERVIEWING, EVENT_REVIEW, PipelineEvent
from squad.pipeline import PipelineError, resume_pipeline, run_pipeline
from squad.slack_service import (
    QUESTION_ANSWER_ACTION_ID,
    QUESTION_MODAL_CALLBACK_ID,
    REVIEW_APPROVE_ACTION_ID,
    REVIEW_REJECT_ACTION_ID,
    REVIEW_REJECT_MODAL_ID,
    REVIEW_STATE_APPROVED,
    REVIEW_STATE_QUEUED,
    REVIEW_STATE_REJECTED,
    SlackResolutionError,
    build_question_modal,
    build_reject_modal,
    create_session_from_slack,
    extract_modal_answer,
    extract_reject_reason,
    find_session_by_thread,
    format_root_message,
    parse_review_action_value,
    post_pending_questions,
    post_pipeline_event,
    post_plans_for_review,
    post_question_ack,
    post_thread_message,
    record_thread_ts,
    update_question_message,
    update_review_message,
)
from squad.workspace import sync_pending_questions

logger = logging.getLogger(__name__)


def _make_event_callback(client, db_path: Path):
    """Return a pipeline event callback that mirrors transitions into Slack.

    Always posts the threaded pipeline-event summary (LOT 2 contract).
    On entry to ``interviewing`` for the ``cadrage`` phase it also posts
    each pending question as a separate message with its answer button
    (LOT 4). Ideation pauses are handled by the dedicated angle-review
    flow (LOT 6) — this callback intentionally does NOT post questions
    for them. Observer errors are caught by the pipeline itself.
    """

    def _callback(event: PipelineEvent) -> None:
        session = get_session(event.session_id, db_path=db_path)
        if session is None:
            return
        post_pipeline_event(event, session, client)
        if event.type == EVENT_INTERVIEWING and event.phase == PHASE_CADRAGE:
            post_pending_questions(client, session, db_path=db_path)
        elif event.type == EVENT_REVIEW:
            post_plans_for_review(client, session, db_path=db_path)

    return _callback


def _run_pipeline_bg(
    session_id: str,
    db_path: Path,
    event_callback=None,
) -> None:
    """Run the pipeline and swallow expected errors (background executor)."""
    try:
        run_pipeline(session_id, db_path=db_path, event_callback=event_callback)
    except PipelineError as exc:
        logger.warning("Pipeline failed for session %s: %s", session_id, exc)
    except Exception:
        # A crash here must not take down the executor worker; log and move on.
        logger.exception("Unexpected pipeline crash for session %s", session_id)


def _resume_pipeline_bg(
    session_id: str,
    db_path: Path,
    event_callback=None,
) -> None:
    """Resume the pipeline on the background executor (error-tolerant)."""
    try:
        resume_pipeline(session_id, db_path=db_path, event_callback=event_callback)
    except PipelineError as exc:
        logger.warning("Pipeline resume failed for session %s: %s", session_id, exc)
    except RuntimeError as exc:
        # e.g. "still has unanswered questions" — shouldn't happen on the
        # Slack flow because we only schedule resume after the last answer,
        # but logging is enough.
        logger.warning("Pipeline resume refused for session %s: %s", session_id, exc)
    except Exception:
        logger.exception("Unexpected resume crash for session %s", session_id)


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

    @app.action(QUESTION_ANSWER_ACTION_ID)
    def _handle_question_action(ack, body, client):
        ack()
        handle_question_action(body=body, client=client, db_path=db_path)

    @app.view(QUESTION_MODAL_CALLBACK_ID)
    def _handle_question_submission(ack, body, view, client):
        ack()
        handle_question_submission(
            body=body,
            view=view,
            client=client,
            db_path=db_path,
            executor=executor,
        )

    @app.action(REVIEW_APPROVE_ACTION_ID)
    def _handle_review_approve(ack, body, client):
        ack()
        handle_review_approve(
            body=body,
            client=client,
            db_path=db_path,
            executor=executor,
        )

    @app.action(REVIEW_REJECT_ACTION_ID)
    def _handle_review_reject(ack, body, client):
        ack()
        handle_review_reject_action(body=body, client=client, db_path=db_path)

    @app.view(REVIEW_REJECT_MODAL_ID)
    def _handle_review_reject_submission(ack, body, view, client):
        ack()
        handle_review_reject_submission(
            body=body, view=view, client=client, db_path=db_path
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

    event_callback = _make_event_callback(client, db_path)
    executor.submit(_run_pipeline_bg, session.id, db_path, event_callback)

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


# ── Question actions + modal submissions (LOT 4) ──────────────────────────────


def _extract_question_id_from_action(body: dict) -> str | None:
    """Pull the ``question_id`` out of a block-actions payload.

    The id was injected as the action element's ``value`` when the
    question was posted (see ``build_question_blocks``). Returns ``None``
    when the payload is malformed — the caller ignores those silently.
    """
    actions = (body or {}).get("actions") or []
    if not actions:
        return None
    first = actions[0]
    value = first.get("value")
    return str(value) if value else None


def handle_question_action(
    *,
    body: dict,
    client,
    db_path: Path,
) -> None:
    """Open the answer modal for a ``Répondre`` button click.

    Ignored silently (logs only) when the click targets a question that
    has no active session or is already answered, per the LOT 4
    idempotence contract.
    """
    question_id = _extract_question_id_from_action(body)
    if not question_id:
        logger.debug("Question action without question_id: %r", body)
        return

    question = get_question(question_id, db_path=db_path)
    if question is None:
        logger.debug("Question action on unknown question %s — ignored", question_id)
        return
    if question.answer is not None:
        logger.debug("Question %s already answered — ignoring re-open", question_id)
        return

    session = get_session(question.session_id, db_path=db_path)
    if session is None:
        logger.debug("Session for question %s vanished — ignoring", question_id)
        return

    trigger_id = (body or {}).get("trigger_id")
    if not trigger_id:
        logger.warning("Question action missing trigger_id — cannot open modal")
        return

    try:
        client.views_open(trigger_id=trigger_id, view=build_question_modal(question))
    except Exception:
        logger.exception("views_open failed for question %s", question_id)


def handle_question_submission(
    *,
    body: dict,
    view: dict,
    client,
    db_path: Path,
    executor,
) -> None:
    """Persist the modal answer and, when it is the last one, resume the pipeline.

    Strict reuse of the CLI primitives — ``answer_question``,
    ``sync_pending_questions`` and ``resume_pipeline`` — so the Slack
    path and the CLI stay behaviour-equivalent. The final resume is
    scheduled on the shared executor so the Socket Mode thread never
    blocks on a pipeline run.
    """
    question_id, answer = extract_modal_answer(view)
    if not question_id or not answer:
        logger.debug("Empty modal submission for question %s", question_id)
        return

    question = get_question(question_id, db_path=db_path)
    if question is None:
        logger.debug("Modal submission on unknown question %s — ignored", question_id)
        return

    session = get_session(question.session_id, db_path=db_path)
    if session is None:
        logger.debug("Session for question %s vanished — ignoring submission", question_id)
        return

    # Remember whether this is the first answer so we can decide on resume;
    # a re-submission on an already-answered question persists the new
    # value but must NOT schedule a duplicate resume.
    was_already_answered = question.answer is not None

    # Persist the answer through the same primitives the CLI uses.
    answer_question(question.id, answer, db_path=db_path)
    sync_pending_questions(question.session_id, db_path=db_path)

    # Refresh the question (now carries answer/answered_at) for UI updates.
    answered = get_question(question.id, db_path=db_path) or question
    update_question_message(client, session, answered, answered=True)
    post_question_ack(client, session, answered, answer)

    if was_already_answered:
        return

    remaining = list_pending_questions(question.session_id, db_path=db_path)
    if not remaining:
        # Last question answered — resume the pipeline off the Socket Mode thread.
        event_callback = _make_event_callback(client, db_path)
        executor.submit(
            _resume_pipeline_bg,
            question.session_id,
            db_path,
            event_callback,
        )


# ── Review actions (LOT 5) ────────────────────────────────────────────────────


def _extract_review_action_value(body: dict) -> tuple[str | None, str | None]:
    """Return ``(session_id, plan_id)`` from a review action payload."""
    actions = (body or {}).get("actions") or []
    if not actions:
        return None, None
    return parse_review_action_value(actions[0].get("value") or "")


def handle_review_approve(
    *,
    body: dict,
    client,
    db_path: Path,
    executor,
) -> None:
    """Handle a click on the ``Approuver`` button.

    Guards on the current session status to stay idempotent: anything
    other than ``review`` (e.g. the session was already approved,
    queued, or rejected) is a no-op. Submits to Forge on the shared
    executor so the Socket Mode thread never blocks on the subprocess
    round-trip.
    """
    session_id, plan_id = _extract_review_action_value(body)
    if not session_id or not plan_id:
        return

    session = get_session(session_id, db_path=db_path)
    plan = get_plan(plan_id, db_path=db_path)
    if session is None or plan is None:
        return

    # Idempotency guard — only sessions still in review can be acted on.
    if session.status != STATUS_REVIEW:
        logger.debug(
            "Approve ignored for session %s (status=%s)", session_id, session.status
        )
        return

    # Dispatch the actual submission off the Socket Mode thread. The
    # wrapper below owns the status transition and Slack feedback.
    executor.submit(_approve_bg, session_id, plan_id, db_path, client)


def _approve_bg(session_id: str, plan_id: str, db_path: Path, client) -> None:
    """Background worker for the Approve button.

    Wraps :func:`squad.forge_bridge.approve_and_submit` — the same path
    the CLI uses for ``squad approve`` — and mirrors the outcome into
    the session thread (buttons disabled via ``chat_update``, a
    confirmation or a fallback message).
    """
    session = get_session(session_id, db_path=db_path)
    plan = get_plan(plan_id, db_path=db_path)
    if session is None or plan is None:
        return

    try:
        outcome = approve_and_submit(session_id, db_path=db_path)
    except (ForgeUnavailable, ForgeQueueBusy, ValueError) as exc:
        logger.warning("Forge submission failed for session %s: %s", session_id, exc)
        refreshed = get_session(session_id, db_path=db_path) or session
        # Session was reverted to review by approve_and_submit — keep the
        # UI in sync and post the fallback message expected by the CLI.
        refreshed_plan = get_plan(plan_id, db_path=db_path) or plan
        update_review_message(
            client,
            refreshed,
            refreshed_plan,
            state=REVIEW_STATE_REJECTED,
            final_note=f":warning: Soumission Forge indisponible — session revenue en review ({exc})",
        )
        post_thread_message(
            client,
            refreshed,
            (
                ":warning: *Forge indisponible* — la session est revenue en _review_. "
                f"Raison : {exc}. Réessayez plus tard ou utilisez `squad approve`."
            ),
        )
        return

    refreshed = get_session(session_id, db_path=db_path) or session
    refreshed_plan = get_plan(plan_id, db_path=db_path) or plan
    update_review_message(
        client,
        refreshed,
        refreshed_plan,
        state=REVIEW_STATE_QUEUED,
        final_note=(
            f":rocket: {outcome.plans_sent} plan(s) envoyé(s) à la queue Forge "
            f"(queue_started={outcome.queue_started})."
        ),
    )
    post_thread_message(
        client,
        refreshed,
        (
            f":rocket: *Approuvé* — {outcome.plans_sent} plan(s) envoyé(s) à la queue Forge "
            f"(queue_started={outcome.queue_started})."
        ),
    )


def handle_review_reject_action(
    *,
    body: dict,
    client,
    db_path: Path,
) -> None:
    """Open the reject-reason modal for a ``Rejeter`` click.

    As for Approve, a session no longer in ``review`` is ignored so a
    double-click cannot trigger a second modal.
    """
    session_id, plan_id = _extract_review_action_value(body)
    if not session_id or not plan_id:
        return

    session = get_session(session_id, db_path=db_path)
    if session is None or session.status != STATUS_REVIEW:
        return

    trigger_id = (body or {}).get("trigger_id")
    if not trigger_id:
        return

    try:
        client.views_open(
            trigger_id=trigger_id,
            view=build_reject_modal(session_id, plan_id),
        )
    except Exception:
        logger.exception("views_open failed for reject modal on session %s", session_id)


def handle_review_reject_submission(
    *,
    body: dict,
    view: dict,
    client,
    db_path: Path,
) -> None:
    """Persist the rejection reason, mark the session as failed, update the UI."""
    session_id, plan_id, reason = extract_reject_reason(view)
    if not session_id or not plan_id or not reason:
        return

    session = get_session(session_id, db_path=db_path)
    plan = get_plan(plan_id, db_path=db_path)
    if session is None or plan is None:
        return

    if session.status != STATUS_REVIEW:
        # Someone else already closed the review — ignore silently.
        logger.debug(
            "Reject submission ignored for session %s (status=%s)",
            session_id,
            session.status,
        )
        return

    update_session_failure_reason(session_id, reason, db_path=db_path)
    update_session_status(session_id, STATUS_FAILED, db_path=db_path)

    refreshed = get_session(session_id, db_path=db_path) or session
    update_review_message(
        client,
        refreshed,
        plan,
        state=REVIEW_STATE_REJECTED,
        final_note=f":x: Rejeté — {reason}",
    )
    post_thread_message(
        client,
        refreshed,
        f":x: *Session rejetée*\nRaison : {reason}",
    )
