"""Eugene Bus Watch (https://eugenebuswatch.com): top navigation across the pages.
Run locally with `make app`.

Every view is a script in app/views/; this file only wires them together.
"""

import os
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="Eugene Bus Watch", page_icon="🚌", layout="wide")

# Look and feel. Streamlit has no setting for most of this, so it's CSS against the
# data-testid names Streamlit puts on its elements (checked against Streamlit 1.64).
st.html(
    "<style>"
    # Streamlit fades whatever is being recomputed; on pages that update every few seconds
    # (and when a bus is clicked) that made the whole map flash. Keep everything solid.
    "[data-stale='true'] { opacity: 1 !important; transition: none !important; }"
    # header: the site's name (a link to the home page, see st.logo below) on the left, the
    # page links beside it at body-text size, a line underneath
    "header[data-testid='stHeader'] { height: 4.2rem; border-bottom: 1px solid #d5dbd8;"
    " background: #ffffff; }"
    "header [data-testid='stToolbar'] { height: 4.2rem; align-items: center; }"
    ".stLogo { height: 2.1rem; max-width: none; margin: 0 1.4rem 0 0.6rem; }"
    "[data-testid='stTopNavLink'] { padding: 0.3rem 0.75rem; }"
    "[data-testid='stTopNavLink'] p { font-size: 1.06rem; font-weight: 500; }"
    "[data-testid='stTopNavLink'][aria-current='page'] p { color: #0b6e4f; font-weight: 650; }"
    # Streamlit's own menu (print, record a screencast, settings): not something visitors use,
    "[data-testid='stMainMenu'] { display: none; }"
    # nor the "running / Stop" indicator, which on the live pages flickered every few seconds
    "[data-testid='stStatusWidget'] { display: none; }"
    "[data-testid='stMainBlockContainer'] { padding-top: 6.2rem; }"
    # pages read as a stack of cards on a pale background
    "[data-testid='stMain'] { background: #f3f5f4; }"
    "[class*='st-key-card_'] {"
    " background: #ffffff; box-shadow: 0 1px 3px rgba(20, 40, 30, 0.08);"
    " padding: 1.1rem 1.3rem 1.2rem 1.3rem; }"
    "[data-testid='stCaptionContainer'] { font-size: 0.95rem; }"
    # bigger buttons and button rows
    "[data-testid='stButtonGroup'] button { font-size: 1.05rem; min-height: 2.6rem;"
    " padding: 0.4rem 1.05rem; }"
    "[data-testid^='stBaseButton'] { min-height: 2.75rem; font-size: 1.05rem;"
    " padding: 0.45rem 1.1rem; }"
    "[data-testid='stPageLink'] p { font-size: 1.05rem; font-weight: 600; }"
    # the way back from a report card to its list (common.back_link, and the Stops page's
    # button that does the same)
    "[class*='st-key-backlink'] p { font-size: 1.2rem !important; font-weight: 650;"
    " color: #0b6e4f; }"
    "[class*='st-key-backlink'] button { padding-left: 0; min-height: 2rem; }"
    "[class*='st-key-backlink'] a { padding-left: 0; }"
    # route buttons (common.route_picker): "All routes" first, set apart from the route numbers
    "[class*='st-key-rp_'] [role='radiogroup'] > button:first-child {"
    " border: 1.5px solid #0b6e4f; margin-right: 0.75rem; }"
    "[class*='st-key-rp_'] [role='radiogroup'] > button:first-child p { font-weight: 700; }"
    "@media (max-width: 640px) {"
    " .stLogo { height: 1.7rem; margin: 0 0.4rem 0 0.2rem; }"
    " header[data-testid='stHeader'], header [data-testid='stToolbar'] { height: 3.8rem; }"
    " [data-testid='stMainBlockContainer'] { padding: 4.8rem 0.6rem 3rem 0.6rem !important; }"
    " [class*='st-key-card_'] {"
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
    # how far off LTD's real-time predictions are; the address stays /accuracy so old links
    # still work
    st.Page("views/accuracy.py", title="Predictions", url_path="accuracy"),
    st.Page("views/methods.py", title="Data & methods", url_path="methods"),
    # behind the scenes, linked from Data & methods rather than the menu
    st.Page("views/arrivals.py", title="Arrivals board", url_path="arrivals", visibility="hidden"),
    st.Page("views/status.py", title="Status", url_path="status", visibility="hidden"),
]
# the site's name in the header; clicking it goes to the home page (Streamlit does that for a
# logo without its own link)
st.logo(str(Path(__file__).parent / "static" / "wordmark.svg"), size="large")
page = st.navigation(pages, position="top")
if os.environ.get("DATABASE_URL"):
    from common import feed_banner, log_page_view

    feed_banner()
    log_page_view(page.url_path or "overview")
page.run()
