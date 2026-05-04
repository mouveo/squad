"""Tests for squad/subject_detector.py — local inspection, heuristic rules, persistence."""

from pathlib import Path
from unittest.mock import patch

import pytest

from squad.constants import (
    PHASE_BENCHMARK,
    PHASE_CHALLENGE,
    PHASE_CONCEPTION,
    PHASE_ETAT_DES_LIEUX,
)
from squad.db import create_session, ensure_schema, get_session
from squad.executor import AgentError
from squad.models import (
    RESEARCH_DEPTH_DEEP,
    RESEARCH_DEPTH_LIGHT,
    RESEARCH_DEPTH_NORMAL,
    SubjectProfile,
)
from squad.subject_detector import (
    MODEL_LIGHT,
    default_agents_for_signals,
    default_depth_for_signals,
    default_subject_type,
    derive_signals,
    detect_and_persist,
    detect_subject,
    heuristic_profile,
    inspect_project,
)

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


def _session(db_path: Path, idea: str, project_dir: Path):
    return create_session(
        title="test",
        project_path=str(project_dir),
        workspace_path=str(project_dir / ".squad" / "s"),
        idea=idea,
        db_path=db_path,
    )


# ── inspect_project ────────────────────────────────────────────────────────────


class TestInspectProject:
    def test_reads_claude_md(self, project_dir):
        (project_dir / "CLAUDE.md").write_text("# Project context\nB2B SaaS")
        assert "CLAUDE.md" in inspect_project(project_dir)

    def test_reads_pyproject_toml(self, project_dir):
        (project_dir / "pyproject.toml").write_text("[project]\nname='x'")
        assert "pyproject.toml" in inspect_project(project_dir)

    def test_reads_requirements(self, project_dir):
        (project_dir / "requirements.txt").write_text("fastapi\n")
        (project_dir / "requirements-dev.txt").write_text("pytest\n")
        result = inspect_project(project_dir)
        assert "requirements.txt" in result
        assert "requirements-dev.txt" in result

    def test_reads_requirements_variant(self, project_dir):
        (project_dir / "requirements-prod.txt").write_text("httpx\n")
        assert "requirements-prod.txt" in inspect_project(project_dir)

    def test_reads_package_json(self, project_dir):
        (project_dir / "package.json").write_text('{"name":"x"}')
        assert "package.json" in inspect_project(project_dir)

    def test_reads_composer_json(self, project_dir):
        (project_dir / "composer.json").write_text('{"name":"y"}')
        assert "composer.json" in inspect_project(project_dir)

    def test_missing_manifests_silent(self, project_dir):
        assert inspect_project(project_dir) == {}

    def test_nonexistent_project_path(self, tmp_path):
        assert inspect_project(tmp_path / "ghost") == {}

    def test_truncates_large_files(self, project_dir):
        big = "x" * 50_000
        (project_dir / "CLAUDE.md").write_text(big)
        snippets = inspect_project(project_dir)
        assert len(snippets["CLAUDE.md"]) < len(big)
        assert "[truncated]" in snippets["CLAUDE.md"]


# ── derive_signals ─────────────────────────────────────────────────────────────


_RETIRED_AGENTS = frozenset(
    {"sales", "data", "customer-success", "delivery", "growth", "ai-lead", "ideation"}
)


class TestDeriveSignals:
    def test_b2b_signal(self):
        assert "b2b" in derive_signals("Build a B2B SaaS", {})

    def test_ai_signal(self):
        assert "ai" in derive_signals("An LLM-powered assistant with RAG", {})

    def test_pricing_signal(self):
        assert "pricing" in derive_signals("A new Stripe checkout pricing page", {})

    def test_integration_signal(self):
        assert "integration" in derive_signals("Add a webhook integration", {})

    def test_growth_signal(self):
        assert "growth" in derive_signals("Optimise the funnel conversion and retention", {})

    def test_signals_pick_from_inspection(self):
        signals = derive_signals("small idea", {"CLAUDE.md": "multi-tenant compliance"})
        assert "b2b" in signals

    def test_no_false_positive_on_empty(self):
        assert derive_signals("generic idea", {}) == set()

    def test_retired_signals_no_longer_emitted(self):
        """Signals that only fed retired agents must not appear anymore."""
        sample = (
            "A CRM helpdesk with sales leads, churn analytics dashboard, "
            "SSO authentication and onboarding signup."
        )
        assert derive_signals(sample, {}) <= {"b2b", "ai", "pricing", "integration", "growth"}


# ── default_agents_for_signals ─────────────────────────────────────────────────


class TestAgentSelectionRules:
    """Agent composition is fixed in v2 — no signal must reintroduce a retired agent."""

    def test_returns_v2_fixed_map(self):
        agents = default_agents_for_signals(set())
        assert agents[PHASE_ETAT_DES_LIEUX] == ["ux"]
        assert agents[PHASE_CONCEPTION] == ["ux", "architect"]
        assert agents[PHASE_CHALLENGE] == ["architect"]

    @pytest.mark.parametrize(
        "signals",
        [
            set(),
            {"b2b"},
            {"ai"},
            {"b2b", "ai", "pricing", "growth", "integration"},
        ],
    )
    def test_no_retired_agent_ever_returned(self, signals):
        agents = default_agents_for_signals(signals)
        for phase, slugs in agents.items():
            for slug in slugs:
                assert slug not in _RETIRED_AGENTS, (
                    f"{phase} returned retired agent {slug!r} for signals {signals!r}"
                )


# ── default_depth_for_signals ──────────────────────────────────────────────────


class TestDepthRules:
    def test_no_signals_defaults_to_normal(self):
        # A short/under-specified idea (zero detected signals) must NOT
        # skip the benchmark: absence of signals != internal tooling.
        assert default_depth_for_signals(set()) == RESEARCH_DEPTH_NORMAL

    def test_few_market_signals_is_normal(self):
        assert default_depth_for_signals({"ai"}) == RESEARCH_DEPTH_NORMAL

    def test_many_market_signals_is_deep(self):
        assert default_depth_for_signals({"b2b", "ai", "pricing"}) == RESEARCH_DEPTH_DEEP

    def test_light_never_returned_by_deterministic_fallback(self):
        # `light` is reachable only via an explicit Claude classification;
        # the deterministic path never produces it.
        for signals in [set(), {"ai"}, {"b2b"}, {"b2b", "ai", "pricing", "growth"}]:
            assert default_depth_for_signals(signals) != RESEARCH_DEPTH_LIGHT


# ── default_subject_type ───────────────────────────────────────────────────────


class TestSubjectType:
    def test_b2b_ai_product(self):
        assert default_subject_type({"b2b", "ai"}) == "b2b_ai_product"

    def test_ai_product(self):
        assert default_subject_type({"ai"}) == "ai_product"

    def test_b2b_saas(self):
        assert default_subject_type({"b2b"}) == "b2b_saas"

    def test_generic_fallback(self):
        assert default_subject_type(set()) == "generic"


# ── heuristic_profile ──────────────────────────────────────────────────────────


class TestHeuristicProfile:
    def test_b2b_ai_profile(self):
        profile = heuristic_profile("A B2B LLM assistant with Stripe billing", {})
        assert profile.subject_type == "b2b_ai_product"
        assert profile.research_depth == RESEARCH_DEPTH_DEEP
        # v2 fixed agent map — never the retired ai-lead/sales picks.
        assert profile.agents_by_phase[PHASE_CONCEPTION] == ["ux", "architect"]
        assert profile.agents_by_phase[PHASE_ETAT_DES_LIEUX] == ["ux"]
        assert profile.agents_by_phase[PHASE_CHALLENGE] == ["architect"]


# ── detect_subject ─────────────────────────────────────────────────────────────


class TestDetectSubject:
    def test_use_llm_false_returns_heuristic(self, project_dir):
        profile = detect_subject("Build a CRM", project_dir, use_llm=False)
        assert isinstance(profile, SubjectProfile)
        assert profile.subject_type  # derived deterministically

    def test_falls_back_to_heuristic_on_agent_error(self, project_dir):
        with patch(
            "squad.subject_detector.run_task_json",
            side_effect=AgentError("claude down"),
        ):
            profile = detect_subject("Build a B2B tool", project_dir)
        assert profile.subject_type == "b2b_saas"

    def test_falls_back_on_invalid_json(self, project_dir):
        with patch(
            "squad.subject_detector.run_task_json",
            side_effect=ValueError("bad json"),
        ):
            profile = detect_subject("Build a B2B tool", project_dir)
        assert profile.subject_type == "b2b_saas"

    def test_uses_llm_result_when_valid(self, project_dir):
        fake = {
            "subject_type": "internal_tool",
            "research_depth": "light",
        }
        with patch("squad.subject_detector.run_task_json", return_value=fake):
            profile = detect_subject("internal tool", project_dir)
        assert profile.subject_type == "internal_tool"
        assert profile.research_depth == RESEARCH_DEPTH_LIGHT
        # The LLM no longer drives agent selection — v2 fixed map wins.
        assert profile.agents_by_phase[PHASE_CONCEPTION] == ["ux", "architect"]
        assert profile.agents_by_phase[PHASE_CHALLENGE] == ["architect"]

    def test_llm_hallucinated_agents_are_ignored(self, project_dir):
        """A Claude payload that lists retired agents must not leak through."""
        fake = {
            "subject_type": "b2b_saas",
            "research_depth": "normal",
            "agents_by_phase": {
                "etat_des_lieux": ["customer-success", "sales", "data", "ux"],
                "conception": ["ai-lead", "growth", "architect", "ux"],
                "challenge": ["security", "delivery", "architect"],
            },
        }
        with patch("squad.subject_detector.run_task_json", return_value=fake):
            profile = detect_subject("idea", project_dir)
        # Retired agents must not appear anywhere in the persisted map.
        for slugs in profile.agents_by_phase.values():
            for slug in slugs:
                assert slug not in _RETIRED_AGENTS
                assert slug != "security"

    def test_uses_light_model_for_classification(self, project_dir):
        fake = {
            "subject_type": "generic",
            "research_depth": "normal",
            "agents_by_phase": {},
        }
        with patch("squad.subject_detector.run_task_json", return_value=fake) as mock_task:
            detect_subject("idea", project_dir)
        assert mock_task.call_args.kwargs["model"] == MODEL_LIGHT


# ── detect_and_persist ─────────────────────────────────────────────────────────


class TestDetectAndPersist:
    def test_persists_profile_on_first_call(self, db_path, project_dir):
        s = _session(db_path, "Build a B2B SaaS CRM", project_dir)
        detect_and_persist(s.id, use_llm=False, db_path=db_path)
        fetched = get_session(s.id, db_path)
        assert fetched.subject_type == "b2b_saas"
        assert fetched.research_depth in {RESEARCH_DEPTH_NORMAL, RESEARCH_DEPTH_DEEP}
        assert fetched.agents_by_phase

    def test_resume_does_not_reclassify(self, db_path, project_dir):
        s = _session(db_path, "Build a B2B SaaS CRM", project_dir)
        detect_and_persist(s.id, use_llm=False, db_path=db_path)
        original = get_session(s.id, db_path).subject_type

        with patch("squad.subject_detector.detect_subject") as mock_detect:
            detect_and_persist(s.id, use_llm=True, db_path=db_path)
        mock_detect.assert_not_called()
        assert get_session(s.id, db_path).subject_type == original

    def test_force_reruns_classification(self, db_path, project_dir):
        s = _session(db_path, "Build a B2B SaaS CRM", project_dir)
        detect_and_persist(s.id, use_llm=False, db_path=db_path)
        forced = SubjectProfile(
            subject_type="overridden",
            research_depth=RESEARCH_DEPTH_DEEP,
            agents_by_phase={
                PHASE_ETAT_DES_LIEUX: ["ux"],
                PHASE_CONCEPTION: ["ux", "architect"],
                PHASE_CHALLENGE: ["architect"],
            },
        )
        with patch("squad.subject_detector.detect_subject", return_value=forced):
            detect_and_persist(s.id, force=True, db_path=db_path)
        assert get_session(s.id, db_path).subject_type == "overridden"

    def test_light_depth_marks_benchmark_skipped(self, db_path, project_dir):
        # `light` is reachable only through an explicit Claude
        # classification now (internal tooling judgement). Mock it here
        # to assert that the skip-benchmark wiring still fires when the
        # depth actually is `light`.
        s = _session(db_path, "Internal admin script", project_dir)
        fake = {
            "subject_type": "internal_tool",
            "research_depth": "light",
            "agents_by_phase": {},
        }
        with patch("squad.subject_detector.run_task_json", return_value=fake):
            profile = detect_and_persist(s.id, use_llm=True, db_path=db_path)
        assert profile.research_depth == RESEARCH_DEPTH_LIGHT
        fetched = get_session(s.id, db_path)
        assert PHASE_BENCHMARK in fetched.skipped_phases
        assert "light" in fetched.skipped_phases[PHASE_BENCHMARK].lower()

    def test_under_specified_idea_does_not_skip_benchmark(self, db_path, project_dir):
        # Regression guard for the "short idea = no benchmark" paradox.
        # A vague 3-word idea with zero detected signals must still run
        # the benchmark phase (depth=normal, not light).
        s = _session(db_path, "generic idea with no market signals", project_dir)
        profile = detect_and_persist(s.id, use_llm=False, db_path=db_path)
        assert profile.research_depth == RESEARCH_DEPTH_NORMAL
        fetched = get_session(s.id, db_path)
        assert PHASE_BENCHMARK not in fetched.skipped_phases

    def test_normal_depth_leaves_benchmark_active(self, db_path, project_dir):
        s = _session(db_path, "Build a B2B SaaS with pricing", project_dir)
        profile = detect_and_persist(s.id, use_llm=False, db_path=db_path)
        # With 'b2b' + 'pricing' signals → normal (2 market signals, <3)
        assert profile.research_depth == RESEARCH_DEPTH_NORMAL
        fetched = get_session(s.id, db_path)
        assert PHASE_BENCHMARK not in fetched.skipped_phases

    def test_raises_on_unknown_session(self, db_path):
        with pytest.raises(ValueError):
            detect_and_persist("ghost-id", db_path=db_path)
