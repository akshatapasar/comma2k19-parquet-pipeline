"""DuckDB access to the partitioned Parquet tables."""
from __future__ import annotations

from pathlib import Path

import duckdb

from .convert import MANIFEST, POSE_COLUMNS, SPECS, TABLE_NAMES

HIVE_TYPES = "{'dongle_id': 'VARCHAR', 'route': 'VARCHAR', 'segment': 'INTEGER'}"

_MANIFEST_COLUMNS = (
    [("chunk", "INTEGER"), ("vehicle", "VARCHAR"), ("source", "VARCHAR"),
     ("t0", "DOUBLE"), ("duration_s", "DOUBLE")]
    + [(f"n_{n}", "BIGINT") for n in TABLE_NAMES]
)


def _columns(table: str) -> list[tuple[str, str]]:
    if table == MANIFEST:
        return _MANIFEST_COLUMNS
    base = [("t", "DOUBLE"), ("t_rel", "DOUBLE")]
    if table == "pose":
        return base + [(c, "DOUBLE") for c in POSE_COLUMNS]
    spec = next(s for s in SPECS if s.name == table)
    return base + [(c, spec.sql_type(c)) for c in spec.kept]


def _quote(path: str) -> str:
    return path.replace("'", "''")


def connect(parquet_root: Path) -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB with one view per table.

    A table with no files still gets an (empty, correctly typed) view so
    queries over e.g. radar do not fail on data that has none.
    """
    parquet_root = Path(parquet_root)
    con = duckdb.connect()
    for table in [*TABLE_NAMES, MANIFEST]:
        files = list((parquet_root / table).glob("dongle_id=*/route=*/segment=*/data.parquet"))
        if files:
            glob = _quote(str(parquet_root / table / "*" / "*" / "*" / "data.parquet"))
            con.execute(
                f"CREATE VIEW {table} AS SELECT * FROM read_parquet('{glob}', "
                f"hive_partitioning = true, hive_types = {HIVE_TYPES})"
            )
        else:
            cols = ", ".join(f"CAST(NULL AS {t}) AS {c}" for c, t in _columns(table))
            con.execute(
                f"CREATE VIEW {table} AS SELECT {cols}, CAST(NULL AS VARCHAR) AS dongle_id, "
                f"CAST(NULL AS VARCHAR) AS route, CAST(NULL AS INTEGER) AS segment WHERE false"
            )
    return con
