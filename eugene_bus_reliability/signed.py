"""The prediction marts' signed running counts (dbt macros/error_counts.sql): how many arrivals
came no later than each of a set of seconds against the time they were given, n_sle_m900s (15
minutes or more earlier) ... n_sle_0s ... n_sle_900s. Unlike percentiles they add up across days,
routes and stops, so the typical bus and the range 8 in 10 buses came in can be read off them for
any period and choice. Shared by the site, the plain report pages and the dump."""

from __future__ import annotations

import math

SIGNED_THRESHOLDS = [
    -900, -600, -480, -360, -300, -240, -180, -150, -120, -90, -60, -45, -30, -15,
    0, 15, 30, 45, 60, 90, 120, 150, 180, 240, 300, 360, 480, 600, 900,
]  # fmt: skip


def signed_col(t: int) -> str:
    return f"n_sle_{'m' + str(-t) if t < 0 else t}s"


SIGNED_COUNT_SUMS = ", ".join(f"sum({signed_col(t)}) as {signed_col(t)}" for t in SIGNED_THRESHOLDS)


def _missing(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def signed_percentile(row, share: float) -> float | None:
    """Seconds (negative = earlier than told) below which `share` of the arrivals in a row with
    n and the signed running counts came, straight-line between thresholds; clamped to 15
    minutes either way. None without the counts."""
    n = row.get("n")
    first = row.get(signed_col(SIGNED_THRESHOLDS[0]))
    if _missing(n) or not n or _missing(first):
        return None
    want = share * float(n)
    prev_t = prev_c = None
    for t in SIGNED_THRESHOLDS:
        c = row.get(signed_col(t))
        c = 0.0 if _missing(c) else float(c)
        if c >= want:
            if prev_t is None or c == prev_c:
                return float(t)
            return prev_t + (want - prev_c) / (c - prev_c) * (t - prev_t)
        prev_t, prev_c = float(t), c
    return float(SIGNED_THRESHOLDS[-1])