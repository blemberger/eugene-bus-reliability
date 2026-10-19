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


def questions(stats: dict | None = None) -> list[tuple[str, str]]:
    """(question, answer) pairs. stats (seconds, last 30 days): overall: median, p10, p90,
    mean_abs; pred: median, p10, p90 (signed, at 5 minutes away), mean_abs, tt_mean_abs."""
    qa: list[tuple[str, str]] = []
    o = (stats or {}).get("overall")
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
            "printed timetable gives for that stop. LTD's live bus positions don't say when a bus "
            "is at a stop, so the time is worked out from where the bus's GPS places it along "
            "its route. At timepoints (the few main stops on each route with an exact time in "
            "LTD's printed timetable, where an early bus waits for its time) and at a trip's "
            "first stop, the later of that and LTD's own recorded departure is used, since "
            "either can catch the bus before it actually leaves. Every stop counts, not only the "
            "timepoints, because riders wait at every stop.",
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
            "Why minutes instead of a percentage on time?",
            "A share of buses on time depends on where the line is drawn (one minute early? "
            "five minutes late?) and says nothing about how late the late buses were. Minutes "
            "against the timetable, with the range 8 in 10 buses came in, show both how late "
            "the buses usually are and how much to allow for.",
        ),
    ]
    p = (stats or {}).get("pred")
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
                "Every prediction LTD publishes is kept and compared with when the bus actually "
                "came, by how many minutes away the prediction said the bus was, beside the "
                "printed timetable for the same buses. The Predictions page shows it for every "
                "route and stop.",
            )
        )
    qa += [
        (
            "How often is it updated?",
            "Live bus positions and predictions every 30 seconds; the numbers about lateness and "
            "predictions every 15 minutes.",
        ),
        (
            "Why are some arrivals or stops left out?",
            "An arrival is left out when its time can't be pinned down to within 2 minutes "
            "(the bus's reports around that stop were too far apart), when it is more than 30 "
            "minutes early or 90 minutes late (in practice a bus reporting the wrong trip), or "
            "at a stop where, trip after trip, the GPS time is out of line with the stops "
            "either side (usually a stop mapped away from where buses really stop). Trips that "
            "never appeared in LTD's live data can't be measured at all. The Data & methods page "
            "shows how much of the service was measured each day.",
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