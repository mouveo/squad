"""Plans review page — approve or reject generated plans from the dashboard.

Reuses the same business services as Slack and CLI so there is no
duplicated write path:

* approve → ``squad.forge_bridge.approve_and_submit`` (identical to the
  Slack ``Approuver`` button and the ``squad approve`` CLI)
* reject → ``squad.review_service.reject_session`` (identical to Slack
  rejection and ``squad review --action reject``)
"""

from __future__ import annotations

import streamlit as st

from squad.constants import STATUS_APPROVED, STATUS_FAILED, STATUS_QUEUED, STATUS_REVIEW
from squad.dashboard.data import (
    PLAN_SOURCE_WORKSPACE,
    PlanReviewItem,
    get_review_plans,
    get_session_detail,
)
from squad.forge_bridge import (
    ForgeQueueBusy,
    ForgeUnavailable,
    approve_and_submit,
)
from squad.review_service import reject_session

_TERMINAL_STATUSES = (STATUS_APPROVED, STATUS_QUEUED, STATUS_FAILED)


def _back_to_session_button(session_id: str) -> None:
    if st.button("← Détail session", key="back_to_session"):
        st.query_params.clear()
        st.query_params["page"] = "session"
        st.query_params["id"] = session_id
        st.rerun()


def _render_plan_item(item: PlanReviewItem) -> None:
    source_label = "(workspace)" if item.source == PLAN_SOURCE_WORKSPACE else "(DB)"
    with st.container(border=True):
        st.markdown(f"### {item.title} {source_label}")
        cols = st.columns(3)
        cols[0].metric("Lots", item.lot_count)
        cols[1].metric("Fichiers touchés", len(item.files))
        forge_label = item.forge_status or "—"
        cols[2].metric("Statut Forge", forge_label)
        if item.validation_errors:
            st.error(
                "Erreurs de format Forge :\n"
                + "\n".join(f"- {err}" for err in item.validation_errors)
            )
        with st.expander("Contenu Markdown du plan", expanded=False):
            st.markdown(item.content)
        if item.file_path:
            st.caption(f"Fichier source : `{item.file_path}`")


def _perform_approve(session_id: str) -> None:
    try:
        outcome = approve_and_submit(session_id)
    except (ForgeUnavailable, ForgeQueueBusy, ValueError) as exc:
        st.error(f"Soumission Forge refusée : {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        st.error(f"Erreur inattendue pendant l'approbation : {exc}")
        return
    st.toast(
        f"✅ {outcome.plans_sent} plan(s) envoyés à Forge "
        f"(queue_started={outcome.queue_started})",
        icon="🚀",
    )
    st.rerun()


def _perform_reject(session_id: str, reason: str) -> None:
    reject_session(session_id, reason)
    st.toast("❌ Session rejetée", icon="🚫")
    st.rerun()


def _render_reject_form(session_id: str) -> None:
    with st.form(f"reject_{session_id}"):
        reason = st.text_area(
            "Raison du rejet (obligatoire)",
            placeholder="Ce qui manque pour que les plans soient exploitables…",
        )
        submitted = st.form_submit_button("Confirmer le rejet", type="primary")
        if submitted:
            if not reason.strip():
                st.warning("La raison ne peut pas être vide.")
            else:
                _perform_reject(session_id, reason.strip())


def render_plans_review_page(session_id: str | None) -> None:
    """Render the review page. Disables actions on terminal statuses."""
    if not session_id:
        st.error("Aucun identifiant de session fourni.")
        return

    detail = get_session_detail(session_id)
    if detail is None:
        st.error(f"Session `{session_id}` introuvable.")
        return

    session = detail.session
    st.header(f"Plans — {session.title}")
    st.caption(f"ID `{session.id}` · Statut : {detail.status_label}")

    _back_to_session_button(session.id)

    plans = get_review_plans(session.id)
    if not plans:
        st.info("Aucun plan généré pour cette session.")
        return

    for plan in plans:
        _render_plan_item(plan)

    if session.status in _TERMINAL_STATUSES:
        st.warning(
            f"Session en statut terminal (`{session.status}`) — les actions "
            "approve / reject sont désactivées. Utilisez `squad review` CLI "
            "pour les cas de bascule manuelle."
        )
        return

    if session.status != STATUS_REVIEW:
        st.info(
            f"Session en statut `{session.status}` — attendre que le "
            "pipeline atteigne `review` pour approuver ou rejeter."
        )
        return

    st.markdown("---")
    col_approve, col_reject = st.columns(2)
    with col_approve:
        if st.button(
            "🚀 Approuver et envoyer à Forge",
            type="primary",
            key=f"approve_{session.id}",
        ):
            _perform_approve(session.id)
    with col_reject:
        if st.button("🚫 Rejeter", key=f"reject_toggle_{session.id}"):
            st.session_state[f"show_reject_form_{session.id}"] = True

    if st.session_state.get(f"show_reject_form_{session.id}"):
        _render_reject_form(session.id)
