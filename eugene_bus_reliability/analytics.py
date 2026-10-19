"""Google Analytics, the outside measure of the site's traffic (the same one the owner's other
sites can report to), on every page of the interactive site and the plain report pages. On only
when GA_MEASUREMENT_ID (the "G-..." id of the site's Google Analytics data stream) is set in the
server's .env; nothing is loaded without it. A browser that opened the site with ?dont_count_me
(the owner's, see app/common.py not_counted) is left out, as WordPress stats leave out a logged-in
owner. Page changes inside the site are counted by Google Analytics itself ("page changes based on
browser history events", on by default for a web data stream)."""

from __future__ import annotations

import json
import os
import re

NOT_COUNTED_COOKIE = "ebw_not_counted"


def measurement_id() -> str | None:
    """The id from the environment, if it looks like one (G- and letters or digits)."""
    mid = (os.environ.get("GA_MEASUREMENT_ID") or "").strip()
    return mid if re.fullmatch(r"G-[A-Z0-9]{4,20}", mid) else None


def loader_js() -> str:
    """JavaScript that loads Google Analytics once per page load (safe to run again), unless
    this browser asked not to be counted. Empty without an id."""
    mid = measurement_id()
    if not mid:
        return ""
    m = json.dumps(mid)
    return (
        "(function () {"
        " if (window.__ebwAnalytics) return; window.__ebwAnalytics = true;"
        f" if (document.cookie.indexOf('{NOT_COUNTED_COOKIE}=1') >= 0"
        " || location.search.indexOf('dont_count_me') >= 0) return;"
        " var s = document.createElement('script'); s.async = true;"
        f" s.src = 'https://www.googletagmanager.com/gtag/js?id=' + {m};"
        " document.head.appendChild(s);"
        " window.dataLayer = window.dataLayer || [];"
        " window.gtag = function () { window.dataLayer.push(arguments); };"
        f" window.gtag('js', new Date()); window.gtag('config', {m});"
        " })();"
    )


def head_tag() -> str:
    """The same as a <script> for a plain HTML page's <head>; empty without an id."""
    js = loader_js()
    return f"<script>{js}</script>" if js else ""