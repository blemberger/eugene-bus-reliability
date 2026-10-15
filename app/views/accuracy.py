"""Predictions: how far LTD's real-time arrival predictions can be trusted, against the printed
timetable, for every route and stop or for one route and/or stop (chosen at the top, kept in the
page's address: /accuracy?route=...&stop=...)."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from common import (
    ALL_ROUTES,
    BRAND,
    PREDICTION_OFF_NOTE,
    as_name,
    card,
    col_count,
    col_time,
    countdown_off_chart,
    current_fv,
    day_label,
    day_sql,
    days_suffix,
    fmt_date,
    fmt_minutes,
    hour_label,
    link_table,
    local_times,
    page_filters,
    prediction_scope_rows,
    q,
    q_live,
    readable_stop_names,
    require_db,
    require_marts,
    scope_picker,
    service_hour_key,
    show_chart,
    table,
)

require_db()
title_slot = st.empty()  # names the route and/or stop chosen just below it
st.caption(
    "LTD's real-time arrival predictions (what apps like Transit show), against when the bus "
    "actually came."
)
require_marts()
FV = str(current_fv())  # schedule version in force today

# one choice for the whole page, as a report card is about one route or stop
scope = scope_picker("pred", in_address=True)
title_slot.title(f"How accurate are LTD's arrival predictions{scope.suffix(long=True)}?")
if scope.route_id or scope.stop_id:
    st.set_page_config(
        page_title=f"How accurate are LTD's predictions{scope.suffix(long=True)}? · Eugene Bus Watch"
    )
if scope.route_id or scope.stop_id:
    if scope.route_id:
        st.page_link(
            "views/route.py",
            label=f"Report card for route {scope.route_name} →",
            query_params={"route": scope.route_id},
        )
    if scope.stop_id:
        st.page_link(
            "views/stops.py",
            label=f"Report card for {scope.stop_name} →",
            query_params={"stop": scope.stop_id},
        )
SUFFIX = scope.suffix()

routes = q(f"select route_id, route_short_name from gtfs.routes where feed_version_id = {FV}")
short = dict(
    zip(routes["route_id"].astype(str), routes["route_short_name"].astype(str), strict=False)
)

# the first chart, then the period and days filters that everything on the page follows (drawn
# in that order, though the filters are read first)
chart_slot = st.container()
start, wt = page_filters()
period = f"{day_label(wt)} since {fmt_date(start)}"
PERIOD = period[0].upper() + period[1:]

# ---- how far off: the prediction at each distance, and the timetable ----------------------
with chart_slot, card():
    st.subheader(
        f"How far off are the predictions{SUFFIX}{days_suffix()}?",
        help=PREDICTION_OFF_NOTE + f" {PERIOD}.",
    )
    rows = prediction_scope_rows(start, wt, scope)
    fig0 = (
        None
        if rows.empty
        else countdown_off_chart(
            rows, {}, [ALL_ROUTES], all_label=as_name(scope.label()), colors={ALL_ROUTES: BRAND}
        )
    )
    if fig0 is None:
        st.caption("Not enough measured arrivals with a prediction yet for this choice.")
    else:
        show_chart(fig0)


# ---- by time of day ---------------------------------------------------------------------
with card():
    st.subheader(
        f"Does accuracy depend on the time of day{SUFFIX}{days_suffix()}?",
        help="Predictions made about this far ahead, by the hour they were on show. Each bar "
        "builds up: the dark part is the share right to within 2 minutes; add the middle part "
        "for within 3 minutes; the whole bar is within 5 minutes. The space above is how often "
        "the bus came more than 5 minutes off. (Windows under 2 minutes would be finer than an "
        "arrival can be timed at every stop.) Hours with fewer than 20 bus arrivals are left "
        f"out. {PERIOD}.",
    )
    AHEAD_CHOICES = {"2 min": (1, 2), "5 min": (5, 5), "10 min": (10, 10), "15 min": (15, 15)}
    ahead = (
        st.segmented_control(
            "Predictions made this far ahead",
            list(AHEAD_CHOICES),
            default="5 min",
            key="tod_ahead",
        )
        or "5 min"
    )
    lo, hi = AHEAD_CHOICES[ahead]
    clause, params = day_sql(wt)
    if scope.stop_id:
        # one stop: from the arrivals themselves (the daily table has no stops)
        where, wparams = scope.where()
        tod = q(
            f"""
            select hour_local, count(*) as n,
                   count(*) filter (where abs(error_s) <= 120) as within1,
                   count(*) filter (where abs(error_s) <= 180) as within2,
                   count(*) filter (where abs(error_s) <= 300) as within3
            from marts.fct_countdown_samples
            where in_comparison and ahead_min between %s and %s and service_date >= %s
              {where} {clause}
            group by 1 order by 1
            """,
            (lo, hi, start, *wparams, *params),
        )
    else:
        tod = q(
            f"""
            select hour_local, sum(n)::bigint as n, sum(n_within_2min)::bigint as within1,
                   sum(n_within_3min)::bigint as within2, sum(n_within_5min)::bigint as within3
            from marts.mart_prediction_daily
            where basis = 'sign' and ahead_min between %s and %s and service_date >= %s {clause}
              {"and route_id = %s" if scope.route_id else ""}
            group by 1 order by 1
            """,
            (lo, hi, start, *params, *([scope.route_id] if scope.route_id else [])),
        )
    tod = tod[tod["n"] >= 20].copy()
    if tod.empty:
        st.caption("Not enough measured bus arrivals this far ahead yet (needs 20 in an hour).")
    else:
        # service order: 5 am first, the hours after midnight last
        tod = tod.assign(_k=tod["hour_local"].map(service_hour_key)).sort_values("_k")
        tod["Hour"] = tod["hour_local"].map(hour_label)
        for k in (1, 2, 3):
            tod[f"p{k}"] = tod[f"within{k}"] / tod["n"]
        # One bar per hour, built up in layers: the dark part is "right within 2 min", adding the
        # middle part gives "within 3 min", the whole bar "within 5 min". One blue, dark to light.
        layers = [
            ("p1", None, "within 2 min", BRAND),
            ("p2", "p1", "2–3 min off", "#5b9bd5"),
            ("p3", "p2", "3–5 min off", "#b3d1ee"),
        ]
        custom = list(zip(tod["p1"], tod["p2"], tod["p3"], tod["n"].astype(int), strict=False))
        fig3 = go.Figure()
        for col, below, name, color in layers:
            fig3.add_trace(
                go.Bar(
                    x=tod["Hour"],
                    y=tod[col] - (tod[below] if below else 0),
                    name=name,
                    marker={"color": color, "line": {"color": "white", "width": 1}},
                    customdata=custom,
                    hovertemplate=(
                        "<b>%{x}</b><br>right within 2 min: %{customdata[0]:.0%}<br>"
                        "within 3 min: %{customdata[1]:.0%}<br>within 5 min: %{customdata[2]:.0%}"
                        "<br>%{customdata[3]:,} bus arrivals<extra></extra>"
                    ),
                )
            )
        fig3.update_layout(
            barmode="stack",
            yaxis_tickformat=".0%",
            yaxis_range=[0, 1],
            xaxis_title="",
            yaxis_title=f"Predictions {ahead} out that were right",
            legend={
                "orientation": "h",
                "yanchor": "bottom",
                "y": 1.02,
                "x": 0,
                "title": "",
                "traceorder": "normal",
            },
        )
        show_chart(fig3)


# ---- tables: each route, each stop -------------------------------------------------------
def off_by(key: str, where: str, params: tuple, daily: bool) -> pd.DataFrame:
    """Average minutes off per `key` (route_id or stop_id) at 5, 10 and 15 minutes away and for
    the timetable, for the page's period and days."""
    clause, wt_params = day_sql(wt)
    table_name = "mart_prediction_daily" if daily else "mart_prediction_stop_daily"
    try:
        return q(
            f"""
            select {key} as id, basis, ahead_min, sum(n) as n,
                   sum(sum_abs_s)::float8 / nullif(sum(n), 0) as mean_abs_s
            from marts.{table_name}
            where service_date >= %s {clause} {where}
              and {key} is not null and (basis = 'timetable' or ahead_min in (5, 10, 15))
            group by 1, 2, 3
            """,
            (start, *wt_params, *params),
        )
    except Exception:  # noqa: BLE001 — the table is being rebuilt
        return pd.DataFrame()


def prediction_table(
    off: pd.DataFrame, first_cols: list[dict], row_of, key: str, sort: str | None, min_n: int = 50
) -> None:
    """A table of how far off the predictions are, 5 and 10 minutes away, against the printed
    timetable on the same arrivals, one row per id in `off` (off_by()); row_of(id, n) gives
    the row's href, hover (which carries the number of arrivals) and first columns' values.
    With no sort, the rows come busiest first, as the stop tables do."""
    if off.empty:
        st.caption("Not enough measured bus arrivals yet for this period.")
        return
    off = off.assign(id=off["id"].astype(str))
    at = {
        k: off[(off["basis"] == "sign") & (off["ahead_min"] == k)].set_index("id")
        for k in (5, 10, 15)
    }
    tt = off[off["basis"] == "timetable"].set_index("id")
    ids = [i for i in tt.index if i in at[15].index and at[15].loc[i, "n"] >= min_n]
    if not ids:
        st.caption("Not enough measured bus arrivals yet for this period.")
        return

    def minutes(frame: pd.DataFrame, i: str) -> float | None:
        if i not in frame.index or pd.isna(frame.loc[i, "mean_abs_s"]):
            return None
        return float(frame.loc[i, "mean_abs_s"]) / 60

    if sort is None:
        ids.sort(key=lambda i: -int(at[15].loc[i, "n"]))
    values = {i: (minutes(at[5], i), minutes(at[10], i), minutes(tt, i)) for i in ids}
    # the two prediction columns share a scale (so 5 and 10 minutes away compare); the
    # timetable column has its own, so its few very large values don't shrink the rest
    top_pred = max(v for vs in values.values() for v in vs[:2] if v is not None)
    top_tt = max((vs[2] for vs in values.values() if vs[2] is not None), default=1.0)

    def bar(v: float | None, colour: str, top: float) -> str:
        if v is None:
            return ""
        return (
            "<span class='ebw-g'><span class='ebw-x'>"
            f"<span class='ebw-x1' style='left:0;top:4px;width:{v / top * 100:.0f}%;"
            f"background:{colour}'></span></span>"
            # a short number, so the bar gets the rest of the column
            f"<span class='ebw-s' style='flex:0 0 3.6em'>{v:.1f} min</span>"
            "</span>"
        )

    rows = []
    for i in ids:
        c5, c10, t = values[i]
        n = int(at[15].loc[i, "n"])
        rows.append(
            {
                **row_of(i, n),
                "c5": bar(c5, BRAND, top_pred),
                "c5_s": c5,
                "c10": bar(c10, BRAND, top_pred),
                "c10_s": c10,
                "tt": bar(t, "#8a8f94", top_tt),
                "tt_s": t,
            }
        )
    cols = list(first_cols)
    for k, label, tip in (
        ("c5", "Prediction, 5 min away", "Average minutes off when the prediction said 5 minutes."),
        (
            "c10",
            "Prediction, 10 min away",
            "Average minutes off when the prediction said 10 minutes.",
        ),
        (
            "tt",
            "Printed timetable",
            "Average minutes off the printed timetable, same bus arrivals.",
        ),
    ):
        cols.append(
            {
                "key": k,
                "label": label,
                "width": "minmax(9em, 1fr)",
                "html": True,
                "help": tip + " Sorts closest first.",
                "sort": f"{k}_s",
                "first": "asc",
            }
        )
    # a row opens that route's or stop's report card at its predictions, like every other
    # table's rows; the choice at the top of this page is how to see one here
    link_table(rows, cols, max_height=520, key=key, default_sort=sort)


TABLE_HELP = (
    "Average minutes the bus came from the time it was given, early or late alike, on the same "
    "bus arrivals: the prediction when it said 5 and 10 minutes away, and the printed timetable. "
    "Lower is better. Rows with at least 50 bus arrivals measured. "
    f"{PERIOD}. Click a column heading to sort; click a row for that report card's predictions."
)

with card():
    at_stop = f" at {scope.stop_name}" if scope.stop_id else ""
    st.subheader(
        f"Which routes have the best predictions{at_stop}{days_suffix()}?", help=TABLE_HELP
    )
    if scope.stop_id:
        by_route = off_by("route_id", "and stop_id = %s", (scope.stop_id,), daily=False)
    else:
        by_route = off_by("route_id", "", (), daily=True)
    prediction_table(
        by_route,
        [{"key": "route", "label": "Route", "width": "3.8em", "bold": True}],
        lambda i, n: {
            "href": f"/route?route={i}#predictions",
            "hover": f"Route {short.get(i, i)}: measured on {n:,} bus arrivals",
            "route": short.get(i, i),
        },
        key="pred_routes",
        sort="route:asc",
        min_n=50 if not scope.stop_id else 20,
    )

with card():
    on_route = f" on route {scope.route_name}" if scope.route_id else ""
    st.subheader(
        f"Which stops have the best predictions{on_route}{days_suffix()}?", help=TABLE_HELP
    )
    by_stop = off_by(
        "stop_id",
        "and route_id = %s" if scope.route_id else "",
        (scope.route_id,) if scope.route_id else (),
        daily=False,
    )
    names = readable_stop_names(
        q(f"select stop_id, stop_name, stop_code from gtfs.stops where feed_version_id = {FV}")
    )
    name_of = dict(zip(names["stop_id"].astype(str), names["stop_name"], strict=False))
    code_of = dict(zip(names["stop_id"].astype(str), names["stop_code"], strict=False))
    prediction_table(
        by_stop,
        [{"key": "stop", "label": "Stop", "width": "minmax(10em, 2fr)", "bold": True}],
        lambda i, n: {
            "href": f"/stops?stop={i}#predictions",
            "hover": f"{name_of.get(i, i)}: measured on {n:,} bus arrivals",
            "stop": name_of.get(i, i) + (f" · #{code_of[i]}" if code_of.get(i) else ""),
        },
        key="pred_stops",
        # busiest first: the stops most riders use
        sort=None,
    )


# ---- one stop, right now: how each coming bus's prediction has been revised --------------
if scope.stop_id:
    with card():
        st.subheader(
            f"Right now at {scope.stop_name}: how has each coming bus's prediction changed?",
            help="Each line is a bus on its way here: when LTD said it (across) and the arrival it "
            "predicted then (up). A flat line is a prediction that held; a line that keeps "
            "stepping later is the prediction being optimistic and correcting itself as the bus "
            "falls behind.",
        )
    hist = q_live(
        f"""
        with coming as (
            select trip_id, start_date, stop_sequence from rt.prediction_current
            where stop_id = %s and coalesce(arrival_time, departure_time) between now() - interval '2 minutes' and now() + interval '45 minutes'
              and last_seen_at > now() - interval '3 minutes'
        )
        select u.trip_id, r.route_short_name as route, t.trip_headsign as headsign,
               u.first_seen_at, u.last_seen_at, coalesce(u.arrival_time, u.departure_time) as predicted
        from (
            select trip_id, start_date, stop_sequence, arrival_time, departure_time, first_seen_at, last_seen_at from rt.prediction_history
            union all
            select trip_id, start_date, stop_sequence, arrival_time, departure_time, first_seen_at, last_seen_at from rt.prediction_current
        ) u
        join coming c using (trip_id, start_date, stop_sequence)
        join gtfs.trips t  on t.trip_id = u.trip_id and t.feed_version_id = {FV}
        join gtfs.routes r on r.route_id = t.route_id and r.feed_version_id = {FV}
        where coalesce(u.arrival_time, u.departure_time) is not null
        order by u.trip_id, u.first_seen_at
        """,
        (scope.stop_id,),
    )
    if hist.empty:
        st.info("No buses predicted for this stop in the next 45 minutes.")
    else:
        hist["Bus"] = (
            "Route "
            + hist["route"].astype(str)
            + " · "
            + hist["headsign"].fillna("").astype(str)
            + " · trip "
            + hist["trip_id"].astype(str)
        )
        hist["asserted"] = pd.to_datetime(hist["first_seen_at"], utc=True).dt.tz_convert(
            "America/Los_Angeles"
        )
        hist["predicted_local"] = pd.to_datetime(hist["predicted"], utc=True).dt.tz_convert(
            "America/Los_Angeles"
        )
        fig4 = go.Figure()
        for bus, g in hist.groupby("Bus"):
            fig4.add_trace(
                go.Scatter(
                    x=g["asserted"],
                    y=g["predicted_local"],
                    mode="lines+markers",
                    name=bus,
                    line={"shape": "hv"},
                )
            )
        fig4.update_layout(
            xaxis_title="When LTD said it",
            yaxis_title="Predicted arrival at this stop",
            legend_title="",
            height=420,
        )
        show_chart(fig4)
        latest = hist.sort_values("first_seen_at").groupby("Bus", as_index=False).last()
        table(
            pd.DataFrame(
                {
                    "Bus": latest["Bus"].to_numpy(),
                    "Now predicted": local_times(latest["predicted"]).to_numpy(),
                    "First predicted": local_times(
                        hist.sort_values("first_seen_at")
                        .groupby("Bus")["predicted"]
                        .first()
                        .reindex(latest["Bus"])
                    ).to_numpy(),
                    "Revisions": hist.groupby("Bus").size().reindex(latest["Bus"]).to_numpy(),
                }
            ),
            hide_index=True,
            width="stretch",
            column_config={
                "Now predicted": col_time("Now predicted"),
                "First predicted": col_time("First predicted"),
                "Revisions": col_count("Revisions"),
            },
        )


# ---- revisions --------------------------------------------------------------------------
with card():
    st.subheader(
        "How often does LTD change a prediction?",
        help="Every route and stop, last 7 days. Every revision is kept; that's what lets a "
        "prediction be scored at the distance it was made, not just the last value shown.",
    )
    # computed once per analysis build (marts.mart_prediction_revisions): it reads every
    # prediction of the week, far too slow to run on each visit
    has_rev = q("select to_regclass('marts.mart_prediction_revisions') is not null as ok")["ok"][0]
    rev = q("select * from marts.mart_prediction_revisions") if has_rev else pd.DataFrame()
    if not rev.empty and rev["trip_stops"][0]:
        r = rev.iloc[0]
        st.write(
            f"Over the last 7 days, a typical stop's prediction was revised **{int(r['median_revisions'])} times** "
            f"(1 in 10 stops: {int(r['p90_revisions'])} or more) while it was on the board for about **{fmt_minutes(r['median_tracked_s'])}**."
        )
