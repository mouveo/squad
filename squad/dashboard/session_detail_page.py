"""Session detail page for the Squad dashboard.

Displays a full session: header with status + project, idea + context,
the 6-phase timeline with outputs inline, attachments
and pending questions. Read-only — actions live on the plans review
page.
"""

from __future__ import annotations

import streamlit as st

from squad.dashboard.data import (
    PHASE_STATE_DONE,
    PHASE_STATE_FAILED,
    PHASE_STATE_PENDING,
    PHASE_STATE_RUNNING,
    PHASE_STATE_SKIPPED,
    PhaseView,
    SessionDetail,
    get_session_detail,
)


_STATE_BADGE: dict[str, str] = {
    PHASE_STATE_DONE: "✅ terminée",
    PHASE_STATE_RUNNING: "🟦 en cours",
    PHASE_STATE_PENDING: "⚪ à venir",
    PHASE_STATE_FAILED: "🔴 en échec",
    PHASE_STATE_SKIPPED: "⏭ sautée",
}


def _back_to_list_button() -> None:
    if st.button("← Liste des sessions", key="back_to_list"):
        st.query_params.clear()
        st.query_params["page"] = "sessions"
        st.rerun()


def _go_to_plans_button(session_id: str, status: str) -> None:
    if status in ("review", "approved", "queued", "failed"):
        if st.button("📋 Voir les plans", key=f"goto_plans_{session_id}"):
            st.query_params.clear()
            st.query_params["page"] = "plans"
            st.query_params["id"] = session_id
            st.rerun()


def _render_header(detail: SessionDetail) -> None:
    session = detail.session
    st.header(session.title)
    cols = st.columns(5)
    cols[0].metric("Statut", detail.status_label)
    cols[1].metric("Phase", session.current_phase or "—")
    cols[2].metric("Mode", session.mode)
    cols[3].metric("Plans", detail.plans_count)
    cols[4].metric("Âge", detail.age_fr)

    st.caption(f"Projet : `{session.project_path}` · ID : `{session.id}`")

    if detail.failure_reason:
        st.error(f"Raison d'échec : {detail.failure_reason}")

    _back_to_list_button()
    _go_to_plans_button(session.id, session.status)


def _render_idea_and_context(detail: SessionDetail) -> None:
    with st.expander("💡 Idée", expanded=False):
        st.markdown(detail.idea)
    if detail.context:
        with st.expander("🗂 Contexte projet (scan)", expanded=False):
            st.markdown(detail.context)


def _render_phase(phase: PhaseView) -> None:
    badge = _STATE_BADGE.get(phase.state, phase.state)
    header = f"**{phase.label}** — {badge}"
    if phase.attempts_count > 1:
        header += f" · {phase.attempts_count} tentatives"
    if phase.skip_reason:
        header += f" · _{phase.skip_reason}_"
    with st.expander(header, expanded=phase.is_current):
        if phase.state == PHASE_STATE_SKIPPED:
            st.info(f"Phase sautée : {phase.skip_reason or '—'}")
            return
        if not phase.attempts or not any(a.outputs for a in phase.attempts):
            st.caption("Aucun output produit pour cette phase.")
            return
        for attempt in phase.attempts:
            if not attempt.outputs:
                continue
            label = f"Tentative {attempt.attempt}"
            if attempt.total_duration_seconds is not None:
                label += f" · {int(attempt.total_duration_seconds)}s"
            if attempt.total_tokens is not None:
                label += f" · {attempt.total_tokens} tokens"
            st.markdown(f"**{label}**")
            if len(attempt.outputs) == 1:
                output = attempt.outputs[0]
                st.caption(f"Agent : {output.agent}")
                st.markdown(output.output)
            else:
                tabs = st.tabs([o.agent for o in attempt.outputs])
                for tab, output in zip(tabs, attempt.outputs):
                    with tab:
                        st.markdown(output.output)


def _render_attachments(detail: SessionDetail) -> None:
    if not detail.attachments:
        return
    st.subheader("📎 Pièces jointes")
    for attachment in detail.attachments:
        cols = st.columns([4, 2, 2])
        cols[0].write(f"`{attachment.filename}`")
        cols[1].write(f"{attachment.size_bytes} octets")
        cols[2].caption(attachment.mime_type or "")


def _render_pending_questions(detail: SessionDetail) -> None:
    if not detail.pending_questions:
        return
    st.subheader("❓ Questions en attente")
    st.caption(
        "Pour répondre : utilisez Slack (boutons dans le thread) ou "
        "`squad answer <session_id> <question_id> \"<réponse>\"`."
    )
    for question in detail.pending_questions:
        with st.container(border=True):
            st.markdown(f"**[{question.agent}]** {question.text}")
            st.caption(f"question_id : `{question.id}`")


def render_session_detail_page(session_id: str | None) -> None:
    """Render the session detail view. Redirects when the id is missing.

    The phase timeline + attachments sections auto-refresh every 5
    seconds via ``st.fragment`` so phase transitions and new attachments
    appear without a manual rerun. The static header is rendered outside
    the fragment so it doesn't flicker on each tick.
    """
    if not session_id:
        st.error("Aucun identifiant de session fourni.")
        _back_to_list_button()
        return

    @st.fragment(run_every="5s")
    def _refreshing_body():
        detail = get_session_detail(session_id)
        if detail is None:
            st.error(f"Session `{session_id}` introuvable.")
            _back_to_list_button()
            return

        _render_header(detail)
        _render_idea_and_context(detail)

        st.subheader("🧵 Timeline des phases")
        for phase in detail.phases:
            _render_phase(phase)

        _render_attachments(detail)
        _render_pending_questions(detail)

    _refreshing_body()
