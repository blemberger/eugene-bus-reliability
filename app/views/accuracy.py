"""Predictions: how far LTD's real-time arrival predictions can be trusted, against the printed
timetable, for every route and stop or for one route and/or stop (chosen at the top, kept in the
page's address: /accuracy?route=...&stop=...)."""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from common import (
    ALL_CHOICE,
    ALL_ROUTES,
    BRAND,
    MIN_RANGE,
    PREDICTION_OFF_NOTE,
    as_name,
    card,
    coming_now_card,
    countdown_off_chart,
    current_fv,
    day_label,
    day_sql,
    days_suffix,
    filters_again,
    fmt_date,
    hour_label,
    link_table,
    not_enough,
    page_filters,
    prediction_scope_rows,
    q,
    range_fields,
    range_scale,
    readable_stop_names,
    require_db,
    require_marts,
    scope_picker,
    service_hour_key,
    show_chart,
    stop_choices,
)

from eugene_bus_reliability.signed import SIGNED_THRESHOLDS, signed_col, signed_percentile

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

# the first chart, then right under it the period and days buttons everything on the page
# follows (drawn in that order, though the buttons are read first); the chart by time of day and
# the routes table; the buttons again; the stops table
chart_slot, filters_slot = st.container(), st.container()
with filters_slot:
    start, wt = page_filters()
period = f"{day_label(wt)} since {fmt_date(start)}"
PERIOD = period[0].upper() + period[1:]

# ---- how far off: the prediction at each distance, and the timetable ----------------------
with chart_slot, card():
    st.subheader(
        f"Prediction accuracy{SUFFIX}{days_suffix()}",
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
        st.caption(not_enough(start, wt, "measured arrivals with a prediction"))
    else:
        show_chart(fig0)


# ---- how far ahead: one choice for the time-of-day chart and both tables, its buttons shown
# above each, so changing any of them changes all three
AHEAD_CHOICES = {"2 min": 2, "5 min": 5, "10 min": 10, "15 min": 15}


def ahead_choice(key: str) -> int:
    """The 'Predictions made this far ahead' buttons, kept to the page's one choice (in
    st.session_state['pred_ahead']): minutes away."""
    chosen = st.session_state.get("pred_ahead", "5 min")
    st.session_state[key] = chosen

    def keep() -> None:
        if st.session_state.get(key):  # clicking the chosen button again changes nothing
            st.session_state["pred_ahead"] = st.session_state[key]

    st.segmented_control(
        "Predictions made this far ahead", list(AHEAD_CHOICES), key=key, on_change=keep
    )
    return AHEAD_CHOICES[st.session_state.get("pred_ahead", "5 min")]


# ---- by time of day ---------------------------------------------------------------------
with card():
    st.subheader(
        f"Prediction accuracy by time of day{SUFFIX}{days_suffix()}",
        help="Predictions made about this far ahead, by the hour they were on show. Each bar "
        "builds up: the dark part is the share right to within 2 minutes; add the middle part "
        "for within 3 minutes; the whole bar is within 5 minutes. The space above is how often "
        "the bus came more than 5 minutes off. (Windows under 2 minutes would be finer than an "
        f"arrival can be timed at every stop.) Hours with fewer than {MIN_RANGE} bus arrivals "
        f"are left out. {PERIOD}.",
    )
    lo = hi = ahead_choice("tod_ahead")
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
    tod = tod[tod["n"] >= MIN_RANGE].copy()
    if tod.empty:
        st.caption(not_enough(start, wt, "measured bus arrivals this far ahead"))
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
            yaxis_title="Share of predictions",
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
SIGNED_SUMS = ", ".join(f"sum({signed_col(t)}) as {signed_col(t)}" for t in SIGNED_THRESHOLDS)


def off_by(key: str, where: str, params: tuple, daily: bool, ahead: int) -> pd.DataFrame:
    """Average minutes off per `key` (route_id or stop_id) when the prediction said `ahead`
    minutes away (basis 'sign') and for the timetable on the same arrivals (basis 'timetable',
    counted once per arrival, so its n is the number of arrivals), for the page's period and
    days."""
    clause, wt_params = day_sql(wt)
    table_name = "mart_prediction_daily" if daily else "mart_prediction_stop_daily"
    try:
        return q(
            f"""
            select {key} as id, basis, sum(n) as n,
                   sum(sum_abs_s)::float8 / nullif(sum(n), 0) as mean_abs_s,
                   {SIGNED_SUMS}
            from marts.{table_name}
            where service_date >= %s {clause} {where}
              and {key} is not null and (basis = 'timetable' or ahead_min = %s)
            group by 1, 2
            """,
            (start, *wt_params, *params, ahead),
        )
    except Exception:  # noqa: BLE001 — the table is being rebuilt
        return pd.DataFrame()


def fmt_vs(seconds: float, what: str) -> str:
    """'0.4 min later than predicted', '1.2 min earlier than predicted', 'as predicted'."""
    m = abs(seconds) / 60
    if round(m, 1) == 0:
        return f"as {what}"
    return f"{m:.1f} min {'later' if seconds > 0 else 'earlier'} than {what}"


def prediction_table(
    off: pd.DataFrame,
    first_cols: list[dict],
    row_of,
    key: str,
    sort: str | None,
    ahead: int,
    min_n: int = 20,
) -> None:
    """A table of how far off the predictions were when they said `ahead` minutes away, beside
    the printed timetable on the same arrivals, one row per id in `off` (off_by()): minutes off
    on average and the 8-in-10 strip for each. The strips share one scale, so a narrow
    prediction strip beside a wide timetable one shows the prediction doing better. row_of(id,
    n) gives the row's href, hover (which carries the number of arrivals) and first columns'
    values. With no sort, the rows come busiest first, as the stop tables do."""
    if off.empty:
        st.caption(not_enough(start, wt, "measured bus arrivals"))
        return
    off = off.assign(id=off["id"].astype(str))
    told = off[off["basis"] == "sign"].set_index("id")
    tt = off[off["basis"] == "timetable"].set_index("id")
    ids = [i for i in tt.index if i in told.index and tt.loc[i, "n"] >= min_n]
    if not ids:
        st.caption(not_enough(start, wt, "measured bus arrivals"))
        return
    if sort is None:
        ids.sort(key=lambda i: -int(tt.loc[i, "n"]))

    def spread(frame: pd.DataFrame, i: str) -> tuple:
        """(average seconds off, typical, low end, high end of the 8 in 10), signed seconds."""
        if i not in frame.index:
            return (None, None, None, None)
        r = frame.loc[i]
        return (
            None if pd.isna(r["mean_abs_s"]) else float(r["mean_abs_s"]),
            signed_percentile(r, 0.5),
            signed_percentile(r, 0.1),
            signed_percentile(r, 0.9),
        )

    vals = {i: (spread(told, i), spread(tt, i)) for i in ids}
    ends = pd.DataFrame(
        [(v[2], v[3]) for vs in vals.values() for v in vs], columns=["p10", "p90"]
    ).dropna()
    lo, hi = range_scale(ends, "p10", "p90")

    rows = []
    for i in ids:
        (p_off, p_med, p_lo, p_hi), (t_off, t_med, t_lo, t_hi) = vals[i]
        base = row_of(i, int(tt.loc[i, "n"]))
        if p_med is not None:
            base["hover"] += (
                f"; {ahead} minutes away, the typical bus came {fmt_vs(p_med, 'predicted')}"
            )
        rows.append(
            {
                **base,
                **{f"p_{k}": v for k, v in range_fields(p_lo, p_med, p_hi, lo, hi, p_off).items()},
                **{f"t_{k}": v for k, v in range_fields(t_lo, t_med, t_hi, lo, hi, t_off).items()},
            }
        )
    OFF = "Average minutes off, early or late alike; lower is better. Sorts closest first."
    STRIP = (
        "The strip is where 8 in 10 buses came, the dark tick the typical bus, the numbers the "
        "range in minutes. Sorts the narrowest first."
    )
    cols = [
        *first_cols,
        {
            "key": "p_off",
            "label": f"Prediction, {ahead} min away",
            "width": "9.6em",
            "num": True,
            "help": f"When LTD's prediction said the bus was {ahead} minutes away. " + OFF,
            "sort": "p_off_s",
            "first": "asc",
        },
        {
            "key": "p_range",
            "label": "8 in 10 buses vs prediction",
            "width": "minmax(13em, 2fr)",
            "html": True,
            "help": f"Against the prediction when it said {ahead} minutes away (the dashed line: the "
            "predicted time; left of it, earlier than predicted). " + STRIP,
            "sort": "p_range_s",
            "first": "asc",
        },
        {
            "key": "t_off",
            "label": "Printed timetable",
            "width": "8.4em",
            "num": True,
            "help": "The printed timetable, for the same bus arrivals. " + OFF,
            "sort": "t_off_s",
            "first": "asc",
        },
        {
            "key": "t_range",
            "label": "8 in 10 buses vs timetable",
            "width": "minmax(13em, 2fr)",
            "html": True,
            "help": "Against the printed timetable, for the same bus arrivals (the dashed line: "
            "the scheduled time; left of it, early). " + STRIP,
            "sort": "t_range_s",
            "first": "asc",
        },
    ]
    # a row opens that route's or stop's report card at its predictions, like every other
    # table's rows; the choice at the top of this page is how to see one here
    link_table(rows, cols, max_height=520, key=key, default_sort=sort)


def table_help(min_n: int) -> str:
    return (
        "How far from the time it was given the bus came, on the same bus arrivals: LTD's "
        "prediction when it said the bus was the chosen number of minutes away, and the printed "
        "timetable. The numbers: minutes off on average, early or late alike; lower is better. "
        "The strips: where 8 in 10 buses came, earlier (left of the dashed line) or later. Rows "
        f"with at least {min_n} bus arrivals measured. {PERIOD}. Click a column heading to "
        "sort; click a row for that report card's predictions."
    )


ROUTES_MIN = 20 if not scope.stop_id else MIN_RANGE
STOPS_MIN = 20

with card():
    at_stop = f" at {scope.stop_name}" if scope.stop_id else ""
    st.subheader(
        f"Prediction accuracy by route{at_stop}{days_suffix()}", help=table_help(ROUTES_MIN)
    )
    ahead = ahead_choice("routes_ahead")
    if scope.stop_id:
        by_route = off_by("route_id", "and stop_id = %s", (scope.stop_id,), False, ahead)
    else:
        by_route = off_by("route_id", "", (), True, ahead)
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
        ahead=ahead,
        min_n=ROUTES_MIN,
    )

filters_again()  # the period and days buttons again, between the two tables

with card():
    on_route = f" on route {scope.route_name}" if scope.route_id else ""
    st.subheader(
        f"Prediction accuracy by stop{on_route}{days_suffix()}", help=table_help(STOPS_MIN)
    )
    ahead = ahead_choice("stops_ahead")
    by_stop = off_by(
        "stop_id",
        "and route_id = %s" if scope.route_id else "",
        (scope.route_id,) if scope.route_id else (),
        False,
        ahead,
    )
    names = readable_stop_names(
        q(f"select stop_id, stop_name, stop_code from gtfs.stops where feed_version_id = {FV}")
    )
    name_of = dict(zip(names["stop_id"].astype(str), names["stop_name"], strict=False))
    code_of = dict(zip(names["stop_id"].astype(str), names["stop_code"], strict=False))
    prediction_table(
        by_stop,
        [{"key": "stop", "label": "Stop", "width": "minmax(12em, 2fr)", "bold": True}],
        lambda i, n: {
            "href": f"/stops?stop={i}#predictions",
            "hover": f"{name_of.get(i, i)}: measured on {n:,} bus arrivals",
            "stop": name_of.get(i, i) + (f" · #{code_of[i]}" if code_of.get(i) else ""),
        },
        key="pred_stops",
        # busiest first: the stops most riders use
        sort=None,
        ahead=ahead,
        min_n=STOPS_MIN,
    )


# ---- one stop, right now: last, as it is about one stop rather than the predictions as a
# whole; the stop list again beside it, the same choice as the one at the top of the page
def _same_stop() -> None:
    st.session_state["pred_stop"] = st.session_state["pred_stop_again"]


def _stop_again() -> None:
    stops = stop_choices(scope.route_id, None)
    label_of = dict(zip(stops["stop_id"].astype(str), stops["label"], strict=False))
    options = [ALL_CHOICE, *label_of]
    chosen = st.session_state.get("pred_stop", ALL_CHOICE)
    st.session_state["pred_stop_again"] = chosen if chosen in options else ALL_CHOICE
    st.selectbox(
        "Stop (type to search)",
        options,
        format_func=lambda s: (
            (f"All stops on route {scope.route_name}" if scope.route_id else "All stops")
            if s == ALL_CHOICE
            else label_of[s]
        ),
        key="pred_stop_again",
        on_change=_same_stop,
    )


coming_now_card(scope.stop_id, scope.stop_name, picker=_stop_again)
