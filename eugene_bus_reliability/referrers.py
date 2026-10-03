"""Where the site's visitors come from, from the web server's access log.

Reads Caddy's JSON access log on stdin (`make visitors` pipes it in) and prints, for the
last 30 days: page loads per day by people (bots left out), the other sites that sent
visitors here, and the pages people landed on. Only full page loads count, not the
site's scripts, images or its live connection. No IP addresses are printed.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import TextIO
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Los_Angeles")
BOTS = re.compile(
    r"bot|crawl|spider|slurp|headless|lighthouse|preview|monitor|python|curl|wget|scan", re.I
)
# Requests that are parts of a page rather than a page someone opened.
NOT_A_PAGE = re.compile(r"^/(_stcore|static|media|component|favicon|robots|\.well-known)|\.\w+$")


def first(headers: dict, name: str) -> str:
    values = headers.get(name) or []
    return values[0] if values else ""


def main(stream: TextIO = sys.stdin, days: int = 30) -> None:
    since = datetime.now(tz=UTC) - timedelta(days=days)
    per_day: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    landing: Counter[str] = Counter()
    bots = 0
    for line in stream:
        try:
            entry = json.loads(line)
            request = entry["request"]
            when = datetime.fromtimestamp(float(entry["ts"]), tz=UTC)
        except (ValueError, KeyError, TypeError):
            continue
        path = urlsplit(request.get("uri", "")).path or "/"
        if (
            when < since
            or request.get("method") != "GET"
            or entry.get("status") != 200
            or NOT_A_PAGE.search(path)
        ):
            continue
        headers = request.get("headers") or {}
        if BOTS.search(first(headers, "User-Agent")) or not first(headers, "User-Agent"):
            bots += 1
            continue
        per_day[when.astimezone(LOCAL_TZ).date().isoformat()] += 1
        landing[path] += 1
        referrer = urlsplit(first(headers, "Referer")).hostname or ""
        own = (request.get("host") or "").removeprefix("www.")
        if referrer and referrer.removeprefix("www.") != own:
            sources[referrer.removeprefix("www.")] += 1

    print(f"== Page loads per day, last {days} days (people; bots left out)")
    if not per_day:
        print("(none yet)")
    for day in sorted(per_day, reverse=True):
        print(f"{day}  {per_day[day]}")
    print("\n== Other sites that sent visitors here (a link someone clicked)")
    if not sources:
        print("(none yet: everyone typed the address, used a bookmark, or their browser hid it)")
    for site, n in sources.most_common(15):
        print(f"{n:6}  {site}")
    print("\n== Pages people landed on")
    for path, n in landing.most_common(10):
        print(f"{n:6}  {path}")
    print(f"\n(bots and crawlers left out: {bots} page loads)")
