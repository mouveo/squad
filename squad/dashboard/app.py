"""Streamlit entry point for the Squad local dashboard.

Launched by ``squad dashboard`` via ``sys.executable -m streamlit run``.
Routing is handled via ``st.query_params`` with three pages :

* ``?page=sessions`` (default) — list of all sessions with filters
* ``?page=session&id=<uuid>`` — aggregated view of a single session
* ``?page=plans&id=<uuid>`` — plans review with approve / reject buttons

All write actions (approve, reject) reuse the same business services
as the Slack handlers (``squad.forge_bridge.approve_and_submit``,
``squad.review_service.reject_session``) so the dashboard and Slack
are never out of sync.
"""

from __future__ import annotations

import streamlit as st

from squad.dashboard.data import count_sessions
from squad.dashboard.plans_review_page import render_plans_review_page
from squad.dashboard.session_detail_page import render_session_detail_page
from squad.dashboard.sessions_page import render_sessions_page

_PAGE_SESSIONS = "sessions"
_PAGE_SESSION = "session"
_PAGE_PLANS = "plans"
_KNOWN_PAGES = {_PAGE_SESSIONS, _PAGE_SESSION, _PAGE_PLANS}


def _resolve_page() -> tuple[str, str | None]:
    """Read the current page + session id from query params, with defaults.

    Unknown page values redirect to the sessions list. The query params
    are not mutated here — the caller decides whether to rewrite them.
    Streamlit exposes values as ``str`` or ``list[str]`` depending on
    repetition; both shapes are normalised.
    """
    raw_page = st.query_params.get("page")
    page = raw_page[0] if isinstance(raw_page, list) and raw_page else raw_page
    raw_id = st.query_params.get("id")
    session_id = raw_id[0] if isinstance(raw_id, list) and raw_id else raw_id
    if page not in _KNOWN_PAGES:
        page = _PAGE_SESSIONS
    return page, session_id


def _render_sidebar(total_sessions: int) -> None:
    with st.sidebar:
        st.markdown("## 🎯 Squad")
        if st.button("📋 Sessions", key="nav_sessions"):
            st.query_params.clear()
            st.query_params["page"] = _PAGE_SESSIONS
            st.rerun()
        st.metric("Total sessions", total_sessions)
        if st.button("🔄 Rafraîchir", key="refresh_sidebar"):
            st.rerun()
        st.caption("Dashboard local — Squad")


def main() -> None:
    st.set_page_config(page_title="Squad Dashboard", layout="wide")

    total = count_sessions()
    _render_sidebar(total)

    page, session_id = _resolve_page()
    if page == _PAGE_SESSION:
        render_session_detail_page(session_id)
    elif page == _PAGE_PLANS:
        render_plans_review_page(session_id)
    else:
        render_sessions_page()


main()
