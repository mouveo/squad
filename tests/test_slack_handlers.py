"""Tests for squad.slack_handlers — /squad new command dispatch."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from squad.db import ensure_schema, list_active_sessions
from squad.slack_handlers import handle_squad_command


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "squad.db"
    ensure_schema(path)
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "target-project"
    p.mkdir()
    return p


@pytest.fixture
def config(project: Path) -> dict:
    return {
        "slack": {
            "allowed_user_ids": ["U123"],
            "channels": {
                "C999": {"project_path": str(project)},
            },
        }
    }


class _InlineExecutor:
    """Minimal executor that runs submitted callables synchronously in tests."""

    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return None


@pytest.fixture
def executor() -> _InlineExecutor:
    return _InlineExecutor()


@pytest.fixture
def client() -> MagicMock:
    m = MagicMock()
    m.chat_postMessage.return_value = {"ts": "1700000000.000100"}
    return m


def _command(text: str, channel_id: str = "C999", user_id: str = "U123") -> dict:
    return {"text": text, "channel_id": channel_id, "user_id": user_id}


# ── /squad new — happy path ────────────────────────────────────────────────────


class TestSquadNew:
    def test_creates_session_and_posts_root_message(
        self, db_path, config, executor, client, project
    ):
        respond = MagicMock()
        handle_squad_command(
            command=_command("new Improve the CRM"),
            respond=respond,
            client=client,
            db_path=db_path,
            executor=executor,
            config=config,
        )
        sessions = list_active_sessions(db_path=db_path)
        assert len(sessions) == 1
        session = sessions[0]
        assert session.slack_channel == "C999"
        assert session.slack_user_id == "U123"
        # Thread ts was captured from the chat_postMessage response
        assert session.slack_thread_ts == "1700000000.000100"
        client.chat_postMessage.assert_called_once()
        assert len(executor.submitted) == 1
        respond.assert_called_once()
        assert "créée" in respond.call_args.args[0] or "cree" in respond.call_args.args[0].lower()

    def test_empty_idea_returns_error(self, db_path, config, executor, client):
        respond = MagicMock()
        handle_squad_command(
            command=_command("new   "),
            respond=respond,
            client=client,
            db_path=db_path,
            executor=executor,
            config=config,
        )
        assert list_active_sessions(db_path=db_path) == []
        respond.assert_called_once()
        assert "vide" in respond.call_args.args[0].lower()
        client.chat_postMessage.assert_not_called()

    def test_unmapped_channel_returns_error(self, db_path, config, executor, client):
        respond = MagicMock()
        handle_squad_command(
            command=_command("new Build CRM", channel_id="CUNKNOWN"),
            respond=respond,
            client=client,
            db_path=db_path,
            executor=executor,
            config=config,
        )
        assert list_active_sessions(db_path=db_path) == []
        respond.assert_called_once()
        assert "mappé" in respond.call_args.args[0]
        client.chat_postMessage.assert_not_called()
        assert executor.submitted == []

    def test_forbidden_user_returns_error(self, db_path, config, executor, client):
        respond = MagicMock()
        handle_squad_command(
            command=_command("new Build CRM", user_id="UFORBIDDEN"),
            respond=respond,
            client=client,
            db_path=db_path,
            executor=executor,
            config=config,
        )
        assert list_active_sessions(db_path=db_path) == []
        respond.assert_called_once()
        assert "autorisé" in respond.call_args.args[0]
        assert executor.submitted == []

    def test_usage_hint_when_no_subcommand(self, db_path, config, executor, client):
        respond = MagicMock()
        handle_squad_command(
            command=_command(""),
            respond=respond,
            client=client,
            db_path=db_path,
            executor=executor,
            config=config,
        )
        respond.assert_called_once()
        assert "Usage" in respond.call_args.args[0]
        assert list_active_sessions(db_path=db_path) == []

    def test_unknown_subcommand_returns_hint(self, db_path, config, executor, client):
        respond = MagicMock()
        handle_squad_command(
            command=_command("bogus foo"),
            respond=respond,
            client=client,
            db_path=db_path,
            executor=executor,
            config=config,
        )
        respond.assert_called_once()
        assert "inconnue" in respond.call_args.args[0]

    def test_pipeline_dispatched_on_executor(self, db_path, config, executor, client):
        respond = MagicMock()
        handle_squad_command(
            command=_command("new Some idea"),
            respond=respond,
            client=client,
            db_path=db_path,
            executor=executor,
            config=config,
        )
        assert len(executor.submitted) == 1
        fn, args, _ = executor.submitted[0]
        session_id = list_active_sessions(db_path=db_path)[0].id
        assert args[0] == session_id
