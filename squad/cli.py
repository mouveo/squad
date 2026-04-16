"""Squad CLI entry point."""

import uuid
from pathlib import Path

import click

from squad import __version__
from squad.config import get_global_db_path, get_project_state_dir
from squad.db import (
    create_session,
    ensure_schema,
    get_session,
    list_active_sessions,
    list_session_history,
)
from squad.workspace import create_workspace, get_context, write_context, write_idea


def _derive_title(idea: str, max_len: int = 60) -> str:
    """Truncate idea to a concise session title."""
    idea = idea.strip()
    if len(idea) <= max_len:
        return idea
    return idea[:max_len].rstrip() + "…"


@click.group()
def cli() -> None:
    """Squad — AI product squad that turns ideas into Forge-executable plans."""


@cli.command()
def version() -> None:
    """Print the current version."""
    click.echo(f"squad {__version__}")


@cli.command()
@click.argument("project_path", type=click.Path(exists=True, file_okay=False))
@click.argument("idea")
@click.option(
    "--mode",
    type=click.Choice(["approval", "autonomous"]),
    default="approval",
    show_default=True,
    help="Execution mode: wait for user approval or run fully autonomous.",
)
def start(project_path: str, idea: str, mode: str) -> None:
    """Start a new Squad session for PROJECT_PATH with IDEA."""
    db_path = get_global_db_path()
    ensure_schema(db_path)

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

    click.echo(f"Session started: {session.id}")
    click.echo(f"  Title   : {session.title}")
    click.echo(f"  Mode    : {session.mode}")
    click.echo(f"  Project : {session.project_path}")
    click.echo(f"  Status  : {session.status}")


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
