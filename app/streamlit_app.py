"""Eugene Bus Watch (https://eugenebuswatch.com): top navigation across the pages.
Run locally with `make app`.

Every view is a script in app/views/; this file only wires them together.
"""

import logging
import os
import time
from pathlib import Path

import streamlit as st

from eugene_bus_reliability import analytics

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
    "header[data-testid='stHeader'] { height: 4.2rem; border-bottom: 1px solid #d6dbe1;"
    " background: #ffffff; }"
    "header [data-testid='stToolbar'] { height: 4.2rem; align-items: center; }"
    ".stLogo { height: 2.1rem; max-width: none; margin: 0 1.4rem 0 0.6rem; }"
    "[data-testid='stTopNavLink'] { padding: 0.3rem 0.75rem; }"
    "[data-testid='stTopNavLink'] p { font-size: 1.06rem; font-weight: 500; }"
    "[data-testid='stTopNavLink'][aria-current='page'] p { color: #1f5f9e; font-weight: 650; }"
    # Streamlit's own menu (print, record a screencast, settings): not something visitors use,
    "[data-testid='stMainMenu'] { display: none; }"
    # nor the "running / Stop" indicator, which on the live pages flickered every few seconds
    "[data-testid='stStatusWidget'] { display: none; }"
    # instead, a thin moving bar along the top of the window while a page takes more than a
    # moment to draw (the live pages' quick refreshes finish before it would show)
    ".stApp[data-test-script-state='running']::before { content: ''; position: fixed; top: 0;"
    " left: 0; width: 40%; height: 3px; background: #1f5f9e; z-index: 1000002; opacity: 0;"
    " animation: ebw-loading 1.3s linear 0.7s infinite; }"
    "@keyframes ebw-loading { 0% { left: -40%; opacity: 1; } 100% { left: 100%; opacity: 1; } }"
    "[data-testid='stMainBlockContainer'] { padding-top: 6.2rem; }"
    # pages read as a stack of cards on a pale background
    "[data-testid='stMain'] { background: #f3f5f7; }"
    "[class*='st-key-card_'] {"
    " background: #ffffff; box-shadow: 0 1px 3px rgba(20, 30, 45, 0.08);"
    " padding: 1.1rem 1.3rem 1.2rem 1.3rem; }"
    "[data-testid='stCaptionContainer'] { font-size: 0.95rem; }"
    # bigger buttons and button rows
    "[data-testid='stButtonGroup'] button { font-size: 1.05rem; min-height: 2.6rem;"
    " padding: 0.4rem 1.05rem; }"
    "[data-testid^='stBaseButton'] { min-height: 2.75rem; font-size: 1.05rem;"
    " padding: 0.45rem 1.1rem; }"
    # a link to another page ("All 43 stops →", "Route 11's report card →"): an outlined button
    # in the green of every such link (common.GO_COLOUR); the way back below is the same shape
    # in blue
    "[data-testid='stPageLink'] a { display: inline-flex; width: auto;"
    " border: 2px solid #1d6b48; border-radius: 8px; background: #ffffff;"
    " padding: 0.35rem 1rem; min-height: 2.6rem; }"
    "[data-testid='stPageLink'] a:hover { background: #e8f3ee; }"
    # Everything that can be clicked answers the pointer: a link button or card lifts a little
    # with a soft shadow under the mouse and sinks when pressed (on a phone, the press is the
    # only feedback a tap gets), and the keyboard's focus shows as a clear ring. No movement
    # for visitors who ask their system for less motion.
    "[data-testid='stPageLink'] a, [class*='st-key-backlink'] a,"
    " [class*='st-key-backlink'] button, [data-testid='stBaseButton-secondary'], [data-testid='stBaseButton-primary'] {"
    " transition: background-color 0.12s, box-shadow 0.12s, transform 0.12s; }"
    "[data-testid='stPageLink'] a:hover, [class*='st-key-backlink'] a:hover,"
    " [class*='st-key-backlink'] button:hover, [data-testid='stBaseButton-secondary']:hover, [data-testid='stBaseButton-primary']:hover {"
    " box-shadow: 0 3px 8px rgba(20, 30, 45, 0.16); transform: translateY(-1px); }"
    "[data-testid='stPageLink'] a:active, [class*='st-key-backlink'] a:active,"
    " [class*='st-key-backlink'] button:active, [data-testid='stBaseButton-secondary']:active, [data-testid='stBaseButton-primary']:active {"
    " box-shadow: 0 1px 2px rgba(20, 30, 45, 0.18); transform: translateY(1px); }"
    "[data-testid='stPageLink'] a:focus-visible, [class*='st-key-backlink'] a:focus-visible {"
    " outline: 3px solid rgba(31, 95, 158, 0.45); outline-offset: 2px; }"
    # the period, days, route and direction buttons: a press darkens them for a moment
    "[data-testid='stButtonGroup'] button { transition: background-color 0.12s, box-shadow 0.12s; }"
    "[data-testid='stButtonGroup'] button:hover { box-shadow: 0 2px 6px rgba(20, 30, 45, 0.14); }"
    "[data-testid='stButtonGroup'] button:active { box-shadow: inset 0 2px 4px rgba(20, 30, 45, 0.2); }"
    "@media (prefers-reduced-motion: reduce) {"
    " [data-testid='stPageLink'] a, [class*='st-key-backlink'] a,"
    " [class*='st-key-backlink'] button, [data-testid='stBaseButton-secondary'], [data-testid='stBaseButton-primary'], .ebw-tile, a.ebw-r"
    " { transition: none !important; transform: none !important; } }"
    "[data-testid='stPageLink'] p { font-size: 1.05rem; font-weight: 650; color: #1d6b48; }"
    # Streamlit's link-to-this-heading icon (a chain link shown on hover): headings change with
    # the choices on a page, so such a link would break; the ? help stays
    "[data-testid='stHeaderActionElements'] a[aria-label='Link to heading'] { display: none; }"
    # a link that opens a page at a section (#predictions) leaves its heading clear of the top bar
    "h3[id] { scroll-margin-top: 5.5rem; }"
    # the period and days buttons repeated further down a long page (common.filters_again): set
    # apart a little from the card above
    "[class*='st-key-filters_again'] { margin-top: 0.6rem; }"
    # the label above every control (Stop, Period, Days, Direction, ...): bold, so it reads as
    # the control's name rather than as text
    "[data-testid='stWidgetLabel'] p { font-size: 1rem !important; font-weight: 650;"
    " color: #262730; }"
    # the way back from a report card to every route or stop (common.back_button): an outlined
    # button in the site's blue, the first thing on the page
    "[class*='st-key-backlink'] a, [class*='st-key-backlink'] button {"
    " display: inline-flex; width: auto; border: 2px solid #1f5f9e !important;"
    " border-radius: 8px; background: #ffffff; padding: 0.4rem 1.1rem !important;"
    " min-height: 2.8rem; }"
    "[class*='st-key-backlink'] a:hover, [class*='st-key-backlink'] button:hover {"
    " background: #eaf1f9; }"
    "[class*='st-key-backlink'] p { font-size: 1.12rem !important; font-weight: 700;"
    " color: #1f5f9e; }"
    # the menu on a narrow screen (Streamlit folds the top links into a side drawer): the usual
    # three-line menu button at the far left of the bar, before the site's name, opening the
    # drawer from that side; in the drawer a plain X closes it, and the links are big enough
    # to tap
    "div:has(> [data-testid='stExpandSidebarButton']) { order: -1; margin-right: 0.5rem; }"
    "[data-testid='stExpandSidebarButton'] { width: 2.6rem; height: 2.6rem;"
    " border: 1px solid #cfd6de; border-radius: 8px;"
    " background: #ffffff url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M3 6h18M3 12h18M3 18h18' stroke='%231b2533' stroke-width='2.2' stroke-linecap='round'/%3E%3C/svg%3E\") center / 1.4rem no-repeat; }"
    "[data-testid='stExpandSidebarButton'] > *, [data-testid='stSidebarCollapseButton'] button > *"
    " { display: none; }"
    "[data-testid='stSidebarCollapseButton'] button { width: 2.6rem; height: 2.6rem;"
    " background: url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M6 6l12 12M18 6L6 18' stroke='%231b2533' stroke-width='2.2' stroke-linecap='round'/%3E%3C/svg%3E\") center / 1.3rem no-repeat; }"
    "[data-testid='stSidebarNavLink'] { min-height: 3rem; }"
    "[data-testid='stSidebarNavLink'] span { font-size: 1.1rem; }"
    "@media (max-width: 640px) {"
    " .stLogo { height: 1.7rem; margin: 0 0.4rem 0 0.2rem; }"
    " header[data-testid='stHeader'], header [data-testid='stToolbar'] { height: 3.8rem; }"
    " [data-testid='stMainBlockContainer'] { padding: 4.8rem 0.6rem 3rem 0.6rem !important; }"
    " [class*='st-key-card_'] {"
    " padding: 0.8rem 0.8rem 0.9rem 0.8rem; }"
    " h1 { font-size: 1.75rem !important; line-height: 1.2 !important; }"
    " h2, h3 { font-size: 1.3rem !important; }"
    " [data-testid='stPageLink'] p { white-space: normal; }"
    # the four headline numbers two by two, not one per row down the screen
    " [data-testid='stHorizontalBlock']:has([data-testid='stMetric']) { flex-wrap: wrap;"
    " flex-direction: row; gap: 0.6rem 0.8rem; }"
    " [data-testid='stHorizontalBlock']:has([data-testid='stMetric']) > [data-testid='stColumn']"
    " { flex: 1 1 calc(50% - 0.8rem) !important; width: calc(50% - 0.8rem) !important;"
    " min-width: calc(50% - 0.8rem) !important; }"
    " [data-testid='stMetricValue'] { font-size: 1.6rem; }"
    " [data-testid='stMetricLabel'] p, [data-testid='stMetricLabel'] div"
    " { white-space: normal !important; overflow: visible !important; }"
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
# the browser tab's (and a search result's) title: the question each page answers, in the
# words people search with; a stop's or a route's report card sets its own
TAB_TITLES = {
    "": "Eugene Bus Watch: how reliable are Eugene's buses?",
    "map": "Where are Eugene's buses right now? Live LTD bus map · Eugene Bus Watch",
    "stops": "How reliable is my bus stop? Every LTD stop · Eugene Bus Watch",
    "routes": "How reliable is my bus route? Every LTD route · Eugene Bus Watch",
    "accuracy": "How accurate are LTD's arrival predictions? · Eugene Bus Watch",
    "methods": "How we measure LTD's buses: data & methods · Eugene Bus Watch",
}
st.set_page_config(
    page_title=TAB_TITLES.get(page.url_path or "", f"{page.title} · Eugene Bus Watch")
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
# Google Analytics, when the server's .env names the site's id (eugene_bus_reliability/analytics.py)
if analytics.loader_js():
    st.html(f"<script>{analytics.loader_js()}</script>", unsafe_allow_javascript=True)
if os.environ.get("DATABASE_URL"):
    from common import feed_banner, log_page_view, start_cache_warmer

    start_cache_warmer()
    feed_banner()
    # the page view is counted once the page is drawn, with how long that took
    t0 = time.monotonic()
    try:
        page.run()
    finally:
        log_page_view(page.url_path or "overview", int(1000 * (time.monotonic() - t0)))
else:
    page.run()
