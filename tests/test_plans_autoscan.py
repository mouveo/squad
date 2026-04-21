"""Tests for squad.plans_autoscan — path extraction + inventory + orchestration."""

from pathlib import Path

import pytest

from squad.config import get_project_config_path
from squad.db import create_session, ensure_schema
from squad.plans_autoscan import (
    AutoScanResult,
    PlanFolderInventory,
    autoscan_and_import_plans,
    extract_plan_paths_from_idea,
    inventory_plan_folder,
)
from squad.workspace import create_workspace


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()`` to a temporary directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ── extract_plan_paths_from_idea ──────────────────────────────────────────────


class TestExtractPlanPathsFromIdea:
    def test_returns_matching_folder(self, tmp_path: Path):
        project = tmp_path / "ressort"
        (project / "plans" / "whaou").mkdir(parents=True)
        result = extract_plan_paths_from_idea(
            "Travailler sur plans/whaou pour la refonte", project
        )
        assert result == [(project / "plans" / "whaou").resolve()]

    def test_tolerates_trailing_slash(self, tmp_path: Path):
        project = tmp_path / "ressort"
        (project / "plans" / "whaou").mkdir(parents=True)
        result = extract_plan_paths_from_idea(
            "voir plans/whaou/ pour le contexte", project
        )
        assert result == [(project / "plans" / "whaou").resolve()]

    def test_strips_trailing_punctuation(self, tmp_path: Path):
        project = tmp_path / "ressort"
        (project / "plans" / "whaou").mkdir(parents=True)
        # Sentence terminates with a comma or period right after the path.
        result = extract_plan_paths_from_idea(
            "voir plans/whaou, et continuer ensuite.", project
        )
        assert result == [(project / "plans" / "whaou").resolve()]

    def test_returns_empty_when_no_plans_mention(self, tmp_path: Path):
        project = tmp_path / "ressort"
        (project / "plans" / "whaou").mkdir(parents=True)
        # The idea mentions "whaou" but NOT the `plans/whaou` path —
        # zero-guesswork policy: nothing imported.
        result = extract_plan_paths_from_idea(
            "ajouter le module whaou à ressort", project
        )
        assert result == []

    def test_returns_empty_when_path_does_not_exist(self, tmp_path: Path):
        project = tmp_path / "ressort"
        (project / "plans").mkdir(parents=True)
        # The idea mentions a folder that doesn't exist — typo dropped.
        result = extract_plan_paths_from_idea(
            "voir plans/typo pour l'audit", project
        )
        assert result == []

    def test_single_file_with_scoped_extension_is_kept(self, tmp_path: Path):
        project = tmp_path / "ressort"
        plans = project / "plans" / "whaou"
        plans.mkdir(parents=True)
        (plans / "brief.md").write_text("# brief")
        result = extract_plan_paths_from_idea(
            "voir plans/whaou/brief.md pour le contexte", project
        )
        assert result == [(plans / "brief.md").resolve()]

    def test_single_file_with_wrong_extension_is_dropped(self, tmp_path: Path):
        project = tmp_path / "ressort"
        plans = project / "plans" / "whaou"
        plans.mkdir(parents=True)
        (plans / "brief.pdf").write_bytes(b"x")
        result = extract_plan_paths_from_idea(
            "voir plans/whaou/brief.pdf pour le visuel", project
        )
        assert result == []

    def test_no_collision_with_unrelated_words(self, tmp_path: Path):
        """Regression : the word ``templates`` exists as a folder under
        ``plans/`` but the idea doesn't write ``plans/templates/``, so
        nothing should be imported from there."""
        project = tmp_path / "sitavista"
        (project / "plans" / "crm").mkdir(parents=True)
        (project / "plans" / "templates").mkdir(parents=True)
        result = extract_plan_paths_from_idea(
            "Refonte du CRM. Lire plans/crm. Email avec templates.",
            project,
        )
        # Only plans/crm, NOT plans/templates, is imported.
        assert result == [(project / "plans" / "crm").resolve()]

    def test_deduplicates_repeated_paths(self, tmp_path: Path):
        project = tmp_path / "ressort"
        (project / "plans" / "whaou").mkdir(parents=True)
        result = extract_plan_paths_from_idea(
            "voir plans/whaou au début, puis plans/whaou à la fin.",
            project,
        )
        assert result == [(project / "plans" / "whaou").resolve()]

    def test_multiple_distinct_paths(self, tmp_path: Path):
        project = tmp_path / "ressort"
        (project / "plans" / "whaou").mkdir(parents=True)
        (project / "plans" / "admin").mkdir(parents=True)
        result = extract_plan_paths_from_idea(
            "voir plans/whaou ET plans/admin", project
        )
        assert len(result) == 2
        assert {p.name for p in result} == {"whaou", "admin"}

    def test_not_preceded_by_slash(self, tmp_path: Path):
        """``foo/plans/x`` should NOT match — the ``plans/`` prefix must
        start a token, not sit inside a longer path."""
        project = tmp_path / "ressort"
        (project / "plans" / "x").mkdir(parents=True)
        result = extract_plan_paths_from_idea(
            "don't match src/plans/x in this text", project
        )
        assert result == []


# ── inventory_plan_folder ─────────────────────────────────────────────────────


class TestInventoryPlanFolder:
    def test_returns_empty_inventory_when_missing(self, tmp_path: Path):
        inv = inventory_plan_folder(tmp_path / "nope")
        assert isinstance(inv, PlanFolderInventory)
        assert inv.files == []
        assert inv.ignored_count == 0

    def test_keeps_scoped_extensions(self, tmp_path: Path):
        (tmp_path / "a.md").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        (tmp_path / "c.csv").write_text("c")
        inv = inventory_plan_folder(tmp_path)
        assert [p.name for p in inv.files] == ["a.md", "b.txt", "c.csv"]
        assert inv.ignored_count == 0

    def test_alphabetical_order(self, tmp_path: Path):
        (tmp_path / "c.md").write_text("c")
        (tmp_path / "a.md").write_text("a")
        (tmp_path / "b.md").write_text("b")
        inv = inventory_plan_folder(tmp_path)
        assert [p.name for p in inv.files] == ["a.md", "b.md", "c.md"]

    def test_filters_out_scope_extensions(self, tmp_path: Path):
        (tmp_path / "a.md").write_text("a")
        (tmp_path / "b.pdf").write_text("b")
        (tmp_path / "c.png").write_text("c")
        inv = inventory_plan_folder(tmp_path)
        assert [p.name for p in inv.files] == ["a.md"]
        assert inv.ignored_count == 2

    def test_ignores_subdirectories(self, tmp_path: Path):
        (tmp_path / "a.md").write_text("a")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.md").write_text("b")
        inv = inventory_plan_folder(tmp_path)
        assert [p.name for p in inv.files] == ["a.md"]
        assert inv.ignored_count == 0  # subdir not counted

    def test_caps_at_max_files(self, tmp_path: Path):
        for i in range(12):
            (tmp_path / f"f-{i:02d}.md").write_text("x")
        inv = inventory_plan_folder(tmp_path, max_files=5)
        assert len(inv.files) == 5
        assert inv.ignored_count == 7

    def test_cap_and_out_of_scope_combine(self, tmp_path: Path):
        for i in range(6):
            (tmp_path / f"ok-{i}.md").write_text("x")
        (tmp_path / "noisy.pdf").write_text("x")
        inv = inventory_plan_folder(tmp_path, max_files=3)
        assert len(inv.files) == 3
        assert inv.ignored_count == 1 + 3  # 1 pdf + 3 overflow

    def test_case_insensitive_extension(self, tmp_path: Path):
        (tmp_path / "a.MD").write_text("a")
        (tmp_path / "b.TXT").write_text("b")
        inv = inventory_plan_folder(tmp_path)
        assert {p.name for p in inv.files} == {"a.MD", "b.TXT"}


# ── autoscan_and_import_plans ─────────────────────────────────────────────────


def _make_session(tmp_path: Path, project: Path, db_path: Path):
    workspace = project / ".squad" / "sessions" / "sess-auto"
    session = create_session(
        title="Test",
        project_path=str(project),
        workspace_path=str(workspace),
        idea="x",
        db_path=db_path,
        session_id="sess-auto",
    )
    create_workspace(session)
    return session


@pytest.fixture
def autoscan_env(fake_home: Path, tmp_path: Path):
    """Build a project with plans/<subject>/ and a session pointing at it.

    The idea fixture mentions the explicit ``plans/whaou`` path to
    match the new extraction behaviour.
    """
    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)

    project = tmp_path / "ressort"
    plans = project / "plans" / "whaou"
    plans.mkdir(parents=True)

    session = _make_session(tmp_path, project, db_path)
    return {
        "db_path": db_path,
        "project": project,
        "plans": plans,
        "session": session,
        "idea": "Ajouter le module whaou à ressort — voir plans/whaou.",
    }


class TestAutoScanAndImportPlans:
    def test_enabled_by_default_imports_scoped_files(self, autoscan_env):
        env = autoscan_env
        plans: Path = env["plans"]
        (plans / "a.md").write_text("# a")
        (plans / "b.txt").write_text("b")

        result = autoscan_and_import_plans(
            env["session"], env["idea"], db_path=env["db_path"]
        )

        assert isinstance(result, AutoScanResult)
        assert result.enabled is True
        assert result.folders == [plans.resolve()]
        assert result.imported_count == 2
        assert result.rejected_count == 0
        assert result.ignored_count == 0
        filenames = {m.filename for m in result.imported}
        assert filenames == {"a.md", "b.txt"}
        attachments = Path(env["session"].workspace_path) / "attachments"
        assert {p.name for p in attachments.iterdir()} == {"a.md", "b.txt"}

    def test_explicit_enabled_false_is_no_op(self, autoscan_env):
        env = autoscan_env
        (env["plans"] / "a.md").write_text("a")
        result = autoscan_and_import_plans(
            env["session"], env["idea"], db_path=env["db_path"], enabled=False
        )
        assert result.enabled is False
        assert result.folders == []
        assert result.imported_count == 0
        attachments = Path(env["session"].workspace_path) / "attachments"
        assert list(attachments.iterdir()) == []

    def test_config_key_false_disables(self, autoscan_env):
        env = autoscan_env
        (env["plans"] / "a.md").write_text("a")

        proj_cfg_path = get_project_config_path(env["project"])
        proj_cfg_path.parent.mkdir(parents=True, exist_ok=True)
        proj_cfg_path.write_text("pipeline:\n  project_plans_autoscan: false\n")

        result = autoscan_and_import_plans(
            env["session"], env["idea"], db_path=env["db_path"]
        )
        assert result.enabled is False
        assert result.imported_count == 0

    def test_explicit_enabled_true_overrides_config_false(self, autoscan_env):
        env = autoscan_env
        (env["plans"] / "a.md").write_text("a")
        proj_cfg_path = get_project_config_path(env["project"])
        proj_cfg_path.parent.mkdir(parents=True, exist_ok=True)
        proj_cfg_path.write_text("pipeline:\n  project_plans_autoscan: false\n")

        result = autoscan_and_import_plans(
            env["session"], env["idea"], db_path=env["db_path"], enabled=True
        )
        assert result.enabled is True
        assert result.imported_count == 1

    def test_no_path_in_idea_returns_empty_result(
        self, fake_home: Path, tmp_path: Path
    ):
        db_path = tmp_path / "squad.db"
        ensure_schema(db_path)
        project = tmp_path / "ressort"
        (project / "plans" / "other").mkdir(parents=True)
        session = _make_session(tmp_path, project, db_path)

        # Idea doesn't mention any plans/ path — nothing imported.
        result = autoscan_and_import_plans(
            session, "ajouter un module inconnu", db_path=db_path
        )
        assert result.enabled is True
        assert result.folders == []
        assert result.imported_count == 0
        assert result.rejected_count == 0

    def test_splits_imported_rejected_ignored(self, autoscan_env):
        env = autoscan_env
        plans: Path = env["plans"]
        for i in range(8):
            (plans / f"ok-{i:02d}.md").write_text("x")
        for i in range(4):
            (plans / f"out-{i}.pdf").write_text("x")

        proj_cfg_path = get_project_config_path(env["project"])
        proj_cfg_path.parent.mkdir(parents=True, exist_ok=True)
        proj_cfg_path.write_text(
            "slack:\n"
            "  attachments:\n"
            "    max_file_bytes: 1024\n"
            "    max_total_bytes: 1048576\n"
        )
        (plans / "ok-06.md").write_bytes(b"x" * 2048)
        (plans / "ok-07.md").write_bytes(b"x" * 2048)

        result = autoscan_and_import_plans(
            env["session"], env["idea"], db_path=env["db_path"]
        )

        assert result.enabled is True
        assert result.folders == [plans.resolve()]
        assert result.imported_count == 6
        assert result.rejected_count == 2
        assert result.ignored_count == 4

    def test_project_config_extension_override_respected(self, autoscan_env):
        env = autoscan_env
        plans: Path = env["plans"]
        (plans / "note.md").write_text("md")
        (plans / "note.txt").write_text("txt")

        proj_cfg_path = get_project_config_path(env["project"])
        proj_cfg_path.parent.mkdir(parents=True, exist_ok=True)
        proj_cfg_path.write_text(
            "slack:\n  attachments:\n    allowed_extensions: [txt]\n"
        )

        result = autoscan_and_import_plans(
            env["session"], env["idea"], db_path=env["db_path"]
        )
        assert result.imported_count == 1
        assert result.rejected_count == 1
        assert {m.filename for m in result.imported} == {"note.txt"}

    def test_oversized_file_rejected_with_config_override(self, autoscan_env):
        env = autoscan_env
        plans: Path = env["plans"]
        (plans / "big.md").write_bytes(b"x" * 2048)
        (plans / "small.md").write_bytes(b"x")

        proj_cfg_path = get_project_config_path(env["project"])
        proj_cfg_path.parent.mkdir(parents=True, exist_ok=True)
        proj_cfg_path.write_text(
            "slack:\n  attachments:\n    max_file_bytes: 1024\n"
        )

        result = autoscan_and_import_plans(
            env["session"], env["idea"], db_path=env["db_path"]
        )
        assert result.imported_count == 1
        assert result.rejected_count == 1
        assert {m.filename for m in result.imported} == {"small.md"}

    def test_logs_matched_folder_and_totals(self, autoscan_env, caplog):
        env = autoscan_env
        (env["plans"] / "a.md").write_text("a")
        (env["plans"] / "b.md").write_text("b")

        with caplog.at_level("INFO", logger="squad.plans_autoscan"):
            autoscan_and_import_plans(
                env["session"], env["idea"], db_path=env["db_path"]
            )

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert str(env["plans"].resolve()) in messages
        assert "imported=2" in messages

    def test_honours_max_files(self, autoscan_env):
        env = autoscan_env
        for i in range(12):
            (env["plans"] / f"f-{i:02d}.md").write_text("x")
        result = autoscan_and_import_plans(
            env["session"], env["idea"], db_path=env["db_path"], max_files=5
        )
        assert result.imported_count == 5
        assert result.ignored_count == 7

    def test_single_file_path_in_idea(self, autoscan_env):
        env = autoscan_env
        plans: Path = env["plans"]
        (plans / "brief.md").write_text("# brief")
        (plans / "other.md").write_text("# other")

        # Mention ONLY the brief.md path — only that single file imported.
        idea = "voir plans/whaou/brief.md pour le contexte"
        result = autoscan_and_import_plans(
            env["session"], idea, db_path=env["db_path"]
        )

        assert result.enabled is True
        assert result.folders == []  # no folder matched, only a single file
        assert result.imported_count == 1
        assert {m.filename for m in result.imported} == {"brief.md"}
