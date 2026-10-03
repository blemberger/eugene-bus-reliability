"""Arrivals: what's about to happen and what just happened on a route — the processed
data as it comes in. One block per bus, with what each stop usually does at this hour."""

from __future__ import annotations

import math

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from common import (
    LIVE_CHECK_SECONDS,
    LOCAL_TZ,
    add_honest_columns,
    clean_headsigns,
    col_ago,
    col_count,
    col_late,
    col_likely,
    col_minutes,
    col_pct,
    col_stop,
    col_time,
    col_usual,
    current_fv,
    data_now,
    empty_message,
    fmt_ago,
    fmt_delay,
    fmt_time,
    is_mobile,
    late_minutes,
    live_marker,
    live_status_line,
    local_times,
    marts_ready,
    q,
    q_fresh,
    require_db,
    route_picker,
    schedule_join,
    share_pct,
    stop_link,
    table,
)

require_db()
st.title("Arrivals board")
st.caption(
    "Upcoming stops with what to expect, and stops buses have just left. Updates as LTD's data arrives (about every 30 s)."
)
FV = str(current_fv())  # schedule version in force today

routes = q(
    f"select route_id, route_short_name from gtfs.routes where feed_version_id = {FV} order by length(route_short_name), route_short_name"
)
route_id = route_picker(routes, key="board_route")
if route_id:
    st.subheader(f"Route {routes.loc[routes['route_id'] == route_id, 'route_short_name'].iloc[0]}")
else:
    st.subheader("All routes")


ALL_ROUTES_WINDOW_MIN = 3
ONE_ROUTE_WINDOW_MIN = 15


LATENESS_SCALE = ["#e6a100", "#2e8b57", "#d9534f", "#8b0000"]


FLOW_BLOCKS = {
    -3: "2–3 min ago",
    -2: "1–2 min ago",
    -1: "under 1 min ago",
    0: "due within 1 min",
    1: "in 1–2 min",
    2: "in 2–3 min",
}
FLOW_BLOCKS_SHORT = {
    -3: "2–3 ago",
    -2: "1–2 ago",
    -1: "<1 ago",
    0: "<1 min",
    1: "1–2 min",
    2: "2–3 min",
}
FLOW_BIN_SECONDS = 15  # width of the time columns inside each one-minute block
PASSING = 99  # block id for "passing now": due, not yet confirmed as left


def _ago(seconds: float) -> str:
    s = int(round(abs(seconds)))
    return f"{s} s" if s < 60 else f"{s // 60} min {s % 60:02d} s"


def flow_chart(
    upcoming: pd.DataFrame, left: pd.DataFrame, now: pd.Timestamp, window_min: int
) -> None:
    cols = ["route", "t", "stop", "delay_s", "headsign", "kind"]
    fut = upcoming.assign(t=upcoming["predicted"], stop=upcoming["stop_name"], kind="coming")[cols]
    past = (
        left.assign(
            t=pd.to_datetime(left["departed"], utc=True), stop=left["stop_name"], kind="left"
        )[cols]
        if not left.empty
        else fut.iloc[0:0]
    )
    df = pd.concat([past, fut], ignore_index=True)
    df["sec"] = (df["t"] - now).dt.total_seconds()
    # A bus whose predicted time has passed but whose departure the feed hasn't confirmed yet
    # (that takes a later message, 20-50 s) is "passing now": drawn on the line itself, not
    # counted as due or as left. Without this, those buses inflated "due within 1 min".
    df.loc[(df["kind"] == "coming") & (df["sec"] < 0), "kind"] = "passing"
    df["block"] = [
        PASSING
        if k == "passing"
        else (-min(3, max(1, math.ceil(-s / 60))) if k == "left" else min(3, math.floor(s / 60)))
        for s, k in zip(df["sec"], df["kind"], strict=False)
    ]
    df = df[
        (df["block"].between(-3, 2) | (df["block"] == PASSING)) & (df["sec"] >= -60 * window_min)
    ].copy()
    if df.empty:
        return
    df["route"] = df["route"].astype(str)
    df["toward"] = clean_headsigns(df)
    mobile = is_mobile()
    # Each minute is split into columns by the second: a badge sits in the column of its time
    # and a column stacks its badges in time order, earliest at the top. 15 s columns; 30 s on
    # a phone, where a minute is too narrow for four badges across.
    bin_s = 30 if mobile else FLOW_BIN_SECONDS
    df["bin"] = [  # "passing now" badges get one column of their own, centred on the line
        PASSING if k == "passing" else math.floor(s / bin_s)
        for s, k in zip(df["sec"], df["kind"], strict=False)
    ]
    df = df.sort_values(["bin", "sec"])
    on_line = df["bin"] == PASSING
    df["y"] = df.groupby("bin").cumcount()
    centre = (df["bin"] + 0.5) * bin_s / 60
    # the columns either side of "now" keep clear of the badges drawn on the line
    df["x"] = centre.where(centre.abs() >= 0.19, 0.19 * centre.map(lambda c: 1 if c > 0 else -1))
    df.loc[on_line, "x"] = 0.0
    df["late_min"] = df["delay_s"] / 60
    df["when"] = [
        f"left {fmt_time(t)} ({_ago(s)} ago)"
        if k == "left"
        else (
            f"due {fmt_time(t)} (in {_ago(s)})"
            if s >= 0
            else f"due {fmt_time(t)}, {_ago(s)} before this data; not yet confirmed as left"
        )
        for t, s, k in zip(df["t"], df["sec"], df["kind"], strict=False)
    ]
    df["late_txt"] = [
        "lateness unknown"
        if d is None or pd.isna(d)
        else (f"left {fmt_delay(d)}" if k == "left" else f"projected {fmt_delay(d)} at this stop")
        for d, k in zip(df["delay_s"], df["kind"], strict=False)
    ]
    rows = int(df["y"].max()) + 1
    fig = go.Figure(
        go.Scatter(
            x=df["x"],
            y=df["y"],
            mode="markers+text",
            text=df["route"],
            textfont={
                "color": "white",
                "size": 10 if mobile else 11,
                "family": "system-ui, sans-serif",
            },
            marker={
                "size": 22 if mobile else 27,
                "color": df["late_min"],
                "colorscale": LATENESS_SCALE,
                "cmin": -3,
                "cmax": 12,
                "line": {
                    "width": [2.5 if k == "passing" else 1 for k in df["kind"]],
                    "color": ["#222" if k == "passing" else "white" for k in df["kind"]],
                },
                "opacity": [0.45 if k == "left" else 1.0 for k in df["kind"]],
                "colorbar": {"title": {"text": "min late"}, "thickness": 12},
                "showscale": not mobile,  # on a phone the color scale takes too much width
            },
            customdata=list(
                zip(df["toward"], df["stop"], df["when"], df["late_txt"], strict=False)
            ),
            hovertemplate=(
                "<b>Route %{text} → %{customdata[0]}</b><br>Stop: %{customdata[1]}<br>"
                "%{customdata[2]}<br>%{customdata[3]}<extra></extra>"
            ),
        )
    )
    for b in range(-3, 3):  # light separators between the one-minute blocks
        if b != 0:
            fig.add_vline(x=b, line_width=1, line_color="#ccc")
    step = bin_s / 60  # fainter dashed lines between the columns inside each minute
    for k in range(int(-3 / step), int(3 / step)):
        if abs(k * step - round(k * step)) > 1e-9:
            fig.add_vline(x=k * step, line_width=1, line_color="#e3e3e3", line_dash="dot")
    fig.add_vline(
        x=0, line_width=3, line_color="#222", layer="below"
    )  # badges on the line stay readable
    counts = df.groupby("block").size()
    fig.update_layout(
        xaxis={
            "range": [-3, 3],
            "tickvals": [b + 0.5 for b in FLOW_BLOCKS],
            "ticktext": [
                f"{(FLOW_BLOCKS_SHORT if mobile else FLOW_BLOCKS)[b]}<br>({counts.get(b, 0)})"
                for b in FLOW_BLOCKS
            ],
            "tickfont": {"size": 11 if mobile else 14},
            "showgrid": False,
            "zeroline": False,
            "side": "top",
            "fixedrange": True,
        },
        yaxis={"visible": False, "range": [rows + 0.2, -0.8]},
        height=max(300, (28 if mobile else 34) * rows + 130),
        margin={"l": 10, "r": 10, "t": 84, "b": 30},
        showlegend=False,
        annotations=[  # the two labels sit either side of the "now" line
            {
                "x": -0.04,
                "y": 1.0,
                "xref": "x",
                "yref": "paper",
                "yshift": 70,
                "xanchor": "right",
                "showarrow": False,
                "text": "<b>← just left</b>" if mobile else "<b>← just left a stop</b>",
                "font": {"size": 14 if mobile else 17},
            },
            {
                "x": 0.04,
                "y": 1.0,
                "xref": "x",
                "yref": "paper",
                "yshift": 70,
                "xanchor": "left",
                "showarrow": False,
                "text": "<b>coming up →</b>" if mobile else "<b>coming up to a stop →</b>",
                "font": {"size": 14 if mobile else 17},
            },
            {
                "x": 0,
                "y": 0,
                "xref": "x",
                "yref": "paper",
                "yshift": -4,
                "yanchor": "top",
                "showarrow": False,
                "text": f"passing now ({int((df['kind'] == 'passing').sum())})",
                "font": {"size": 11 if mobile else 13},
            },
        ],
    )
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    st.caption(
        "Each badge is one bus at one stop, labelled with its route number, as of the time of LTD's data "
        "above. Right of the line: due at a stop in that minute. Left: left a stop in that minute (faded). "
        "On the line (dark outline): due now or just past due, but the feed hasn't confirmed yet that the bus "
        "left; a departure can only be confirmed by a later message, so these move left once one arrives. "
        "Inside each minute, dotted lines split it into 15-second columns (30 seconds on a phone): a badge "
        "sits in the column of its time, and each column is stacked in time order, earliest at the top. Color = minutes late: how late it left, or how late "
        "LTD's prediction puts it at that stop. Hover a badge for the stop and exact times."
    )


def route_chart(upcoming: pd.DataFrame, left: pd.DataFrame, now: pd.Timestamp) -> None:
    fut = upcoming.assign(t=upcoming["predicted"].dt.tz_convert(LOCAL_TZ), kind="predicted")[
        ["trip_id", "Bus", "stop_name", "t", "delay_s", "kind", "direction_id", "stop_sequence"]
    ]
    bus_of = dict(zip(upcoming["trip_id"], upcoming["Bus"], strict=False))
    past = left[left["trip_id"].isin(bus_of)].copy() if not left.empty else left
    if not past.empty:
        past = past.assign(
            t=pd.to_datetime(past["departed"], utc=True).dt.tz_convert(LOCAL_TZ),
            Bus=past["trip_id"].map(bus_of),
            kind="left",
            direction_id=None,
        )[["trip_id", "Bus", "stop_name", "t", "delay_s", "kind", "direction_id", "stop_sequence"]]
        df = pd.concat([past, fut], ignore_index=True)
    else:
        df = fut
    order = list(
        upcoming.sort_values(["direction_id", "stop_sequence"])["stop_name"].drop_duplicates()
    )
    order += [s for s in df["stop_name"].dropna().unique() if s not in order]
    symbols = [
        "circle",
        "square",
        "diamond",
        "triangle-up",
        "star",
        "hexagon",
        "cross",
        "x",
        "pentagon",
    ]
    fig = go.Figure()
    for i, (bus, g) in enumerate(df.groupby("Bus", sort=False)):
        for kind, gg in g.groupby("kind"):
            fig.add_trace(
                go.Scatter(
                    x=gg["t"],
                    y=gg["stop_name"],
                    mode="markers",
                    name=bus,
                    legendgroup=bus,
                    showlegend=kind == "predicted",
                    marker={
                        "symbol": symbols[i % len(symbols)],
                        "size": 13,
                        "color": gg["delay_s"] / 60,
                        "colorscale": LATENESS_SCALE,
                        "cmin": -3,
                        "cmax": 12,
                        "opacity": 0.4 if kind == "left" else 1.0,
                        "line": {"width": 1, "color": "white"},
                    },
                    customdata=[
                        (f"left {fmt_delay(d)}" if kind == "left" else f"projected {fmt_delay(d)}")
                        for d in gg["delay_s"]
                    ],
                    hovertemplate=(
                        f"<b>{bus}</b><br>Stop: %{{y}}<br>"
                        + ("Left" if kind == "left" else "Predicted")
                        + " %{x|%-I:%M %p}<br>%{customdata}<extra></extra>"
                    ),
                )
            )
    fig.add_vline(x=now.tz_convert(LOCAL_TZ), line_width=2, line_color="#333")
    fig.update_layout(
        xaxis_title="time (Eugene) — the vertical line is now",
        yaxis={
            "title": "",
            "categoryorder": "array",
            "categoryarray": order,
            "autorange": "reversed",
        },
        height=max(320, 24 * df["stop_name"].nunique() + 100),
        legend_title="",
        margin={"l": 0, "r": 0, "t": 10, "b": 0},
    )
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    st.caption(
        "Each point is one bus at one stop: stops in route order down the side, time across. Solid points are "
        "LTD's predicted times still to come; faded points are stops just left. Color = minutes late "
        "(projected for stops still to come). Hover a point for details."
    )


@st.fragment(run_every=f"{LIVE_CHECK_SECONDS}s")
def board() -> None:
    sj, sched = schedule_join("p")
    has_marts = marts_ready()
    live_status_line()
    marker = live_marker()["fid"]
    window_min = ONE_ROUTE_WINDOW_MIN if route_id else ALL_ROUTES_WINDOW_MIN

    stats_join = stats_cols = ""
    if has_marts:
        stats_join = """
            left join marts.mart_stop_route_hourly m
              on m.stop_id = p.stop_id and m.route_id = t.route_id
             and m.direction_id is not distinct from t.direction_id
             and m.hour_local = extract(hour from coalesce(p.arrival_time, p.departure_time) at time zone 'America/Los_Angeles')::int
             and m.weekday_type = case extract(isodow from p.start_date) when 6 then 'saturday' when 7 then 'sunday' else 'weekday' end"""
        stats_cols = ", m.n_events, m.n_on_time, m.median_delay_s"
    upcoming = q_fresh(
        f"""
        select p.trip_id, p.stop_sequence, p.stop_id, t.route_id, t.trip_headsign as headsign, t.direction_id, s.stop_name, r.route_short_name as route,
               coalesce(stt.timepoint, case when stt.arrival_seconds is null then 0 else 1 end) = 1 as is_timepoint,
               coalesce(p.arrival_time, p.departure_time) as predicted, {sched} as scheduled,
               extract(epoch from coalesce(p.arrival_time, p.departure_time) - {sched})::int as delay_s
               {stats_cols}
        from rt.prediction_current p
        join gtfs.trips t  on t.trip_id = p.trip_id and t.feed_version_id = {FV}
        join gtfs.routes r on r.route_id = t.route_id and r.feed_version_id = {FV}
        left join gtfs.stops s on s.stop_id = p.stop_id and s.feed_version_id = {FV}
        left join gtfs.stop_times stt on stt.feed_version_id = {FV} and stt.trip_id = p.trip_id
                                     and stt.stop_sequence = p.stop_sequence
        left join lateral (
            select x.last_seen_at as next_seen from rt.prediction_current x
            where x.trip_id = p.trip_id and x.start_date = p.start_date
              and x.stop_sequence > p.stop_sequence
            order by x.stop_sequence limit 1
        ) nx on true
        {sj}
        {stats_join}
        where (%s::text is null or t.route_id = %s)
          -- still to come, or due in the last 2 minutes but not yet confirmed as left by the
          -- same rule "Just left" uses (so each stop event is in exactly one of the two lists)
          and coalesce(p.arrival_time, p.departure_time)
              between now() - interval '2 minutes' and now() + make_interval(mins => %s)
          and not (
              coalesce(p.arrival_time, p.departure_time) < now()
              and (p.last_seen_at >= coalesce(p.arrival_time, p.departure_time) + interval '20 seconds'
                   or nx.next_seen >= coalesce(p.arrival_time, p.departure_time) + interval '20 seconds')
          )
          and p.last_seen_at > now() - interval '3 minutes'
        order by p.trip_id, p.stop_sequence
        limit 2000
        """,
        (route_id, route_id, window_min),
        marker=marker,
    )

    # ---- just left: the feed's settled departure times, live -----------------------------
    left = q_fresh(
        f"""
        with left_rows as (
            select p.trip_id, p.stop_sequence, p.stop_id, t.trip_headsign as headsign, s.stop_name, r.route_short_name as route,
                   coalesce(p.arrival_time, p.departure_time) as departed, {sched} as scheduled,
                   extract(epoch from coalesce(p.arrival_time, p.departure_time) - {sched})::int as delay_s,
                   nx.stop_id as next_stop_id, ns.stop_name as next_stop, nx.next_time
            from rt.prediction_current p
            join gtfs.trips t  on t.trip_id = p.trip_id and t.feed_version_id = {FV}
            join gtfs.routes r on r.route_id = t.route_id and r.feed_version_id = {FV}
            left join gtfs.stops s on s.stop_id = p.stop_id and s.feed_version_id = {FV}
            left join lateral (
                select x.stop_id, coalesce(x.arrival_time, x.departure_time) as next_time,
                       x.last_seen_at as next_seen
                from rt.prediction_current x
                where x.trip_id = p.trip_id and x.start_date = p.start_date
                  and x.stop_sequence > p.stop_sequence
                order by x.stop_sequence limit 1
            ) nx on true
            left join gtfs.stops ns on ns.stop_id = nx.stop_id and ns.feed_version_id = {FV}
            {sj}
            where (%s::text is null or t.route_id = %s)
              and coalesce(p.arrival_time, p.departure_time) between now() - make_interval(mins => %s) and now()
              -- A stop counts as left when the feed kept reporting it after its time, OR when the
              -- bus's next stop was still being updated after it (the feed often drops a stop once
              -- the bus has passed it). `make dump` (BOARD BALANCE) measures both rules.
              and (p.last_seen_at >= coalesce(p.arrival_time, p.departure_time) + interval '20 seconds'
                   or nx.next_seen >= coalesce(p.arrival_time, p.departure_time) + interval '20 seconds')
        )
        select * from left_rows order by departed desc limit 500
        """,
        (route_id, route_id, window_min),
        marker=marker,
    )
    st.markdown(
        f"**Coming up in the next {window_min} minutes**"
        if route_id
        else f"**Around now: {window_min} minutes either side**"
    )
    if upcoming.empty:
        st.info(empty_message(f"Nothing predicted in the next {window_min} minutes."))
    else:
        now = data_now()  # "as of" the newest data, so the board changes only with new data
        upcoming["predicted"] = pd.to_datetime(upcoming["predicted"], utc=True)
        upcoming["mins"] = ((upcoming["predicted"] - now).dt.total_seconds() / 60).clip(lower=0)
        upcoming["toward"] = clean_headsigns(upcoming)
        # several buses can share a destination; number them by their first upcoming stop
        first = upcoming.groupby("trip_id")["predicted"].min().sort_values()
        label = {
            tid: f"Toward {upcoming.loc[upcoming.trip_id == tid, 'toward'].iloc[0]} · bus {i + 1}"
            for i, tid in enumerate(first.index)
        }
        upcoming["Bus"] = upcoming["trip_id"].map(label)
        upcoming["Lateness (min)"] = upcoming["delay_s"] / 60

        if route_id is None:
            flow_chart(upcoming, left, now, window_min)
        else:
            route_chart(upcoming, left, now)

        # ---- one block per bus (one route only) ----
        for tid in first.index if route_id else []:
            block = add_honest_columns(
                upcoming[upcoming["trip_id"] == tid], "mins", "route_id", "is_timepoint"
            )
            running = block["delay_s"].iloc[0]
            st.markdown(
                f"**Route {block['route'].iloc[0]} {label[tid]}** — projected {fmt_delay(running)} at its next stop "
                f"(LTD's prediction vs the timetable) · next stop in {block['mins'].iloc[0]:.0f} min"
            )
            out = pd.DataFrame(
                {
                    "Stop": [
                        stop_link(i, n)
                        for i, n in zip(block["stop_id"], block["stop_name"], strict=False)
                    ],
                    "Predicted": local_times(block["predicted"]).to_numpy(),
                    "In": block["mins"].round().to_numpy(),
                    "Likely in": block["likely_min"].to_numpy(),
                    "80% of the time": block["usual_range"].to_numpy(),
                    "Scheduled": local_times(block["scheduled"]).to_numpy(),
                }
            )
            config = {
                "Stop": col_stop("Stop"),
                "Predicted": col_time("Predicted"),
                "In": col_minutes("In", help="Minutes until LTD's predicted time (0 = now)."),
                "Likely in": col_likely(),
                "80% of the time": col_usual(),
                "Scheduled": col_time("Scheduled"),
            }
            if has_marts:
                out["Usually on time"] = share_pct(block["n_on_time"], block["n_events"]).to_numpy()
                out["Usually runs"] = late_minutes(block["median_delay_s"]).to_numpy()
                out["Sample"] = block["n_events"].to_numpy()
                config |= {
                    "Usually on time": col_pct("Usually on time", bar=True),
                    "Usually runs": col_late("Usually runs (min late)"),
                    "Sample": col_count("Sample", help="Arrivals behind the 'usually' numbers."),
                }
            table(out, hide_index=True, width="stretch", column_config=config)
        if has_marts and route_id:
            st.caption(
                "'Usually' = this stop, this route and direction, this hour of day, on this kind of day, across all collected days."
            )

    st.markdown(f"**Just left: every stop in the last {window_min} minutes (from the feed)**")
    if left.empty:
        st.caption(f"No departures recorded in the last {window_min} minutes.")
    else:
        table(
            pd.DataFrame(
                {
                    "Route": left["route"],
                    "Left": [
                        stop_link(i, n)
                        for i, n in zip(left["stop_id"], left["stop_name"], strict=False)
                    ],
                    "At": local_times(left["departed"]),
                    "Scheduled": local_times(left["scheduled"]),
                    "Min late": late_minutes(left["delay_s"]),
                    "Next stop": [
                        stop_link(i, n)
                        for i, n in zip(left["next_stop_id"], left["next_stop"], strict=False)
                    ],
                    "Due there": local_times(left["next_time"]),
                    "Toward": clean_headsigns(left),
                    "Ago": local_times(left["departed"]),
                }
            ),
            hide_index=True,
            width="stretch",
            column_config={
                "Left": col_stop("Left"),
                "Next stop": col_stop("Next stop"),
                "At": col_time("At"),
                "Scheduled": col_time("Scheduled"),
                "Min late": col_late(
                    "Min late",
                    help="How late the bus left this stop, by the feed's final time for it; negative = early.",
                ),
                "Due there": col_time("Due there"),
                "Ago": col_ago(),
            },
        )
        st.caption(
            "The feed keeps reporting a stop's time after the bus has passed; that final value is LTD's record of the departure "
            "(exact at timepoints, its last prediction elsewhere). Toward = the destination on the bus's sign; "
            "an empty Next stop means the end of the trip. Stop names open that stop's page."
        )

    # ---- observed arrivals from positions, as of the last analysis build ----------------
    if has_marts and route_id:
        obs = q(
            f"""
            select s.stop_name, o.vehicle_id, o.observed_arrival, o.uncertainty_s, o.scheduled_arrival,
                   extract(epoch from o.observed_arrival - o.scheduled_arrival)::int as delay_s
            from intermediate.int_observed_arrivals o
            left join gtfs.stops s on s.stop_id = o.stop_id and s.feed_version_id = {FV}
            where o.route_id = %s and o.is_bounded and o.is_plausible
              and o.observed_arrival > now() - interval '45 minutes'
            order by o.observed_arrival desc limit 60
            """,
            (route_id,),
        )
        built = q("select max(finished_at) as t from analytics.build_log").iloc[0]["t"]
        st.markdown(
            f"**Arrivals measured from positions** (as of the last analysis build, {fmt_ago(built)})"
        )
        if obs.empty:
            st.caption("None in the last 45 minutes at the last build.")
        else:
            table(
                pd.DataFrame(
                    {
                        "Stop": obs["stop_name"],
                        "Arrived": local_times(obs["observed_arrival"]),
                        "±": obs["uncertainty_s"],
                        "Scheduled": local_times(obs["scheduled_arrival"]),
                        "Min late": late_minutes(obs["delay_s"]),
                        "Bus": obs["vehicle_id"],
                    }
                ),
                hide_index=True,
                width="stretch",
                column_config={
                    "Arrived": col_time("Arrived"),
                    "±": col_minutes("±", fmt="%.0f s", help="Uncertainty of the measured time."),
                    "Scheduled": col_time("Scheduled"),
                    "Min late": col_late(),
                },
            )


board()
