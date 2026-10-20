"""Questions and answers about the site, in one place: shown at the foot of the Data & methods
page and as a plain page of their own (/reports/faq/, static_site.py), which search engines and
AI answer engines can read (the site itself is drawn by JavaScript, which they mostly don't
run). Answers are plain text; `stats`, when given, adds the questions answered with the latest
numbers (the last 30 days)."""

from __future__ import annotations

from eugene_bus_reliability import analytics

GITHUB = "https://github.com/blemberger/eugene-bus-reliability"
CONTACT_EMAIL = "contact@benlemberger.com"


def _minutes(seconds: float) -> str:
    m = abs(float(seconds)) / 60
    return f"{m:.1f}" if m < 9.95 else f"{m:.0f}"


def _late(seconds: float, late: str = "late", early: str = "early") -> str:
    if round(abs(float(seconds)) / 60, 1) == 0:
        return "on time"
    return f"{_minutes(seconds)} min {late if seconds > 0 else early}"


def _range(lo: float, hi: float) -> str:
    def word(v: float) -> str:
        r = round(float(v) / 60)
        if r == 0:
            return "on time"
        return f"{abs(r)} min {'late' if r > 0 else 'early'}"

    return f"between {word(lo)} and {word(hi)}"


def stats_from_lateness(
    overall: dict | None, routes: list[dict], route_label, min_route_arrivals: int = 500
) -> dict:
    """questions()' "overall" and "routes" numbers from mart_lateness rows for the last 30
    days, all days: the row for everything, and one per route (route_id, n, mean_abs_delay_s);
    route_label(route_id) names a route. Routes with fewer than min_route_arrivals arrivals
    are left out of the steadiest and least steady."""
    stats: dict = {}
    if overall and overall.get("n"):
        n = float(overall["n"])
        stats["overall"] = {
            "median": overall["median_delay_s"],
            "p10": overall["p10_delay_s"],
            "p90": overall["p90_delay_s"],
            "mean_abs": overall["mean_abs_delay_s"],
        }
        if overall.get("n_on_time") is not None:
            stats["overall"] |= {
                "on_time": float(overall["n_on_time"]) / n,
                "early": float(overall["n_early"]) / n,
                "late": float(overall["n_late"]) / n,
            }
    busy = [
        r
        for r in routes
        if r.get("n") and r["n"] >= min_route_arrivals and r.get("mean_abs_delay_s") is not None
    ]
    if len(busy) >= 2:
        busy.sort(key=lambda r: float(r["mean_abs_delay_s"]))
        stats["routes"] = {
            "best": (route_label(busy[0]["route_id"]), float(busy[0]["mean_abs_delay_s"])),
            "worst": (route_label(busy[-1]["route_id"]), float(busy[-1]["mean_abs_delay_s"])),
        }
    return stats


def _pct(share: float) -> str:
    if 0 < share < 0.005:
        return "under 1%"
    return f"{round(100 * share)}%"


def questions(stats: dict | None = None) -> list[tuple[str, str]]:
    """(question, answer) pairs, the questions riders (and anyone judging the site) ask first
    first. stats (seconds and shares, last 30 days; see stats_from_lateness): overall: median,
    p10, p90, mean_abs, and on_time, early, late (shares); routes: best and worst (name,
    mean_abs); pred: median, p10, p90 (signed, at 5 minutes away), mean_abs, tt_mean_abs.
    Without them, the questions answered by numbers are left out or answered in general."""
    stats = stats or {}
    qa: list[tuple[str, str]] = []
    o = stats.get("overall")
    if o:
        qa.append(
            (
                "How late are LTD buses in Eugene and Springfield?",
                "Over the last 30 days, the typical Lane Transit District bus was "
                f"{_late(o['median'])} against the printed timetable, and 8 in 10 buses came "
                f"{_range(o['p10'], o['p90'])}. On average the buses were "
                f"{_minutes(o['mean_abs'])} minutes off the timetable, early or late alike. "
                "Every route and stop has its own numbers on this site.",
            )
        )
    if o and "on_time" in o:
        qa.append(
            (
                "What share of LTD buses are on time?",
                f"Over the last 30 days, {_pct(o['on_time'])} of buses reached their stops "
                "between 1 minute early and 5 minutes late, a definition of on time common "
                f"among U.S. transit agencies. {_pct(o['late'])} were more than 5 minutes late "
                f"and {_pct(o['early'])} more than 1 minute early. The site leads with minutes "
                "instead, because a share on time doesn't say how late the late buses were.",
            )
        )
    r = stats.get("routes")
    qa.append(
        (
            "Which LTD routes are the most reliable?",
            (
                f"Over the last 30 days, route {r['best'][0]} kept closest to its timetable "
                f"({_minutes(r['best'][1])} minutes off on average) and route {r['worst'][0]} "
                f"strayed furthest ({_minutes(r['worst'][1])} minutes off). "
                if r
                else ""
            )
            + "The Routes page ranks every route by how far its buses run from the timetable, "
            "and each route's page shows where along it and at what time of day the buses lose "
            "time.",
        )
    )
    early_tip = (
        f"Across Eugene and Springfield, 1 bus in 10 came more than "
        f"{max(1, round(abs(o['p10']) / 60))} min early over the last 30 days, so being at "
        f"the stop that many minutes before the scheduled time would have caught 9 buses in 10. "
        if o and o["p10"] < -30
        else ""
    )
    qa += [
        (
            "How early should I get to my bus stop?",
            early_tip + "Buses run differently from stop to stop: look up yours on the Stops "
            "page. Where its 8 in 10 buses range starts early, be there that many minutes "
            "before the scheduled time, and only 1 bus in 10 will have come earlier. The page "
            "also gives the range by route and by time of day.",
        ),
        (
            "Can an LTD bus come before its scheduled time?",
            "Yes. LTD's printed timetable has exact times only at timepoints, the few main "
            "stops on each route, and a bus that is early is meant to wait there until its time. The times at "
            "the stops in between are estimates, and a bus running ahead of them passes those "
            "stops early. Each stop's page shows how early its buses have come.",
        ),
    ]
    p = stats.get("pred")
    if p:
        qa.append(
            (
                "How accurate are LTD's real-time arrival predictions?",
                "Over the last 30 days, when LTD's prediction (what apps like Transit show) said "
                "a bus was 5 minutes away, the typical bus came "
                + (
                    "as predicted"
                    if round(abs(p["median"]) / 60, 1) == 0
                    else f"{_late(p['median'], 'later', 'earlier')} than predicted"
                )
                + ", and 8 in 10 buses came between "
                f"{_late(p['p10'], 'later', 'earlier')} and "
                f"{_late(p['p90'], 'later', 'earlier')} than predicted. On average the prediction "
                f"was {_minutes(p['mean_abs'])} minutes off, against "
                f"{_minutes(p['tt_mean_abs'])} minutes for the printed timetable on the same "
                "buses.",
            )
        )
    else:
        qa.append(
            (
                "How accurate are LTD's real-time arrival predictions?",
                "Every prediction LTD publishes (what apps like Transit show) is kept and "
                "compared with when the bus actually came, by how many minutes away the "
                "prediction said the bus was, beside the printed timetable for the same buses. "
                "The Predictions page shows it for every route and stop.",
            )
        )
    qa += [
        (
            "Where does the data come from?",
            "From Lane Transit District's own public data: its GTFS schedule and its "
            "GTFS-Realtime feeds of live bus positions, arrival predictions and service alerts, "
            "read every 30 seconds. Eugene Bus Watch is an independent project, not affiliated "
            "with LTD.",
        ),
        (
            "How is lateness measured?",
            "For every bus at every stop: the time it actually got there minus the time the "
            "printed timetable gives for that stop. LTD's live data doesn't say when a bus is at "
            "a stop, so that time is worked out from where the bus's GPS places it along its "
            "route. At timepoints and at a trip's first stop, where buses wait, the later of "
            "that and LTD's own recorded departure is used, since either can catch the bus "
            "before it actually leaves. Every stop counts, not only the timepoints, because "
            "riders wait at every stop.",
        ),
        (
            "What do typical bus, 8 in 10 buses and minutes off mean?",
            "Typical bus: the median, so half the buses were later than this and half earlier. "
            "8 in 10 buses: the range from the 10th to the 90th percentile; one bus in ten came "
            "earlier than its low end and one in ten later than its high end, so its width is "
            "how much to allow for. Minutes off: how far the buses were from the timetable, early "
            "or late alike, on average.",
        ),
        (
            "Is every bus measured?",
            "Every bus that appears in LTD's live data is measured at every stop it passes, "
            "with a few exceptions: when its time can't be pinned down to within 2 minutes (its "
            "GPS reports around that stop were too far apart), when it is more than 30 minutes "
            "early or 90 minutes late (in practice a bus reporting the wrong trip), and at a "
            "few stops where the GPS time is out of line with the stops either side trip after "
            "trip (usually a stop mapped away from where buses really stop). Trips that never "
            "appear in LTD's live data can't be measured. The Data & methods page shows how "
            "much of the timetable was measured each day.",
        ),
        (
            "How often is it updated?",
            "Live bus positions and predictions every 30 seconds; the numbers about lateness and "
            "predictions every 15 minutes.",
        ),
    ]
    if analytics.measurement_id():
        # Google Analytics' terms require telling visitors it is used and that it sets cookies
        qa.append(
            (
                "Does the site use cookies?",
                "Only Google Analytics', to count visits. Google's account of what it does with "
                "them: https://policies.google.com/technologies/partner-sites",
            )
        )
    qa += [
        (
            "Who makes Eugene Bus Watch?",
            "It is an independent project, not affiliated with Lane Transit District. The source "
            f"code is public: {GITHUB}. Questions, corrections or ideas: {CONTACT_EMAIL}",
        ),
    ]
    return qa
