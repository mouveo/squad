"""Slack-thread attachments → workspace storage and listing.

A drag-drop in the session thread triggers a ``file_shared`` event in
``squad.slack_handlers``; the handler then drives this module to:

1. Validate filename and reported size against the configured policy.
2. Download the raw bytes via the Slack ``url_private_download`` URL.
3. Store them under ``{workspace}/attachments/{filename}`` (collisions
   are made unique with a numeric suffix).
4. Return the resulting :class:`AttachmentMeta` so the caller can post a
   confirmation in the thread.

All limits live here so the slack handler stays a thin adapter.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from pathlib import Path

import httpx

from squad.db import get_session
from squad.models import AttachmentMeta

logger = logging.getLogger(__name__)


# ── defaults ──────────────────────────────────────────────────────────────────

DEFAULT_ALLOWED_EXTENSIONS: tuple[str, ...] = (
    "md",
    "txt",
    "csv",
    "pdf",
    "png",
    "jpg",
    "jpeg",
)

# Hard-coded limits per the LOT 3 spec; can be overridden in config.
DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB
DEFAULT_MAX_TOTAL_BYTES = 50 * 1024 * 1024  # 50 MB

# Extensions whose content is inlined verbatim in the cumulative context.
INLINE_TEXT_EXTENSIONS: tuple[str, ...] = ("md", "txt", "csv")

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\-]+")


class AttachmentError(Exception):
    """Raised when an attachment cannot be stored.

    The message is user-facing and posted back into the Slack thread, so
    keep it short and actionable.
    """


# ── helpers ───────────────────────────────────────────────────────────────────


def _attachment_policy(config: dict | None) -> tuple[tuple[str, ...], int, int]:
    """Return ``(allowed_extensions, max_file_bytes, max_total_bytes)`` from config."""
    cfg = ((config or {}).get("slack") or {}).get("attachments") or {}
    raw_exts = cfg.get("allowed_extensions") or DEFAULT_ALLOWED_EXTENSIONS
    allowed = tuple(str(e).lower().lstrip(".") for e in raw_exts)
    max_file = int(cfg.get("max_file_bytes") or DEFAULT_MAX_FILE_BYTES)
    max_total = int(cfg.get("max_total_bytes") or DEFAULT_MAX_TOTAL_BYTES)
    return allowed, max_file, max_total


def _attachments_dir(session_id: str, db_path: Path | None) -> Path:
    """Return the ``attachments/`` directory for ``session_id``.

    Created on demand so older workspaces (predating LOT 3) keep working
    without a manual migration step.
    """
    session = get_session(session_id, db_path=db_path)
    if session is None:
        raise AttachmentError(f"Session inconnue : {session_id}")
    path = Path(session.workspace_path) / "attachments"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_filename(name: str) -> str:
    """Return a filename safe for the local filesystem (no path traversal)."""
    base = Path(name).name  # strip any directory component
    base = _SAFE_FILENAME_RE.sub("_", base).strip("._-")
    return base or "attachment"


def _ext_of(name: str) -> str:
    return Path(name).suffix.lstrip(".").lower()


def _unique_path(base_dir: Path, filename: str) -> Path:
    """Return a non-clashing path inside ``base_dir`` for ``filename``."""
    candidate = base_dir / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    n = 1
    while True:
        alt = base_dir / f"{stem}-{n}{suffix}"
        if not alt.exists():
            return alt
        n += 1


# ── validation ────────────────────────────────────────────────────────────────


def validate_attachment(
    filename: str,
    size_bytes: int,
    *,
    session_id: str,
    config: dict | None = None,
    db_path: Path | None = None,
) -> None:
    """Raise :class:`AttachmentError` when the attachment must be rejected.

    Checks (in order): non-empty filename, allowed extension, per-file
    size cap, cumulative session cap. The cumulative check sums the
    sizes of files already on disk so retries after a partial failure
    behave consistently.
    """
    allowed, max_file, max_total = _attachment_policy(config)

    if get_session(session_id, db_path=db_path) is None:
        raise AttachmentError(f"Session inconnue : {session_id}")

    safe = _safe_filename(filename)
    if not safe:
        raise AttachmentError("Nom de fichier vide ou invalide.")

    ext = _ext_of(safe)
    if ext not in allowed:
        allowed_disp = ", ".join(allowed)
        raise AttachmentError(
            f"Extension `.{ext}` non autorisée. Autorisées : {allowed_disp}."
        )

    if size_bytes <= 0:
        raise AttachmentError(f"Fichier `{safe}` vide.")
    if size_bytes > max_file:
        mb = max_file / (1024 * 1024)
        raise AttachmentError(
            f"Fichier `{safe}` trop volumineux ({size_bytes / 1024 / 1024:.1f} Mo). "
            f"Limite : {mb:.0f} Mo par fichier."
        )

    used = _used_bytes(session_id, db_path=db_path)
    if used + size_bytes > max_total:
        mb_total = max_total / (1024 * 1024)
        raise AttachmentError(
            f"Quota d'attachements de session dépassé "
            f"({(used + size_bytes) / 1024 / 1024:.1f} Mo > {mb_total:.0f} Mo)."
        )


def _used_bytes(session_id: str, db_path: Path | None) -> int:
    """Return the total bytes already stored under the session's attachments/."""
    try:
        directory = _attachments_dir(session_id, db_path=db_path)
    except AttachmentError:
        return 0
    return sum(f.stat().st_size for f in directory.iterdir() if f.is_file())


# ── download + store ──────────────────────────────────────────────────────────


def download_file(
    url: str,
    bot_token: str,
    *,
    timeout: float = 30.0,
    client: httpx.Client | None = None,
) -> bytes:
    """Fetch the bytes of a Slack ``url_private_download`` URL.

    Slack requires a ``Bearer`` auth header with the bot token. Network
    errors propagate as :class:`AttachmentError` so the caller can post a
    single user-friendly message in the thread. ``client`` is exposed to
    let tests inject a stub.
    """
    headers = {"Authorization": f"Bearer {bot_token}"}
    try:
        if client is not None:
            response = client.get(url, headers=headers, timeout=timeout)
        else:
            response = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        return response.content
    except httpx.HTTPError as exc:
        raise AttachmentError(f"Téléchargement Slack échoué : {exc}") from exc


def store_attachment(
    session_id: str,
    filename: str,
    content: bytes,
    *,
    mime_type: str | None = None,
    slack_file_id: str | None = None,
    config: dict | None = None,
    db_path: Path | None = None,
) -> AttachmentMeta:
    """Persist ``content`` under the session's attachments directory.

    Re-validates against the policy using the actual byte length (the
    pre-download check used Slack's reported size). Filenames are
    sanitised and de-duplicated against existing files.
    """
    validate_attachment(
        filename,
        len(content),
        session_id=session_id,
        config=config,
        db_path=db_path,
    )
    base_dir = _attachments_dir(session_id, db_path=db_path)
    safe = _safe_filename(filename)
    target = _unique_path(base_dir, safe)
    target.write_bytes(content)

    meta = AttachmentMeta(
        session_id=session_id,
        filename=target.name,
        path=str(target),
        size_bytes=len(content),
        mime_type=mime_type,
        slack_file_id=slack_file_id,
    )
    logger.info(
        "Stored attachment %s (%d bytes) for session %s",
        meta.filename,
        meta.size_bytes,
        session_id,
    )
    return meta


# ── local import ──────────────────────────────────────────────────────────────


def import_local_attachment(
    session_id: str,
    src_path: Path,
    *,
    config: dict | None = None,
    db_path: Path | None = None,
) -> AttachmentMeta:
    """Import a local file into the session's attachments directory.

    Reads the file from ``src_path`` and delegates to :func:`store_attachment`
    so the size / extension / cumulative-quota policy stays centralised.
    Filesystem errors are converted into short :class:`AttachmentError`
    messages suitable for surfacing to the user.
    """
    src = Path(src_path)
    if not src.exists():
        raise AttachmentError(f"Fichier introuvable : {src}")
    if not src.is_file():
        raise AttachmentError(f"Chemin non lisible (pas un fichier) : {src}")
    try:
        content = src.read_bytes()
    except OSError as exc:
        raise AttachmentError(f"Lecture impossible de `{src.name}` : {exc}") from exc

    return store_attachment(
        session_id,
        src.name,
        content,
        slack_file_id=None,
        config=config,
        db_path=db_path,
    )


# ── listing ───────────────────────────────────────────────────────────────────


def list_attachments(
    session_id: str,
    db_path: Path | None = None,
) -> list[AttachmentMeta]:
    """Return every stored attachment for a session, sorted by name.

    The DB session record is looked up to resolve the workspace path.
    Files added out-of-band (e.g. dropped manually) are listed too — this
    keeps the function honest as a "what's on disk" mirror.
    """
    try:
        directory = _attachments_dir(session_id, db_path=db_path)
    except AttachmentError:
        return []
    metas: list[AttachmentMeta] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        metas.append(
            AttachmentMeta(
                session_id=session_id,
                filename=path.name,
                path=str(path),
                size_bytes=path.stat().st_size,
            )
        )
    return metas


# ── helpers re-exported for context_builder ──────────────────────────────────


def is_inline_text(meta: AttachmentMeta) -> bool:
    """Return True when the attachment should be inlined in the prompt."""
    return meta.extension in INLINE_TEXT_EXTENSIONS


def with_extension(meta: AttachmentMeta, ext: str) -> AttachmentMeta:
    """Return a copy of ``meta`` with the given extension (test helper)."""
    return replace(meta, extension=ext)
