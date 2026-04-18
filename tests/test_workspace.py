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
    list_benchmarks,
    list_plans,
    read_benchmark,
    read_pending_questions,
    read_phase_outputs,
    write_benchmark,
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
        for subdir in ("questions", "plans", "research", "attachments"):
            assert (workspace / subdir).is_dir(), f"Missing directory: {subdir}"

    def test_creates_attachments_dir(self, workspace: Path):
        # Plan 4 — LOT 3: Slack drag-drops land here.
        attachments = workspace / "attachments"
        assert attachments.is_dir()
        assert list(attachments.iterdir()) == []

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
        assert "### CLAUDE.md" in context

    def test_empty_project_signals_absence(self, project_path: Path):
        context = get_context(str(project_path))
        assert "No CLAUDE.md, README, manifests" in context
        assert project_path.name in context

    def test_header_includes_project_path(self, project_path: Path):
        context = get_context(str(project_path))
        assert str(project_path) in context

    def test_includes_readme_when_present(self, project_path: Path):
        (project_path / "README.md").write_text("# Sitavista\n\nProduction SaaS")
        context = get_context(str(project_path))
        assert "### README.md" in context
        assert "Production SaaS" in context

    def test_includes_manifests_for_stack(self, project_path: Path):
        (project_path / "package.json").write_text(
            '{"name": "sitavista", "dependencies": {"next": "14.0.0"}}'
        )
        (project_path / "composer.json").write_text('{"require": {"php": "^8.2"}}')
        context = get_context(str(project_path))
        assert "### Manifests" in context
        assert "package.json" in context
        assert '"next"' in context
        assert "composer.json" in context
        assert "php" in context

    def test_includes_top_level_tree(self, project_path: Path):
        (project_path / "app").mkdir()
        (project_path / "src").mkdir()
        (project_path / "node_modules").mkdir()  # must be excluded
        (project_path / ".git").mkdir()  # must be excluded
        (project_path / "README.md").write_text("x")
        context = get_context(str(project_path))
        assert "### Top-level tree" in context
        assert "app/" in context
        assert "src/" in context
        assert "README.md" in context
        assert "node_modules" not in context
        assert ".git" not in context

    def test_truncates_large_readme(self, project_path: Path):
        huge = "x" * 10_000
        (project_path / "README.md").write_text(huge)
        context = get_context(str(project_path))
        assert "[… truncated]" in context
        assert len(context) < 10_000 + 1_000  # budget + overhead

    def test_includes_git_log_when_repo(self, project_path: Path):
        import subprocess as sp

        sp.run(["git", "init", "-q"], cwd=project_path, check=False)
        sp.run(["git", "config", "user.email", "t@t"], cwd=project_path, check=False)
        sp.run(["git", "config", "user.name", "t"], cwd=project_path, check=False)
        (project_path / "a.txt").write_text("hi")
        sp.run(["git", "add", "."], cwd=project_path, check=False)
        sp.run(
            ["git", "commit", "-q", "-m", "initial commit msg"],
            cwd=project_path,
            check=False,
        )
        context = get_context(str(project_path))
        assert "### Recent commits" in context
        assert "initial commit msg" in context


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


# ── research / benchmark ───────────────────────────────────────────────────────


class TestBenchmark:
    def test_write_returns_path(self, session: Session, workspace: Path, db_path: Path):
        path = write_benchmark(session.id, "my-idea", "# Benchmark\ncontent", db_path=db_path)
        assert path.exists()
        assert path.parent == workspace / "research"
        assert path.name == "benchmark-my-idea.md"

    def test_write_slug_is_sanitised(self, session: Session, db_path: Path):
        path = write_benchmark(session.id, "B2B SaaS / CRM !!", "body", db_path=db_path)
        assert "b2b-saas" in path.name
        assert "/" not in path.name

    def test_read_returns_content(self, session: Session, db_path: Path):
        write_benchmark(session.id, "s", "# Benchmark\nhi", db_path=db_path)
        assert read_benchmark(session.id, "s", db_path=db_path) == "# Benchmark\nhi"

    def test_read_missing_returns_none(self, session: Session, db_path: Path):
        assert read_benchmark(session.id, "ghost", db_path=db_path) is None

    def test_list_benchmarks_sorted(self, session: Session, db_path: Path):
        write_benchmark(session.id, "beta", "b", db_path=db_path)
        write_benchmark(session.id, "alpha", "a", db_path=db_path)
        names = [p.name for p in list_benchmarks(session.id, db_path=db_path)]
        assert names == ["benchmark-alpha.md", "benchmark-beta.md"]

    def test_overwrite_existing_benchmark(self, session: Session, db_path: Path):
        write_benchmark(session.id, "s", "v1", db_path=db_path)
        write_benchmark(session.id, "s", "v2", db_path=db_path)
        assert read_benchmark(session.id, "s", db_path=db_path) == "v2"

    def test_unicode_preserved(self, session: Session, db_path: Path):
        write_benchmark(session.id, "unicode", "# Benchmark\néèà", db_path=db_path)
        assert "éèà" in read_benchmark(session.id, "unicode", db_path=db_path)


# ── sync_pending_questions ─────────────────────────────────────────────────────


from squad.db import answer_question, create_question  # noqa: E402
from squad.workspace import sync_pending_questions  # noqa: E402


class TestSyncPendingQuestions:
    def test_writes_current_db_state(self, session: Session, workspace: Path, db_path: Path):
        q1 = create_question(session.id, "pm", "cadrage", "q1?", db_path=db_path)
        create_question(session.id, "pm", "cadrage", "q2?", db_path=db_path)
        path = sync_pending_questions(session.id, db_path=db_path)
        assert path.exists()
        content = path.read_text()
        assert q1.id in content
        assert "q1?" in content
        assert "q2?" in content

    def test_drops_answered_questions(self, session: Session, workspace: Path, db_path: Path):
        q1 = create_question(session.id, "pm", "cadrage", "q1?", db_path=db_path)
        create_question(session.id, "pm", "cadrage", "q2?", db_path=db_path)
        answer_question(q1.id, "answer", db_path=db_path)
        sync_pending_questions(session.id, db_path=db_path)
        content = (workspace / "questions" / "pending.json").read_text()
        assert "q2?" in content
        assert "q1?" not in content

    def test_empty_when_all_answered(self, session: Session, workspace: Path, db_path: Path):
        q = create_question(session.id, "pm", "cadrage", "only?", db_path=db_path)
        answer_question(q.id, "yes", db_path=db_path)
        sync_pending_questions(session.id, db_path=db_path)
        content = (workspace / "questions" / "pending.json").read_text()
        assert content.strip() == "[]"


# ── copy_plans_to_project ──────────────────────────────────────────────────────


from squad.workspace import copy_plans_to_project  # noqa: E402


class TestCopyPlansToProject:
    def test_copies_workspace_plans_to_project(
        self, session: Session, workspace: Path, db_path: Path, project_path: Path
    ):
        (workspace / "plans" / "p1.md").write_text("content p1")
        (workspace / "plans" / "p2.md").write_text("content p2")
        copied = copy_plans_to_project(session.id, db_path=db_path)
        assert len(copied) == 2
        assert (project_path / "plans" / "p1.md").read_text() == "content p1"
        assert (project_path / "plans" / "p2.md").read_text() == "content p2"

    def test_overwrites_existing_target(
        self, session: Session, workspace: Path, db_path: Path, project_path: Path
    ):
        (workspace / "plans" / "p1.md").write_text("v2")
        (project_path / "plans").mkdir()
        (project_path / "plans" / "p1.md").write_text("v1")
        copy_plans_to_project(session.id, db_path=db_path)
        assert (project_path / "plans" / "p1.md").read_text() == "v2"

    def test_empty_when_no_plans(
        self, session: Session, workspace: Path, db_path: Path, project_path: Path
    ):
        assert copy_plans_to_project(session.id, db_path=db_path) == []

    def test_unknown_session_raises(self, db_path: Path):
        with pytest.raises(ValueError):
            copy_plans_to_project("ghost", db_path=db_path)

    def test_creates_target_dir(
        self, session: Session, workspace: Path, db_path: Path, project_path: Path
    ):
        (workspace / "plans" / "p.md").write_text("x")
        copy_plans_to_project(session.id, db_path=db_path)
        assert (project_path / "plans").is_dir()
