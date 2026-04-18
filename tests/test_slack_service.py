"""Tests for squad.slack_service — channel resolution, allowlist, session creation."""

from pathlib import Path

import pytest

from squad.db import ensure_schema, get_session, list_active_sessions
from squad.slack_service import (
    SlackResolutionError,
    assert_user_allowed,
    create_session_from_slack,
    format_root_message,
    record_thread_ts,
    resolve_project_path,
)


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


# ── resolve_project_path ───────────────────────────────────────────────────────


class TestResolveProjectPath:
    def test_returns_configured_path(self, config, project):
        assert Path(resolve_project_path("C999", config)) == project.resolve()

    def test_unmapped_channel_raises(self, config):
        with pytest.raises(SlackResolutionError, match="n'est mappé"):
            resolve_project_path("CUNKNOWN", config)

    def test_missing_project_path_raises(self, project):
        config = {"slack": {"channels": {"C999": {}}}}
        with pytest.raises(SlackResolutionError, match="project_path"):
            resolve_project_path("C999", config)

    def test_nonexistent_directory_raises(self, tmp_path):
        config = {"slack": {"channels": {"C1": {"project_path": str(tmp_path / "ghost")}}}}
        with pytest.raises(SlackResolutionError, match="n'existe pas"):
            resolve_project_path("C1", config)

    def test_empty_config_raises(self):
        with pytest.raises(SlackResolutionError):
            resolve_project_path("C999", {})


# ── assert_user_allowed ────────────────────────────────────────────────────────


class TestAssertUserAllowed:
    def test_allowed_user_passes(self, config):
        assert_user_allowed("U123", config)  # no exception

    def test_forbidden_user_raises(self, config):
        with pytest.raises(SlackResolutionError, match="n'est pas autorisé"):
            assert_user_allowed("UOTHER", config)

    def test_empty_allowlist_disables_check(self):
        assert_user_allowed("anyone", {"slack": {"allowed_user_ids": []}})
        assert_user_allowed("anyone", {})


# ── create_session_from_slack ──────────────────────────────────────────────────


class TestCreateSessionFromSlack:
    def test_create_session_from_slack(self, db_path, config, project):
        session = create_session_from_slack(
            idea="Improve the CRM",
            channel_id="C999",
            user_id="U123",
            db_path=db_path,
            config=config,
        )
        assert session.title == "Improve the CRM"
        assert session.slack_channel == "C999"
        assert session.slack_user_id == "U123"
        assert session.slack_thread_ts is None
        assert Path(session.project_path) == project.resolve()
        assert (Path(session.workspace_path) / "idea.md").exists()
        assert (Path(session.workspace_path) / "context.md").exists()

    def test_persisted_in_db(self, db_path, config):
        session = create_session_from_slack(
            idea="idea",
            channel_id="C999",
            user_id="U123",
            db_path=db_path,
            config=config,
        )
        fetched = get_session(session.id, db_path=db_path)
        assert fetched is not None
        assert fetched.slack_channel == "C999"
        assert fetched.slack_user_id == "U123"

    def test_empty_idea_raises(self, db_path, config):
        with pytest.raises(SlackResolutionError, match="Idée vide"):
            create_session_from_slack(
                idea="   ",
                channel_id="C999",
                user_id="U123",
                db_path=db_path,
                config=config,
            )

    def test_forbidden_user_raises_no_session(self, db_path, config):
        with pytest.raises(SlackResolutionError):
            create_session_from_slack(
                idea="idea",
                channel_id="C999",
                user_id="UFORBIDDEN",
                db_path=db_path,
                config=config,
            )
        assert list_active_sessions(db_path=db_path) == []

    def test_unmapped_channel_raises_no_session(self, db_path, config):
        with pytest.raises(SlackResolutionError):
            create_session_from_slack(
                idea="idea",
                channel_id="CUNKNOWN",
                user_id="U123",
                db_path=db_path,
                config=config,
            )
        assert list_active_sessions(db_path=db_path) == []


# ── record_thread_ts ───────────────────────────────────────────────────────────


class TestRecordThreadTs:
    def test_persists_thread_ts(self, db_path, config):
        session = create_session_from_slack(
            idea="idea",
            channel_id="C999",
            user_id="U123",
            db_path=db_path,
            config=config,
        )
        record_thread_ts(session.id, "1700000000.000100", db_path=db_path)
        fetched = get_session(session.id, db_path=db_path)
        assert fetched.slack_thread_ts == "1700000000.000100"


# ── format_root_message ────────────────────────────────────────────────────────


class TestFormatRootMessage:
    def test_includes_short_id_and_title(self, db_path, config):
        session = create_session_from_slack(
            idea="Improve CRM",
            channel_id="C999",
            user_id="U123",
            db_path=db_path,
            config=config,
        )
        msg = format_root_message(session)
        assert session.id[:8] in msg
        assert "Improve CRM" in msg
