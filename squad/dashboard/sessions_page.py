"""Sessions list page for the Squad dashboard.

Renders the table of Squad sessions with status pastille, current phase,
age, and a clickable ID that routes to the session detail page via
``?page=session&id=<uuid>`` query params. All data comes from
``list_sessions_for_dashboard`` in the typed read layer.
"""

from __future__ import annotations

from datetime import datetime

import streamlit as st

from squad.constants import STATUS_LABELS
from squad.dashboard.data import SessionRow, list_sessions_for_dashboard


# Map status_tone (from constants.STATUS_TONES) to a markdown-friendly dot.
# Kept here so the data layer stays free of visual concerns.
_TONE_DOT: dict[str, str] = {
    "info": "🔵",
    "warning": "🟠",
    "success": "🟢",
    "danger": "🔴",
    "neutral": "⚪",
}


def _status_cell(row: SessionRow) -> str:
    dot = _TONE_DOT.get(row.status_tone, "⚪")
    return f"{dot} {row.status_label}"


def _phase_cell(row: SessionRow) -> str:
    if not row.current_phase_label:
        return "—"
    return row.current_phase_label


def render_sessions_page() -> None:
    """Render the session list with filters and navigation hooks."""
    st.header("Sessions")

    with st.sidebar:
        st.subheader("Filtres")
        only_active = st.checkbox(
            "Sessions actives seulement",
            value=True,
            help="Cache les sessions approved / failed / queued.",
        )
        status_choices = sorted(STATUS_LABELS.keys())
        selected_statuses = st.multiselect(
            "Statut",
            options=status_choices,
            default=[],
            format_func=lambda s: STATUS_LABELS.get(s, s),
        )
        project_filter = st.text_input(
            "Projet (sous-chaîne de project_path)",
            value="",
            placeholder="sitavista",
        )
        sort_choice = st.radio(
            "Tri",
            options=["created_at_desc", "updated_at_desc"],
            format_func=lambda s: (
                "Création décroissante"
                if s == "created_at_desc"
                else "Dernière MAJ décroissante"
            ),
            index=1,
        )

    rows = list_sessions_for_dashboard(
        status=selected_statuses or None,
        project_path=None,  # filter client-side via substring below
        sort=sort_choice,
    )

    if only_active:
        rows = [r for r in rows if r.is_active]
    if project_filter:
        rows = [r for r in rows if project_filter.lower() in r.project_path.lower()]

    st.caption(
        f"{len(rows)} session(s) · dernière MAJ "
        f"{datetime.utcnow().strftime('%H:%M:%S')} UTC"
    )

    if not rows:
        st.info("Aucune session ne correspond aux filtres.")
        return

    # Render as a Streamlit data editor with clickable short IDs.
    # Streamlit does not natively support cell-click actions, so we emit
    # a grid of rows with a dedicated "Ouvrir" button per row.
    header_cols = st.columns([2, 5, 3, 3, 3, 2, 2])
    header_cols[0].markdown("**ID**")
    header_cols[1].markdown("**Titre**")
    header_cols[2].markdown("**Projet**")
    header_cols[3].markdown("**Statut**")
    header_cols[4].markdown("**Phase**")
    header_cols[5].markdown("**Âge**")
    header_cols[6].markdown("**Actions**")

    for row in rows:
        cols = st.columns([2, 5, 3, 3, 3, 2, 2])
        cols[0].code(row.id[:8], language=None)
        cols[1].write(row.title)
        cols[2].write(row.project_path.split("/")[-1] or row.project_path)
        cols[3].write(_status_cell(row))
        cols[4].write(_phase_cell(row))
        cols[5].write(row.age_fr)
        if cols[6].button("Ouvrir", key=f"open_{row.id}"):
            st.query_params["page"] = "session"
            st.query_params["id"] = row.id
            st.rerun()
