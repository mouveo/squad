"""Squad CLI entry point."""

import click

from squad import __version__


@click.group()
def cli() -> None:
    """Squad — AI product squad that turns ideas into Forge-executable plans."""


@cli.command()
def version() -> None:
    """Print the current version."""
    click.echo(f"squad {__version__}")
