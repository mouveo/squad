"""Tests for squad.slack_service — channel resolution, allowlist, session creation."""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from squad.db import ensure_schema, get_session, list_active_sessions
from squad.models import (
    EVENT_FAILED,
    EVENT_INTERVIEWING,
    EVENT_REVIEW,
    EVENT_WORKING,
    PipelineEvent,
    Session,
)
from squad.slack_service import (
    SlackResolutionError,
    assert_user_allowed,
    create_session_from_slack,
    format_pipeline_event,
    format_root_message,
    post_pipeline_event,
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


# ── Pipeline events (LOT 2) ────────────────────────────────────────────────────


def _event(type_: str, **overrides) -> PipelineEvent:
    base = dict(
        type=type_,
        session_id="sess-1",
        timestamp_utc=datetime(2026, 4, 18, 10, 25, 13),
        elapsed_seconds=125.0,
    )
    base.update(overrides)
    return PipelineEvent(**base)


def test_post_pipeline_event(tmp_path):
    """Covers working, interviewing, review and failed rendering + posting."""
    session = Session(
        id="sess-1",
        title="Test",
        project_path="/tmp/proj",
        workspace_path=str(tmp_path / "ws"),
        idea="x",
        slack_channel="C999",
        slack_thread_ts="1700000000.000100",
    )
    client = MagicMock()

    events = [
        _event(EVENT_WORKING, phase="cadrage"),
        _event(EVENT_INTERVIEWING, phase="cadrage", pending_questions=3),
        _event(EVENT_REVIEW, plans_count=2, elapsed_seconds=3605),
        _event(EVENT_FAILED, failure_reason="pm exploded"),
    ]
    for evt in events:
        post_pipeline_event(evt, session, client)

    assert client.chat_postMessage.call_count == 4
    kwargs = [c.kwargs for c in client.chat_postMessage.call_args_list]
    for k in kwargs:
        assert k["channel"] == "C999"
        assert k["thread_ts"] == "1700000000.000100"

    working_text = kwargs[0]["text"]
    assert "Cadrage" in working_text or "cadrage" in working_text
    assert "2026-04-18" in working_text

    interviewing_text = kwargs[1]["text"]
    assert "3 question" in interviewing_text

    review_text = kwargs[2]["text"]
    assert "2 plan" in review_text
    assert "1h" in review_text  # 3605s → 1h ...

    failed_text = kwargs[3]["text"]
    assert "pm exploded" in failed_text


def test_post_pipeline_event_noop_without_thread(tmp_path):
    """Sessions not created from Slack must not attempt any post."""
    session = Session(
        id="sess-1",
        title="Test",
        project_path="/tmp/proj",
        workspace_path=str(tmp_path / "ws"),
        idea="x",
    )
    client = MagicMock()
    post_pipeline_event(_event(EVENT_WORKING, phase="cadrage"), session, client)
    client.chat_postMessage.assert_not_called()


def test_post_pipeline_event_swallows_slack_errors(tmp_path):
    """A failing Slack client must not propagate out of the observer."""
    session = Session(
        id="sess-1",
        title="Test",
        project_path="/tmp/proj",
        workspace_path=str(tmp_path / "ws"),
        idea="x",
        slack_channel="C999",
        slack_thread_ts="1700000000.000100",
    )
    client = MagicMock()
    client.chat_postMessage.side_effect = RuntimeError("slack down")
    # Should NOT raise
    post_pipeline_event(_event(EVENT_WORKING, phase="cadrage"), session, client)


class TestQuestionBlocks:
    def test_build_question_blocks_includes_button_when_pending(self):
        from squad.models import Question
        from squad.slack_service import build_question_blocks

        q = Question(
            id="q-abc",
            session_id="s1",
            agent="pm",
            phase="cadrage",
            question="Quel segment ?",
        )
        blocks = build_question_blocks(q)
        # Button present with the question id as value
        actions = [b for b in blocks if b["type"] == "actions"]
        assert actions
        button = actions[0]["elements"][0]
        assert button["action_id"] == "squad_question_answer"
        assert button["value"] == "q-abc"

    def test_answered_blocks_omit_button(self):
        from squad.models import Question
        from squad.slack_service import build_question_blocks

        q = Question(
            id="q-abc",
            session_id="s1",
            agent="pm",
            phase="cadrage",
            question="Quel segment ?",
            answer="SMBs",
        )
        blocks = build_question_blocks(q, answered=True)
        assert not any(b["type"] == "actions" for b in blocks)

    def test_build_modal_embeds_question_id(self):
        from squad.models import Question
        from squad.slack_service import build_question_modal

        q = Question(
            id="q-xyz",
            session_id="s1",
            agent="pm",
            phase="cadrage",
            question="Quel segment ?",
        )
        view = build_question_modal(q)
        assert view["callback_id"] == "squad_question_submit"
        assert view["private_metadata"] == "q-xyz"

    def test_extract_modal_answer(self):
        from squad.slack_service import (
            QUESTION_MODAL_INPUT_ACTION_ID,
            QUESTION_MODAL_INPUT_BLOCK_ID,
            extract_modal_answer,
        )

        view = {
            "private_metadata": "q-xyz",
            "state": {
                "values": {
                    QUESTION_MODAL_INPUT_BLOCK_ID: {
                        QUESTION_MODAL_INPUT_ACTION_ID: {"value": "  SMBs  "}
                    }
                }
            },
        }
        qid, answer = extract_modal_answer(view)
        assert qid == "q-xyz"
        assert answer == "SMBs"


class TestFormatPipelineEvent:
    def test_working_includes_phase_and_timestamp(self):
        txt = format_pipeline_event(_event(EVENT_WORKING, phase="cadrage"))
        assert "cadrage" in txt
        assert "UTC" in txt
        assert "écoulé" in txt

    def test_interviewing_includes_pending_count(self):
        txt = format_pipeline_event(_event(EVENT_INTERVIEWING, pending_questions=5))
        assert "5 question" in txt

    def test_review_includes_plans_and_total_duration(self):
        txt = format_pipeline_event(_event(EVENT_REVIEW, plans_count=3))
        assert "3 plan" in txt
        assert "durée totale" in txt

    def test_failed_includes_reason(self):
        txt = format_pipeline_event(_event(EVENT_FAILED, failure_reason="boom"))
        assert "boom" in txt

    def test_failed_without_reason_uses_placeholder(self):
        txt = format_pipeline_event(_event(EVENT_FAILED))
        assert "inconnue" in txt
