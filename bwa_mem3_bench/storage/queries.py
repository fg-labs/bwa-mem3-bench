"""DataFrame-level helpers wrapping `bwa_mem3_bench.storage.sqlite`."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from bwa_mem3_bench.storage.sqlite import connect


def query_df(
    db_path: Path,
    sql: str,
    params: tuple[object, ...] = (),
) -> pd.DataFrame:
    """Open a connection, run `pd.read_sql`, close, return the DataFrame."""
    conn = connect(db_path)
    try:
        return pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()
