"""The order a bus reaches the stops of a route, in one direction, from the timetable.

A route's trips don't all stop at the same stops: some start or end part way along (short
trips, trips to or from the garage), and a loop route passes its first stop again at the end
of the trip. So neither a stop's smallest stop number (stop_sequence) nor one trip's stop list
puts every stop in order. Here the longest stop list sets the order, and each other trip's
stops are fitted into it in their own order: a stop it shares is matched with the first place
that stop has after the trip's previous stop (so a loop's last stop is matched with the end of
the loop, not its start), and a stop no longer list has goes between its neighbours.

Each place in the order is one visit to a stop: on a loop the first stop has two places, its
start and its end, which are timed very differently.
"""

from __future__ import annotations

from collections import defaultdict

import psycopg
from psycopg.rows import tuple_row


def merge_patterns(patterns: list[list[str]]) -> tuple[list[str], list[list[int]]]:
    """patterns: each distinct stop list (stop ids in stop_sequence order), most important
    first. Returns the merged order (stop ids, a stop repeated for each separate visit) and,
    for each pattern, the place in that order of each of its stops."""
    order: list[str] = []
    places: list[list[int]] = []
    for pattern in patterns:
        if not order:
            order = list(pattern)
            places.append(list(range(len(pattern))))
            continue
        # match each stop with its first place after the previous match; stops with no match
        # wait and go in just after the previous match (or before the first one)
        matched: list[int | None] = []
        cursor = -1
        for stop in pattern:
            j = next((i for i in range(cursor + 1, len(order)) if order[i] == stop), None)
            matched.append(j)
            if j is not None:
                cursor = j
        if all(m is None for m in matched):  # nothing in common: after everything
            start = len(order)
            order.extend(pattern)
            places.append(list(range(start, start + len(pattern))))
            continue
        # insert the unmatched stops, working from the end so earlier indexes stay valid
        first = next(m for m in matched if m is not None)
        new_at: dict[int, list[int]] = defaultdict(list)  # insert point -> pattern indexes
        prev = None
        for k, m in enumerate(matched):
            if m is None:
                new_at[first if prev is None else prev + 1].append(k)
            else:
                prev = m
        final: list[int | None] = list(matched)
        for point in sorted(new_at, reverse=True):
            ks = new_at[point]
            order[point:point] = [pattern[k] for k in ks]
            shift = len(ks)
            # everything already placed at or after the insert point moves along
            for p_list in places:
                for i, v in enumerate(p_list):
                    if v >= point:
                        p_list[i] = v + shift
            for i, v in enumerate(final):
                if v is not None and v >= point:
                    final[i] = v + shift
            for n, k in enumerate(ks):
                final[k] = point + n
        places.append([int(v) for v in final if v is not None])
    return order, places


def route_stop_order(
    conn: psycopg.Connection, feed_version_id: int, route_id: str, direction: int
) -> tuple[list[str], dict[tuple[str, int], int]]:
    """The order of one route's stops in one direction (stop ids; a stop visited twice on a
    trip appears twice), and the place in it of each (stop_id, stop_sequence) the timetable
    has. Keyed by stop and stop number rather than by trip, so arrivals on trips of an older
    timetable with the same stop lists still find their place."""
    rows = (
        conn.cursor(row_factory=tuple_row)
        .execute(
            """
        select array_agg(st.stop_id::text order by st.stop_sequence) as stops,
               array_agg(st.stop_sequence order by st.stop_sequence) as seqs
        from gtfs.stop_times st
        join gtfs.trips t on t.trip_id = st.trip_id and t.feed_version_id = st.feed_version_id
        where st.feed_version_id = %s and t.route_id = %s and t.direction_id = %s
        group by st.trip_id
        """,
            (feed_version_id, route_id, direction),
        )
        .fetchall()
    )
    trips: dict[tuple[str, ...], int] = defaultdict(int)  # stop list -> trips using it
    seqs_of: dict[tuple[str, ...], list[int]] = {}
    for stops, seqs in rows:
        trips[tuple(stops)] += 1
        seqs_of[tuple(stops)] = list(seqs)
    # the longest stop list first (ties: the one most trips use), then by how many trips use it
    patterns = sorted(trips, key=lambda p: (-len(p), -trips[p], p))
    patterns = patterns[:1] + sorted(patterns[1:], key=lambda p: (-trips[p], -len(p), p))
    order, places = merge_patterns([list(p) for p in patterns])
    # where stop lists disagree on a (stop, stop number), the place most trips give it
    votes: dict[tuple[str, int], dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for pattern, pl in zip(patterns, places, strict=True):
        for stop, seq, place in zip(pattern, seqs_of[pattern], pl, strict=True):
            votes[(stop, seq)][place] += trips[pattern]
    place_of = {k: max(v, key=lambda p: (v[p], -p)) for k, v in votes.items()}
    return order, place_of
