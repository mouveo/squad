"""Shared pytest fixtures."""

from pathlib import Path

import pytest


@pytest.fixture
def squad_home(tmp_path: Path) -> Path:
    """Temporary Squad home directory (~/.squad equivalent)."""
    home = tmp_path / ".squad"
    home.mkdir()
    return home


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Temporary target project directory."""
    project = tmp_path / "my-project"
    project.mkdir()
    return project
