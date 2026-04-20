"""Tests for squad.plans_autoscan — discovery + inventory (pure helpers)."""

from pathlib import Path

from squad.plans_autoscan import (
    PlanFolderInventory,
    discover_plans_subfolder,
    inventory_plan_folder,
)


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
