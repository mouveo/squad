"""Configuration helpers — filesystem paths and YAML config loading.

Squad reads a global YAML at ``~/.squad/config.yaml`` and (optionally) a
project-level YAML at ``{project_path}/.squad/config.yaml``. Project
values are deep-merged on top of the global ones, then ``${VAR}``
placeholders are expanded against the current environment.
"""

import os
import re
from pathlib import Path
from typing import Any

import yaml

# ── Filesystem path helpers (existing API — preserved verbatim) ────────────────


def get_squad_home() -> Path:
    """Return the global Squad home directory (~/.squad)."""
    return Path.home() / ".squad"


def get_global_db_path() -> Path:
    """Return the path to the global SQLite database."""
    return get_squad_home() / "squad.db"


def get_project_state_dir(project_path: str | Path) -> Path:
    """Return the .squad state directory inside a project."""
    return Path(project_path) / ".squad"


# ── Config file paths ──────────────────────────────────────────────────────────


def get_global_config_path() -> Path:
    """Return the path to the global YAML config (~/.squad/config.yaml)."""
    return get_squad_home() / "config.yaml"


def get_project_config_path(project_path: str | Path) -> Path:
    """Return the path to a project-level YAML config."""
    return get_project_state_dir(project_path) / "config.yaml"


# ── Default YAML template ──────────────────────────────────────────────────────


DEFAULT_CONFIG_YAML = """\
# Squad configuration
#
# Global file: ~/.squad/config.yaml
# Project override: {project}/.squad/config.yaml (deep-merged on top of global)
# ${VAR} placeholders are expanded against the current environment.

# Default execution mode when `--mode` is not passed on the CLI.
# Valid values: approval, autonomous
mode: approval

# Default Claude model used by the executor.
# model: claude-opus-4-7[1m]

# Root directory scanned for idea → project auto-discovery. When an
# idea mentions a folder name present under this root (e.g. "Ajouter un
# CRM à sitavista"), Squad resolves the project path automatically.
# Overridden by explicit `slack.channels.<id>.project_path` mappings.
dev_root: ~/Developer

# Slack notifications (squad.notifier).
slack:
  # webhook: ${SQUAD_SLACK_WEBHOOK}
  #
  # Interactive Slack app — `squad serve` (Socket Mode).
  # bot_token: ${SQUAD_SLACK_BOT_TOKEN}
  # app_token: ${SQUAD_SLACK_APP_TOKEN}
  #
  # Allowlist of Slack user IDs permitted to drive Squad (empty = no allowlist).
  # allowed_user_ids: []
  #
  # Map a Slack channel ID to the local project path Squad should target.
  # channels:
  #   C0123456789:
  #     project_path: /absolute/path/to/project
  #
  # Attachments uploaded to a session thread (Plan 4 — LOT 3).
  # attachments:
  #   allowed_extensions: [md, txt, csv, pdf, png, jpg, jpeg]
  #   max_file_bytes: 10485760       # 10 MB per file
  #   max_total_bytes: 52428800      # 50 MB cumulés par session

# Forge integration.
forge:
  # CLI binary used to enqueue plans; defaults to `forge` on PATH.
  # cli: forge

# Pipeline tuning.
pipeline:
  # Per-agent timeout in seconds.
  # agent_timeout: 900
  #
  # Soft budget in characters for the cumulative context injected into
  # each phase prompt (~15k tokens at 4 chars/token). The context
  # builder compresses the oldest phase outputs first, then drops their
  # summaries with an explicit omission marker, and as a last resort
  # truncates with a `[… contexte tronqué au-delà du budget]` marker.
  # context_budget_chars: 60000
"""


# ── Internal helpers ───────────────────────────────────────────────────────────


_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_env_vars(value: Any) -> Any:
    """Recursively expand ``${VAR}`` placeholders inside strings.

    Unknown variables are left as-is so configuration mistakes surface
    instead of silently becoming empty strings.
    """
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), m.group(0)),
            value,
        )
    if isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_vars(v) for v in value]
    return value


def _deep_merge(base: dict, override: dict) -> dict:
    """Return a new dict with ``override`` deep-merged onto ``base``.

    Nested mappings are merged key by key; everything else (lists, scalars)
    is replaced by the override.
    """
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for key, val in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(val, dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _load_yaml(path: Path) -> dict:
    """Load a YAML file as a dict, returning ``{}`` if it does not exist."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config at {path} must be a YAML mapping, got {type(data).__name__}")
    return data


# ── Public config API ──────────────────────────────────────────────────────────


def load_config(project_path: str | Path | None = None) -> dict:
    """Load the merged Squad configuration.

    Reads the global config first, then overlays the project-level config
    when ``project_path`` is provided. Environment variables of the form
    ``${VAR}`` are expanded after merging. Returns ``{}`` when no config
    file exists.
    """
    merged = _load_yaml(get_global_config_path())
    if project_path is not None:
        merged = _deep_merge(merged, _load_yaml(get_project_config_path(project_path)))
    return _resolve_env_vars(merged)


def get_config_value(
    key: str,
    project_path: str | Path | None = None,
    default: Any = None,
) -> Any:
    """Return ``key`` from the merged config using dot notation.

    Returns ``default`` when any segment of the path is missing.
    """
    cur: Any = load_config(project_path)
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def write_default_config(path: str | Path, force: bool = False) -> bool:
    """Write the commented default YAML config at ``path``.

    Creates parent directories as needed. Returns ``True`` if a file was
    written, ``False`` if one already existed and ``force`` was ``False``.
    """
    target = Path(path)
    if target.exists() and not force:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")
    return True
