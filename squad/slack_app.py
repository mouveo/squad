"""Slack Bolt app bootstrap — ``squad serve`` entry point.

This module wires up the Bolt ``App`` (tokens, handlers) and the
Socket Mode handler. It intentionally stays thin: the Bolt app and the
handlers are created here, but all business logic lives in
:mod:`squad.slack_service`. A shared :class:`ThreadPoolExecutor` is
owned by ``run_serve`` so long-running pipeline executions never block
the Socket Mode thread.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from squad.config import load_config
from squad.db import ensure_schema
from squad.slack_handlers import register_handlers

logger = logging.getLogger(__name__)


class SlackConfigError(RuntimeError):
    """Raised when required Slack configuration is missing."""


def _require_token(config: dict, path: tuple[str, ...], env_hint: str) -> str:
    """Return a non-empty token from the nested config or raise."""
    cur: object = config
    for part in path:
        if not isinstance(cur, dict):
            cur = None
            break
        cur = cur.get(part)
    if not isinstance(cur, str) or not cur.strip() or cur.startswith("${"):
        dotted = ".".join(path)
        raise SlackConfigError(
            f"Missing Slack token `{dotted}` — set {env_hint} in the environment "
            f"or configure it directly in ~/.squad/config.yaml."
        )
    return cur


def build_app(config: dict):
    """Build the Bolt ``App`` instance from a resolved config dict.

    Import of ``slack_bolt`` is local so the rest of Squad can import
    this module without requiring the optional ``slack`` extra.
    """
    from slack_bolt import App

    bot_token = _require_token(config, ("slack", "bot_token"), "SQUAD_SLACK_BOT_TOKEN")
    return App(token=bot_token)


def run_serve(
    *,
    db_path: Path,
    config: dict | None = None,
    max_workers: int = 4,
) -> None:
    """Start the Slack Socket Mode loop.

    Blocks until the process is interrupted. The shared executor used
    for pipeline runs is created here and shut down cleanly on exit so
    background agents are not abandoned mid-run.
    """
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    ensure_schema(db_path)
    cfg = config if config is not None else load_config()

    app_token = _require_token(cfg, ("slack", "app_token"), "SQUAD_SLACK_APP_TOKEN")
    app = build_app(cfg)

    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="squad-pipeline")
    register_handlers(app, db_path=db_path, executor=executor, config=cfg)

    handler = SocketModeHandler(app, app_token)
    logger.info("Starting Slack Socket Mode (max_workers=%d)", max_workers)
    try:
        handler.start()
    finally:
        executor.shutdown(wait=True, cancel_futures=False)
