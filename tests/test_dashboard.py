"""Tests for the dashboard read layer and shared status semantics."""

from datetime import datetime, timedelta
from pathlib import Path

from squad.constants import (
    ACTIVE_STATUSES,
    PHASE_BENCHMARK,
    PHASE_CADRAGE,
    PHASE_CHALLENGE,
    PHASE_CONCEPTION,
    PHASE_ETAT_DES_LIEUX,
    PHASE_SYNTHESE,
    SESSION_STATUSES,
    STATUS_APPROVED,
    STATUS_DONE,
    STATUS_DRAFT,
    STATUS_FAILED,
    STATUS_INTERVIEWING,
    STATUS_LABELS,
    STATUS_QUEUED,
    STATUS_REVIEW,
    STATUS_TONES,
    STATUS_WORKING,
    TERMINAL_STATUSES,
)
from squad.dashboard.data import (
    PHASE_STATE_DONE,
    PHASE_STATE_FAILED,
    PHASE_STATE_PENDING,
    PHASE_STATE_RUNNING,
    PHASE_STATE_SKIPPED,
    PLAN_SOURCE_DB,
    PLAN_SOURCE_WORKSPACE,
    SessionDetail,
    SessionRow,
    count_sessions,
    get_review_plans,
    get_session_detail,
    humanize_age_fr,
    list_sessions_for_dashboard,
)
from squad.db import (
    create_phase_output,
    create_plan,
    create_session,
    ensure_schema,
    increment_phase_attempt,
    mark_phase_skipped,
    update_session_status,
)


def test_count_sessions_zero_on_fresh_db(tmp_path: Path) -> None:
    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    assert count_sessions(db_path=db_path) == 0


def test_count_sessions_missing_db_file(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"
    assert count_sessions(db_path=db_path) == 0


def test_count_sessions_after_inserts(tmp_path: Path) -> None:
    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    for i in range(3):
        create_session(
            title=f"session-{i}",
            project_path=str(tmp_path),
            workspace_path=str(tmp_path / f"ws-{i}"),
            idea=f"idea {i}",
            db_path=db_path,
        )
    assert count_sessions(db_path=db_path) == 3


# ── Shared status semantics used by the dashboard ──────────────────────────────


def test_terminal_statuses_are_only_done_and_failed() -> None:
    assert TERMINAL_STATUSES == {STATUS_DONE, STATUS_FAILED}


def test_active_statuses_exclude_only_terminal() -> None:
    assert STATUS_DONE not in ACTIVE_STATUSES
    assert STATUS_FAILED not in ACTIVE_STATUSES
    # Every non-terminal status must be considered active.
    for status in SESSION_STATUSES:
        if status in TERMINAL_STATUSES:
            continue
        assert status in ACTIVE_STATUSES


def test_active_statuses_aligned_with_db_layer(tmp_path: Path) -> None:
    """The dashboard's notion of "active" must match the DB's own filter.

    The CLI `squad status` relies on `list_active_sessions`, which
    itself uses `TERMINAL_STATUSES`. If these two ever drift, a session
    would appear "active" in one surface and "terminated" in another.
    """
    from squad.db import list_active_sessions

    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    # Seed one session per status, then confirm only active ones list.
    active_ids: set[str] = set()
    for i, status in enumerate(SESSION_STATUSES):
        sess = create_session(
            title=f"s-{status}",
            project_path=str(tmp_path),
            workspace_path=str(tmp_path / f"ws-{i}"),
            idea="x",
            db_path=db_path,
        )
        # Flip to target status (create_session defaults to draft)
        from squad.db import update_session_status

        update_session_status(sess.id, status, db_path=db_path)
        if status in ACTIVE_STATUSES:
            active_ids.add(sess.id)
    listed = {s.id for s in list_active_sessions(db_path=db_path)}
    assert listed == active_ids


def test_status_labels_cover_all_eight_statuses() -> None:
    expected = {
        STATUS_DRAFT,
        STATUS_WORKING,
        STATUS_INTERVIEWING,
        STATUS_REVIEW,
        STATUS_APPROVED,
        STATUS_QUEUED,
        STATUS_DONE,
        STATUS_FAILED,
    }
    assert set(STATUS_LABELS.keys()) == expected
    # No empty label.
    assert all(isinstance(v, str) and v for v in STATUS_LABELS.values())


def test_status_tones_cover_all_eight_statuses() -> None:
    assert set(STATUS_TONES.keys()) == set(STATUS_LABELS.keys())
    allowed = {"neutral", "progress", "info", "warning", "success", "muted", "danger"}
    for status, tone in STATUS_TONES.items():
        assert tone in allowed, f"{status} has unexpected tone {tone!r}"


# ── Shared reject service ─────────────────────────────────────────────────────


def test_reject_session_persists_reason_and_flips_status(tmp_path: Path) -> None:
    from squad.db import get_session
    from squad.review_service import reject_session

    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    sess = create_session(
        title="t",
        project_path=str(tmp_path),
        workspace_path=str(tmp_path / "ws"),
        idea="x",
        db_path=db_path,
    )
    reject_session(sess.id, "not clear enough", db_path=db_path)
    refreshed = get_session(sess.id, db_path=db_path)
    assert refreshed.status == STATUS_FAILED
    assert refreshed.failure_reason == "not clear enough"


# ── humanize_age_fr (pure) ────────────────────────────────────────────────────


def test_humanize_age_fr_none() -> None:
    assert humanize_age_fr(None) == "—"


def test_humanize_age_fr_just_now() -> None:
    now = datetime(2026, 4, 18, 12, 0, 0)
    assert humanize_age_fr(now - timedelta(seconds=3), now=now) == "à l'instant"


def test_humanize_age_fr_seconds() -> None:
    now = datetime(2026, 4, 18, 12, 0, 0)
    assert humanize_age_fr(now - timedelta(seconds=42), now=now) == "il y a 42 s"


def test_humanize_age_fr_minutes_hours_days_months_years() -> None:
    now = datetime(2026, 4, 18, 12, 0, 0)
    assert humanize_age_fr(now - timedelta(minutes=5), now=now) == "il y a 5 min"
    assert humanize_age_fr(now - timedelta(hours=3), now=now) == "il y a 3 h"
    assert humanize_age_fr(now - timedelta(days=4), now=now) == "il y a 4 j"
    assert humanize_age_fr(now - timedelta(days=65), now=now) == "il y a 2 mois"
    assert humanize_age_fr(now - timedelta(days=800), now=now) == "il y a 2 ans"


def test_humanize_age_fr_future_is_safe() -> None:
    now = datetime(2026, 4, 18, 12, 0, 0)
    assert humanize_age_fr(now + timedelta(seconds=10), now=now) == "dans le futur"


# ── list_sessions_for_dashboard (filter + sort) ───────────────────────────────


def _seed(tmp_path: Path, title: str, db_path: Path, status: str, project: str):
    sess = create_session(
        title=title,
        project_path=project,
        workspace_path=str(tmp_path / f"ws-{title}"),
        idea="x",
        db_path=db_path,
    )
    if status != STATUS_DRAFT:
        update_session_status(sess.id, status, db_path=db_path)
    return sess


def test_list_sessions_for_dashboard_filters_by_status(tmp_path: Path) -> None:
    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    _seed(tmp_path, "a", db_path, STATUS_REVIEW, str(tmp_path))
    _seed(tmp_path, "b", db_path, STATUS_DONE, str(tmp_path))
    _seed(tmp_path, "c", db_path, STATUS_REVIEW, str(tmp_path))

    rows = list_sessions_for_dashboard(status=STATUS_REVIEW, db_path=db_path)
    titles = {r.title for r in rows}
    assert titles == {"a", "c"}
    assert all(isinstance(r, SessionRow) for r in rows)
    assert all(r.status == STATUS_REVIEW for r in rows)
    assert all(r.is_active for r in rows)


def test_list_sessions_for_dashboard_accepts_iterable_status(tmp_path: Path) -> None:
    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    _seed(tmp_path, "a", db_path, STATUS_REVIEW, str(tmp_path))
    _seed(tmp_path, "b", db_path, STATUS_DONE, str(tmp_path))
    _seed(tmp_path, "c", db_path, STATUS_WORKING, str(tmp_path))

    rows = list_sessions_for_dashboard(
        status=[STATUS_REVIEW, STATUS_WORKING], db_path=db_path
    )
    assert {r.title for r in rows} == {"a", "c"}


def test_list_sessions_for_dashboard_filters_by_project(tmp_path: Path) -> None:
    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    _seed(tmp_path, "a", db_path, STATUS_DRAFT, "/proj/one")
    _seed(tmp_path, "b", db_path, STATUS_DRAFT, "/proj/two")
    _seed(tmp_path, "c", db_path, STATUS_DRAFT, "/proj/one")

    rows = list_sessions_for_dashboard(project_path="/proj/one", db_path=db_path)
    assert {r.title for r in rows} == {"a", "c"}


def test_list_sessions_for_dashboard_badge_fields_are_populated(tmp_path: Path) -> None:
    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    _seed(tmp_path, "a", db_path, STATUS_APPROVED, str(tmp_path))
    _seed(tmp_path, "b", db_path, STATUS_FAILED, str(tmp_path))
    rows = list_sessions_for_dashboard(db_path=db_path)
    by_title = {r.title: r for r in rows}
    assert by_title["a"].status_label == STATUS_LABELS[STATUS_APPROVED]
    assert by_title["a"].status_tone == STATUS_TONES[STATUS_APPROVED]
    assert by_title["a"].is_active is True
    assert by_title["b"].status_label == STATUS_LABELS[STATUS_FAILED]
    assert by_title["b"].is_active is False


def test_list_sessions_for_dashboard_rejects_unknown_sort(tmp_path: Path) -> None:
    import pytest

    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    with pytest.raises(ValueError):
        list_sessions_for_dashboard(sort="DROP TABLE", db_path=db_path)


# ── get_session_detail (phase aggregation, retries, skips) ────────────────────


def test_get_session_detail_missing_returns_none(tmp_path: Path) -> None:
    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    assert get_session_detail("unknown", db_path=db_path) is None


def test_get_session_detail_aggregates_phases_with_retries(tmp_path: Path) -> None:
    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sess = create_session(
        title="retry",
        project_path=str(tmp_path),
        workspace_path=str(workspace),
        idea="idea",
        db_path=db_path,
    )
    # Phase 1 — cadrage: completed once (pm)
    increment_phase_attempt(sess.id, PHASE_CADRAGE, db_path=db_path)
    create_phase_output(
        session_id=sess.id,
        phase=PHASE_CADRAGE,
        agent="pm",
        output="cadrage output",
        file_path=str(workspace / "c.md"),
        duration_seconds=12.0,
        tokens_used=100,
        attempt=1,
        db_path=db_path,
    )
    # Phase 2 — etat_des_lieux: skipped
    mark_phase_skipped(
        sess.id, PHASE_ETAT_DES_LIEUX, "light mode", db_path=db_path
    )
    # Phase 4 — conception: retried twice, with outputs on both attempts
    increment_phase_attempt(sess.id, PHASE_CONCEPTION, db_path=db_path)
    create_phase_output(
        session_id=sess.id,
        phase=PHASE_CONCEPTION,
        agent="architect",
        output="first",
        file_path=str(workspace / "i1.md"),
        duration_seconds=8.0,
        tokens_used=50,
        attempt=1,
        db_path=db_path,
    )
    increment_phase_attempt(sess.id, PHASE_CONCEPTION, db_path=db_path)
    create_phase_output(
        session_id=sess.id,
        phase=PHASE_CONCEPTION,
        agent="architect",
        output="second",
        file_path=str(workspace / "i2.md"),
        duration_seconds=9.0,
        tokens_used=60,
        attempt=2,
        db_path=db_path,
    )
    # Phase 4 — benchmark: running now (current phase)
    increment_phase_attempt(sess.id, PHASE_BENCHMARK, db_path=db_path)
    update_session_status(
        sess.id, STATUS_WORKING, current_phase=PHASE_BENCHMARK, db_path=db_path
    )

    detail = get_session_detail(sess.id, db_path=db_path)
    assert isinstance(detail, SessionDetail)
    by_id = {p.id: p for p in detail.phases}

    # cadrage → done with a single attempt carrying one output
    cadrage = by_id[PHASE_CADRAGE]
    assert cadrage.state == PHASE_STATE_DONE
    assert cadrage.attempts_count == 1
    assert len(cadrage.attempts) == 1
    assert cadrage.attempts[0].agents == ["pm"]
    assert cadrage.attempts[0].total_duration_seconds == 12.0
    assert cadrage.attempts[0].total_tokens == 100

    # etat_des_lieux → skipped with reason preserved
    etat = by_id[PHASE_ETAT_DES_LIEUX]
    assert etat.state == PHASE_STATE_SKIPPED
    assert etat.skip_reason == "light mode"

    # conception → done with TWO distinct attempts (retry not flattened)
    conception = by_id[PHASE_CONCEPTION]
    assert conception.state == PHASE_STATE_DONE
    assert conception.attempts_count == 2
    assert [a.attempt for a in conception.attempts] == [1, 2]
    assert len(conception.attempts[0].outputs) == 1
    assert len(conception.attempts[1].outputs) == 1

    # benchmark → running (current phase) with no output yet
    benchmark = by_id[PHASE_BENCHMARK]
    assert benchmark.state == PHASE_STATE_RUNNING
    assert benchmark.is_current is True
    assert benchmark.attempts_count == 1  # attempt recorded, no output stored
    assert benchmark.attempts[0].outputs == []

    # challenge / synthese → pending
    assert by_id[PHASE_CHALLENGE].state == PHASE_STATE_PENDING
    assert by_id[PHASE_SYNTHESE].state == PHASE_STATE_PENDING


def test_get_session_detail_failed_phase_is_reflected(tmp_path: Path) -> None:
    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    sess = create_session(
        title="boom",
        project_path=str(tmp_path),
        workspace_path=str(tmp_path / "ws"),
        idea="x",
        db_path=db_path,
    )
    increment_phase_attempt(sess.id, PHASE_CONCEPTION, db_path=db_path)
    update_session_status(
        sess.id, STATUS_FAILED, current_phase=PHASE_CONCEPTION, db_path=db_path
    )
    detail = get_session_detail(sess.id, db_path=db_path)
    conception = next(p for p in detail.phases if p.id == PHASE_CONCEPTION)
    assert conception.state == PHASE_STATE_FAILED
    assert conception.is_current is True


def test_get_session_detail_reads_idea_and_context_from_workspace(tmp_path: Path) -> None:
    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "idea.md").write_text("edited idea\n", encoding="utf-8")
    (workspace / "context.md").write_text("project context\n", encoding="utf-8")
    sess = create_session(
        title="t",
        project_path=str(tmp_path),
        workspace_path=str(workspace),
        idea="original idea",
        db_path=db_path,
    )
    detail = get_session_detail(sess.id, db_path=db_path)
    assert detail.idea == "edited idea\n"
    assert detail.context == "project context\n"


# ── get_review_plans (workspace-first, DB fallback) ───────────────────────────


def _valid_plan_md(title: str = "Plan de test") -> str:
    header = f"# {title}\n\n"
    lots = []
    for i in range(1, 6):
        lots.append(
            f"## LOT {i} — Titre {i}\n\n"
            f"Description du lot {i}.\n\n"
            f"**Success criteria**: ok\n\n"
            f"**Files**: `file{i}.py`\n"
        )
    return header + "\n".join(lots)


def test_get_review_plans_prefers_workspace_file(tmp_path: Path) -> None:
    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    workspace = tmp_path / "ws"
    (workspace / "plans").mkdir(parents=True)
    sess = create_session(
        title="t",
        project_path=str(tmp_path),
        workspace_path=str(workspace),
        idea="x",
        db_path=db_path,
    )
    workspace_plan = workspace / "plans" / "plan-1.md"
    workspace_plan.write_text(_valid_plan_md("Plan édité"), encoding="utf-8")
    create_plan(
        session_id=sess.id,
        title="Plan original",
        file_path=str(workspace_plan),
        content=_valid_plan_md("Plan original"),
        db_path=db_path,
    )
    items = get_review_plans(sess.id, db_path=db_path)
    assert len(items) == 1
    item = items[0]
    assert item.source == PLAN_SOURCE_WORKSPACE
    assert "Plan édité" in item.content
    assert item.lot_count == 5
    assert item.validation_errors == []


def test_get_review_plans_falls_back_to_db_when_file_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    workspace = tmp_path / "ws"
    (workspace / "plans").mkdir(parents=True)
    sess = create_session(
        title="t",
        project_path=str(tmp_path),
        workspace_path=str(workspace),
        idea="x",
        db_path=db_path,
    )
    # file_path points nowhere on disk — the fallback must kick in
    create_plan(
        session_id=sess.id,
        title="Plan DB",
        file_path=str(workspace / "plans" / "missing.md"),
        content=_valid_plan_md("Plan DB"),
        db_path=db_path,
    )
    items = get_review_plans(sess.id, db_path=db_path)
    assert len(items) == 1
    assert items[0].source == PLAN_SOURCE_DB
    assert "Plan DB" in items[0].content
    assert items[0].lot_count == 5


def test_get_review_plans_reports_validation_errors(tmp_path: Path) -> None:
    db_path = tmp_path / "squad.db"
    ensure_schema(db_path)
    workspace = tmp_path / "ws"
    (workspace / "plans").mkdir(parents=True)
    sess = create_session(
        title="t",
        project_path=str(tmp_path),
        workspace_path=str(workspace),
        idea="x",
        db_path=db_path,
    )
    invalid = "# Plan\n\n## LOT 1 — Missing fields\n\nBody only, no Files/Success.\n"
    create_plan(
        session_id=sess.id,
        title="Bad plan",
        file_path=str(workspace / "plans" / "bad.md"),
        content=invalid,
        db_path=db_path,
    )
    items = get_review_plans(sess.id, db_path=db_path)
    assert items[0].validation_errors  # at least one error reported
