"""Tests for squad/workspace.py — directory structure and artifact read/write."""

from pathlib import Path

import pytest

from squad.constants import PHASE_CADRAGE, PHASE_CONCEPTION, PHASE_DIRS
from squad.db import create_session, ensure_schema
from squad.models import Session
from squad.workspace import (
    create_workspace,
    get_context,
    get_session_workspace,
    list_plans,
    read_pending_questions,
    read_phase_outputs,
    write_context,
    write_idea,
    write_pending_questions,
    write_phase_output,
    write_plan,
)

# ── fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / ".squad" / "squad.db"
    ensure_schema(path)
    return path


@pytest.fixture
def project_path(tmp_path: Path) -> Path:
    p = tmp_path / "my-project"
    p.mkdir()
    return p


@pytest.fixture
def session(db_path: Path, project_path: Path) -> Session:
    workspace_path = project_path / ".squad" / "sessions" / "sess-test"
    return create_session(
        title="Test session",
        project_path=str(project_path),
        workspace_path=str(workspace_path),
        idea="improve the CRM",
        db_path=db_path,
    )


@pytest.fixture
def workspace(session: Session) -> Path:
    return create_workspace(session)


# ── create_workspace ───────────────────────────────────────────────────────────


class TestCreateWorkspace:
    def test_returns_path(self, workspace: Path):
        assert isinstance(workspace, Path)
        assert workspace.exists()

    def test_creates_all_phase_dirs(self, workspace: Path):
        for phase_dir_name in PHASE_DIRS.values():
            assert (workspace / "phases" / phase_dir_name).is_dir(), (
                f"Missing phase directory: {phase_dir_name}"
            )

    def test_creates_support_dirs(self, workspace: Path):
        for subdir in ("questions", "plans", "research"):
            assert (workspace / subdir).is_dir(), f"Missing directory: {subdir}"

    def test_no_agent_files_precreated(self, workspace: Path):
        for phase_dir_name in PHASE_DIRS.values():
            phase_dir = workspace / "phases" / phase_dir_name
            assert list(phase_dir.glob("*.md")) == [], (
                f"Agent files should not be pre-created in {phase_dir_name}"
            )

    def test_idempotent(self, session: Session):
        create_workspace(session)
        create_workspace(session)  # second call must not raise


# ── get_session_workspace ──────────────────────────────────────────────────────


class TestGetSessionWorkspace:
    def test_resolves_from_db(self, session: Session, workspace: Path, db_path: Path):
        resolved = get_session_workspace(session.id, db_path=db_path)
        assert resolved == Path(session.workspace_path)

    def test_raises_for_unknown_session(self, db_path: Path):
        with pytest.raises(ValueError, match="Session not found"):
            get_session_workspace("nonexistent", db_path=db_path)


# ── write_idea ─────────────────────────────────────────────────────────────────


class TestWriteIdea:
    def test_writes_file(self, session: Session, workspace: Path, db_path: Path):
        path = write_idea(session.id, "improve the CRM", db_path=db_path)
        assert path.exists()
        assert path.name == "idea.md"
        assert path.read_text() == "improve the CRM"

    def test_overwrites_existing(self, session: Session, workspace: Path, db_path: Path):
        write_idea(session.id, "first idea", db_path=db_path)
        write_idea(session.id, "second idea", db_path=db_path)
        idea_file = Path(session.workspace_path) / "idea.md"
        assert idea_file.read_text() == "second idea"


# ── write_context / get_context ────────────────────────────────────────────────


class TestWriteContext:
    def test_writes_file(self, session: Session, workspace: Path, db_path: Path):
        path = write_context(session.id, "# Project context\nsome details", db_path=db_path)
        assert path.exists()
        assert path.name == "context.md"
        assert "context" in path.read_text()


class TestGetContext:
    def test_returns_claude_md_when_present(self, project_path: Path):
        (project_path / "CLAUDE.md").write_text("# My project\nsome context")
        context = get_context(str(project_path))
        assert "# My project" in context
        assert "some context" in context

    def test_returns_minimal_stub_when_absent(self, project_path: Path):
        context = get_context(str(project_path))
        assert "No CLAUDE.md" in context
        assert project_path.name in context

    def test_stub_includes_project_path(self, project_path: Path):
        context = get_context(str(project_path))
        assert str(project_path) in context


# ── write_phase_output / read_phase_outputs ────────────────────────────────────


class TestPhaseOutputs:
    def test_write_creates_agent_file(self, session: Session, workspace: Path, db_path: Path):
        path = write_phase_output(
            session.id, PHASE_CADRAGE, "pm", "cadrage output", db_path=db_path
        )
        assert path.exists()
        assert path.name == "pm.md"
        assert "cadrage output" in path.read_text()

    def test_file_in_correct_phase_dir(self, session: Session, workspace: Path, db_path: Path):
        path = write_phase_output(session.id, PHASE_CADRAGE, "pm", "output", db_path=db_path)
        expected_dir = workspace / "phases" / PHASE_DIRS[PHASE_CADRAGE]
        assert path.parent == expected_dir

    def test_read_all_phases(self, session: Session, workspace: Path, db_path: Path):
        write_phase_output(session.id, PHASE_CADRAGE, "pm", "pm output", db_path=db_path)
        write_phase_output(session.id, PHASE_CONCEPTION, "ux", "ux output", db_path=db_path)
        outputs = read_phase_outputs(session.id, db_path=db_path)
        assert PHASE_CADRAGE in outputs
        assert PHASE_CONCEPTION in outputs
        assert outputs[PHASE_CADRAGE]["pm"] == "pm output"
        assert outputs[PHASE_CONCEPTION]["ux"] == "ux output"

    def test_read_single_phase(self, session: Session, workspace: Path, db_path: Path):
        write_phase_output(session.id, PHASE_CADRAGE, "pm", "pm output", db_path=db_path)
        write_phase_output(session.id, PHASE_CONCEPTION, "ux", "ux output", db_path=db_path)
        outputs = read_phase_outputs(session.id, phase=PHASE_CADRAGE, db_path=db_path)
        assert PHASE_CADRAGE in outputs
        assert PHASE_CONCEPTION not in outputs

    def test_empty_phase_excluded_from_read_all(
        self, session: Session, workspace: Path, db_path: Path
    ):
        write_phase_output(session.id, PHASE_CADRAGE, "pm", "content", db_path=db_path)
        outputs = read_phase_outputs(session.id, db_path=db_path)
        assert PHASE_CADRAGE in outputs
        assert PHASE_CONCEPTION not in outputs

    def test_multiple_agents_same_phase(self, session: Session, workspace: Path, db_path: Path):
        write_phase_output(session.id, PHASE_CONCEPTION, "ux", "ux out", db_path=db_path)
        write_phase_output(session.id, PHASE_CONCEPTION, "architect", "arch out", db_path=db_path)
        outputs = read_phase_outputs(session.id, phase=PHASE_CONCEPTION, db_path=db_path)
        assert set(outputs[PHASE_CONCEPTION].keys()) == {"ux", "architect"}


# ── write_plan / list_plans ────────────────────────────────────────────────────


class TestPlans:
    def test_write_creates_file(self, session: Session, workspace: Path, db_path: Path):
        path = write_plan(session.id, "Plan 1 — CRM", "## LOT 1\n...", db_path=db_path)
        assert path.exists()
        assert path.suffix == ".md"
        assert "## LOT 1" in path.read_text()

    def test_slug_used_as_filename(self, session: Session, workspace: Path, db_path: Path):
        path = write_plan(session.id, "Plan 1 — CRM leads", "content", db_path=db_path)
        assert path.name == "plan-1-crm-leads.md"

    def test_list_plans_returns_files(self, session: Session, workspace: Path, db_path: Path):
        write_plan(session.id, "Plan A", "content a", db_path=db_path)
        write_plan(session.id, "Plan B", "content b", db_path=db_path)
        plans = list_plans(session.id, db_path=db_path)
        assert len(plans) == 2

    def test_list_plans_empty_initially(self, session: Session, workspace: Path, db_path: Path):
        assert list_plans(session.id, db_path=db_path) == []

    def test_list_plans_sorted(self, session: Session, workspace: Path, db_path: Path):
        write_plan(session.id, "zz plan", "z", db_path=db_path)
        write_plan(session.id, "aa plan", "a", db_path=db_path)
        plans = list_plans(session.id, db_path=db_path)
        names = [p.name for p in plans]
        assert names == sorted(names)


# ── write/read pending questions ───────────────────────────────────────────────


class TestPendingQuestions:
    def test_write_and_read(self, session: Session, workspace: Path, db_path: Path):
        questions = [
            {"id": "q1", "question": "Who is the target?", "agent": "pm"},
            {"id": "q2", "question": "What is the timeline?", "agent": "pm"},
        ]
        write_pending_questions(session.id, questions, db_path=db_path)
        result = read_pending_questions(session.id, db_path=db_path)
        assert result == questions

    def test_read_returns_empty_list_when_no_file(
        self, session: Session, workspace: Path, db_path: Path
    ):
        assert read_pending_questions(session.id, db_path=db_path) == []

    def test_write_overwrites_previous(self, session: Session, workspace: Path, db_path: Path):
        write_pending_questions(session.id, [{"id": "q1", "question": "Q1?"}], db_path=db_path)
        write_pending_questions(session.id, [{"id": "q2", "question": "Q2?"}], db_path=db_path)
        result = read_pending_questions(session.id, db_path=db_path)
        assert len(result) == 1
        assert result[0]["id"] == "q2"

    def test_unicode_preserved(self, session: Session, workspace: Path, db_path: Path):
        questions = [{"id": "q1", "question": "Quel est le périmètre ?"}]
        write_pending_questions(session.id, questions, db_path=db_path)
        result = read_pending_questions(session.id, db_path=db_path)
        assert result[0]["question"] == "Quel est le périmètre ?"
