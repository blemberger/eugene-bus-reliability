"""Give the page Streamlit serves a real title and description, for search engines and for
the preview a link shows when shared. Streamlit fills in each page's content with JavaScript
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
    index.write_text(html)
    print(f"{index}: title and description set")


if __name__ == "__main__":
    main()
