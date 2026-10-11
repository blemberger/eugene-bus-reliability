"""Give the page Streamlit serves a real title and description, for search engines and for
the preview a link shows when shared, and something visible while the app starts. Streamlit fills in each page's content with JavaScript
after loading, so these lines in the HTML itself are what a crawler or a link preview reads.

Run once when the dashboard image is built (Dockerfile.app); safe to run again.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

import streamlit

TITLE = "Eugene Bus Watch: how reliable are Eugene's buses?"
DESCRIPTION = (
    "How late Lane Transit District buses really run in Eugene and Springfield, Oregon: every "
    "route and stop measured against the timetable, hour by hour, plus how far off LTD's "
    "real-time arrival predictions are. Independent; updated every 15 minutes."
)
HEAD = (
    f"<title>{escape(TITLE)}</title>\n"
    f'    <meta name="description" content="{escape(DESCRIPTION)}" />\n'
    f'    <meta property="og:title" content="{escape(TITLE)}" />\n'
    f'    <meta property="og:description" content="{escape(DESCRIPTION)}" />\n'
    '    <meta property="og:type" content="website" />\n'
    '    <meta property="og:site_name" content="Eugene Bus Watch" />'
)
# Shown the moment the HTML arrives, until Streamlit draws the page over it (React replaces
# whatever is inside the root element). Without it the page is blank for the seconds Streamlit
# takes to start, which speed tests report as "no first contentful paint" (NO_FCP).
SHELL = (
    "<div id=\"root\"><div style=\"font-family:'Source Sans','Source Sans Pro',sans-serif;"
    'max-width:46rem;margin:0 auto;padding:4.5rem 1rem 1rem;color:#262730">'
    '<div style="font-size:1.6rem;font-weight:700">Eugene Bus Watch</div>'
    '<p style="font-size:1rem;color:#5f6368">How reliable are Eugene\'s buses? '
    "Loading the latest data&hellip;</p></div></div>"
)
NOSCRIPT = (
    f"<noscript><h1>{escape(TITLE)}</h1><p>{escape(DESCRIPTION)}</p>"
    "<p>This site needs JavaScript.</p></noscript>"
)


def main() -> None:
    index = Path(streamlit.__file__).parent / "static" / "index.html"
    html = index.read_text()
    html = html.replace("<title>Streamlit</title>", HEAD)
    html = html.replace(
        "<noscript>You need to enable JavaScript to run this app.</noscript>", NOSCRIPT
    )
    if '<div id="root"></div>' in html:
        html = html.replace('<div id="root"></div>', SHELL)
    index.write_text(html)
    print(f"{index}: title, description and loading shell set")


if __name__ == "__main__":
    main()
