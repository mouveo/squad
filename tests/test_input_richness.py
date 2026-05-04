"""Tests for squad/input_richness.py — sparse vs rich classification."""

from __future__ import annotations

from pathlib import Path

import pytest

from squad.db import (
    create_session,
    ensure_schema,
)
from squad.input_richness import (
    CLAUDE_MD_RICH_CHARS,
    IDEA_LONG_CHARS,
    IDEA_VERY_LONG_CHARS,
    TEXT_ATTACHMENT_RICH_CHARS,
    score_input_richness,
)

# ── fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / ".squad" / "squad.db"
    ensure_schema(path)
    return path


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    project = tmp_path / "target-project"
    project.mkdir()
    return project


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


def _make_session(
    db_path: Path,
    project_dir: Path,
    workspace: Path,
    *,
    idea: str,
):
    return create_session(
        title="t",
        project_path=str(project_dir),
        workspace_path=str(workspace),
        idea=idea,
        db_path=db_path,
    )


def _write_attachment(workspace: Path, name: str, content: str) -> None:
    """Write a file directly under {workspace}/attachments/{name}."""
    attachments = workspace / "attachments"
    attachments.mkdir(parents=True, exist_ok=True)
    (attachments / name).write_text(content, encoding="utf-8")


def _write_claude_md(project_dir: Path, content: str) -> None:
    (project_dir / "CLAUDE.md").write_text(content, encoding="utf-8")


# ── sparse cases ───────────────────────────────────────────────────────────────


class TestSparse:
    def test_short_idea_no_attachment_no_claude_md(
        self, db_path, project_dir, workspace
    ):
        s = _make_session(db_path, project_dir, workspace, idea="Build a thing")
        assert score_input_richness(s.id, db_path=db_path) == "sparse"

    def test_long_idea_alone_is_sparse(self, db_path, project_dir, workspace):
        """Idea > 500 chars but no other signal → only 1 point → sparse."""
        idea = "x" * (IDEA_VERY_LONG_CHARS + 100)
        s = _make_session(db_path, project_dir, workspace, idea=idea)
        # Only the idea-long signal fires (1 point), threshold needs 2.
        assert score_input_richness(s.id, db_path=db_path) == "sparse"

    def test_idea_300_to_500_plus_claude_md_no_long_signal_is_sparse(
        self, db_path, project_dir, workspace
    ):
        """Score 2 but no long-form signal → falls back to sparse."""
        idea = "x" * (IDEA_LONG_CHARS + 50)  # 350 chars: 1 point, not "very long"
        _write_claude_md(project_dir, "y" * (CLAUDE_MD_RICH_CHARS + 100))
        s = _make_session(db_path, project_dir, workspace, idea=idea)
        # score=2 but no long-form signal (idea is not > 500, no big attachment)
        assert score_input_richness(s.id, db_path=db_path) == "sparse"

    def test_short_attachment_does_not_trigger_rich(
        self, db_path, project_dir, workspace
    ):
        """Attachment under threshold gives 0 point."""
        _write_attachment(
            workspace, "brief.md", "x" * (TEXT_ATTACHMENT_RICH_CHARS - 100)
        )
        s = _make_session(db_path, project_dir, workspace, idea="short")
        assert score_input_richness(s.id, db_path=db_path) == "sparse"


# ── rich cases ─────────────────────────────────────────────────────────────────


class TestRich:
    def test_short_idea_plus_long_text_attachment(
        self, db_path, project_dir, workspace
    ):
        """Attachment alone gives 2 points + long-form signal → rich."""
        _write_attachment(
            workspace, "deepsearch.md", "x" * (TEXT_ATTACHMENT_RICH_CHARS + 5000)
        )
        s = _make_session(
            db_path, project_dir, workspace, idea="onboarding for SaaS"
        )
        assert score_input_richness(s.id, db_path=db_path) == "rich"

    def test_idea_500_plus_plus_rich_claude_md(self, db_path, project_dir, workspace):
        """Idea > 500 + CLAUDE.md > 1000 → 2 points + long idea → rich."""
        idea = "x" * (IDEA_VERY_LONG_CHARS + 100)
        _write_claude_md(project_dir, "y" * (CLAUDE_MD_RICH_CHARS + 200))
        s = _make_session(db_path, project_dir, workspace, idea=idea)
        assert score_input_richness(s.id, db_path=db_path) == "rich"

    def test_idea_around_200_plus_long_attachment(
        self, db_path, project_dir, workspace
    ):
        """Idea ~200 chars (no point) + 2-point attachment → rich."""
        _write_attachment(
            workspace, "brief.txt", "x" * (TEXT_ATTACHMENT_RICH_CHARS + 1000)
        )
        s = _make_session(
            db_path, project_dir, workspace, idea="x" * 200
        )
        assert score_input_richness(s.id, db_path=db_path) == "rich"

    def test_csv_attachment_counts_as_inline_text(
        self, db_path, project_dir, workspace
    ):
        _write_attachment(
            workspace, "users.csv", "x" * (TEXT_ATTACHMENT_RICH_CHARS + 500)
        )
        s = _make_session(db_path, project_dir, workspace, idea="seg")
        assert score_input_richness(s.id, db_path=db_path) == "rich"


# ── "added after session creation" case ───────────────────────────────────────


class TestRecomputedAtCallTime:
    def test_attachment_added_after_session_creation_is_picked_up(
        self, db_path, project_dir, workspace
    ):
        """An attachment uploaded post-creation must change the verdict."""
        s = _make_session(db_path, project_dir, workspace, idea="idea")
        # Initially: short idea, no attachment, no CLAUDE.md → sparse.
        assert score_input_richness(s.id, db_path=db_path) == "sparse"

        # The Slack handler later drops a deep-search file in the workspace.
        _write_attachment(
            workspace,
            "deepsearch.md",
            "x" * (TEXT_ATTACHMENT_RICH_CHARS + 7000),
        )

        # A subsequent score call picks the file up without any explicit
        # cache invalidation.
        assert score_input_richness(s.id, db_path=db_path) == "rich"


# ── binary / non-text resilience ──────────────────────────────────────────────


class TestBinaryAttachmentsIgnored:
    def test_pdf_extension_is_not_counted(self, db_path, project_dir, workspace):
        """No PDF parsing — a .pdf attachment must not contribute to the score."""
        _write_attachment(
            workspace, "report.pdf", "x" * (TEXT_ATTACHMENT_RICH_CHARS + 5000)
        )
        s = _make_session(db_path, project_dir, workspace, idea="short idea")
        # Without an inline-text attachment we fall back to sparse even
        # though a fat .pdf sits in the workspace.
        assert score_input_richness(s.id, db_path=db_path) == "sparse"

    def test_undecodable_text_attachment_does_not_crash(
        self, db_path, project_dir, workspace
    ):
        """Bytes that can't be decoded as UTF-8 are silently skipped."""
        attachments = workspace / "attachments"
        attachments.mkdir(parents=True, exist_ok=True)
        (attachments / "bad.txt").write_bytes(b"\xff\xfe\x00\x00 binary garbage")
        s = _make_session(db_path, project_dir, workspace, idea="short")
        assert score_input_richness(s.id, db_path=db_path) == "sparse"


# ── unknown session ───────────────────────────────────────────────────────────


class TestUnknownSession:
    def test_raises_on_missing_session(self, db_path):
        with pytest.raises(ValueError, match="Session not found"):
            score_input_richness("ghost", db_path=db_path)


# Migration note: pipeline integration tests covering the v1 auto-rescore
# entry hook were removed when the corresponding phase was retired in v2
# (see plan squad-v2-lot-1). ``score_input_richness`` is still exercised
# by the standalone unit tests above and consumed by ``research.py``.
