"""Tests for squad/plan_generator.py — prompt build, generate, persistence."""

from pathlib import Path
from unittest.mock import patch

import pytest

from squad.constants import PHASE_CHALLENGE, PHASE_SYNTHESE
from squad.db import (
    create_phase_output,
    create_session,
    ensure_schema,
)
from squad.db import (
    list_plans as db_list_plans,
)
from squad.forge_format import ForgeFormatError
from squad.plan_generator import (
    build_plan_prompt,
    copy_plans_to_project,
    generate_plans,
    generate_plans_from_session,
)
from squad.workspace import create_workspace, list_plans

# ── fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "s.db"
    ensure_schema(path)
    return path


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    p = tmp_path / "target"
    p.mkdir()
    return p


@pytest.fixture
def session(db_path: Path, project_dir: Path, tmp_path: Path):
    s = create_session(
        title="Test",
        project_path=str(project_dir),
        workspace_path=str(tmp_path / "ws"),
        idea="Build a CRM for SMBs",
        db_path=db_path,
    )
    create_workspace(s)
    return s


def _lot(n: int) -> str:
    return f"## LOT {n} — Title {n}\n\nBody for lot {n}.\n\n**Files**: `file_{n}.py`\n"


def _valid_plan(n_lots: int = 6) -> str:
    return (
        "# project — Plan 1/1: Test plan\n\n"
        "> Description.\n> Prérequis : aucun.\n\n"
        "---\n\n" + "\n\n".join(_lot(i) for i in range(1, n_lots + 1))
    )


# ── build_plan_prompt ──────────────────────────────────────────────────────────


class TestBuildPlanPrompt:
    def test_contains_core_sections(self):
        prompt = build_plan_prompt(
            project_name="p",
            project_path="/tmp/p",
            idea="idea",
            decision_summary="ship it",
            plan_inputs=["A", "B"],
            open_questions=["Q1"],
            unresolved_blockers=["must add rate limiting"],
        )
        assert "Decision summary" in prompt
        assert "ship it" in prompt
        assert "- A" in prompt
        assert "Unresolved blockers" in prompt
        assert "rate limiting" in prompt

    def test_handles_empty_lists(self):
        prompt = build_plan_prompt(
            project_name="p",
            project_path="/tmp/p",
            idea="idea",
            decision_summary="",
            plan_inputs=[],
            open_questions=[],
            unresolved_blockers=[],
        )
        assert "(none)" in prompt


# ── generate_plans ─────────────────────────────────────────────────────────────


class TestGeneratePlans:
    def test_happy_path_persists_plan(self, session, db_path):
        with patch(
            "squad.plan_generator.run_task_text",
            return_value=_valid_plan(6),
        ):
            drafts = generate_plans(
                session_id=session.id,
                decision_summary="ship it",
                plan_inputs=["A"],
                open_questions=[],
                unresolved_blockers=[],
                db_path=db_path,
            )
        assert len(drafts) == 1
        assert drafts[0].workspace_path.exists()
        assert drafts[0].db_id is not None
        # Persisted in DB
        plans_in_db = db_list_plans(session.id, db_path=db_path)
        assert len(plans_in_db) == 1

    def test_strips_outer_code_fence(self, session, db_path):
        wrapped = "```markdown\n" + _valid_plan(5) + "\n```\n"
        with patch("squad.plan_generator.run_task_text", return_value=wrapped):
            drafts = generate_plans(
                session_id=session.id,
                decision_summary="s",
                plan_inputs=[],
                open_questions=[],
                unresolved_blockers=[],
                db_path=db_path,
            )
        assert len(drafts) == 1

    def test_too_few_lots_raises(self, session, db_path):
        with patch("squad.plan_generator.run_task_text", return_value=_valid_plan(3)):
            with pytest.raises(ForgeFormatError):
                generate_plans(
                    session_id=session.id,
                    decision_summary="s",
                    plan_inputs=[],
                    open_questions=[],
                    unresolved_blockers=[],
                    db_path=db_path,
                )

    def test_large_plan_is_split_into_multiple(self, session, db_path):
        with patch("squad.plan_generator.run_task_text", return_value=_valid_plan(20)):
            drafts = generate_plans(
                session_id=session.id,
                decision_summary="s",
                plan_inputs=[],
                open_questions=[],
                unresolved_blockers=[],
                db_path=db_path,
            )
        assert len(drafts) >= 2
        # Each draft persisted
        files = list_plans(session.id, db_path=db_path)
        assert len(files) == len(drafts)

    def test_executor_failure_surfaces(self, session, db_path):
        from squad.executor import AgentError

        with patch(
            "squad.plan_generator.run_task_text",
            side_effect=AgentError("down"),
        ):
            with pytest.raises(RuntimeError):
                generate_plans(
                    session_id=session.id,
                    decision_summary="s",
                    plan_inputs=[],
                    open_questions=[],
                    unresolved_blockers=[],
                    db_path=db_path,
                )

    def test_session_not_found(self, db_path):
        with pytest.raises(ValueError):
            generate_plans(
                session_id="ghost",
                decision_summary="s",
                plan_inputs=[],
                open_questions=[],
                unresolved_blockers=[],
                db_path=db_path,
            )


# ── generate_plans_from_session ────────────────────────────────────────────────


_SYNTHESE_OUTPUT = (
    "# Synthese\n\n"
    '```json\n{"decision_summary": "ship MVP", '
    '"open_questions": [], '
    '"plan_inputs": ["ship pricing", "ship onboarding"]}\n```'
)

_CHALLENGE_OUTPUT_WITH_BLOCKER = (
    "# Challenge\n"
    '```json\n{"blockers": [{"id": "b1", "severity": "blocking", '
    '"constraint": "rate limiting required"}]}\n```'
)


class TestGeneratePlansFromSession:
    def test_reads_synthesis_contract_and_blockers(self, session, db_path):
        create_phase_output(
            session.id,
            PHASE_SYNTHESE,
            "pm",
            _SYNTHESE_OUTPUT,
            "/f.md",
            attempt=1,
            db_path=db_path,
        )
        create_phase_output(
            session.id,
            PHASE_CHALLENGE,
            "security",
            _CHALLENGE_OUTPUT_WITH_BLOCKER,
            "/c.md",
            attempt=1,
            db_path=db_path,
        )

        captured_prompt: dict = {}

        def _fake_run(prompt, **kwargs):
            captured_prompt["prompt"] = prompt
            return _valid_plan(6)

        with patch("squad.plan_generator.run_task_text", side_effect=_fake_run):
            drafts = generate_plans_from_session(session.id, db_path=db_path)

        assert len(drafts) == 1
        prompt = captured_prompt["prompt"]
        assert "ship MVP" in prompt
        assert "ship pricing" in prompt
        assert "rate limiting required" in prompt

    def test_raises_when_no_synthese_output(self, session, db_path):
        with pytest.raises(ValueError, match="No synthese"):
            generate_plans_from_session(session.id, db_path=db_path)

    def test_raises_when_contract_malformed(self, session, db_path):
        create_phase_output(
            session.id,
            PHASE_SYNTHESE,
            "pm",
            "no contract here",
            "/f.md",
            attempt=1,
            db_path=db_path,
        )
        with pytest.raises(ValueError, match="synthesis contract"):
            generate_plans_from_session(session.id, db_path=db_path)


# ── copy_plans_to_project ──────────────────────────────────────────────────────


class TestCopyPlansToProject:
    def test_copies_workspace_plans(self, session, db_path, project_dir):
        # Drop a plan file directly in the workspace plans dir
        ws = Path(session.workspace_path)
        plan_file = ws / "plans" / "plan-1-test.md"
        plan_file.write_text("# plan content")

        copied = copy_plans_to_project(session.id, db_path=db_path)
        assert len(copied) == 1
        target = project_dir / "plans" / "plan-1-test.md"
        assert target.exists()
        assert target.read_text() == "# plan content"

    def test_no_plans_no_copy(self, session, db_path, project_dir):
        # Remove the plans dir to simulate a missing workspace
        ws = Path(session.workspace_path)
        plans_dir = ws / "plans"
        for f in plans_dir.glob("*.md"):
            f.unlink()
        # No md files → no copies, no crash
        assert copy_plans_to_project(session.id, db_path=db_path) == []

    def test_overwrites_existing_target(self, session, db_path, project_dir):
        ws = Path(session.workspace_path)
        (ws / "plans" / "plan-1.md").write_text("new")
        (project_dir / "plans").mkdir()
        (project_dir / "plans" / "plan-1.md").write_text("old")

        copy_plans_to_project(session.id, db_path=db_path)
        assert (project_dir / "plans" / "plan-1.md").read_text() == "new"
