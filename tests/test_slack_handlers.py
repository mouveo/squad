"""Tests for squad.slack_handlers — /squad new command dispatch."""

from pathlib import Path
from unittest.mock import MagicMock, patch

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


# ── file_shared handler (LOT 3) ───────────────────────────────────────────────


from pathlib import Path as _Path  # noqa: E402

from squad.db import update_session_slack_thread  # noqa: E402
from squad.slack_handlers import handle_file_shared  # noqa: E402


def _slack_session(db_path, config, executor, client):
    """Create a Squad session via /squad new and capture the resulting session row."""
    respond = MagicMock()
    handle_squad_command(
        command=_command("new Build CRM"),
        respond=respond,
        client=client,
        db_path=db_path,
        executor=executor,
        config=config,
    )
    return list_active_sessions(db_path=db_path)[0]


def _file_info(file_id, *, name, size, channel, thread_ts, mime="text/markdown"):
    return {
        "ok": True,
        "file": {
            "id": file_id,
            "name": name,
            "size": size,
            "mimetype": mime,
            "url_private_download": "https://files.slack.com/x",
            "shares": {
                "public": {channel: [{"ts": thread_ts, "thread_ts": thread_ts}]},
            },
        },
    }


class TestFileShared:
    def _config_with_token(self, base_config):
        cfg = dict(base_config)
        cfg["slack"] = {**base_config["slack"], "bot_token": "xoxb-test"}
        return cfg

    def test_attaches_file_to_session(self, db_path, config, executor, client, tmp_path):
        cfg = self._config_with_token(config)
        session = _slack_session(db_path, cfg, executor, client)
        # Simulate Slack thread ts being known on the session
        update_session_slack_thread(session.id, "1700000000.000100", db_path=db_path)

        client.files_info.return_value = _file_info(
            "F123", name="brief.md", size=12, channel="C999", thread_ts="1700000000.000100"
        )
        with patch(
            "squad.slack_handlers.download_file", return_value=b"# brief body"
        ) as m_download:
            handle_file_shared(
                event={"file_id": "F123"},
                client=client,
                db_path=db_path,
                config=cfg,
            )

        m_download.assert_called_once()
        attachments = _Path(session.workspace_path) / "attachments"
        assert (attachments / "brief.md").read_bytes() == b"# brief body"
        # Confirmation posted in the thread
        post_calls = [
            c for c in client.chat_postMessage.call_args_list
            if c.kwargs.get("thread_ts") == "1700000000.000100"
        ]
        assert any("attaché" in c.kwargs.get("text", "") for c in post_calls)

    def test_unrelated_thread_is_ignored(self, db_path, config, executor, client):
        cfg = self._config_with_token(config)
        session = _slack_session(db_path, cfg, executor, client)
        update_session_slack_thread(session.id, "1700000000.000100", db_path=db_path)

        client.files_info.return_value = _file_info(
            "F123", name="brief.md", size=12, channel="C999", thread_ts="9999.000000"
        )
        with patch("squad.slack_handlers.download_file") as m_download:
            handle_file_shared(
                event={"file_id": "F123"}, client=client, db_path=db_path, config=cfg
            )
        m_download.assert_not_called()

    def test_oversized_file_posts_error_no_storage(
        self, db_path, config, executor, client
    ):
        from squad.attachment_service import DEFAULT_MAX_FILE_BYTES

        cfg = self._config_with_token(config)
        session = _slack_session(db_path, cfg, executor, client)
        update_session_slack_thread(session.id, "1700000000.000100", db_path=db_path)

        client.files_info.return_value = _file_info(
            "F123",
            name="huge.md",
            size=DEFAULT_MAX_FILE_BYTES + 1,
            channel="C999",
            thread_ts="1700000000.000100",
        )
        with patch("squad.slack_handlers.download_file") as m_download:
            handle_file_shared(
                event={"file_id": "F123"}, client=client, db_path=db_path, config=cfg
            )
        m_download.assert_not_called()
        attachments = _Path(session.workspace_path) / "attachments"
        assert list(attachments.iterdir()) == []
        # Error posted in thread
        warning = [
            c for c in client.chat_postMessage.call_args_list
            if "rejetée" in c.kwargs.get("text", "")
        ]
        assert warning

    def test_disallowed_extension_posts_error_no_storage(
        self, db_path, config, executor, client
    ):
        cfg = self._config_with_token(config)
        session = _slack_session(db_path, cfg, executor, client)
        update_session_slack_thread(session.id, "1700000000.000100", db_path=db_path)

        client.files_info.return_value = _file_info(
            "F123",
            name="payload.exe",
            size=100,
            channel="C999",
            thread_ts="1700000000.000100",
        )
        with patch("squad.slack_handlers.download_file") as m_download:
            handle_file_shared(
                event={"file_id": "F123"}, client=client, db_path=db_path, config=cfg
            )
        m_download.assert_not_called()
        attachments = _Path(session.workspace_path) / "attachments"
        assert list(attachments.iterdir()) == []

    def test_no_thread_share_silently_ignored(self, db_path, config, executor, client):
        cfg = self._config_with_token(config)
        session = _slack_session(db_path, cfg, executor, client)
        update_session_slack_thread(session.id, "1700000000.000100", db_path=db_path)

        client.files_info.return_value = {
            "file": {"id": "F123", "name": "brief.md", "size": 10, "shares": {}}
        }
        with patch("squad.slack_handlers.download_file") as m_download:
            handle_file_shared(
                event={"file_id": "F123"}, client=client, db_path=db_path, config=cfg
            )
        m_download.assert_not_called()


# ── Question actions + modal (LOT 4) ──────────────────────────────────────────


from squad.db import (  # noqa: E402
    answer_question as _answer_question,
    create_question,
    get_question,
    list_pending_questions,
)
from squad.slack_handlers import (  # noqa: E402
    handle_question_action,
    handle_question_submission,
)
from squad.slack_service import (  # noqa: E402
    QUESTION_MODAL_INPUT_ACTION_ID,
    QUESTION_MODAL_INPUT_BLOCK_ID,
)


def _make_view(question_id: str, answer: str) -> dict:
    return {
        "private_metadata": question_id,
        "state": {
            "values": {
                QUESTION_MODAL_INPUT_BLOCK_ID: {
                    QUESTION_MODAL_INPUT_ACTION_ID: {"value": answer}
                }
            }
        },
    }


@pytest.fixture
def interviewing_session(db_path, config, executor, client):
    """Create a Squad session from Slack with a thread_ts and two pending questions."""
    cfg = dict(config)
    session = _slack_session(db_path, cfg, executor, client)
    update_session_slack_thread(session.id, "1700000000.000100", db_path=db_path)
    q1 = create_question(session.id, "pm", "cadrage", "Quel segment ?", db_path=db_path)
    q2 = create_question(session.id, "pm", "cadrage", "Quel prix ?", db_path=db_path)
    return session, [q1, q2]


class TestQuestionAction:
    def test_opens_modal_with_question_id(self, db_path, interviewing_session, client):
        _, questions = interviewing_session
        q1 = questions[0]
        body = {
            "trigger_id": "T123",
            "actions": [{"value": q1.id, "action_id": "squad_question_answer"}],
        }
        handle_question_action(body=body, client=client, db_path=db_path)
        client.views_open.assert_called_once()
        kwargs = client.views_open.call_args.kwargs
        assert kwargs["trigger_id"] == "T123"
        assert kwargs["view"]["private_metadata"] == q1.id

    def test_question_action_ignored_outside_session(self, db_path, client):
        # No session / no question matches this id
        body = {
            "trigger_id": "T999",
            "actions": [{"value": "nonexistent-id", "action_id": "squad_question_answer"}],
        }
        handle_question_action(body=body, client=client, db_path=db_path)
        client.views_open.assert_not_called()

    def test_ignored_on_already_answered_question(self, db_path, interviewing_session, client):
        _, questions = interviewing_session
        q1 = questions[0]
        _answer_question(q1.id, "an answer", db_path=db_path)
        body = {
            "trigger_id": "T123",
            "actions": [{"value": q1.id, "action_id": "squad_question_answer"}],
        }
        handle_question_action(body=body, client=client, db_path=db_path)
        client.views_open.assert_not_called()

    def test_missing_trigger_id_silently_ignored(self, db_path, interviewing_session, client):
        _, questions = interviewing_session
        body = {"actions": [{"value": questions[0].id}]}
        handle_question_action(body=body, client=client, db_path=db_path)
        client.views_open.assert_not_called()


class TestQuestionSubmission:
    def test_persists_answer_and_syncs_pending(
        self, db_path, interviewing_session, executor, client
    ):
        session, questions = interviewing_session
        q1 = questions[0]
        view = _make_view(q1.id, "SMBs")
        handle_question_submission(
            body={},
            view=view,
            client=client,
            db_path=db_path,
            executor=executor,
        )
        fetched = get_question(q1.id, db_path=db_path)
        assert fetched.answer == "SMBs"
        # pending.json synced
        pending_file = Path(session.workspace_path) / "questions" / "pending.json"
        assert pending_file.exists()

    def test_question_modal_submission_triggers_resume(
        self, db_path, interviewing_session, executor, client
    ):
        session, questions = interviewing_session
        q1, q2 = questions
        # Pre-answer the first question via DB, then submit the second (last) via Slack
        _answer_question(q1.id, "answer1", db_path=db_path)

        view = _make_view(q2.id, "answer2")
        handle_question_submission(
            body={},
            view=view,
            client=client,
            db_path=db_path,
            executor=executor,
        )
        # All questions answered → resume scheduled exactly once
        assert list_pending_questions(session.id, db_path=db_path) == []
        resumes = [s for s in executor.submitted if "_resume_pipeline_bg" in s[0].__name__]
        assert len(resumes) == 1
        assert resumes[0][1][0] == session.id

    def test_not_last_question_does_not_resume(
        self, db_path, interviewing_session, executor, client
    ):
        _, questions = interviewing_session
        q1 = questions[0]
        view = _make_view(q1.id, "answer1")
        handle_question_submission(
            body={},
            view=view,
            client=client,
            db_path=db_path,
            executor=executor,
        )
        resumes = [s for s in executor.submitted if "_resume_pipeline_bg" in s[0].__name__]
        assert resumes == []

    def test_double_submit_last_wins_no_double_resume(
        self, db_path, interviewing_session, executor, client
    ):
        session, questions = interviewing_session
        q1, q2 = questions
        _answer_question(q1.id, "answer1", db_path=db_path)

        # Submit twice for q2 — second answer should win, still exactly one resume
        for answer_text in ("first", "second-and-winning"):
            handle_question_submission(
                body={},
                view=_make_view(q2.id, answer_text),
                client=client,
                db_path=db_path,
                executor=executor,
            )

        fetched = get_question(q2.id, db_path=db_path)
        assert fetched.answer == "second-and-winning"
        resumes = [s for s in executor.submitted if "_resume_pipeline_bg" in s[0].__name__]
        # Two submissions, two schedules is acceptable? The spec says
        # "pas de reprise doublonnée" — we want at most one. Our code
        # schedules each time the last remaining question gets answered;
        # after the first submit no pending remain, so the second submit
        # also sees zero pending → another schedule. Expected real behaviour:
        # the pipeline itself handles idempotence. But the plan is strict:
        # verify only one was scheduled.
        # Accept: the second submission updates the answer but ideally
        # doesn't double-resume. We enforce that here.
        assert len(resumes) <= 1

    def test_unknown_question_silently_ignored(self, db_path, executor, client):
        view = _make_view("ghost", "x")
        handle_question_submission(
            body={},
            view=view,
            client=client,
            db_path=db_path,
            executor=executor,
        )
        assert executor.submitted == []

    def test_empty_answer_ignored(self, db_path, interviewing_session, executor, client):
        _, questions = interviewing_session
        view = _make_view(questions[0].id, "   ")
        handle_question_submission(
            body={},
            view=view,
            client=client,
            db_path=db_path,
            executor=executor,
        )
        fetched = get_question(questions[0].id, db_path=db_path)
        assert fetched.answer is None


# ── Review approve/reject (LOT 5) ─────────────────────────────────────────────


from squad.constants import STATUS_FAILED, STATUS_QUEUED, STATUS_REVIEW  # noqa: E402
from squad.db import (  # noqa: E402
    create_plan,
    get_plan,
    update_plan_slack_message_ts,
    update_session_status,
)
from squad.forge_bridge import (  # noqa: E402
    ForgeUnavailable,
    SubmitOutcome,
)
from squad.slack_handlers import (  # noqa: E402
    _approve_bg,
    handle_review_approve,
    handle_review_reject_action,
    handle_review_reject_submission,
)
from squad.slack_service import (  # noqa: E402
    REVIEW_REJECT_INPUT_ACTION_ID,
    REVIEW_REJECT_INPUT_BLOCK_ID,
)


@pytest.fixture
def review_session(db_path, config, executor, client):
    """Create a Squad session in review with one plan posted in Slack."""
    session = _slack_session(db_path, config, executor, client)
    update_session_slack_thread(session.id, "1700000000.000100", db_path=db_path)
    update_session_status(session.id, STATUS_REVIEW, db_path=db_path)
    plan = create_plan(
        session.id,
        "Plan 1",
        "/tmp/plan-1.md",
        "## LOT 1 — t\n**Files**: `a.py`\n",
        db_path=db_path,
    )
    # Simulate that a review message was already posted for this plan
    update_plan_slack_message_ts(plan.id, "1700000000.000200", db_path=db_path)
    return session, plan


def _review_body(session_id: str, plan_id: str) -> dict:
    return {
        "trigger_id": "T123",
        "actions": [{"value": f"{session_id}:{plan_id}"}],
    }


class TestReviewApproveAction:
    def test_approve_action_submits_to_forge(
        self, db_path, review_session, executor, client
    ):
        session, plan = review_session
        with patch(
            "squad.slack_handlers.approve_and_submit",
            return_value=SubmitOutcome(plans_sent=1, queue_started=True),
        ) as m_submit:
            # handle_review_approve schedules _approve_bg on the executor,
            # which in our inline executor stores the call; we invoke it
            # manually below to exercise the full path.
            before = list(executor.submitted)
            handle_review_approve(
                body=_review_body(session.id, plan.id),
                client=client,
                db_path=db_path,
                executor=executor,
            )
            new_submits = [s for s in executor.submitted if s not in before]
            approve_submits = [
                s for s in new_submits if "_approve_bg" in s[0].__name__
            ]
            assert len(approve_submits) == 1
            fn, args, _ = approve_submits[0]
            fn(*args)
        m_submit.assert_called_once_with(session.id, db_path=db_path)
        # Updates posted in Slack
        update_calls = [
            c for c in client.chat_update.call_args_list
            if c.kwargs.get("ts") == "1700000000.000200"
        ]
        assert update_calls
        post_calls = [
            c for c in client.chat_postMessage.call_args_list
            if "Approuvé" in c.kwargs.get("text", "")
        ]
        assert post_calls

    def test_approve_action_idempotent(self, db_path, review_session, executor, client):
        session, plan = review_session
        # First click flips session to queued via approve_and_submit
        update_session_status(session.id, STATUS_QUEUED, db_path=db_path)

        before = list(executor.submitted)
        with patch("squad.slack_handlers.approve_and_submit") as m_submit:
            handle_review_approve(
                body=_review_body(session.id, plan.id),
                client=client,
                db_path=db_path,
                executor=executor,
            )
        # Non-review status → no _approve_bg scheduled, no Forge call
        new_submits = [s for s in executor.submitted if s not in before]
        assert [s for s in new_submits if "_approve_bg" in s[0].__name__] == []
        m_submit.assert_not_called()

    def test_approve_forge_unavailable_falls_back(
        self, db_path, review_session, executor, client
    ):
        session, plan = review_session
        with patch(
            "squad.slack_handlers.approve_and_submit",
            side_effect=ForgeUnavailable("down"),
        ):
            _approve_bg(session.id, plan.id, db_path, client)
        # Fallback message posted in thread
        post_calls = [
            c for c in client.chat_postMessage.call_args_list
            if "Forge indisponible" in c.kwargs.get("text", "")
        ]
        assert post_calls

    def test_approve_unknown_session_silently_ignored(
        self, db_path, executor, client
    ):
        before = list(executor.submitted)
        with patch("squad.slack_handlers.approve_and_submit") as m_submit:
            handle_review_approve(
                body=_review_body("ghost", "ghost"),
                client=client,
                db_path=db_path,
                executor=executor,
            )
        m_submit.assert_not_called()
        assert executor.submitted == before


class TestReviewRejectAction:
    def test_reject_action_opens_modal(self, db_path, review_session, client):
        session, plan = review_session
        handle_review_reject_action(
            body=_review_body(session.id, plan.id),
            client=client,
            db_path=db_path,
        )
        client.views_open.assert_called_once()
        view = client.views_open.call_args.kwargs["view"]
        assert view["private_metadata"] == f"{session.id}:{plan.id}"

    def test_reject_ignored_on_non_review_session(
        self, db_path, review_session, client
    ):
        session, plan = review_session
        update_session_status(session.id, STATUS_QUEUED, db_path=db_path)
        handle_review_reject_action(
            body=_review_body(session.id, plan.id),
            client=client,
            db_path=db_path,
        )
        client.views_open.assert_not_called()

    def test_reject_submission_marks_session_failed(
        self, db_path, review_session, client
    ):
        session, plan = review_session
        view = {
            "private_metadata": f"{session.id}:{plan.id}",
            "state": {
                "values": {
                    REVIEW_REJECT_INPUT_BLOCK_ID: {
                        REVIEW_REJECT_INPUT_ACTION_ID: {
                            "value": "pas assez de détail"
                        }
                    }
                }
            },
        }
        handle_review_reject_submission(
            body={}, view=view, client=client, db_path=db_path
        )
        from squad.db import get_session as _get

        refreshed = _get(session.id, db_path=db_path)
        assert refreshed.status == STATUS_FAILED
        assert refreshed.failure_reason == "pas assez de détail"
        # Review card updated
        client.chat_update.assert_called_once()

    def test_reject_submission_idempotent(self, db_path, review_session, client):
        session, plan = review_session
        # Pre-flip to queued — should short-circuit
        update_session_status(session.id, STATUS_QUEUED, db_path=db_path)

        view = {
            "private_metadata": f"{session.id}:{plan.id}",
            "state": {
                "values": {
                    REVIEW_REJECT_INPUT_BLOCK_ID: {
                        REVIEW_REJECT_INPUT_ACTION_ID: {"value": "too late"}
                    }
                }
            },
        }
        handle_review_reject_submission(
            body={}, view=view, client=client, db_path=db_path
        )
        from squad.db import get_session as _get

        # Session status untouched, failure_reason not overwritten
        refreshed = _get(session.id, db_path=db_path)
        assert refreshed.status == STATUS_QUEUED
        assert refreshed.failure_reason is None
