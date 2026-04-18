"""Tests for squad/config.py — path helpers, YAML loading, merging, env vars."""

from pathlib import Path

import pytest

from squad.config import (
    DEFAULT_CONFIG_YAML,
    _deep_merge,
    _resolve_env_vars,
    get_config_value,
    get_global_config_path,
    get_global_db_path,
    get_project_config_path,
    get_project_state_dir,
    get_squad_home,
    load_config,
    write_default_config,
)


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()`` to a temporary directory."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# ── Path helpers preserved ─────────────────────────────────────────────────────


class TestPathHelpers:
    def test_squad_home_under_home(self, fake_home: Path):
        assert get_squad_home() == fake_home / ".squad"

    def test_global_db_path(self, fake_home: Path):
        assert get_global_db_path() == fake_home / ".squad" / "squad.db"

    def test_project_state_dir(self, tmp_path: Path):
        proj = tmp_path / "p"
        assert get_project_state_dir(proj) == proj / ".squad"

    def test_global_config_path(self, fake_home: Path):
        assert get_global_config_path() == fake_home / ".squad" / "config.yaml"

    def test_project_config_path(self, tmp_path: Path):
        proj = tmp_path / "proj"
        assert get_project_config_path(proj) == proj / ".squad" / "config.yaml"


# ── _resolve_env_vars ─────────────────────────────────────────────────────────


class TestResolveEnvVars:
    def test_string_replacement(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("FOO", "bar")
        assert _resolve_env_vars("${FOO}") == "bar"

    def test_unknown_var_left_as_is(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("DOES_NOT_EXIST", raising=False)
        assert _resolve_env_vars("${DOES_NOT_EXIST}") == "${DOES_NOT_EXIST}"

    def test_dict_recursive(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("X", "1")
        assert _resolve_env_vars({"a": "${X}", "b": {"c": "${X}"}}) == {
            "a": "1",
            "b": {"c": "1"},
        }

    def test_list_recursive(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("X", "y")
        assert _resolve_env_vars(["${X}", "z"]) == ["y", "z"]

    def test_scalar_passthrough(self):
        assert _resolve_env_vars(42) == 42
        assert _resolve_env_vars(None) is None
        assert _resolve_env_vars(True) is True

    def test_partial_string(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HOST", "example.com")
        assert _resolve_env_vars("https://${HOST}/api") == "https://example.com/api"

    def test_multiple_vars_in_one_string(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        assert _resolve_env_vars("${A}-${B}") == "1-2"


# ── _deep_merge ───────────────────────────────────────────────────────────────


class TestDeepMerge:
    def test_overlays_keys(self):
        assert _deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_override_wins_on_scalar_conflict(self):
        assert _deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_nested_merge(self):
        result = _deep_merge(
            {"x": {"a": 1, "b": 2}},
            {"x": {"b": 3, "c": 4}},
        )
        assert result == {"x": {"a": 1, "b": 3, "c": 4}}

    def test_lists_are_replaced_not_concatenated(self):
        assert _deep_merge({"a": [1, 2]}, {"a": [3]}) == {"a": [3]}

    def test_does_not_mutate_inputs(self):
        base = {"a": {"b": 1}}
        override = {"a": {"c": 2}}
        _deep_merge(base, override)
        assert base == {"a": {"b": 1}}
        assert override == {"a": {"c": 2}}


# ── load_config ───────────────────────────────────────────────────────────────


class TestLoadConfig:
    def test_returns_empty_when_no_files(self, fake_home: Path, tmp_path: Path):
        assert load_config(tmp_path / "proj") == {}

    def test_loads_global_only(self, fake_home: Path):
        cfg = get_global_config_path()
        cfg.parent.mkdir(parents=True)
        cfg.write_text("mode: autonomous\n")
        assert load_config() == {"mode": "autonomous"}

    def test_project_overrides_global(self, fake_home: Path, tmp_path: Path):
        cfg = get_global_config_path()
        cfg.parent.mkdir(parents=True)
        cfg.write_text("mode: approval\nmodel: opus\n")

        proj = tmp_path / "p"
        proj_cfg = get_project_config_path(proj)
        proj_cfg.parent.mkdir(parents=True)
        proj_cfg.write_text("mode: autonomous\n")

        assert load_config(proj) == {"mode": "autonomous", "model": "opus"}

    def test_project_deep_merges_nested_keys(self, fake_home: Path, tmp_path: Path):
        cfg = get_global_config_path()
        cfg.parent.mkdir(parents=True)
        cfg.write_text("slack:\n  webhook: https://global\n  channel: ops\n")

        proj = tmp_path / "p"
        proj_cfg = get_project_config_path(proj)
        proj_cfg.parent.mkdir(parents=True)
        proj_cfg.write_text("slack:\n  webhook: https://project\n")

        assert load_config(proj) == {"slack": {"webhook": "https://project", "channel": "ops"}}

    def test_resolves_env_vars_after_merge(self, fake_home: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("WEBHOOK", "https://hooks.test/abc")
        cfg = get_global_config_path()
        cfg.parent.mkdir(parents=True)
        cfg.write_text("slack:\n  webhook: ${WEBHOOK}\n")

        assert load_config()["slack"]["webhook"] == "https://hooks.test/abc"

    def test_invalid_yaml_root_raises(self, fake_home: Path):
        cfg = get_global_config_path()
        cfg.parent.mkdir(parents=True)
        cfg.write_text("- one\n- two\n")
        with pytest.raises(ValueError):
            load_config()


# ── get_config_value ──────────────────────────────────────────────────────────


class TestGetConfigValue:
    def test_dot_notation(self, fake_home: Path):
        cfg = get_global_config_path()
        cfg.parent.mkdir(parents=True)
        cfg.write_text("a:\n  b:\n    c: 42\n")
        assert get_config_value("a.b.c") == 42

    def test_top_level(self, fake_home: Path):
        cfg = get_global_config_path()
        cfg.parent.mkdir(parents=True)
        cfg.write_text("mode: autonomous\n")
        assert get_config_value("mode") == "autonomous"

    def test_default_on_missing(self, fake_home: Path):
        assert get_config_value("missing.key", default="x") == "x"

    def test_default_when_segment_is_not_dict(self, fake_home: Path):
        cfg = get_global_config_path()
        cfg.parent.mkdir(parents=True)
        cfg.write_text("mode: autonomous\n")
        assert get_config_value("mode.something", default="fallback") == "fallback"

    def test_project_override_visible(self, fake_home: Path, tmp_path: Path):
        cfg = get_global_config_path()
        cfg.parent.mkdir(parents=True)
        cfg.write_text("mode: approval\n")
        proj = tmp_path / "p"
        proj_cfg = get_project_config_path(proj)
        proj_cfg.parent.mkdir(parents=True)
        proj_cfg.write_text("mode: autonomous\n")
        assert get_config_value("mode", project_path=proj) == "autonomous"


# ── write_default_config ──────────────────────────────────────────────────────


class TestWriteDefaultConfig:
    def test_writes_when_missing(self, tmp_path: Path):
        target = tmp_path / "sub" / "config.yaml"
        assert write_default_config(target) is True
        assert target.exists()
        assert target.read_text() == DEFAULT_CONFIG_YAML

    def test_creates_parent_directories(self, tmp_path: Path):
        target = tmp_path / "deep" / "nested" / "config.yaml"
        write_default_config(target)
        assert target.exists()

    def test_does_not_overwrite_by_default(self, tmp_path: Path):
        target = tmp_path / "config.yaml"
        target.write_text("existing: true\n")
        assert write_default_config(target) is False
        assert "existing" in target.read_text()

    def test_force_overwrites(self, tmp_path: Path):
        target = tmp_path / "config.yaml"
        target.write_text("existing: true\n")
        assert write_default_config(target, force=True) is True
        assert target.read_text() == DEFAULT_CONFIG_YAML

    def test_default_yaml_is_loadable(self, fake_home: Path):
        target = get_global_config_path()
        write_default_config(target)
        loaded = load_config()
        assert loaded["mode"] == "approval"


# ── Slack config (LOT 1 — Plan 4) ─────────────────────────────────────────────


class TestSlackConfig:
    def test_default_yaml_documents_slack_keys(self):
        # Interactive Slack keys are documented (commented) in the default template
        # so users know what to set when enabling `squad serve`.
        assert "bot_token" in DEFAULT_CONFIG_YAML
        assert "app_token" in DEFAULT_CONFIG_YAML
        assert "allowed_user_ids" in DEFAULT_CONFIG_YAML
        assert "channels:" in DEFAULT_CONFIG_YAML
        assert "project_path" in DEFAULT_CONFIG_YAML

    def test_channel_mapping_roundtrip(self, fake_home: Path, tmp_path: Path):
        cfg = get_global_config_path()
        cfg.parent.mkdir(parents=True)
        cfg.write_text(
            "slack:\n"
            "  bot_token: xoxb-abc\n"
            "  app_token: xapp-abc\n"
            "  allowed_user_ids: [U1, U2]\n"
            "  channels:\n"
            "    C999:\n"
            "      project_path: /tmp/proj\n",
            encoding="utf-8",
        )
        loaded = load_config()
        assert loaded["slack"]["bot_token"] == "xoxb-abc"
        assert loaded["slack"]["app_token"] == "xapp-abc"
        assert loaded["slack"]["allowed_user_ids"] == ["U1", "U2"]
        assert loaded["slack"]["channels"]["C999"]["project_path"] == "/tmp/proj"

    def test_slack_env_interpolation(self, fake_home: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SQUAD_SLACK_BOT_TOKEN", "xoxb-env")
        monkeypatch.setenv("SQUAD_SLACK_APP_TOKEN", "xapp-env")
        cfg = get_global_config_path()
        cfg.parent.mkdir(parents=True)
        cfg.write_text(
            "slack:\n"
            "  bot_token: ${SQUAD_SLACK_BOT_TOKEN}\n"
            "  app_token: ${SQUAD_SLACK_APP_TOKEN}\n",
            encoding="utf-8",
        )
        loaded = load_config()
        assert loaded["slack"]["bot_token"] == "xoxb-env"
        assert loaded["slack"]["app_token"] == "xapp-env"
