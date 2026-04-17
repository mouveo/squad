"""Squad CLI entry point."""

import uuid
from pathlib import Path

import click

from squad import __version__
from squad.config import (
    get_config_value,
    get_global_config_path,
    get_global_db_path,
    get_project_config_path,
    get_project_state_dir,
    write_default_config,
)
from squad.constants import (
    MODE_APPROVAL,
    MODE_AUTONOMOUS,
    SESSION_MODES,
    STATUS_APPROVED,
    STATUS_FAILED,
    STATUS_INTERVIEWING,
    STATUS_REVIEW,
)
from squad.db import (
    answer_question,
    create_session,
    ensure_schema,
    get_session,
    list_active_sessions,
    list_pending_questions,
    list_session_history,
    update_session_status,
)
from squad.db import (
    list_plans as db_list_plans,
)
from squad.forge_bridge import (
    ForgeQueueBusy,
    ForgeUnavailable,
    submit_session_to_forge,
)
from squad.forge_format import validate_plan
from squad.notifier import notify_fallback_review, notify_queued
from squad.pipeline import PipelineError, resume_pipeline, run_pipeline
from squad.workspace import (
    create_workspace,
    get_context,
    sync_pending_questions,
    write_context,
    write_idea,
    write_plan,
)


def _derive_title(idea: str, max_len: int = 60) -> str:
    """Truncate idea to a concise session title."""
    idea = idea.strip()
    if len(idea) <= max_len:
        return idea
    return idea[:max_len].rstrip() + "…"


def _resolve_mode(project_path: str | None) -> str:
    """Return the execution mode from config, defaulting to approval.

    Used when no explicit ``--mode`` flag is given on the CLI; an
    unrecognised value in the config is silently ignored so a typo cannot
    block a session.
    """
    cfg_mode = get_config_value("mode", project_path=project_path)
    if cfg_mode in SESSION_MODES:
        return cfg_mode
    return MODE_APPROVAL


def _create_and_init_session(project_path: str, idea: str, mode: str, db_path: Path):
    """Create a session row + workspace and persist the idea / project context.

    Shared by ``start`` and ``run`` so both commands stay in sync. Returns
    the persisted ``Session`` (already with workspace files on disk).
    """
    session_id = str(uuid.uuid4())
    workspace_path = get_project_state_dir(project_path) / "sessions" / session_id
    title = _derive_title(idea)
    session = create_session(
        title=title,
        project_path=str(Path(project_path).resolve()),
        workspace_path=str(workspace_path),
        idea=idea,
        mode=mode,
        db_path=db_path,
        session_id=session_id,
    )
    create_workspace(session)
    write_idea(session.id, idea, db_path=db_path)
    context = get_context(project_path)
    write_context(session.id, context, db_path=db_path)
    return session


@click.group()
def cli() -> None:
    """Squad — AI product squad that turns ideas into Forge-executable plans."""


@cli.command()
def version() -> None:
    """Print the current version."""
    click.echo(f"squad {__version__}")


@cli.command()
@click.option(
    "--project",
    "project_path",
    type=click.Path(exists=True, file_okay=False),
    default=None,
    help="Initialise a project-level config at {PROJECT}/.squad/config.yaml.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing config file.",
)
def init(project_path: str | None, force: bool) -> None:
    """Write a default Squad config (global, or per project)."""
    target = (
        get_project_config_path(project_path)
        if project_path is not None
        else get_global_config_path()
    )
    if write_default_config(target, force=force):
        click.echo(f"Wrote default config to {target}")
    else:
        click.echo(f"Config already exists at {target} (use --force to overwrite).")


@cli.command()
@click.argument("project_path", type=click.Path(exists=True, file_okay=False))
@click.argument("idea")
@click.option(
    "--mode",
    type=click.Choice(SESSION_MODES),
    default=None,
    help=(
        "Execution mode: wait for user approval or run fully autonomous. "
        "Falls back to the configured `mode` (default: approval) when omitted."
    ),
)
def start(project_path: str, idea: str, mode: str | None) -> None:
    """Start a new Squad session for PROJECT_PATH with IDEA."""
    db_path = get_global_db_path()
    ensure_schema(db_path)

    if mode is None:
        mode = _resolve_mode(project_path)

    session = _create_and_init_session(project_path, idea, mode, db_path)

    click.echo(f"Session started: {session.id}")
    click.echo(f"  Title   : {session.title}")
    click.echo(f"  Mode    : {session.mode}")
    click.echo(f"  Project : {session.project_path}")
    click.echo(f"  Status  : {session.status}")

    try:
        run_pipeline(session.id, db_path=db_path)
    except PipelineError as exc:
        raise click.ClickException(f"Pipeline failed: {exc}")
    except Exception as exc:
        raise click.ClickException(f"Pipeline failed: {exc}")

    final = get_session(session.id, db_path=db_path)
    if final is not None:
        click.echo(f"Pipeline finished with status: {final.status}")

    # Autonomous mode: push the generated plans straight to Forge. On any
    # bridge failure, fall back to review (plans stay in the workspace).
    if final is not None and final.mode == MODE_AUTONOMOUS and final.status == STATUS_REVIEW:
        _autonomous_submit(session.id, final.title, db_path=db_path)


@cli.command(name="run")
@click.argument("project_path", type=click.Path(exists=True, file_okay=False))
@click.argument("idea")
@click.option(
    "--mode",
    type=click.Choice(SESSION_MODES),
    default=None,
    help=(
        "Execution mode (overrides the configured `mode` when provided). "
        "Defaults to `approval`, which drives questions and review inline."
    ),
)
def run_cmd(project_path: str, idea: str, mode: str | None) -> None:
    """One-shot: start, answer pending questions inline, review, submit.

    ``squad run`` orchestrates the same primitives as the asynchronous
    commands (``start`` → ``answer`` → ``resume`` → ``review`` →
    ``approve``) without chaining the CLI commands themselves. In
    ``autonomous`` mode the interactive prompts are skipped and the
    submission to Forge happens automatically when the pipeline reaches
    ``review``.
    """
    db_path = get_global_db_path()
    ensure_schema(db_path)

    if mode is None:
        mode = _resolve_mode(project_path)

    session = _create_and_init_session(project_path, idea, mode, db_path)

    click.echo(f"Session started: {session.id}")
    click.echo(f"  Mode    : {session.mode}")
    click.echo(f"  Project : {session.project_path}")

    try:
        run_pipeline(session.id, db_path=db_path)
    except PipelineError as exc:
        raise click.ClickException(f"Pipeline failed: {exc}") from exc

    if session.mode == MODE_APPROVAL:
        _drive_interactive_questions(session.id, db_path=db_path)

    final = get_session(session.id, db_path=db_path)
    if final is None:
        raise click.ClickException(f"Session vanished: {session.id}")

    click.echo(f"\nPipeline finished with status: {final.status}")

    if final.status != STATUS_REVIEW:
        return

    if final.mode == MODE_AUTONOMOUS:
        _autonomous_submit(session.id, final.title, db_path=db_path)
    else:
        _interactive_review_and_submit(session.id, final.title, db_path=db_path)


def _autonomous_submit(session_id: str, title: str, db_path: Path) -> None:
    """Submit plans to Forge for an autonomous session; fall back to review on error."""
    try:
        outcome = submit_session_to_forge(session_id, db_path=db_path)
    except (ForgeUnavailable, ForgeQueueBusy, ValueError) as exc:
        notify_fallback_review(session_id, title, str(exc))
        click.echo(
            f"Autonomous submit failed ({exc}); session left in review "
            f"status — run `squad approve {session_id}` once Forge is available."
        )
        return
    notify_queued(session_id, title, outcome.plans_sent)
    click.echo(
        f"Autonomous submit ok: {outcome.plans_sent} plan(s) queued "
        f"(queue_started={outcome.queue_started})."
    )


# ── interactive helpers (used by `squad run`) ─────────────────────────────────


def _drive_interactive_questions(session_id: str, db_path: Path) -> None:
    """Loop while the session is paused on questions: ask, persist, resume.

    Reuses ``answer_question`` + ``sync_pending_questions`` + ``resume_pipeline``
    rather than shelling into the ``answer``/``resume`` commands so the
    transition path stays single-sourced through the pipeline primitives.
    """
    while True:
        session = get_session(session_id, db_path=db_path)
        if session is None or session.status != STATUS_INTERVIEWING:
            return

        pending = list_pending_questions(session_id, db_path=db_path)
        if not pending:
            # Status says interviewing but nothing to ask — try to resume
            # so the pipeline can re-evaluate and either advance or fail.
            try:
                resume_pipeline(session_id, db_path=db_path)
            except PipelineError as exc:
                raise click.ClickException(f"Pipeline failed during resume: {exc}") from exc
            continue

        click.echo(f"\n{len(pending)} question(s) en attente :")
        for q in pending:
            click.echo(f"\n  [{q.agent} / {q.phase}] {q.question}")
            answer = click.prompt("  Réponse", default="", show_default=False).strip()
            if not answer:
                raise click.ClickException(
                    "Empty answer; aborting interactive run "
                    "(use `squad answer` to retry asynchronously)."
                )
            answer_question(q.id, answer, db_path=db_path)

        sync_pending_questions(session_id, db_path=db_path)

        try:
            resume_pipeline(session_id, db_path=db_path)
        except PipelineError as exc:
            raise click.ClickException(f"Pipeline failed during resume: {exc}") from exc


def _interactive_review_and_submit(session_id: str, title: str, db_path: Path) -> None:
    """Display generated plans inline and prompt for approve / reject / quit.

    On approve: persist ``approved`` then submit to Forge; on Forge error,
    revert to ``review`` and notify (same fallback as ``squad approve``).
    On reject: mark the session ``failed``. On quit: leave it in
    ``review`` so the user can still drive it via ``squad review`` /
    ``squad approve`` later.
    """
    plans = db_list_plans(session_id, db_path=db_path)
    click.echo(f"\n{len(plans)} plan(s) prêt(s) pour validation :\n")
    for plan in plans:
        click.echo(f"=== {plan.title} ({plan.file_path}) ===\n")
        click.echo(plan.content)
        click.echo("")

    decision = click.prompt(
        "Approuver et envoyer à Forge ? [y]es / [n]o / [q]uit",
        type=click.Choice(["y", "n", "q"]),
        default="y",
        show_choices=False,
    )

    if decision == "n":
        update_session_status(session_id, STATUS_FAILED, db_path=db_path)
        click.echo(f"Session {session_id} marquée comme failed.")
        return
    if decision == "q":
        click.echo(
            f"Session {session_id} laissée en review. "
            f"Reprenez avec `squad review` ou `squad approve {session_id}`."
        )
        return

    # decision == "y"
    update_session_status(session_id, STATUS_APPROVED, db_path=db_path)
    try:
        outcome = submit_session_to_forge(session_id, db_path=db_path)
    except (ForgeUnavailable, ForgeQueueBusy, ValueError) as exc:
        update_session_status(session_id, STATUS_REVIEW, db_path=db_path)
        notify_fallback_review(session_id, title, str(exc))
        raise click.ClickException(
            f"Forge submit failed: {exc}. Session reverted to review."
        ) from exc
    notify_queued(session_id, title, outcome.plans_sent)
    click.echo(
        f"Approved and queued {outcome.plans_sent} plan(s) (queue_started={outcome.queue_started})."
    )


@cli.command()
@click.argument("session_id", required=False)
def status(session_id: str | None) -> None:
    """Show status of SESSION_ID, or list all active sessions."""
    db_path = get_global_db_path()
    ensure_schema(db_path)

    if session_id:
        session = get_session(session_id, db_path=db_path)
        if not session:
            raise click.ClickException(f"Session not found: {session_id}")
        click.echo(f"ID      : {session.id}")
        click.echo(f"Title   : {session.title}")
        click.echo(f"Status  : {session.status}")
        click.echo(f"Mode    : {session.mode}")
        click.echo(f"Phase   : {session.current_phase or '—'}")
        click.echo(f"Project : {session.project_path}")
        click.echo(f"Created : {session.created_at.strftime('%Y-%m-%d %H:%M')}")
    else:
        sessions = list_active_sessions(db_path=db_path)
        if not sessions:
            click.echo("No active sessions.")
            return
        for s in sessions:
            phase = s.current_phase or "—"
            click.echo(f"{s.id[:8]}  [{s.status:12s}]  {phase:20s}  {s.title}")


@cli.command()
@click.argument("session_id")
@click.argument("question_id")
@click.argument("answer_text")
def answer(session_id: str, question_id: str, answer_text: str) -> None:
    """Record an answer to a pending question and keep pending.json in sync."""
    db_path = get_global_db_path()
    ensure_schema(db_path)

    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise click.ClickException(f"Session not found: {session_id}")

    answer_question(question_id, answer_text, db_path=db_path)
    sync_pending_questions(session_id, db_path=db_path)

    remaining = list_pending_questions(session_id, db_path=db_path)
    click.echo(f"Answer recorded for question {question_id}.")
    click.echo(f"Remaining pending questions: {len(remaining)}")


@cli.command()
@click.argument("session_id")
def resume(session_id: str) -> None:
    """Resume a paused or crashed session at its next safe phase."""
    db_path = get_global_db_path()
    ensure_schema(db_path)

    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise click.ClickException(f"Session not found: {session_id}")

    try:
        resume_point = resume_pipeline(session_id, db_path=db_path)
    except PipelineError as exc:
        raise click.ClickException(f"Pipeline failed: {exc}") from exc
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    if resume_point is None:
        click.echo(f"Nothing to resume (status: {session.status}).")
        return

    click.echo(f"Resumed at phase {resume_point.phase} — {resume_point.reason}")
    final = get_session(session_id, db_path=db_path)
    if final is not None:
        click.echo(f"Pipeline finished with status: {final.status}")


@cli.command()
@click.argument("session_id")
@click.option(
    "--action",
    type=click.Choice(["show", "approve", "reject", "edit"]),
    default="show",
    show_default=True,
    help="Action to perform on the reviewed plans.",
)
def review(session_id: str, action: str) -> None:
    """Review generated plans for SESSION_ID and optionally approve/reject/edit.

    Without ``--action`` (or with ``show``), prints each plan and its
    workspace path. ``--action approve`` flips the session to
    ``approved`` (leaving actual submission to ``squad approve``).
    ``--action reject`` marks the session as ``failed``. ``--action edit``
    opens each plan in ``$EDITOR``; edits must keep the Forge format.
    """
    db_path = get_global_db_path()
    ensure_schema(db_path)

    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise click.ClickException(f"Session not found: {session_id}")

    plans = db_list_plans(session_id, db_path=db_path)
    if not plans:
        raise click.ClickException(f"No plans to review for session {session_id}")

    click.echo(f"Session {session_id} — mode={session.mode} status={session.status}")
    click.echo(f"Project: {session.project_path}")
    click.echo(f"Plans  : {len(plans)}")

    if action == "show":
        for plan in plans:
            click.echo(f"\n=== {plan.title} ({plan.file_path}) ===\n")
            click.echo(plan.content)
        return

    if action == "approve":
        update_session_status(session_id, STATUS_APPROVED, db_path=db_path)
        click.echo(
            f"Session {session_id} marked as approved. "
            f"Run `squad approve {session_id}` to push plans to Forge."
        )
        return

    if action == "reject":
        update_session_status(session_id, STATUS_FAILED, db_path=db_path)
        click.echo(f"Session {session_id} marked as failed.")
        return

    # action == "edit"
    for plan in plans:
        click.echo(f"\nOpening editor for plan: {plan.title}")
        edited = click.edit(plan.content)
        if edited is None:
            click.echo(f"  (no changes to {plan.title})")
            continue
        result = validate_plan(edited)
        if not result.valid:
            raise click.ClickException(
                f"Edited plan {plan.title!r} is invalid: " + "; ".join(result.errors)
            )
        # Persist the edited version in the workspace and DB
        write_plan(session_id, plan.title, edited, db_path=db_path)
        click.echo(f"  Saved edited {plan.title}")


@cli.command()
@click.argument("session_id")
def approve(session_id: str) -> None:
    """Send the approved plans of SESSION_ID to Forge's queue.

    Transitions the session from ``review`` or ``approved`` to ``queued``
    on success. On any Forge failure the session stays in ``review`` and
    a Slack notification is sent; the generated plans remain intact.
    """
    db_path = get_global_db_path()
    ensure_schema(db_path)

    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise click.ClickException(f"Session not found: {session_id}")

    if session.status not in {STATUS_REVIEW, STATUS_APPROVED}:
        raise click.ClickException(
            f"Session {session_id} is in status {session.status!r}; "
            "expected 'review' or 'approved'."
        )

    if session.status == STATUS_REVIEW:
        update_session_status(session_id, STATUS_APPROVED, db_path=db_path)

    try:
        outcome = submit_session_to_forge(session_id, db_path=db_path)
    except (ForgeUnavailable, ForgeQueueBusy, ValueError) as exc:
        update_session_status(session_id, STATUS_REVIEW, db_path=db_path)
        notify_fallback_review(session_id, session.title, str(exc))
        raise click.ClickException(
            f"Forge submit failed: {exc}. Session reverted to review."
        ) from exc

    notify_queued(session_id, session.title, outcome.plans_sent)
    click.echo(
        f"Approved and queued {outcome.plans_sent} plan(s) (queue_started={outcome.queue_started})."
    )


@cli.command()
@click.option("--project", "project_path", default=None, help="Filter by project path.")
@click.option("--limit", default=10, show_default=True, help="Maximum number of sessions.")
def history(project_path: str | None, limit: int) -> None:
    """Show recent session history."""
    db_path = get_global_db_path()
    ensure_schema(db_path)

    sessions = list_session_history(project_path=project_path, limit=limit, db_path=db_path)
    if not sessions:
        click.echo("No sessions found.")
        return
    for s in sessions:
        date = s.created_at.strftime("%Y-%m-%d")
        click.echo(f"{s.id[:8]}  {date}  [{s.status:8s}]  {s.title}")
