"""Tests for squad/research.py — budget, axes, prompt building, persistence."""

from pathlib import Path
from unittest.mock import patch

import pytest

from squad.constants import PHASE_BENCHMARK
from squad.db import (
    create_session,
    ensure_schema,
    list_phase_outputs,
    update_session_profile,
)
from squad.models import (
    RESEARCH_DEPTH_DEEP,
    RESEARCH_DEPTH_LIGHT,
    RESEARCH_DEPTH_NORMAL,
)
from squad.research import (
    DEEP_BUDGET,
    NORMAL_BUDGET,
    REPO_SKILL_PATH,
    RESEARCH_TOOLS,
    BenchmarkReport,
    ResearchBudget,
    _derive_slug,
    _truncate_output,
    budget_for_depth,
    build_research_prompt,
    load_research_skill,
    persist_benchmark,
    prepare_research_axes,
    run_research,
)
from squad.workspace import create_workspace, read_benchmark

# ── fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / ".squad" / "squad.db"
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
        idea="Build a B2B SaaS CRM for sales teams",
        db_path=db_path,
    )
    create_workspace(s)
    update_session_profile(
        session_id=s.id,
        subject_type="b2b_saas",
        research_depth=RESEARCH_DEPTH_NORMAL,
        agents_by_phase={},
        db_path=db_path,
    )
    return s


# ── budget_for_depth ───────────────────────────────────────────────────────────


class TestBudgetForDepth:
    def test_normal_returns_normal_budget(self):
        assert budget_for_depth(RESEARCH_DEPTH_NORMAL) is NORMAL_BUDGET

    def test_deep_returns_deep_budget(self):
        assert budget_for_depth(RESEARCH_DEPTH_DEEP) is DEEP_BUDGET

    def test_light_rejected(self):
        with pytest.raises(ValueError, match="skipped"):
            budget_for_depth(RESEARCH_DEPTH_LIGHT)

    def test_unknown_rejected(self):
        with pytest.raises(ValueError):
            budget_for_depth("extreme")

    def test_deep_has_larger_budget_than_normal(self):
        assert DEEP_BUDGET.max_axes > NORMAL_BUDGET.max_axes
        assert DEEP_BUDGET.max_prompt_chars > NORMAL_BUDGET.max_prompt_chars
        assert DEEP_BUDGET.max_output_chars > NORMAL_BUDGET.max_output_chars
        assert DEEP_BUDGET.timeout_seconds >= NORMAL_BUDGET.timeout_seconds

    def test_budget_is_frozen(self):
        with pytest.raises(Exception):  # dataclass frozen
            NORMAL_BUDGET.max_axes = 99  # type: ignore[misc]


# ── prepare_research_axes ──────────────────────────────────────────────────────


class TestPrepareResearchAxes:
    def test_normal_returns_three_axes(self):
        axes = prepare_research_axes("b2b_saas", RESEARCH_DEPTH_NORMAL)
        assert len(axes) == 3

    def test_deep_returns_five_axes(self):
        axes = prepare_research_axes("b2b_saas", RESEARCH_DEPTH_DEEP)
        assert len(axes) == 5

    def test_at_least_three_axes(self):
        assert len(prepare_research_axes(None, RESEARCH_DEPTH_NORMAL)) >= 3

    def test_deep_axes_include_pricing_and_compliance(self):
        axes = prepare_research_axes(None, RESEARCH_DEPTH_DEEP)
        joined = " ".join(axes).lower()
        assert "pricing" in joined
        assert "compliance" in joined or "regulatory" in joined

    def test_light_rejected(self):
        with pytest.raises(ValueError):
            prepare_research_axes("x", RESEARCH_DEPTH_LIGHT)


# ── build_research_prompt ──────────────────────────────────────────────────────


class TestBuildResearchPrompt:
    def test_contains_idea(self):
        prompt = build_research_prompt("Ship a CRM", ["axis 1"], NORMAL_BUDGET)
        assert "Ship a CRM" in prompt

    def test_lists_axes(self):
        prompt = build_research_prompt("x", ["A1", "A2", "A3"], NORMAL_BUDGET)
        assert "A1" in prompt
        assert "A2" in prompt
        assert "A3" in prompt

    def test_mentions_report_structure(self):
        prompt = build_research_prompt("x", ["a"], NORMAL_BUDGET)
        assert "Résumé exécutif" in prompt
        assert "Concurrents" in prompt
        assert "Sources" in prompt

    def test_respects_max_prompt_chars(self):
        big_ctx = "x" * 50_000
        prompt = build_research_prompt("idea", ["a"], NORMAL_BUDGET, extra_context=big_ctx)
        assert len(prompt) <= NORMAL_BUDGET.max_prompt_chars

    def test_truncation_marker_present_when_capped(self):
        prompt = build_research_prompt("idea", ["a"], NORMAL_BUDGET, extra_context="y" * 50_000)
        assert "truncated" in prompt.lower()


# ── _truncate_output ───────────────────────────────────────────────────────────


class TestTruncateOutput:
    def test_short_text_unchanged(self):
        assert _truncate_output("short", NORMAL_BUDGET) == "short"

    def test_long_text_truncated(self):
        big = "x" * (NORMAL_BUDGET.max_output_chars + 100)
        out = _truncate_output(big, NORMAL_BUDGET)
        assert len(out) <= NORMAL_BUDGET.max_output_chars
        assert "truncated" in out.lower()

    def test_prefers_paragraph_boundary(self):
        parts = ["x" * 1000, "y" * 1000] * 20
        text = "\n\n".join(parts)
        out = _truncate_output(
            text,
            ResearchBudget(
                max_axes=3,
                max_prompt_chars=1_000,
                max_output_chars=5_000,
                timeout_seconds=60,
            ),
        )
        # Should break on a double-newline close to the cutoff
        assert out.endswith("*[Report truncated to fit the research budget.]*")


# ── _derive_slug ───────────────────────────────────────────────────────────────


class TestDeriveSlug:
    def test_basic(self):
        assert _derive_slug("B2B SaaS CRM") == "b2b-saas-crm"

    def test_strips_special_chars(self):
        assert "/" not in _derive_slug("hello/world")

    def test_caps_length(self):
        assert len(_derive_slug("x" * 200)) <= 40

    def test_fallback_when_empty(self):
        assert _derive_slug("!!!") == "benchmark"


# ── persist_benchmark ──────────────────────────────────────────────────────────


class TestPersistBenchmark:
    def test_writes_workspace_and_phase_output(self, session, db_path):
        report = persist_benchmark(
            session_id=session.id,
            slug="crm",
            content="# Benchmark\nhello",
            axes=["a"],
            attempt=1,
            db_path=db_path,
        )
        assert isinstance(report, BenchmarkReport)
        assert report.file_path is not None and report.file_path.exists()
        assert read_benchmark(session.id, "crm", db_path=db_path) == "# Benchmark\nhello"
        outputs = list_phase_outputs(session.id, db_path=db_path)
        assert any(po.phase == PHASE_BENCHMARK and po.agent == "research" for po in outputs)

    def test_attempt_is_recorded(self, session, db_path):
        persist_benchmark(
            session_id=session.id,
            slug="crm",
            content="v1",
            axes=[],
            attempt=1,
            db_path=db_path,
        )
        persist_benchmark(
            session_id=session.id,
            slug="crm",
            content="v2",
            axes=[],
            attempt=2,
            db_path=db_path,
        )
        outputs = list_phase_outputs(session.id, phase=PHASE_BENCHMARK, db_path=db_path)
        attempts = {po.attempt for po in outputs}
        assert attempts == {1, 2}


# ── run_research ───────────────────────────────────────────────────────────────


class TestRunResearch:
    def test_happy_path_normal_depth(self, session, db_path):
        fake_output = (
            "# Benchmark\n\n## Résumé exécutif\n- x\n\n## Concurrents\n\n## Sources\n- https://x"
        )
        with patch("squad.research.run_task_text", return_value=fake_output) as mock_exec:
            report = run_research(session.id, db_path=db_path)

        assert mock_exec.call_count == 1
        kwargs = mock_exec.call_args.kwargs
        assert kwargs["timeout"] == NORMAL_BUDGET.timeout_seconds
        assert kwargs["allowed_tools"] == list(RESEARCH_TOOLS)
        assert report.content == fake_output
        assert report.attempt == 1
        assert report.file_path.exists()

    def test_deep_depth_uses_deep_budget(self, session, db_path):
        update_session_profile(
            session_id=session.id,
            subject_type="b2b_ai_product",
            research_depth=RESEARCH_DEPTH_DEEP,
            agents_by_phase={},
            db_path=db_path,
        )
        with patch("squad.research.run_task_text", return_value="# Benchmark") as mock_exec:
            run_research(session.id, db_path=db_path)
        assert mock_exec.call_args.kwargs["timeout"] == DEEP_BUDGET.timeout_seconds

    def test_light_depth_rejected(self, session, db_path):
        update_session_profile(
            session_id=session.id,
            subject_type="internal_tool",
            research_depth=RESEARCH_DEPTH_LIGHT,
            agents_by_phase={},
            db_path=db_path,
        )
        with pytest.raises(ValueError, match="skipped"):
            run_research(session.id, db_path=db_path)

    def test_missing_depth_rejected(self, db_path, project_dir, tmp_path):
        s = create_session(
            title="t",
            project_path=str(project_dir),
            workspace_path=str(tmp_path / "ws2"),
            idea="x",
            db_path=db_path,
        )
        create_workspace(s)
        with pytest.raises(ValueError, match="research_depth"):
            run_research(s.id, db_path=db_path)

    def test_missing_session_rejected(self, db_path):
        with pytest.raises(ValueError, match="Session not found"):
            run_research("ghost-id", db_path=db_path)

    def test_output_truncation_applied(self, session, db_path):
        big = "x" * (NORMAL_BUDGET.max_output_chars + 500)
        with patch("squad.research.run_task_text", return_value=big):
            report = run_research(session.id, db_path=db_path)
        assert len(report.content) <= NORMAL_BUDGET.max_output_chars

    def test_custom_slug_honoured(self, session, db_path):
        with patch("squad.research.run_task_text", return_value="# x"):
            report = run_research(session.id, slug="custom", db_path=db_path)
        assert report.slug == "custom"
        assert "benchmark-custom.md" in report.file_path.name


# ── load_research_skill ────────────────────────────────────────────────────────


class TestLoadResearchSkill:
    def test_loads_repo_local_skill_by_default(self):
        """The repo-local SKILL.md is the canonical fallback."""
        body = load_research_skill()
        assert body is not None
        assert "Deep Research Protocol" in body or "Protocole" in body

    def test_strips_yaml_frontmatter(self, tmp_path: Path):
        skill = tmp_path / "SKILL.md"
        skill.write_text(
            "---\nname: test\ndescription: x\n---\n\n# Body\nProtocol text.\n",
            encoding="utf-8",
        )
        body = load_research_skill(skill)
        assert body is not None
        assert "name: test" not in body
        assert body.startswith("# Body")

    def test_returns_none_when_missing(self, tmp_path: Path):
        assert load_research_skill(tmp_path / "absent.md") is None

    def test_returns_none_when_empty(self, tmp_path: Path):
        empty = tmp_path / "SKILL.md"
        empty.write_text("---\nname: x\ndescription: y\n---\n\n", encoding="utf-8")
        assert load_research_skill(empty) is None

    def test_no_frontmatter_returns_full_body(self, tmp_path: Path):
        skill = tmp_path / "SKILL.md"
        skill.write_text("Just a body.\n", encoding="utf-8")
        assert load_research_skill(skill) == "Just a body."

    def test_repo_skill_path_points_to_existing_file(self):
        """LOT 2 ships the canonical skill at this path — guard against drift."""
        assert REPO_SKILL_PATH.exists()


# ── build_research_prompt with protocol ───────────────────────────────────────


class TestBuildResearchPromptWithProtocol:
    def test_protocol_section_injected(self):
        prompt = build_research_prompt(
            "idea",
            ["a"],
            NORMAL_BUDGET,
            protocol="STEP 1: cadrer.",
        )
        assert "## Research protocol" in prompt
        assert "STEP 1: cadrer." in prompt

    def test_no_section_when_protocol_missing(self):
        prompt = build_research_prompt("idea", ["a"], NORMAL_BUDGET, protocol=None)
        assert "## Research protocol" not in prompt

    def test_budget_respected_with_large_protocol(self):
        big_protocol = "p" * 50_000
        prompt = build_research_prompt(
            "idea",
            ["a"],
            NORMAL_BUDGET,
            protocol=big_protocol,
        )
        assert len(prompt) <= NORMAL_BUDGET.max_prompt_chars

    def test_context_truncated_before_protocol(self):
        ctx = "c" * 50_000
        proto = "PROTOCOL_MARK"
        prompt = build_research_prompt(
            "idea",
            ["a"],
            NORMAL_BUDGET,
            extra_context=ctx,
            protocol=proto,
        )
        assert "PROTOCOL_MARK" in prompt
        assert "context truncated" in prompt.lower()


# ── run_research integrates skill protocol ────────────────────────────────────


class TestRunResearchUsesSkill:
    def test_protocol_injected_into_prompt(self, session, db_path):
        with patch("squad.research.run_task_text", return_value="# Benchmark") as m:
            run_research(session.id, db_path=db_path)
        prompt_arg = m.call_args.args[0]
        assert "## Research protocol" in prompt_arg

    def test_falls_back_when_skill_missing(self, session, db_path, tmp_path):
        with (
            patch("squad.research.REPO_SKILL_PATH", tmp_path / "missing.md"),
            patch("squad.research.run_task_text", return_value="# Benchmark") as m,
        ):
            run_research(session.id, db_path=db_path)
        prompt_arg = m.call_args.args[0]
        assert "## Research protocol" not in prompt_arg
        # The benchmark still runs and produces a report.
        assert m.call_count == 1


# ── integration marker ─────────────────────────────────────────────────────────


@pytest.mark.integration
class TestRunResearchLive:
    """Placeholder for live Claude CLI integration — skipped by default.

    To run: ``pytest -m integration``. The test is gated on the real
    Claude CLI being installed and reachable.
    """

    def test_end_to_end_live_call(self, session, db_path):
        pytest.skip("Requires live Claude CLI; enable explicitly with -m integration")


# ── LOT 7 — prompt directives + cwd forwarding ────────────────────────────────


class TestBuildResearchPromptDirectives:
    def _axes(self):
        return ["Landscape", "Pain points", "Patterns"]

    def test_rich_adds_gap_filling_directive(self):
        prompt = build_research_prompt(
            idea="x",
            axes=self._axes(),
            budget=NORMAL_BUDGET,
            input_richness="rich",
        )
        assert "combler les angles morts" in prompt
        assert "généraliste" in prompt

    def test_sparse_omits_gap_filling_directive(self):
        prompt = build_research_prompt(
            idea="x",
            axes=self._axes(),
            budget=NORMAL_BUDGET,
            input_richness="sparse",
        )
        assert "combler les angles morts" not in prompt

    def test_none_richness_omits_directive(self):
        prompt = build_research_prompt(
            idea="x",
            axes=self._axes(),
            budget=NORMAL_BUDGET,
        )
        assert "combler les angles morts" not in prompt


class TestRunResearchCwdForwarding:
    def test_forwards_existing_project_path(self, session, db_path):
        with patch("squad.research.run_task_text", return_value="# r") as mock_exec:
            run_research(session.id, db_path=db_path)
        assert mock_exec.call_args.kwargs["cwd"] == session.project_path

    def test_falls_back_when_project_path_missing(
        self, session, db_path, tmp_path, caplog
    ):
        from squad.db import _now, _open

        ghost = tmp_path / "ghost"
        _open(db_path)["sessions"].update(
            session.id,
            {"project_path": str(ghost), "updated_at": _now()},
        )
        with patch("squad.research.run_task_text", return_value="# r") as mock_exec:
            with caplog.at_level("WARNING"):
                run_research(session.id, db_path=db_path)
        assert mock_exec.call_args.kwargs["cwd"] is None
        assert any("does not exist" in r.message for r in caplog.records)

    def test_forwards_input_richness_into_prompt(self, session, db_path):
        from squad.db import update_input_richness

        update_input_richness(db_path, session.id, "rich")
        captured: dict[str, str] = {}

        def _capture(prompt, **_kwargs):
            captured["prompt"] = prompt
            return "# r"

        with patch("squad.research.run_task_text", side_effect=_capture):
            run_research(session.id, db_path=db_path)
        assert "combler les angles morts" in captured["prompt"]

