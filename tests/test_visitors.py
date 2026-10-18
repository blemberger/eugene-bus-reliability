import io
import json
from datetime import UTC, date, datetime

from eugene_bus_reliability.visitors import access_log, text_bars


def test_text_bars_visitors_inside_views():
    rows = [
        {"start": date(2026, 10, 16), "views": 40, "visitors": 10},
        {"start": date(2026, 10, 17), "views": 20, "visitors": None},
    ]
    lines = text_bars(rows, "day", width=40)
    assert "█" * 10 + "░" * 30 in lines[1]
    assert lines[1].rstrip().endswith("10")
    assert "░" * 20 in lines[2] and "█" not in lines[2]
    assert lines[2].rstrip().endswith("–")


def test_access_log_counts_people_not_bots_or_parts():
    now = datetime.now(tz=UTC).timestamp()

    def line(uri, ua="Mozilla/5.0", referer=None, status=200):
        headers = {"User-Agent": [ua]}
        if referer:
            headers["Referer"] = [referer]
        return json.dumps(
            {
                "ts": now,
                "status": status,
                "request": {"uri": uri, "method": "GET", "host": "x.com", "headers": headers},
            }
        )

    log = "\n".join(
        [
            line("/", referer="https://www.reddit.com/r/Eugene"),
            line("/reports/route/11/"),
            line("/static/js/main.js"),
            line("/", ua="Googlebot/2.1"),
            line("/stops", referer="https://x.com/route"),
        ]
    )
    d = access_log(io.StringIO(log))
    assert d["sources"] == {"reddit.com": 1}
    assert d["report_pages"] == {"/reports/route/11/": 1}
    assert d["landing"]["/"] == 1
