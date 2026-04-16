"""Configuration helpers — filesystem paths for Squad home and project state."""

from pathlib import Path


def get_squad_home() -> Path:
    """Return the global Squad home directory (~/.squad)."""
    return Path.home() / ".squad"


def get_global_db_path() -> Path:
    """Return the path to the global SQLite database."""
    return get_squad_home() / "squad.db"


def get_project_state_dir(project_path: str | Path) -> Path:
    """Return the .squad state directory inside a project."""
    return Path(project_path) / ".squad"
