"""Thin database helpers. All SQL that matters lives next to the code that runs it."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import psycopg
from psycopg import sql


def copy_rows(
    conn: psycopg.Connection,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[object]],
) -> int:
    """Bulk-load rows with COPY. `table` may be schema-qualified."""
    schema, _, name = table.rpartition(".")
    target = sql.Identifier(schema, name) if schema else sql.Identifier(name)
    stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
        target, sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    )
    n = 0
    with conn.cursor() as cur, cur.copy(stmt) as copy:
        for row in rows:
            copy.write_row(row)
            n += 1
    return n
