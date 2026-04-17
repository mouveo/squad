"""Tests for squad/phase_config.py — phase configs, critical agents, skip policy."""

import pytest

from squad.constants import (
    PHASE_BENCHMARK,
    PHASE_CADRAGE,
    PHASE_CHALLENGE,
    PHASE_CONCEPTION,
    PHASE_ETAT_DES_LIEUX,
    PHASE_SYNTHESE,
    PHASES,
)
from squad.phase_config import (
    PHASE_CONFIGS,
    PhaseConfig,
    get_phase_config,
    is_critical_agent,
    iter_phases,
    should_skip_phase,
)


class TestPhaseConfigsCoverage:
    def test_every_constant_phase_has_config(self):
        for phase in PHASES:
            assert phase in PHASE_CONFIGS

    def test_no_extra_phase_configs(self):
        assert set(PHASE_CONFIGS) == set(PHASES)

    def test_orders_are_contiguous_and_match_constant_order(self):
        for expected_order, phase in enumerate(PHASES, start=1):
            assert PHASE_CONFIGS[phase].order == expected_order


class TestGetPhaseConfig:
    def test_returns_config_for_known_phase(self):
        cfg = get_phase_config(PHASE_CADRAGE)
        assert isinstance(cfg, PhaseConfig)
        assert cfg.phase == PHASE_CADRAGE

    def test_raises_for_unknown_phase(self):
        with pytest.raises(KeyError):
            get_phase_config("nope")


class TestIterPhases:
    def test_returns_phases_in_canonical_order(self):
        phases = [cfg.phase for cfg in iter_phases()]
        assert phases == PHASES


class TestCadrageConfig:
    def test_pm_is_default_and_critical(self):
        cfg = get_phase_config(PHASE_CADRAGE)
        assert cfg.default_agents == ("pm",)
        assert "pm" in cfg.critical_agents

    def test_can_pause_with_max_questions(self):
        cfg = get_phase_config(PHASE_CADRAGE)
        assert cfg.can_pause is True
        assert cfg.max_questions > 0


class TestEtatDesLieuxConfig:
    def test_all_real_agents_listed(self):
        cfg = get_phase_config(PHASE_ETAT_DES_LIEUX)
        assert set(cfg.default_agents) == {"customer-success", "data", "sales", "ux"}

    def test_is_parallel(self):
        cfg = get_phase_config(PHASE_ETAT_DES_LIEUX)
        assert cfg.parallel is True

    def test_no_critical_agents(self):
        cfg = get_phase_config(PHASE_ETAT_DES_LIEUX)
        assert cfg.critical_agents == ()


class TestBenchmarkConfig:
    def test_default_agent_is_research(self):
        cfg = get_phase_config(PHASE_BENCHMARK)
        assert cfg.default_agents == ("research",)

    def test_skippable_for_light_depth(self):
        cfg = get_phase_config(PHASE_BENCHMARK)
        assert cfg.skip_policy.skippable is True
        assert "light" in cfg.skip_policy.skip_when_depth


class TestConceptionConfig:
    def test_parallel_with_real_agents(self):
        cfg = get_phase_config(PHASE_CONCEPTION)
        assert cfg.parallel is True
        assert set(cfg.default_agents) == {"ai-lead", "architect", "growth", "ux"}

    def test_allows_one_retry(self):
        cfg = get_phase_config(PHASE_CONCEPTION)
        assert cfg.retry_policy.max_attempts == 2
        assert cfg.retry_policy.retry_on_contract_field == "blockers"


class TestChallengeConfig:
    def test_challenge_uses_security_delivery_architect_not_finops(self):
        cfg = get_phase_config(PHASE_CHALLENGE)
        assert set(cfg.default_agents) == {"security", "delivery", "architect"}
        assert "finops" not in cfg.default_agents

    def test_is_parallel(self):
        cfg = get_phase_config(PHASE_CHALLENGE)
        assert cfg.parallel is True


class TestSyntheseConfig:
    def test_pm_is_default_and_critical(self):
        cfg = get_phase_config(PHASE_SYNTHESE)
        assert cfg.default_agents == ("pm",)
        assert "pm" in cfg.critical_agents

    def test_cannot_pause(self):
        cfg = get_phase_config(PHASE_SYNTHESE)
        assert cfg.can_pause is False


class TestIsCriticalAgent:
    def test_pm_is_critical_in_cadrage(self):
        assert is_critical_agent("pm", PHASE_CADRAGE) is True

    def test_pm_is_critical_in_synthese(self):
        assert is_critical_agent("pm", PHASE_SYNTHESE) is True

    def test_architect_not_critical_in_challenge(self):
        assert is_critical_agent("architect", PHASE_CHALLENGE) is False

    def test_unknown_agent_returns_false(self):
        assert is_critical_agent("unknown", PHASE_CADRAGE) is False


class TestShouldSkipPhase:
    def test_benchmark_skipped_for_light_depth(self):
        assert should_skip_phase(PHASE_BENCHMARK, "light") is True

    def test_benchmark_not_skipped_for_normal_depth(self):
        assert should_skip_phase(PHASE_BENCHMARK, "normal") is False

    def test_cadrage_never_skipped(self):
        assert should_skip_phase(PHASE_CADRAGE, "light") is False

    def test_none_depth_never_skips(self):
        assert should_skip_phase(PHASE_BENCHMARK, None) is False
