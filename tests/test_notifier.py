"""Tests for squad/notifier.py — Slack webhook notifications."""

import logging
from unittest.mock import MagicMock, patch

from squad.notifier import notify_agent_error, notify_plans_ready, notify_questions_pending

# ── helpers ────────────────────────────────────────────────────────────────────


def _mock_response(status_code: int = 200) -> MagicMock:
    mock = MagicMock()
    mock.status_code = status_code
    mock.raise_for_status = MagicMock()
    if status_code >= 400:
        mock.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return mock


# ── webhook resolution ─────────────────────────────────────────────────────────


class TestWebhookResolution:
    @patch("squad.notifier.httpx.post")
    def test_uses_squad_webhook_when_set(self, mock_post, monkeypatch):
        monkeypatch.setenv("SQUAD_SLACK_WEBHOOK", "https://hooks.slack.com/squad")
        monkeypatch.delenv("FORGE_SLACK_WEBHOOK", raising=False)
        mock_post.return_value = _mock_response()
        notify_plans_ready("sess-1", "My project", 2)
        mock_post.assert_called_once()
        assert mock_post.call_args[0][0] == "https://hooks.slack.com/squad"

    @patch("squad.notifier.httpx.post")
    def test_falls_back_to_forge_webhook(self, mock_post, monkeypatch):
        monkeypatch.delenv("SQUAD_SLACK_WEBHOOK", raising=False)
        monkeypatch.setenv("FORGE_SLACK_WEBHOOK", "https://hooks.slack.com/forge")
        mock_post.return_value = _mock_response()
        notify_plans_ready("sess-1", "My project", 2)
        mock_post.assert_called_once()
        assert mock_post.call_args[0][0] == "https://hooks.slack.com/forge"

    @patch("squad.notifier.httpx.post")
    def test_squad_takes_priority_over_forge(self, mock_post, monkeypatch):
        monkeypatch.setenv("SQUAD_SLACK_WEBHOOK", "https://squad-webhook")
        monkeypatch.setenv("FORGE_SLACK_WEBHOOK", "https://forge-webhook")
        mock_post.return_value = _mock_response()
        notify_plans_ready("sess-1", "My project", 2)
        assert mock_post.call_args[0][0] == "https://squad-webhook"

    @patch("squad.notifier.httpx.post")
    def test_no_call_when_no_webhook(self, mock_post, monkeypatch):
        monkeypatch.delenv("SQUAD_SLACK_WEBHOOK", raising=False)
        monkeypatch.delenv("FORGE_SLACK_WEBHOOK", raising=False)
        notify_plans_ready("sess-1", "My project", 2)
        mock_post.assert_not_called()

    @patch("squad.notifier.httpx.post")
    def test_logs_warning_when_no_webhook(self, mock_post, monkeypatch, caplog):
        monkeypatch.delenv("SQUAD_SLACK_WEBHOOK", raising=False)
        monkeypatch.delenv("FORGE_SLACK_WEBHOOK", raising=False)
        with caplog.at_level(logging.WARNING, logger="squad.notifier"):
            notify_plans_ready("sess-1", "My project", 2)
        assert any("webhook" in r.message.lower() for r in caplog.records)


# ── resilience ─────────────────────────────────────────────────────────────────


class TestResilience:
    @patch("squad.notifier.httpx.post")
    def test_does_not_raise_on_http_error(self, mock_post, monkeypatch):
        monkeypatch.setenv("SQUAD_SLACK_WEBHOOK", "https://hooks.slack.com/x")
        mock_post.return_value = _mock_response(500)
        notify_plans_ready("sess-1", "proj", 1)  # must not raise

    @patch("squad.notifier.httpx.post")
    def test_does_not_raise_on_network_error(self, mock_post, monkeypatch):
        monkeypatch.setenv("SQUAD_SLACK_WEBHOOK", "https://hooks.slack.com/x")
        mock_post.side_effect = Exception("connection refused")
        notify_plans_ready("sess-1", "proj", 1)  # must not raise

    @patch("squad.notifier.httpx.post")
    def test_logs_warning_on_http_error(self, mock_post, monkeypatch, caplog):
        monkeypatch.setenv("SQUAD_SLACK_WEBHOOK", "https://hooks.slack.com/x")
        mock_post.side_effect = Exception("timeout")
        with caplog.at_level(logging.WARNING, logger="squad.notifier"):
            notify_plans_ready("sess-1", "proj", 1)
        assert any("Failed" in r.message for r in caplog.records)


# ── payload structure ──────────────────────────────────────────────────────────


class TestPayloadStructure:
    def _capture_payload(self, fn, monkeypatch, *args):
        monkeypatch.setenv("SQUAD_SLACK_WEBHOOK", "https://hooks.slack.com/x")
        with patch("squad.notifier.httpx.post") as mock_post:
            mock_post.return_value = _mock_response()
            fn(*args)
            return mock_post.call_args[1]["json"]

    def test_questions_pending_payload(self, monkeypatch):
        payload = self._capture_payload(
            notify_questions_pending, monkeypatch, "sess-1", "CRM project", 3
        )
        assert payload["session_id"] == "sess-1"
        assert payload["title"] == "CRM project"
        assert "timestamp" in payload
        assert "3" in payload["text"]

    def test_plans_ready_payload(self, monkeypatch):
        payload = self._capture_payload(
            notify_plans_ready, monkeypatch, "sess-2", "Auth project", 2
        )
        assert payload["session_id"] == "sess-2"
        assert payload["title"] == "Auth project"
        assert "timestamp" in payload
        assert "2" in payload["text"]

    def test_agent_error_payload(self, monkeypatch):
        payload = self._capture_payload(
            notify_agent_error, monkeypatch, "sess-3", "Search project", "ux", "timeout error"
        )
        assert payload["session_id"] == "sess-3"
        assert payload["title"] == "Search project"
        assert "timestamp" in payload
        assert "ux" in payload["text"]
        assert "timeout error" in payload["text"]

    def test_agent_error_truncates_long_error(self, monkeypatch):
        long_error = "x" * 500
        monkeypatch.setenv("SQUAD_SLACK_WEBHOOK", "https://hooks.slack.com/x")
        with patch("squad.notifier.httpx.post") as mock_post:
            mock_post.return_value = _mock_response()
            notify_agent_error("sess-1", "proj", "pm", long_error)
            payload = mock_post.call_args[1]["json"]
        assert len(payload["text"]) < 600  # truncated in the text portion
