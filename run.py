"""Which critical facilities sit in mapped flood hazard areas? (Texas Gulf Coast)

Downloads FEMA NFHL county packages and facility points, runs sql/01..06 in
DuckDB (spatial), cross-checks the SFHA counts with GeoPandas, and writes
results/, figures/ and docs/map.html.

    python run.py                                  # defaults: 5 counties, 4 facility types
    python run.py --counties 48167 48355 --facility-types hospital public_school
    python run.py --crs EPSG:6580 --skip-download  # reuse data/raw, other projected CRS
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("MKL_THREADING_LAYER", "SEQUENTIAL")  # see floodsql/__init__.py

import floodsql  # noqa: E402  (sets PATH for the conda DLLs before geo imports)
from floodsql import COUNTIES, FACILITY_TYPES, db, download, maps, report  # noqa: E402

ROOT = Path(__file__).resolve().parent


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--counties", nargs="+", default=list(COUNTIES), help="county FIPS codes (default: %(default)s)")
    p.add_argument("--facility-types", nargs="+", default=list(FACILITY_TYPES), choices=FACILITY_TYPES)
    p.add_argument("--crs", default="EPSG:3083", help="projected CRS in metres (default: Texas Centric Albers)")
    p.add_argument("--near", nargs=2, type=int, default=[100, 250], help="proximity thresholds in metres")
    p.add_argument("--search", type=int, default=1000, help="max distance searched for the nearest SFHA (m)")
    p.add_argument("--bands", nargs="+", type=int, default=list(db.BANDS_M),
                   help="band widths (m) for the sensitivity table (default: %(default)s)")
    p.add_argument("--data-dir", type=Path, default=ROOT / "data")
    p.add_argument("--skip-download", action="store_true", help="use data/raw/manifest.json as is")
    p.add_argument("--refresh", action="store_true", help="download again even if files exist")
    p.add_argument("--no-maps", action="store_true")
    return p.parse_args()


def main():
    a = parse_args()
    t0 = time.perf_counter()
    data = a.data_dir.resolve()
    print("1/5 download")
    if a.skip_download:
        manifest = json.loads((data / "raw" / "manifest.json").read_text())
    else:
        manifest = download.fetch_all(a.counties, a.facility_types, data, refresh=a.refresh)
    missing = [n["county_fips"] for n in manifest["nfhl"] if not n.get("available")]
    if missing:
        print(f"   no effective NFHL county package for {missing}; those counties are skipped")
    t_dl = time.perf_counter() - t0

    print("2/5 SQL pipeline (DuckDB)")
    db_path = data / "flood.duckdb"
    db_path.unlink(missing_ok=True)
    con = db.connect(db_path)
    timings = db.build(con, manifest, a.crs, near_m=tuple(a.near), search_m=a.search, bands_m=tuple(a.bands))
    print("   ", timings)

    print("3/5 exports")
    report.export(con, ROOT / "results", data / "out", a.crs)

    print("4/5 GeoPandas cross-check")
    t = time.perf_counter()
    cross = report.crosscheck(con, manifest, a.crs, a.search, tuple(a.near), tuple(a.bands))
    timings["crosscheck_geopandas"] = round(time.perf_counter() - t, 2)
    print(f"    agree (clean tables): {cross['agree_clean']}   agree (raw files): {cross['agree_raw']}   "
          f"distances and bands: {cross['agree_distance']} (max diff {cross['distance_check']['max_abs_diff_m']} m)")

    if not a.no_maps:
        print("5/5 figures and web map")
        t = time.perf_counter()
        (ROOT / "figures").mkdir(exist_ok=True)
        (ROOT / "docs").mkdir(exist_ok=True)
        counties, zones, fac = maps.load_layers(con, a.crs)
        summary = con.execute("SELECT * FROM exposure_summary").df()
        totals = summary[(summary.county == "All") & (summary.facility_type == "All")].iloc[0].to_dict()
        maps.hero(counties, zones, fac, totals, ROOT / "figures" / "hero.png")
        maps.county_maps(counties, zones, fac, ROOT / "figures" / "county_maps.png")
        maps.exposure_bars(summary, ROOT / "figures" / "exposure_by_type_county.png")
        maps.proximity_plot(fac, tuple(a.near), a.search, ROOT / "figures" / "proximity_distribution.png")
        size = maps.web_map(con, fac, a.crs, ROOT / "docs" / "map.html")
        print(f"    docs/map.html {size / 1e6:.2f} MB")
        timings["figures_and_map"] = round(time.perf_counter() - t, 2)

    timings["download_or_cache"] = round(t_dl, 2)
    timings["total"] = round(time.perf_counter() - t0, 2)
    run_args = {"counties": a.counties, "facility_types": a.facility_types, "near_m": a.near, "search_m": a.search, "bands_m": a.bands,
                "python": sys.version.split()[0], "duckdb": __import__("duckdb").__version__,
                "spatial_extension": con.execute("SELECT extension_version FROM duckdb_extensions() "
                                                 "WHERE extension_name = 'spatial'").fetchone()[0],
                "package": floodsql.__version__}
    m = report.metrics(con, manifest, a.crs, cross, timings, run_args)
    report.write_metrics(m, ROOT / "results" / "metrics.json")
    con.close()
    print(f"done in {timings['total']:.0f} s")
    if not (cross["agree_clean"] and cross["agree_raw"] and cross["agree_distance"]):
        sys.exit("GeoPandas cross-check disagrees with SQL; see results/metrics.json")


if __name__ == "__main__":
    main()
