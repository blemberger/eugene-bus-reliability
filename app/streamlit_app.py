"""Eugene Bus Watch (https://eugenebuswatch.com): top navigation across the pages.
Run locally with `make app`.

Every view is a script in app/views/; this file only wires them together.
"""

import os

import streamlit as st

st.set_page_config(page_title="Eugene Bus Watch", page_icon="🚌", layout="wide")

st.html(
    "<style>"
    "[data-testid='stCaptionContainer'] { font-size: 0.95rem; }"
    "[data-testid='stButtonGroup'] button { font-size: 1.05rem; min-height: 2.5rem;"
    " padding: 0.35rem 1rem; }"
    "@media (max-width: 640px) {"
    " [data-testid='stMainBlockContainer'] { padding: 3.5rem 0.8rem 3rem 0.8rem !important; }"
    " h1 { font-size: 1.75rem !important; line-height: 1.2 !important; }"
    " h2, h3 { font-size: 1.3rem !important; }"
    " [data-testid='stButtonGroup'] button { font-size: 0.95rem; min-height: 2.3rem;"
    " padding: 0.25rem 0.75rem; }"
    "}"
    "</style>"
)

pages = [
    st.Page("views/overview.py", title="Overview", icon="🚌", default=True),
    st.Page("views/map.py", title="Live map", icon="📍", url_path="map"),
    st.Page("views/arrivals.py", title="Arrivals", icon="🕒", url_path="arrivals"),
    st.Page("views/stops.py", title="Stops", icon="🚏", url_path="stops"),
    st.Page("views/routes.py", title="Routes", icon="🗺️", url_path="routes"),
    # one route's report card: no menu entry, reached from the Routes table or /route?route=...
    st.Page("views/route.py", title="Route", icon="🗺️", url_path="route", visibility="hidden"),
    st.Page("views/accuracy.py", title="Accuracy", icon="⏱️", url_path="accuracy"),
    st.Page("views/methods.py", title="Data & methods", icon="📐", url_path="methods"),
    st.Page("views/status.py", title="Status", icon="🔧", url_path="status"),
]
page = st.navigation(pages, position="top")
if os.environ.get("DATABASE_URL"):
    from common import feed_banner, log_page_view

    feed_banner()
    log_page_view(page.url_path or "overview")
page.run()
