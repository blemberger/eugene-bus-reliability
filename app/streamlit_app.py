"""Eugene Bus Watch (https://eugenebuswatch.com): top navigation across the pages.
Run locally with `make app`.

Every view is a script in app/views/; this file only wires them together.
"""

import os

import streamlit as st

st.set_page_config(page_title="Eugene Bus Watch", page_icon="🚌", layout="wide")

# Look and feel. Streamlit has no setting for most of this, so it's CSS against the
# data-testid names Streamlit puts on its elements (checked against Streamlit 1.64).
st.html(
    "<style>"
    # Streamlit fades whatever is being recomputed; on pages that update every few seconds
    # (and when a bus is clicked) that made the whole map flash. Keep everything solid.
    "[data-stale='true'] { opacity: 1 !important; transition: none !important; }"
    # header: the site's name on the left, larger menu links, a line underneath
    "header[data-testid='stHeader'] { height: 4.6rem; border-bottom: 1px solid #d5dbd8;"
    " background: #ffffff; }"
    "header [data-testid='stToolbar'] { height: 4.6rem; align-items: center; }"
    "header [data-testid='stToolbar'] > div:first-child > div:first-child::before {"
    " content: 'Eugene Bus Watch'; font-weight: 800; font-size: 1.45rem; color: #0b6e4f;"
    " letter-spacing: -0.01em; white-space: nowrap; margin: 0 1.6rem 0 1.2rem; }"
    "[data-testid='stTopNavLink'] { padding: 0.35rem 0.8rem; }"
    "[data-testid='stTopNavLink'] p { font-size: 1.3rem; font-weight: 600; }"
    "[data-testid='stTopNavLink'][aria-current='page'] p { color: #0b6e4f; }"
    "[data-testid='stMainBlockContainer'] { padding-top: 7rem; }"
    # pages read as a stack of cards on a pale background
    "[data-testid='stMain'] { background: #f3f5f4; }"
    "[data-testid='stLayoutWrapper'] > [data-testid='stVerticalBlock'] {"
    " background: #ffffff; box-shadow: 0 1px 3px rgba(20, 40, 30, 0.08);"
    " padding: 1.1rem 1.3rem 1.2rem 1.3rem; }"
    "[data-testid='stCaptionContainer'] { font-size: 0.95rem; }"
    # bigger buttons and button rows
    "[data-testid='stButtonGroup'] button { font-size: 1.05rem; min-height: 2.6rem;"
    " padding: 0.4rem 1.05rem; }"
    "[data-testid^='stBaseButton'] { min-height: 2.75rem; font-size: 1.05rem;"
    " padding: 0.45rem 1.1rem; }"
    "[data-testid='stPageLink'] p { font-size: 1.05rem; font-weight: 600; }"
    "@media (max-width: 640px) {"
    " header [data-testid='stToolbar'] > div:first-child > div:first-child::before {"
    " font-size: 1.15rem; margin: 0 0.4rem 0 0.2rem; }"
    " header[data-testid='stHeader'], header [data-testid='stToolbar'] { height: 3.8rem; }"
    " [data-testid='stMainBlockContainer'] { padding: 4.8rem 0.6rem 3rem 0.6rem !important; }"
    " [data-testid='stLayoutWrapper'] > [data-testid='stVerticalBlock'] {"
    " padding: 0.8rem 0.8rem 0.9rem 0.8rem; }"
    " h1 { font-size: 1.75rem !important; line-height: 1.2 !important; }"
    " h2, h3 { font-size: 1.3rem !important; }"
    " [data-testid='stPageLink'] p { white-space: normal; }"
    " [data-testid='stButtonGroup'] button { font-size: 0.98rem; min-height: 2.4rem;"
    " padding: 0.3rem 0.8rem; }"
    "}"
    "</style>"
)

pages = [
    st.Page("views/overview.py", title="Overview", default=True),
    st.Page("views/map.py", title="Live map", url_path="map"),
    st.Page("views/stops.py", title="Stops", url_path="stops"),
    st.Page("views/routes.py", title="Routes", url_path="routes"),
    # one route's report card: no menu entry, reached from the Routes table or /route?route=...
    st.Page("views/route.py", title="Route", url_path="route", visibility="hidden"),
    # the countdown signs' accuracy; the address stays /accuracy so old links still work
    st.Page("views/accuracy.py", title="Countdown", url_path="accuracy"),
    st.Page("views/methods.py", title="Data & methods", url_path="methods"),
    # behind the scenes, linked from Data & methods rather than the menu
    st.Page("views/arrivals.py", title="Arrivals board", url_path="arrivals", visibility="hidden"),
    st.Page("views/status.py", title="Status", url_path="status", visibility="hidden"),
]
page = st.navigation(pages, position="top")
if os.environ.get("DATABASE_URL"):
    from common import feed_banner, log_page_view

    feed_banner()
    log_page_view(page.url_path or "overview")
page.run()
