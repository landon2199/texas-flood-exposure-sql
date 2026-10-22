"""Run the sql/ files against DuckDB (spatial) in order.

Each .sql file is plain SQL. Files can hold several named blocks, marked with a
``-- name: <block>`` comment line, so 01_load.sql can be run once per input file.
``${param}`` placeholders (paths, CRS, distance thresholds) are filled in here;
string values are quoted-escaped by the caller-facing helpers.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import duckdb

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
_BLOCK = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.M)
_PARAM = re.compile(r"\$\{(\w+)\}")

# Order matters: later files read the tables earlier files create.
PIPELINE = ("02_clean.sql", "03_exposure.sql", "04_proximity.sql", "05_summary.sql")


def connect(path: str | Path = ":memory:") -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path))
    con.execute("INSTALL spatial; LOAD spatial;")
    return con


def blocks(sql_file: Path) -> dict[str, str]:
    """Split a .sql file into its named blocks (a file without markers is one block, 'main')."""
    text = Path(sql_file).read_text(encoding="utf-8")
    parts = _BLOCK.split(text)
    if len(parts) == 1:
        return {"main": text}
    out = {}
    for name, body in zip(parts[1::2], parts[2::2]):
        out[name] = body
    return out


def render(sql: str, params: dict) -> str:
    def sub(m):
        key = m.group(1)
        if key not in params:
            raise KeyError(f"SQL parameter ${{{key}}} not supplied")
        return str(params[key])
    return _PARAM.sub(sub, sql)


def lit(value: str | Path) -> str:
    """Make a value safe inside a single-quoted SQL literal (forward slashes for paths)."""
    return str(value).replace("\\", "/").replace("'", "''")


def run_block(con, sql_file: str, block: str = "main", sql_dir: Path = SQL_DIR, **params):
    sql = blocks(sql_dir / sql_file)[block]
    con.execute(render(sql, params))


def run_file(con, sql_file: str, sql_dir: Path = SQL_DIR, **params) -> float:
    t = time.perf_counter()
    for body in blocks(sql_dir / sql_file).values():
        con.execute(render(body, params))
    return time.perf_counter() - t


def load_inputs(con, manifest: dict, sql_dir: Path = SQL_DIR):
    """01_load.sql: create the raw tables, then load each downloaded file with its block."""
    run_block(con, "01_load.sql", "schema", sql_dir)
    run_block(con, "01_load.sql", "load_counties", sql_dir, path=lit(manifest["counties"]["file"]))
    for n in manifest["nfhl"]:
        if not n.get("available"):
            continue
        run_block(con, "01_load.sql", "load_nfhl", sql_dir, path=lit(n["flood_shp"]), dfirm_id=lit(n["dfirm_id"]))
        run_block(con, "01_load.sql", "load_panels", sql_dir, path=lit(n["panel_shp"]), dfirm_id=lit(n["dfirm_id"]))
    for f in manifest["facilities"]:
        run_block(con, "01_load.sql", f"load_{f['ftype']}", sql_dir, path=lit(f["file"]))


def build(con, manifest: dict, crs: str = "EPSG:3083", near_m=(100, 250), search_m: int = 1000,
          sql_dir: Path = SQL_DIR) -> dict:
    """Run the whole SQL pipeline; returns seconds spent per file."""
    timings = {}
    t = time.perf_counter()
    load_inputs(con, manifest, sql_dir)
    timings["01_load.sql"] = time.perf_counter() - t
    params = {"crs": lit(crs), "near1_m": near_m[0], "near2_m": near_m[1], "search_m": search_m}
    for f in PIPELINE:
        timings[f] = run_file(con, f, sql_dir, **params)
    return {k: round(v, 2) for k, v in timings.items()}
