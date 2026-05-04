"""Tests for squad.attachment_service — validation, download, store, list."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from squad.attachment_service import (
    DEFAULT_ALLOWED_EXTENSIONS,
    DEFAULT_MAX_FILE_BYTES,
    INLINE_TEXT_EXTENSIONS,
    AttachmentError,
    download_file,
    import_local_attachment,
    list_attachments,
    store_attachment,
    validate_attachment,
)
from squad.db import create_session, ensure_schema
from squad.workspace import create_workspace


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "squad.db"
    ensure_schema(path)
    return path


@pytest.fixture
def session(db_path: Path, tmp_path: Path):
    project = tmp_path / "proj"
    project.mkdir()
    workspace = project / ".squad" / "sessions" / "sess-1"
    s = create_session(
        title="Test",
        project_path=str(project),
        workspace_path=str(workspace),
        idea="x",
        db_path=db_path,
        session_id="sess-1",
    )
    create_workspace(s)
    return s


# ── validate_attachment ────────────────────────────────────────────────────────


class TestValidate:
    def test_default_extensions_cover_required_kinds(self):
        for ext in ("md", "txt", "csv", "pdf", "png", "jpg", "jpeg"):
            assert ext in DEFAULT_ALLOWED_EXTENSIONS

    def test_inline_text_extensions(self):
        assert set(INLINE_TEXT_EXTENSIONS) == {"md", "txt", "csv"}

    def test_accepts_allowed_extension(self, db_path, session):
        validate_attachment("brief.md", 100, session_id=session.id, db_path=db_path)

    def test_rejects_unknown_extension(self, db_path, session):
        with pytest.raises(AttachmentError, match="non autorisée"):
            validate_attachment("script.exe", 100, session_id=session.id, db_path=db_path)

    def test_rejects_zero_size(self, db_path, session):
        with pytest.raises(AttachmentError, match="vide"):
            validate_attachment("brief.md", 0, session_id=session.id, db_path=db_path)

    def test_rejects_oversized_file(self, db_path, session):
        with pytest.raises(AttachmentError, match="trop volumineux"):
            validate_attachment(
                "brief.md",
                DEFAULT_MAX_FILE_BYTES + 1,
                session_id=session.id,
                db_path=db_path,
            )

    def test_rejects_when_cumulative_quota_exceeded(self, db_path, session):
        attachments = Path(session.workspace_path) / "attachments"
        # Pre-fill with files just under the cumulative cap
        big_chunk = b"x" * (DEFAULT_MAX_FILE_BYTES)
        for i in range(5):  # 5 × 10 MB = 50 MB exactly
            (attachments / f"prev-{i}.bin").write_bytes(big_chunk)

        with pytest.raises(AttachmentError, match="Quota"):
            validate_attachment(
                "extra.md",
                1024,
                session_id=session.id,
                db_path=db_path,
            )

    def test_unknown_session_raises(self, db_path):
        with pytest.raises(AttachmentError, match="Session"):
            validate_attachment("brief.md", 10, session_id="ghost", db_path=db_path)

    def test_config_overrides_extensions(self, db_path, session):
        cfg = {"slack": {"attachments": {"allowed_extensions": ["mdx"]}}}
        with pytest.raises(AttachmentError, match="non autorisée"):
            validate_attachment("brief.md", 10, session_id=session.id, config=cfg, db_path=db_path)
        validate_attachment("note.mdx", 10, session_id=session.id, config=cfg, db_path=db_path)


# ── store_attachment ───────────────────────────────────────────────────────────


class TestStoreAttachment:
    def test_stores_file_under_workspace(self, db_path, session):
        meta = store_attachment(session.id, "brief.md", b"# brief", db_path=db_path)
        path = Path(session.workspace_path) / "attachments" / "brief.md"
        assert path.exists()
        assert path.read_bytes() == b"# brief"
        assert meta.path == str(path)
        assert meta.size_bytes == len(b"# brief")
        assert meta.extension == "md"

    def test_collision_appends_suffix(self, db_path, session):
        store_attachment(session.id, "brief.md", b"first", db_path=db_path)
        meta = store_attachment(session.id, "brief.md", b"second", db_path=db_path)
        assert meta.filename == "brief-1.md"
        assert (Path(session.workspace_path) / "attachments" / "brief-1.md").exists()

    def test_sanitises_path_traversal(self, db_path, session):
        meta = store_attachment(session.id, "../../etc/passwd.md", b"x", db_path=db_path)
        # Path traversal must be neutralised
        path = Path(meta.path)
        assert path.parent.name == "attachments"
        assert ".." not in path.name

    def test_rejects_after_quota(self, db_path, session):
        # Fill workspace just under the cap
        attachments = Path(session.workspace_path) / "attachments"
        for i in range(5):
            (attachments / f"f-{i}.bin").write_bytes(b"x" * DEFAULT_MAX_FILE_BYTES)
        with pytest.raises(AttachmentError):
            store_attachment(session.id, "extra.md", b"x" * 1024, db_path=db_path)

    def test_rejects_oversized_content(self, db_path, session):
        with pytest.raises(AttachmentError, match="trop volumineux"):
            store_attachment(
                session.id, "huge.md", b"x" * (DEFAULT_MAX_FILE_BYTES + 1), db_path=db_path
            )


# ── list_attachments ──────────────────────────────────────────────────────────


class TestListAttachments:
    def test_empty_when_none(self, db_path, session):
        assert list_attachments(session.id, db_path=db_path) == []

    def test_lists_stored_files(self, db_path, session):
        store_attachment(session.id, "a.md", b"a", db_path=db_path)
        store_attachment(session.id, "b.txt", b"bb", db_path=db_path)
        metas = list_attachments(session.id, db_path=db_path)
        names = [m.filename for m in metas]
        assert names == ["a.md", "b.txt"]
        assert metas[1].size_bytes == 2

    def test_unknown_session_returns_empty(self, db_path):
        assert list_attachments("ghost", db_path=db_path) == []


# ── import_local_attachment ───────────────────────────────────────────────────


class TestImportLocalAttachment:
    def test_stores_local_markdown(self, db_path, session, tmp_path):
        src = tmp_path / "brief.md"
        src.write_bytes(b"x" * 1024)

        meta = import_local_attachment(session.id, src, db_path=db_path)

        assert meta.slack_file_id is None
        assert meta.filename == "brief.md"
        assert meta.size_bytes == 1024
        stored = Path(session.workspace_path) / "attachments" / "brief.md"
        assert stored.exists()
        assert stored.read_bytes() == b"x" * 1024

    def test_rejects_disallowed_extension(self, db_path, session, tmp_path):
        src = tmp_path / "evil.exe"
        src.write_bytes(b"MZ")
        with pytest.raises(AttachmentError, match="non autorisée"):
            import_local_attachment(session.id, src, db_path=db_path)

    def test_rejects_oversized_file(self, db_path, session, tmp_path):
        src = tmp_path / "huge.md"
        src.write_bytes(b"x" * (DEFAULT_MAX_FILE_BYTES + 1))
        with pytest.raises(AttachmentError, match="trop volumineux"):
            import_local_attachment(session.id, src, db_path=db_path)

    def test_quota_applied_with_existing_slack_attachments(self, db_path, session, tmp_path):
        # Simulate prior Slack-sourced attachments filling the cumulative cap
        attachments = Path(session.workspace_path) / "attachments"
        for i in range(5):
            (attachments / f"slack-{i}.bin").write_bytes(b"x" * DEFAULT_MAX_FILE_BYTES)

        src = tmp_path / "extra.md"
        src.write_bytes(b"x" * 1024)

        with pytest.raises(AttachmentError, match="Quota"):
            import_local_attachment(session.id, src, db_path=db_path)

    def test_missing_path_raises_attachment_error(self, db_path, session, tmp_path):
        missing = tmp_path / "does-not-exist.md"
        with pytest.raises(AttachmentError, match="introuvable"):
            import_local_attachment(session.id, missing, db_path=db_path)

    def test_directory_path_raises_attachment_error(self, db_path, session, tmp_path):
        a_dir = tmp_path / "some-dir"
        a_dir.mkdir()
        with pytest.raises(AttachmentError, match="pas un fichier"):
            import_local_attachment(session.id, a_dir, db_path=db_path)

    def test_unreadable_file_raises_attachment_error(self, db_path, session, tmp_path):
        src = tmp_path / "secret.md"
        src.write_bytes(b"x")

        def _boom(self, *args, **kwargs):
            raise OSError("permission denied")

        with patch.object(Path, "read_bytes", _boom):
            with pytest.raises(AttachmentError, match="Lecture impossible"):
                import_local_attachment(session.id, src, db_path=db_path)

    def test_respects_config_overrides(self, db_path, session, tmp_path):
        src = tmp_path / "note.mdx"
        src.write_bytes(b"hello")
        cfg = {"slack": {"attachments": {"allowed_extensions": ["mdx"]}}}
        meta = import_local_attachment(session.id, src, config=cfg, db_path=db_path)
        assert meta.filename == "note.mdx"
        assert meta.slack_file_id is None


# ── download_file ─────────────────────────────────────────────────────────────


class TestDownloadFile:
    def test_uses_bearer_token(self):
        client = MagicMock(spec=httpx.Client)
        response = MagicMock()
        response.content = b"hello"
        response.raise_for_status.return_value = None
        client.get.return_value = response

        data = download_file("https://files.slack.com/abc", "xoxb-bot", client=client)
        assert data == b"hello"
        kwargs = client.get.call_args.kwargs
        assert kwargs["headers"]["Authorization"] == "Bearer xoxb-bot"

    def test_http_error_wrapped(self):
        client = MagicMock(spec=httpx.Client)
        client.get.side_effect = httpx.HTTPError("boom")
        with pytest.raises(AttachmentError, match="Téléchargement"):
            download_file("https://files.slack.com/abc", "xoxb-bot", client=client)
