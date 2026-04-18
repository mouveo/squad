"""Slack Bolt app bootstrap — ``squad serve`` entry point.

This module wires up the Bolt ``App`` (tokens, handlers) and the
Socket Mode handler. It intentionally stays thin: the Bolt app and the
handlers are created here, but all business logic lives in
:mod:`squad.slack_service`. A shared :class:`ThreadPoolExecutor` is
owned by ``run_serve`` so long-running pipeline executions never block
the Socket Mode thread.

Resilience: ``run_serve`` wraps the blocking ``handler.start()`` in a
supervisor loop that auto-reconnects on disconnect with exponential
backoff, runs a heartbeat thread that periodically logs liveness, and
installs SIGTERM/SIGINT handlers for clean shutdown.
"""

from __future__ import annotations

import logging
import logging.handlers
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from squad.config import load_config
from squad.db import ensure_schema
from squad.slack_handlers import register_handlers

logger = logging.getLogger(__name__)

# Supervisor defaults — tuned for a solo-user Mac install. All overridable
# via ``run_serve`` kwargs exposed on the CLI.
_DEFAULT_BACKOFF_START_SECONDS: int = 5
_DEFAULT_BACKOFF_MAX_SECONDS: int = 600  # 10 minutes ceiling
_DEFAULT_HEARTBEAT_SECONDS: int = 300  # 5 minutes
_LOG_FILE_MAX_BYTES: int = 5 * 1024 * 1024  # 5 MB
_LOG_FILE_BACKUP_COUNT: int = 3

# Tag attached to handlers installed by ``configure_logging`` so we can
# detect prior installs without confusing them with handlers added by
# third parties (pytest's caplog, an embedding app, etc.).
_HANDLER_TAG: str = "_squad_serve_handler"


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


def configure_logging(log_file: Path | None) -> None:
    """Attach console + rotating-file handlers to the root logger.

    Idempotent: handlers installed by a previous call are detected via
    a private tag attribute and the function short-circuits. Handlers
    added by third parties (pytest caplog, embedding applications) are
    left in place and do not block this install.

    The file handler rotates at 5 MB and keeps 3 backups (~20 MB max on
    disk). The parent directory is created if missing.
    """
    root = logging.getLogger()
    if any(getattr(h, _HANDLER_TAG, False) for h in root.handlers):
        return

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    handlers: list[logging.Handler] = []

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    handlers.append(stream)

    if log_file is not None:
        log_file = Path(log_file).expanduser()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            str(log_file),
            maxBytes=_LOG_FILE_MAX_BYTES,
            backupCount=_LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    for handler in handlers:
        setattr(handler, _HANDLER_TAG, True)
        root.addHandler(handler)
    # Only bump the level if the root logger is below INFO — never silence
    # an embedding app that configured DEBUG upstream.
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)


def _heartbeat_loop(
    shutdown_event: threading.Event,
    executor: ThreadPoolExecutor,
    interval_seconds: int,
) -> None:
    """Log a liveness message at fixed intervals until shutdown.

    The message includes the count of pipeline threads currently alive
    (approximate, based on thread name prefix) so a tail on the log file
    reveals stuck pipelines without needing an extra tool.
    """
    while not shutdown_event.wait(interval_seconds):
        active = sum(
            1
            for t in threading.enumerate()
            if t.name.startswith("squad-pipeline") and t.is_alive()
        )
        logger.info("Heartbeat — pipelines running: %d", active)


def _supervisor_loop(
    handler_factory,
    shutdown_event: threading.Event,
    backoff_start_seconds: int,
    backoff_max_seconds: int,
) -> None:
    """Run the Socket Mode handler with auto-reconnect.

    ``handler_factory`` is a zero-arg callable that returns a fresh
    ``SocketModeHandler`` — we re-create on each retry to avoid reusing
    a handler in a broken state. Backoff is exponential, capped at
    ``backoff_max_seconds``, and interrupted by the shutdown event so
    SIGTERM during a backoff exits promptly.
    """
    backoff = backoff_start_seconds
    attempt = 1
    while not shutdown_event.is_set():
        handler = handler_factory()
        logger.info("Socket Mode starting (attempt %d)", attempt)
        try:
            handler.start()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — requesting shutdown")
            shutdown_event.set()
            break
        except Exception:
            logger.exception("Socket Mode crashed with exception")
        else:
            if shutdown_event.is_set():
                logger.info("Socket Mode exited cleanly after shutdown request")
                return
            logger.warning("Socket Mode returned without exception — will reconnect")

        if shutdown_event.is_set():
            return

        wait = min(backoff, backoff_max_seconds)
        logger.info("Reconnecting in %ds (attempt %d)", wait, attempt + 1)
        if shutdown_event.wait(wait):
            return
        backoff = min(backoff * 2, backoff_max_seconds)
        attempt += 1


def _install_signal_handlers(shutdown_event: threading.Event) -> None:
    """Install SIGTERM/SIGINT handlers that set the shutdown event.

    On a non-main thread (e.g. during tests), ``signal.signal`` raises
    ``ValueError``; we swallow it so the supervisor can still be driven
    by the caller setting the event directly.
    """

    def _handler(signum: int, _frame: Any) -> None:
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        logger.warning("Received %s — requesting shutdown", name)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError) as exc:
            logger.debug("Cannot install handler for %s: %s", sig, exc)


def run_serve(
    *,
    db_path: Path,
    config: dict | None = None,
    max_workers: int = 4,
    log_file: Path | None = None,
    reconnect: bool = True,
    heartbeat_seconds: int = _DEFAULT_HEARTBEAT_SECONDS,
    backoff_start_seconds: int = _DEFAULT_BACKOFF_START_SECONDS,
    backoff_max_seconds: int = _DEFAULT_BACKOFF_MAX_SECONDS,
    _shutdown_event: threading.Event | None = None,
) -> None:
    """Start the Slack Socket Mode loop with supervision.

    Blocks until SIGTERM/SIGINT or an explicit shutdown. The supervisor
    auto-reconnects on disconnects; set ``reconnect=False`` to fall back
    to a single-shot ``handler.start()`` (useful for tests or when an
    external supervisor like launchd owns the restart policy).

    ``log_file`` enables the rotating file handler. Pass ``None`` to log
    to stdout only.

    ``_shutdown_event`` is an injection point for tests; in production
    it is created internally and bound to the OS signals.
    """
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    configure_logging(log_file)
    ensure_schema(db_path)
    cfg = config if config is not None else load_config()

    app_token = _require_token(cfg, ("slack", "app_token"), "SQUAD_SLACK_APP_TOKEN")
    app = build_app(cfg)

    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="squad-pipeline")
    register_handlers(app, db_path=db_path, executor=executor, config=cfg)

    shutdown_event = _shutdown_event if _shutdown_event is not None else threading.Event()
    _install_signal_handlers(shutdown_event)

    heartbeat_thread: threading.Thread | None = None
    if heartbeat_seconds > 0:
        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(shutdown_event, executor, heartbeat_seconds),
            name="squad-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()

    logger.info(
        "Starting Slack Socket Mode (max_workers=%d, reconnect=%s, heartbeat=%ds, log_file=%s)",
        max_workers,
        reconnect,
        heartbeat_seconds,
        log_file or "<stdout only>",
    )

    try:
        if reconnect:
            _supervisor_loop(
                handler_factory=lambda: SocketModeHandler(app, app_token),
                shutdown_event=shutdown_event,
                backoff_start_seconds=backoff_start_seconds,
                backoff_max_seconds=backoff_max_seconds,
            )
        else:
            handler = SocketModeHandler(app, app_token)
            try:
                handler.start()
            except KeyboardInterrupt:
                shutdown_event.set()
    finally:
        shutdown_event.set()  # notify heartbeat to stop
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2)
        logger.info("Shutting down pipeline executor")
        executor.shutdown(wait=True, cancel_futures=False)
        logger.info("Squad serve stopped")
