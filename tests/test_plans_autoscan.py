"""Tests for squad.plans_autoscan — discovery + inventory + orchestration."""

from pathlib import Path

import pytest

from squad.config import get_project_config_path
from squad.db import create_session, ensure_schema
from squad.plans_autoscan import (
    AutoScanResult,
    PlanFolderInventory,
    autoscan_and_import_plans,
    discover_plans_subfolder,
    inventory_plan_folder,
)
from squad.workspace import create_workspace


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()`` to a temporary directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ── discover_plans_subfolder ──────────────────────────────────────────────────


class TestDiscoverPlansSubfolder:
    def test_returns_matching_subfolder(self, tmp_path: Path):
        project = tmp_path / "ressort"
        (project / "plans" / "whaou").mkdir(parents=True)
        result = discover_plans_subfolder("Ajouter le module whaou à ressort", project)
        assert result == project / "plans" / "whaou"

    def test_returns_none_when_plans_dir_missing(self, tmp_path: Path):
        project = tmp_path / "ressort"
        project.mkdir()
        assert discover_plans_subfolder("Ajouter le module whaou à ressort", project) is None

    def test_returns_none_when_no_token_matches(self, tmp_path: Path):
        project = tmp_path / "ressort"
        (project / "plans" / "other").mkdir(parents=True)
        assert discover_plans_subfolder("Ajouter le module whaou", project) is None

    def test_longest_match_wins(self, tmp_path: Path):
        project = tmp_path / "ressort"
        (project / "plans" / "whaou").mkdir(parents=True)
        (project / "plans" / "whaou-admin").mkdir(parents=True)
        result = discover_plans_subfolder("Ajouter whaou-admin et whaou", project)
        assert result == project / "plans" / "whaou-admin"

    def test_alpha_tie_break(self, tmp_path: Path):
        project = tmp_path / "ressort"
        (project / "plans" / "beta").mkdir(parents=True)
        (project / "plans" / "alpha").mkdir(parents=True)
        result = discover_plans_subfolder("work on alpha and beta", project)
        assert result == project / "plans" / "alpha"

    def test_short_tokens_ignored(self, tmp_path: Path):
        project = tmp_path / "ressort"
        (project / "plans" / "ab").mkdir(parents=True)
        # "ab" is 2 chars, below the >=3 threshold
        assert discover_plans_subfolder("work on ab module", project) is None

    def test_case_insensitive(self, tmp_path: Path):
        project = tmp_path / "ressort"
        (project / "plans" / "whaou").mkdir(parents=True)
        result = discover_plans_subfolder("Ajouter WHAOU", project)
        assert result == project / "plans" / "whaou"

    def test_ignores_files_in_plans_dir(self, tmp_path: Path):
        project = tmp_path / "ressort"
        plans = project / "plans"
        plans.mkdir(parents=True)
        (plans / "whaou").write_text("not a dir")
        assert discover_plans_subfolder("Ajouter whaou", project) is None

    def test_ignores_dot_prefixed_subfolders(self, tmp_path: Path):
        project = tmp_path / "ressort"
        (project / "plans" / ".hidden").mkdir(parents=True)
        assert discover_plans_subfolder("work on hidden", project) is None


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
        for name in ("z.md", "a.md", "m.md"):
            (tmp_path / name).write_text("x")
        inv = inventory_plan_folder(tmp_path)
        assert [p.name for p in inv.files] == ["a.md", "m.md", "z.md"]

    def test_filters_out_scope_extensions(self, tmp_path: Path):
        (tmp_path / "good.md").write_text("x")
        (tmp_path / "bad.pdf").write_text("x")
        (tmp_path / "script.exe").write_text("x")
        inv = inventory_plan_folder(tmp_path)
        assert [p.name for p in inv.files] == ["good.md"]
        assert inv.ignored_count == 2

    def test_ignores_subdirectories(self, tmp_path: Path):
        (tmp_path / "a.md").write_text("a")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.md").write_text("b")
        inv = inventory_plan_folder(tmp_path)
        assert [p.name for p in inv.files] == ["a.md"]
        assert inv.ignored_count == 0

    def test_caps_at_max_files(self, tmp_path: Path):
        for i in range(13):
            (tmp_path / f"f-{i:02d}.md").write_text("x")
        inv = inventory_plan_folder(tmp_path, max_files=10)
        assert len(inv.files) == 10
        assert [p.name for p in inv.files] == [f"f-{i:02d}.md" for i in range(10)]
        assert inv.ignored_count == 3

    def test_cap_and_out_of_scope_combine(self, tmp_path: Path):
        # 12 eligible .md + 2 out-of-scope → cap drops 2 eligibles, 2 already ignored
        for i in range(12):
            (tmp_path / f"g-{i:02d}.md").write_text("x")
        (tmp_path / "x.pdf").write_text("x")
        (tmp_path / "y.bin").write_text("x")
        inv = inventory_plan_folder(tmp_path, max_files=10)
        assert len(inv.files) == 10
        assert inv.ignored_count == 4

    def test_case_insensitive_extension(self, tmp_path: Path):
        (tmp_path / "A.MD").write_text("x")
        (tmp_path / "B.TXT").write_text("x")
        inv = inventory_plan_folder(tmp_path)
        assert [p.name for p in inv.files] == ["A.MD", "B.TXT"]
        assert inv.ignored_count == 0


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
    """Build a project with plans/<subject>/ and a session pointing at it."""
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
        "idea": "Ajouter le module whaou à ressort",
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
        assert result.folder == plans
        assert result.imported_count == 2
        assert result.rejected_count == 0
        assert result.ignored_count == 0
        filenames = {m.filename for m in result.imported}
        assert filenames == {"a.md", "b.txt"}
        # Files landed in the workspace attachments dir
        attachments = Path(env["session"].workspace_path) / "attachments"
        assert {p.name for p in attachments.iterdir()} == {"a.md", "b.txt"}

    def test_explicit_enabled_false_is_no_op(self, autoscan_env):
        env = autoscan_env
        (env["plans"] / "a.md").write_text("a")
        result = autoscan_and_import_plans(
            env["session"], env["idea"], db_path=env["db_path"], enabled=False
        )
        assert result.enabled is False
        assert result.folder is None
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

    def test_no_matching_folder_returns_enabled_with_no_folder(
        self, fake_home: Path, tmp_path: Path
    ):
        db_path = tmp_path / "squad.db"
        ensure_schema(db_path)
        project = tmp_path / "ressort"
        (project / "plans" / "other").mkdir(parents=True)
        session = _make_session(tmp_path, project, db_path)

        result = autoscan_and_import_plans(
            session, "ajouter un module inconnu", db_path=db_path
        )
        assert result.enabled is True
        assert result.folder is None
        assert result.imported_count == 0
        assert result.rejected_count == 0
        assert result.ignored_count == 0

    def test_splits_imported_rejected_ignored(self, autoscan_env):
        env = autoscan_env
        plans: Path = env["plans"]
        # 8 imported (eligible .md) — small
        for i in range(8):
            (plans / f"ok-{i:02d}.md").write_text("x")
        # 4 out-of-scope → ignored by inventory
        for i in range(4):
            (plans / f"out-{i}.pdf").write_text("x")

        # Project config raises the per-file cap so oversized remain acceptable
        # via overrides while we can still reject 2 of them with another rule.
        # Strategy: configure allowed_extensions to "md,txt" (still allows .md),
        # but make 2 specific files oversized to trigger rejection at import.
        proj_cfg_path = get_project_config_path(env["project"])
        proj_cfg_path.parent.mkdir(parents=True, exist_ok=True)
        proj_cfg_path.write_text(
            "slack:\n"
            "  attachments:\n"
            "    max_file_bytes: 1024\n"
            "    max_total_bytes: 1048576\n"
        )
        # Override 2 of the 8 .md to be oversized (>1 KB): these will be
        # rejected by import_local_attachment.
        (plans / "ok-06.md").write_bytes(b"x" * 2048)
        (plans / "ok-07.md").write_bytes(b"x" * 2048)

        result = autoscan_and_import_plans(
            env["session"], env["idea"], db_path=env["db_path"]
        )

        assert result.enabled is True
        assert result.folder == plans
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
        # Only allow .txt at the policy layer; inventory still returns both
        # (.md/.txt/.csv scope) so .md will be rejected by the policy.
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
        assert str(env["plans"]) in messages
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
