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
    LINE_COLOURS,
    LINE_MAIN,
    LINE_REF,
    MARKER_MAIN,
    MIN_RANGE,
    PREDICTION_OFF_NOTE,
    REF_GREY,
    as_name,
    card,
    clean_headsigns,
    col_count,
    col_time,
    countdown_off_chart,
    current_fv,
    day_label,
    day_sql,
    days_suffix,
    filters_again,
    fmt_date,
    fmt_off,
    hour_label,
    is_mobile,
    link_table,
    local_times,
    not_enough,
    page_filters,
    prediction_scope_rows,
    q,
    q_live,
    range_fields,
    range_scale,
    readable_stop_names,
    require_db,
    require_marts,
    scope_picker,
    service_hour_key,
    show_chart,
    table,
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
# the signed running counts only where the tables use them (5 minutes away and the timetable):
# adding up the others too made the stops table's query twice as slow
SIGNED_5 = ", ".join(
    f"sum({signed_col(t)}) filter (where basis = 'timetable' or ahead_min = 5) as {signed_col(t)}"
    for t in SIGNED_THRESHOLDS
)


def off_by(key: str, where: str, params: tuple, daily: bool) -> pd.DataFrame:
    """Average minutes off per `key` (route_id or stop_id) at 5, 10 and 15 minutes away and for
    the timetable, for the page's period and days."""
    clause, wt_params = day_sql(wt)
    table_name = "mart_prediction_daily" if daily else "mart_prediction_stop_daily"
    try:
        return q(
            f"""
            select {key} as id, basis, ahead_min, sum(n) as n,
                   sum(sum_abs_s)::float8 / nullif(sum(n), 0) as mean_abs_s,
                   {SIGNED_5}
            from marts.{table_name}
            where service_date >= %s {clause} {where}
              and {key} is not null and (basis = 'timetable' or ahead_min in (5, 10, 15))
            group by 1, 2, 3
            """,
            (start, *wt_params, *params),
        )
    except Exception:  # noqa: BLE001 — the table is being rebuilt
        return pd.DataFrame()


def fmt_vs(seconds: float, what: str) -> str:
    """'0.4 min later than predicted', '1.2 min earlier than predicted', 'as predicted'."""
    m = abs(seconds) / 60
    if round(m, 1) == 0:
        return f"as {what}"
    return f"{m:.1f} min {'later' if seconds > 0 else 'earlier'} than {what}"


def _local(s: pd.Series) -> pd.Series:
    """UTC timestamps -> Eugene clock times without a zone, as the charts' time axes take them."""
    return pd.to_datetime(s, utc=True).dt.tz_convert("America/Los_Angeles").dt.tz_localize(None)


def coming_now_chart(hist: pd.DataFrame) -> None:
    """The buses coming to one stop now: for each, the arrival LTD predicted at each moment
    (a solid step line, a dot where it changed, running on to when it was last said) and its
    timetable time (dashed, the same colour); then a table of the same. hist: one row per
    prediction value (trip_id, route, headsign, first_seen_at, last_seen_at, predicted,
    scheduled)."""
    hist = hist.copy()
    hist["toward"] = clean_headsigns(hist)
    trips = (
        hist.groupby("trip_id")
        .agg(
            route=("route", "first"),
            toward=("toward", "first"),
            scheduled=("scheduled", "first"),
            now=("predicted", "last"),
        )
        .sort_values("now")
    )
    # a bus is named by its route; where two coming buses share one, by its timetable time too
    repeats = trips["route"].duplicated(keep=False)
    names = {}
    for trip, r in trips.iterrows():
        name = f"Route {r['route']}"
        if repeats[trip]:
            due = _local(pd.Series([r["scheduled"] if pd.notna(r["scheduled"]) else r["now"]]))[0]
            name += f", due {due.strftime('%I:%M %p').lstrip('0').lower()}"
        names[trip] = name
    palette = [BRAND, *LINE_COLOURS]
    fig = go.Figure()
    ys: list = []
    xs: list = []
    for i, (trip, r) in enumerate(trips.iterrows()):
        colour = palette[i % len(palette)]
        g = hist[hist["trip_id"] == trip].sort_values("first_seen_at")
        said = list(_local(g["first_seen_at"]))
        pred = list(_local(g["predicted"]))
        # the last prediction holds until LTD last said it
        x = [*said, _local(g["last_seen_at"]).iloc[-1]]
        y = [*pred, pred[-1]]
        clock = lambda t: t.strftime("%I:%M %p").lstrip("0").lower()  # noqa: E731
        toward = f" → {r['toward']}" if r["toward"] else ""
        fig.add_trace(
            go.Scatter(
                x=x,
                y=y,
                mode="lines+markers",
                name=names[trip],
                legendgroup=trip,
                line={"color": colour, "width": LINE_MAIN, "shape": "hv"},
                marker={"size": [MARKER_MAIN + 2] * len(said) + [0], "color": colour},
                hovertext=[
                    f"<b>{names[trip]}{toward}</b><br>at {clock(a)} LTD said: arrives {clock(p)}"
                    for a, p in zip(said, pred, strict=False)
                ]
                + [f"<b>{names[trip]}{toward}</b><br>still predicted: arrives {clock(pred[-1])}"],
                hoverinfo="text",
            )
        )
        xs += x
        ys += y
        if pd.notna(r["scheduled"]):
            sched = _local(pd.Series([r["scheduled"]]))[0]
            ys.append(sched)
            fig.add_trace(
                go.Scatter(
                    x=[None],  # spans the chart: drawn below once the time range is known
                    y=[sched],
                    mode="lines",
                    name=f"{names[trip]} timetable",
                    legendgroup=trip,
                    showlegend=False,
                    line={"color": colour, "width": LINE_REF, "dash": "dash"},
                    hovertext=f"<b>{names[trip]}{toward}</b><br>timetable: {clock(sched)}",
                    hoverinfo="text",
                )
            )
    # the time range: the predictions shown, at least the last 10 minutes
    x_hi = max(xs) + pd.Timedelta(minutes=1)
    x_lo = min(min(xs), x_hi - pd.Timedelta(minutes=10)) - pd.Timedelta(minutes=1)
    for tr in fig.data:
        if tr.x == (None,):
            tr.x = [x_lo, x_hi]
            tr.y = [tr.y[0], tr.y[0]]
    if any(tr.line.dash == "dash" for tr in fig.data):
        # one legend entry saying what the dashed lines are
        fig.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="lines",
                name="Timetable (dashed)",
                line={"color": REF_GREY, "width": LINE_REF, "dash": "dash"},
                hoverinfo="skip",
            )
        )
    # the arrival-time range: the times shown, at least 10 minutes tall
    y_lo, y_hi = min(ys), max(ys)
    extra = max(pd.Timedelta(minutes=10) - (y_hi - y_lo), pd.Timedelta(0)) / 2
    y_lo, y_hi = y_lo - extra - pd.Timedelta(minutes=2), y_hi + extra + pd.Timedelta(minutes=2)
    fig.update_layout(
        xaxis={
            "title": "When LTD said it",
            "range": [x_lo, x_hi],
            "tickformat": "%-I:%M",
            "type": "date",
        },
        yaxis={
            "title": "Predicted arrival here",
            "range": [y_lo, y_hi],
            "tickformat": "%-I:%M %p",
            "type": "date",
        },
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "x": 0, "xanchor": "left"},
        hovermode="closest",
        height=440,
    )
    show_chart(fig)
    first = hist.sort_values("first_seen_at").groupby("trip_id")["predicted"].first()
    revisions = hist.groupby("trip_id").size() - 1
    rows = pd.DataFrame(
        {
            "Bus": [names[t] for t in trips.index],
            "Toward": trips["toward"].to_numpy(),
            "Now predicted": local_times(trips["now"]).to_numpy(),
            "Timetable": local_times(trips["scheduled"]).to_numpy(),
            "First predicted": local_times(first.reindex(trips.index)).to_numpy(),
            "Changes": revisions.reindex(trips.index).to_numpy(),
        }
    )
    if is_mobile():  # what fits a phone's width: the bus, its two times, and the changes
        rows = rows.drop(columns=["Toward", "First predicted"])
    table(
        rows,
        hide_index=True,
        width="stretch",
        column_config={
            "Now predicted": col_time("Now predicted"),
            "Timetable": col_time("Timetable"),
            "First predicted": col_time("First predicted"),
            "Changes": col_count(
                "Changes", help="How many times LTD has changed this bus's prediction here."
            ),
        },
    )


def prediction_table(
    off: pd.DataFrame, first_cols: list[dict], row_of, key: str, sort: str | None, min_n: int = 20
) -> None:
    """A table of how far off the predictions were when they said 5 minutes away, beside the
    printed timetable on the same arrivals, one row per id in `off` (off_by()): minutes off on
    average and the 8-in-10 strip for each (10 minutes away is in the row's hover text). The
    strips share one scale, so a narrow prediction strip beside a wide timetable one shows the
    prediction doing better. row_of(id, n) gives the row's href, hover (which carries the number
    of arrivals) and first columns' values. With no sort, the rows come busiest first, as the
    stop tables do."""
    if off.empty:
        st.caption(not_enough(start, wt, "measured bus arrivals"))
        return
    off = off.assign(id=off["id"].astype(str))
    at = {
        k: off[(off["basis"] == "sign") & (off["ahead_min"] == k)].set_index("id")
        for k in (5, 10, 15)
    }
    tt = off[off["basis"] == "timetable"].set_index("id")
    ids = [i for i in tt.index if i in at[15].index and at[15].loc[i, "n"] >= min_n]
    if not ids:
        st.caption(not_enough(start, wt, "measured bus arrivals"))
        return
    if sort is None:
        ids.sort(key=lambda i: -int(at[15].loc[i, "n"]))

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

    vals = {i: (spread(at[5], i), spread(tt, i)) for i in ids}
    ends = pd.DataFrame(
        [(v[2], v[3]) for vs in vals.values() for v in vs], columns=["p10", "p90"]
    ).dropna()
    lo, hi = range_scale(ends, "p10", "p90")

    rows = []
    for i in ids:
        (p_off, p_med, p_lo, p_hi), (t_off, t_med, t_lo, t_hi) = vals[i]
        base = row_of(i, int(at[15].loc[i, "n"]))
        ten = at[10].loc[i, "mean_abs_s"] if i in at[10].index else None
        ten = None if ten is None or pd.isna(ten) else float(ten)
        if p_med is not None:
            base["hover"] += f"; 5 minutes away, the typical bus came {fmt_vs(p_med, 'predicted')}"
        # 10 minutes away: here, as the chart above shows every distance
        if ten is not None:
            base["hover"] += f"; 10 minutes away, {fmt_off(ten)} off on average"
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
            "label": "Prediction, 5 min away",
            "width": "9.6em",
            "num": True,
            "help": "When LTD's prediction said the bus was 5 minutes away. " + OFF,
            "sort": "p_off_s",
            "first": "asc",
        },
        {
            "key": "p_range",
            "label": "8 in 10 buses vs prediction",
            "width": "minmax(13em, 2fr)",
            "html": True,
            "help": "Against the prediction when it said 5 minutes away (the dashed line: the "
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


TABLE_HELP = (
    "How far from the time it was given the bus came, on the same bus arrivals: LTD's "
    "prediction when it said 5 minutes away (hover a row for 10 minutes), and the printed "
    "timetable. The numbers: "
    "minutes off on average, early or late alike; lower is better. The strips: where 8 in 10 "
    "buses came, earlier (left of the dashed line) or later. Rows with at least 50 bus arrivals "
    "measured. "
    f"{PERIOD}. Click a column heading to sort; click a row for that report card's predictions."
)

with card():
    at_stop = f" at {scope.stop_name}" if scope.stop_id else ""
    st.subheader(f"Prediction accuracy by route{at_stop}{days_suffix()}", help=TABLE_HELP)
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
        min_n=20 if not scope.stop_id else MIN_RANGE,
    )

filters_again()  # the period and days buttons again, between the two tables

with card():
    on_route = f" on route {scope.route_name}" if scope.route_id else ""
    st.subheader(f"Prediction accuracy by stop{on_route}{days_suffix()}", help=TABLE_HELP)
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
        [{"key": "stop", "label": "Stop", "width": "minmax(12em, 2fr)", "bold": True}],
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
            f"Predictions for the buses coming to {scope.stop_name} now",
            help="Each solid line is a bus on its way here: the arrival time LTD predicted (up) "
            "at each moment (across). A flat line is a prediction that held; a line that keeps "
            "stepping later is the prediction correcting itself as the bus falls behind. The "
            "dashed line in the same colour is the bus's time in the printed timetable.",
        )
        hist = q_live(
            f"""
            with coming as (
                select trip_id, start_date, stop_sequence from rt.prediction_current
                where stop_id = %s
                  and coalesce(arrival_time, departure_time)
                      between now() - interval '2 minutes' and now() + interval '45 minutes'
                  and last_seen_at > now() - interval '3 minutes'
            )
            select u.trip_id, r.route_short_name as route, t.trip_headsign as headsign,
                   u.first_seen_at, u.last_seen_at,
                   coalesce(u.arrival_time, u.departure_time) as predicted,
                   se.scheduled_arrival as scheduled
            from (
                select trip_id, start_date, stop_sequence, arrival_time, departure_time,
                       first_seen_at, last_seen_at
                from rt.prediction_history
                union all
                select trip_id, start_date, stop_sequence, arrival_time, departure_time,
                       first_seen_at, last_seen_at
                from rt.prediction_current
            ) u
            join coming c using (trip_id, start_date, stop_sequence)
            join gtfs.trips t  on t.trip_id = u.trip_id and t.feed_version_id = {FV}
            join gtfs.routes r on r.route_id = t.route_id and r.feed_version_id = {FV}
            left join intermediate.int_scheduled_stop_events se
              on se.service_date = c.start_date and se.trip_id = c.trip_id
             and se.stop_sequence = c.stop_sequence
            where coalesce(u.arrival_time, u.departure_time) is not null
            order by u.trip_id, u.first_seen_at
            """,
            (scope.stop_id,),
        )
        if hist.empty:
            st.caption("No buses predicted for this stop in the next 45 minutes.")
        else:
            coming_now_chart(hist)
