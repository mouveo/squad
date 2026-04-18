"""Streamlit entry point for the Squad local dashboard.

Launched by ``squad dashboard`` via ``sys.executable -m streamlit run``.
This LOT 1 skeleton only validates the wiring (title + session count);
routing, session list, detail and plan review are added in later lots.
"""

import streamlit as st

from squad.dashboard.data import count_sessions

st.set_page_config(page_title="Squad Dashboard", layout="wide")
st.title("Squad Dashboard")

total = count_sessions()
st.metric("Sessions totales", total)
st.info("Dashboard local opérationnel — câblage validé.")
