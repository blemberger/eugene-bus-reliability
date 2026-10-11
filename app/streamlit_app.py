"""Eugene Bus Watch (https://eugenebuswatch.com): top navigation across the pages.
Run locally with `make app`.

Every view is a script in app/views/; this file only wires them together.
"""

import logging
import os
from pathlib import Path

import streamlit as st

# the background cache warmer (common.warm_caches) runs cached queries outside a page, which
# Streamlit would otherwise warn about on every call
logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context").setLevel(logging.ERROR)

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
    # buttons that pick a stop on the page (common.stop_buttons): drawn like the stop cards
    "[class*='st-key-stopbtn_'] button { justify-content: flex-start; text-align: left;"
    " border-left: 4px solid #0b6e4f; border-radius: 6px; padding: 0.45rem 0.65rem;"
    " min-height: 0; }"
    "[class*='st-key-stopbtn_'] button > div { justify-content: flex-start; }"
    "[class*='st-key-stopbtn_'] button p { text-align: left; }"
    "[class*='st-key-stopbtn_'] button * { white-space: normal !important;"
    " overflow: visible !important; text-overflow: clip !important; }"
    # the label above every control (Stop, Period, Days, Direction, ...): bold, so it reads as
    # the control's name rather than as text
    "[data-testid='stWidgetLabel'] p { font-size: 1rem !important; font-weight: 650;"
    " color: #262730; }"
    # the way back from a report card to every route or stop (common.back_button): an outlined
    # green button, the first thing on the page
    "[class*='st-key-backlink'] a, [class*='st-key-backlink'] button {"
    " display: inline-flex; width: auto; border: 2px solid #0b6e4f !important;"
    " border-radius: 8px; background: #ffffff; padding: 0.4rem 1.1rem !important;"
    " min-height: 2.8rem; }"
    "[class*='st-key-backlink'] a:hover, [class*='st-key-backlink'] button:hover {"
    " background: #eef6f2; }"
    "[class*='st-key-backlink'] p { font-size: 1.12rem !important; font-weight: 700;"
    " color: #0b6e4f; }"
    # route and line buttons (common.route_picker, line_choice, route_toggles): "All routes"
    # first, set apart from the rest; the same whether one or several can be on at once
    "[class*='st-key-rp_'] [data-testid='stButtonGroup'] [data-orientation] > button:first-child {"
    " border: 1.5px solid #0b6e4f; margin-right: 0.75rem; }"
    "[class*='st-key-rp_'] [data-testid='stButtonGroup'] [data-orientation] > button:first-child p"
    " { font-weight: 700; }"
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
    # Two links Streamlit gets wrong, put right in the browser:
    # - the site's name links to the home page, but on the home page Streamlit opens it in a
    #   new tab; open it in the same tab, as every other link to a page on this site does;
    # - the Stops tab, clicked while a stop's report card is open, does nothing (it is already
    #   the Stops page); go to the list of every stop, as it does from any other page.
    "<script>"
    "if (!window.ebwLinks) { window.ebwLinks = true;"
    " document.addEventListener('click', (e) => {"
    "  if (e.metaKey || e.ctrlKey || e.shiftKey || !e.target.closest) return;"
    "  const logo = e.target.closest('a[data-testid=\"stLogoLink\"]');"
    "  if (logo) { e.preventDefault(); window.location.assign(logo.href); return; }"
    "  const tab = e.target.closest('[data-testid=\"stTopNavLink\"]');"
    "  if (tab && tab.href) {"
    "   const here = new URL(window.location.href), there = new URL(tab.href);"
    "   if (here.pathname === there.pathname && here.searchParams.has('stop')) {"
    "    e.preventDefault(); e.stopPropagation(); here.searchParams.delete('stop');"
    "    window.location.assign(here.toString()); }"
    "  }"
    " }, true); }"
    "</script>",
    unsafe_allow_javascript=True,
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
page = st.navigation(pages, position="top")
# the browser tab's (and a search result's) title; a stop's or a route's report card sets its own
st.set_page_config(
    page_title="Eugene Bus Watch: how reliable are Eugene's buses?"
    if page.url_path in ("", None)
    else f"{page.title} · Eugene Bus Watch"
)
# the site's name in the header goes to the home page. On the other pages Streamlit does that
# itself for a logo without a link of its own; on the home page it would be a dead picture, so
# there it's a plain link to the home page (which reloads it).
home = None
if page.url_path in ("", None):
    try:
        host = st.context.headers.get("Host")
        scheme = st.context.headers.get("X-Forwarded-Proto", "http")
        home = f"{scheme}://{host}/" if host else None
    except Exception:  # noqa: BLE001 — no browser (tests, scripts)
        home = None
st.logo(str(Path(__file__).parent / "static" / "wordmark.svg"), size="large", link=home)
if os.environ.get("DATABASE_URL"):
    from common import feed_banner, log_page_view, start_cache_warmer

    start_cache_warmer()
    feed_banner()
    log_page_view(page.url_path or "overview")
page.run()
