"""Tests for squad.slack_app — logging config + supervisor resilience."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from squad.slack_app import (
    _HANDLER_TAG,
    _heartbeat_loop,
    _install_signal_handlers,
    _supervisor_loop,
    configure_logging,
    run_serve,
)


@pytest.fixture(autouse=True)
def _reset_squad_handlers():
    """Drop any handler previously tagged by configure_logging."""
    root = logging.getLogger()
    before = [h for h in root.handlers if not getattr(h, _HANDLER_TAG, False)]
    root.handlers = before
    yield
    root.handlers = [h for h in root.handlers if not getattr(h, _HANDLER_TAG, False)]


def _squad_handlers():
    return [h for h in logging.getLogger().handlers if getattr(h, _HANDLER_TAG, False)]


# ── configure_logging ──────────────────────────────────────────────────────────


class TestConfigureLogging:
    def test_attaches_stream_handler_when_no_file(self):
        configure_logging(None)
        ours = _squad_handlers()
        assert len(ours) == 1
        assert isinstance(ours[0], logging.StreamHandler)

    def test_attaches_rotating_file_handler_when_file_given(self, tmp_path: Path):
        import logging.handlers as _handlers

        log_file = tmp_path / "sub" / "serve.log"
        configure_logging(log_file)
        ours = _squad_handlers()
        assert any(isinstance(h, _handlers.RotatingFileHandler) for h in ours)
        assert log_file.parent.exists()

    def test_is_idempotent_across_repeated_calls(self, tmp_path: Path):
        configure_logging(tmp_path / "serve.log")
        first = list(_squad_handlers())
        configure_logging(tmp_path / "serve.log")
        second = list(_squad_handlers())
        assert first == second  # no duplicate install

    def test_does_not_remove_third_party_handlers(self, tmp_path: Path):
        root = logging.getLogger()
        sentinel = logging.NullHandler()
        root.addHandler(sentinel)
        configure_logging(tmp_path / "serve.log")
        assert sentinel in root.handlers  # we preserved caplog/embedding handlers

    def test_log_file_is_actually_written(self, tmp_path: Path):
        log_file = tmp_path / "serve.log"
        configure_logging(log_file)
        logging.getLogger("squad.test").info("hello from test")
        for h in _squad_handlers():
            h.flush()
        assert log_file.exists()
        assert "hello from test" in log_file.read_text()


# ── _supervisor_loop ───────────────────────────────────────────────────────────


class TestSupervisorLoop:
    def test_returns_immediately_if_shutdown_already_set(self):
        event = threading.Event()
        event.set()
        factory = MagicMock()
        _supervisor_loop(factory, event, 1, 10)
        factory.assert_not_called()

    def test_runs_handler_and_exits_cleanly_on_shutdown(self):
        event = threading.Event()

        def fake_start():
            event.set()  # simulate a graceful exit path

        handler = MagicMock()
        handler.start.side_effect = fake_start
        factory = MagicMock(return_value=handler)

        _supervisor_loop(factory, event, 1, 10)

        factory.assert_called_once()
        handler.start.assert_called_once()

    def test_retries_with_backoff_on_exception(self):
        event = threading.Event()
        attempts: list[int] = []

        def fake_start():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("first failure")
            event.set()

        handler = MagicMock()
        handler.start.side_effect = fake_start
        factory = MagicMock(return_value=handler)

        # Use 0s backoff so the test doesn't sleep.
        _supervisor_loop(factory, event, 0, 0)

        assert len(attempts) == 2
        assert factory.call_count == 2  # fresh handler per attempt

    def test_reconnects_when_handler_returns_without_shutdown(self):
        event = threading.Event()
        calls: list[int] = []

        def fake_start():
            calls.append(1)
            if len(calls) == 1:
                return  # Socket Mode returned unexpectedly
            event.set()

        handler = MagicMock()
        handler.start.side_effect = fake_start
        factory = MagicMock(return_value=handler)

        _supervisor_loop(factory, event, 0, 0)

        assert len(calls) == 2

    def test_keyboard_interrupt_breaks_out(self):
        event = threading.Event()
        handler = MagicMock()
        handler.start.side_effect = KeyboardInterrupt()
        factory = MagicMock(return_value=handler)

        _supervisor_loop(factory, event, 0, 0)

        assert event.is_set()
        factory.assert_called_once()

    def test_caps_backoff_at_max(self):
        """Backoff must not grow past the ceiling even after many failures."""
        from squad import slack_app as sa

        event = threading.Event()
        waits: list[float] = []

        real_wait = event.wait

        def spy_wait(timeout):
            waits.append(timeout)
            if len(waits) >= 5:
                event.set()
            return real_wait(0)  # don't actually sleep

        event.wait = spy_wait  # type: ignore[assignment]

        handler = MagicMock()
        handler.start.side_effect = RuntimeError("fail")
        factory = MagicMock(return_value=handler)

        sa._supervisor_loop(factory, event, 1, 4)
        # Expected backoff sequence capped at 4: [1, 2, 4, 4, 4]
        assert waits[:5] == [1, 2, 4, 4, 4]


# ── _heartbeat_loop ────────────────────────────────────────────────────────────


class TestHeartbeatLoop:
    def test_exits_when_shutdown_event_is_set(self):
        event = threading.Event()
        event.set()
        executor = MagicMock()
        # Should return immediately without logging (interval is irrelevant here).
        _heartbeat_loop(event, executor, interval_seconds=1000)

    def test_logs_at_least_once_before_shutdown(self, caplog):
        event = threading.Event()

        def stop_after_first_wait():
            event.set()
            return True

        with caplog.at_level(logging.INFO, logger="squad.slack_app"):
            with patch.object(event, "wait", side_effect=[False, True]):
                _heartbeat_loop(event, MagicMock(), interval_seconds=0)

        assert any("Heartbeat" in record.message for record in caplog.records)


# ── _install_signal_handlers ───────────────────────────────────────────────────


class TestInstallSignalHandlers:
    def test_swallows_value_error_when_not_on_main_thread(self):
        event = threading.Event()

        def run():
            _install_signal_handlers(event)  # must not raise

        t = threading.Thread(target=run)
        t.start()
        t.join(timeout=2)
        assert not t.is_alive()


# ── run_serve (integration-ish, handler mocked) ────────────────────────────────


class TestRunServe:
    def test_shutdown_event_exits_cleanly(self, tmp_path: Path):
        """Injecting a pre-set shutdown event must short-circuit the supervisor."""
        event = threading.Event()
        event.set()

        handler = MagicMock()
        cfg = {
            "slack": {"bot_token": "xoxb-test", "app_token": "xapp-test"},
        }

        with (
            patch("squad.slack_app.SocketModeHandler", return_value=handler, create=True)
            if False
            else patch(
                "slack_bolt.adapter.socket_mode.SocketModeHandler", return_value=handler
            ),
            patch("squad.slack_app.build_app", return_value=MagicMock()),
            patch("squad.slack_app.register_handlers"),
        ):
            run_serve(
                db_path=tmp_path / "squad.db",
                config=cfg,
                max_workers=1,
                log_file=None,
                reconnect=True,
                heartbeat_seconds=0,
                backoff_start_seconds=0,
                backoff_max_seconds=0,
                _shutdown_event=event,
            )
        # Handler never started because event was already set.
        handler.start.assert_not_called()

    def test_no_reconnect_flag_goes_through_single_shot(self, tmp_path: Path):
        event = threading.Event()

        handler = MagicMock()
        handler.start.side_effect = lambda: event.set()
        cfg = {"slack": {"bot_token": "xoxb-test", "app_token": "xapp-test"}}

        with (
            patch(
                "slack_bolt.adapter.socket_mode.SocketModeHandler", return_value=handler
            ),
            patch("squad.slack_app.build_app", return_value=MagicMock()),
            patch("squad.slack_app.register_handlers"),
        ):
            run_serve(
                db_path=tmp_path / "squad.db",
                config=cfg,
                max_workers=1,
                log_file=None,
                reconnect=False,
                heartbeat_seconds=0,
                _shutdown_event=event,
            )

        handler.start.assert_called_once()
