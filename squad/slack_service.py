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

from sqlite_utils import Database

from squad.config import get_global_db_path, get_project_state_dir, load_config
from squad.constants import MODE_APPROVAL, PHASE_LABELS, SESSION_MODES
from squad.db import (
    _to_session,
    create_session,
    list_pending_questions,
    update_question_slack_message_ts,
    update_session_slack_thread,
)
from squad.models import (
    EVENT_FAILED,
    EVENT_INTERVIEWING,
    EVENT_REVIEW,
    EVENT_WORKING,
    PipelineEvent,
    Question,
    Session,
)
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


# ── Pipeline live updates (LOT 2) ─────────────────────────────────────────────


def _format_elapsed(seconds: float) -> str:
    """Return a compact ``h m s`` elapsed-time string (e.g. ``1h 02m 03s``)."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _format_utc(ts) -> str:
    """Return an ISO-like UTC timestamp (second precision) for Slack display."""
    return ts.strftime("%Y-%m-%d %H:%M:%S UTC")


def format_pipeline_event(event: PipelineEvent) -> str:
    """Render a pipeline event into the threaded Slack message body.

    Always includes the event time (UTC) and the elapsed time since the
    session started. ``review`` and ``failed`` carry a context-specific
    summary (plan count, failure reason). Unknown event types fall back
    to a minimal representation so a mis-typed event never crashes.
    """
    stamp = _format_utc(event.timestamp_utc)
    elapsed = _format_elapsed(event.elapsed_seconds)

    if event.type == EVENT_WORKING:
        label = PHASE_LABELS.get(event.phase or "", event.phase or "—")
        return (
            f":gear: *Phase : {label}* (`{event.phase}`)\n"
            f"{stamp} · écoulé : {elapsed}"
        )
    if event.type == EVENT_INTERVIEWING:
        plural = "s" if event.pending_questions != 1 else ""
        return (
            f":pause_button: *En attente de réponses* — "
            f"{event.pending_questions} question{plural} en attente\n"
            f"{stamp} · écoulé : {elapsed}"
        )
    if event.type == EVENT_REVIEW:
        plural = "s" if event.plans_count != 1 else ""
        return (
            f":white_check_mark: *Review prête* — {event.plans_count} plan{plural} généré{plural}\n"
            f"{stamp} · durée totale : {elapsed}"
        )
    if event.type == EVENT_FAILED:
        reason = event.failure_reason or "raison inconnue"
        return (
            f":x: *Pipeline échoué* — {reason}\n"
            f"{stamp} · écoulé : {elapsed}"
        )
    return f"{event.type}: {stamp} · écoulé : {elapsed}"


def find_session_by_thread(
    channel_id: str,
    thread_ts: str,
    db_path: Path | None = None,
) -> Session | None:
    """Return the session whose Slack thread matches ``(channel_id, thread_ts)``.

    Used by the ``file_shared`` handler to ignore drops that target a
    thread Squad is not tracking. Returns ``None`` when no session
    matches — the caller must silently skip those, never crash.
    """
    if not channel_id or not thread_ts:
        return None
    path = db_path or get_global_db_path()
    db = Database(path)
    if "sessions" not in db.table_names():
        return None
    rows = list(
        db["sessions"].rows_where(
            "slack_channel = ? AND slack_thread_ts = ?",
            [channel_id, thread_ts],
            limit=1,
        )
    )
    if not rows:
        return None
    return _to_session(dict(rows[0]))


def post_thread_message(client, session: Session, text: str) -> None:
    """Send a plain text message in the session's Slack thread.

    No-ops when the session has no Slack thread (CLI-only sessions).
    Slack errors are logged and swallowed so callers can use this in
    error paths without nesting another try/except.
    """
    if not session.slack_channel or not session.slack_thread_ts:
        return
    try:
        client.chat_postMessage(
            channel=session.slack_channel,
            thread_ts=session.slack_thread_ts,
            text=text,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to post thread message for session %s", session.id)


def post_pipeline_event(event: PipelineEvent, session: Session, client) -> None:
    """Post ``event`` in the session's Slack thread if one is recorded.

    Silently no-ops for sessions that were not created from Slack (no
    ``slack_channel`` / ``slack_thread_ts``). Slack API errors are
    logged and swallowed so live-updates never break the pipeline.
    """
    if not session.slack_channel or not session.slack_thread_ts:
        return
    try:
        client.chat_postMessage(
            channel=session.slack_channel,
            thread_ts=session.slack_thread_ts,
            text=format_pipeline_event(event),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to post pipeline event %r for session %s", event.type, session.id
        )


# ── Question Q&A (LOT 4) ──────────────────────────────────────────────────────

# Stable identifiers for Block Kit actions and view submissions. The
# handler switches on these so renaming them would break in-flight
# Slack interactions — change only together with a persisted migration.
QUESTION_ANSWER_ACTION_ID = "squad_question_answer"
QUESTION_MODAL_CALLBACK_ID = "squad_question_submit"
QUESTION_MODAL_INPUT_BLOCK_ID = "answer_block"
QUESTION_MODAL_INPUT_ACTION_ID = "answer_input"


def build_question_blocks(question: Question, *, answered: bool = False) -> list[dict]:
    """Return the Block Kit payload rendered for one pending question.

    When ``answered`` is True the ``Répondre`` button is omitted and the
    block header switches to a "Répondu" marker — used by ``chat_update``
    after the modal submission to close the loop visually.
    """
    header = ":question: *Question Squad*" if not answered else ":white_check_mark: *Répondue*"
    body = f"*{question.agent} / {question.phase}*\n{question.question}"
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{header}\n\n{body}"}},
    ]
    if not answered:
        blocks.append(
            {
                "type": "actions",
                "block_id": f"sq_q_{question.id}",
                "elements": [
                    {
                        "type": "button",
                        "action_id": QUESTION_ANSWER_ACTION_ID,
                        "text": {"type": "plain_text", "text": "Répondre"},
                        "value": question.id,
                        "style": "primary",
                    }
                ],
            }
        )
    return blocks


def build_question_modal(question: Question) -> dict:
    """Return the ``views_open`` payload for a single question.

    The ``question_id`` is embedded in ``private_metadata`` so the view
    submission handler can locate the DB row without trusting any text
    extracted from the thread.
    """
    return {
        "type": "modal",
        "callback_id": QUESTION_MODAL_CALLBACK_ID,
        "private_metadata": question.id,
        "title": {"type": "plain_text", "text": "Répondre à la question"},
        "submit": {"type": "plain_text", "text": "Envoyer"},
        "close": {"type": "plain_text", "text": "Annuler"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{question.agent} / {question.phase}*\n{question.question}",
                },
            },
            {
                "type": "input",
                "block_id": QUESTION_MODAL_INPUT_BLOCK_ID,
                "label": {"type": "plain_text", "text": "Réponse"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": QUESTION_MODAL_INPUT_ACTION_ID,
                    "multiline": True,
                },
            },
        ],
    }


def extract_modal_answer(view: dict) -> tuple[str | None, str]:
    """Return ``(question_id, answer)`` from a view submission payload."""
    question_id = (view or {}).get("private_metadata") or None
    state_values = ((view or {}).get("state") or {}).get("values") or {}
    block = state_values.get(QUESTION_MODAL_INPUT_BLOCK_ID) or {}
    entry = block.get(QUESTION_MODAL_INPUT_ACTION_ID) or {}
    answer = (entry.get("value") or "").strip()
    return question_id, answer


def post_question_message(
    client,
    session: Session,
    question: Question,
    db_path: Path | None = None,
) -> str | None:
    """Post one pending question in the session thread and persist its ``ts``.

    Returns the Slack message ``ts`` on success (also persisted in DB),
    or None when the session has no thread or the call fails. Failures
    are logged and swallowed — the CLI ``squad answer`` / ``squad resume``
    flow remains a fully functional fallback.
    """
    if not session.slack_channel or not session.slack_thread_ts:
        return None
    try:
        response = client.chat_postMessage(
            channel=session.slack_channel,
            thread_ts=session.slack_thread_ts,
            text=f"Question : {question.question}",
            blocks=build_question_blocks(question),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to post question %s in Slack", question.id)
        return None
    ts = (
        response.get("ts")
        if isinstance(response, dict)
        else getattr(response, "get", lambda _k: None)("ts")
    )
    if ts:
        update_question_slack_message_ts(question.id, ts, db_path=db_path)
    return ts


def post_pending_questions(
    client,
    session: Session,
    db_path: Path | None = None,
) -> list[str]:
    """Post every pending question for a session; return the Slack ``ts`` list.

    Questions that already carry a ``slack_message_ts`` are skipped so a
    crash-then-resume cycle does not duplicate the thread messages.
    """
    posted: list[str] = []
    for question in list_pending_questions(session.id, db_path=db_path):
        if question.slack_message_ts:
            continue
        ts = post_question_message(client, session, question, db_path=db_path)
        if ts:
            posted.append(ts)
    return posted


def update_question_message(
    client,
    session: Session,
    question: Question,
    *,
    answered: bool,
) -> None:
    """``chat_update`` the in-thread question message to reflect its new state.

    Silently no-ops when the session is CLI-only, the message has not
    been posted yet, or the Slack API errors out.
    """
    if not session.slack_channel or not question.slack_message_ts:
        return
    try:
        client.chat_update(
            channel=session.slack_channel,
            ts=question.slack_message_ts,
            text=(
                f"Question répondue : {question.question}"
                if answered
                else f"Question : {question.question}"
            ),
            blocks=build_question_blocks(question, answered=answered),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to chat_update question %s", question.id)


def post_question_ack(
    client,
    session: Session,
    question: Question,
    answer: str,
) -> None:
    """Post a short "answer received" reply in the thread after a submission."""
    preview = (answer[:160] + "…") if len(answer) > 160 else answer
    post_thread_message(
        client,
        session,
        f":white_check_mark: Réponse enregistrée pour _{question.agent} / {question.phase}_ :\n> {preview}",
    )
