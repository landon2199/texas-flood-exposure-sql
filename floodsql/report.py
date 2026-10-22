"""Exports, the GeoPandas cross-check and results/metrics.json."""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

SFHA_ZONES = ("A", "AE", "AH", "AO", "AR", "A99", "V", "VE")
RANK = {"SFHA": 1, "X_0.2PCT": 2, "X_LEVEE": 3, "X_MINIMAL": 4, "OPEN_WATER": 5, "D": 6, "NOT_INCLUDED": 7, "OTHER": 8}


def classify(zone: pd.Series, subty: pd.Series) -> pd.Series:
    """Python twin of the CASE expression in 02_clean.sql (kept independent on purpose)."""
    subty = subty.fillna("")
    out = np.select(
        [zone.isin(SFHA_ZONES),
         (zone == "X") & subty.str.startswith("0.2 PCT ANNUAL CHANCE"),
         (zone == "X") & subty.str.contains("REDUCED FLOOD RISK DUE TO LEVEE"),
         zone == "X", zone == "D", zone == "OPEN WATER", zone == "AREA NOT INCLUDED"],
        ["SFHA", "X_0.2PCT", "X_LEVEE", "X_MINIMAL", "D", "OPEN_WATER", "NOT_INCLUDED"], default="OTHER")
    return pd.Series(out, index=zone.index)


def _gdf(con, sql: str, crs) -> gpd.GeoDataFrame:
    df = con.execute(sql).df()
    geom = shapely.from_wkb(df.pop("wkb").map(bytes))
    return gpd.GeoDataFrame(df, geometry=geom, crs=crs)


def _best_class(points: gpd.GeoDataFrame, zones: gpd.GeoDataFrame) -> pd.Series:
    j = gpd.sjoin(points[["fac_id", "geometry"]], zones[["zone_class", "geometry"]], predicate="intersects", how="left")
    j["rank"] = j["zone_class"].map(RANK).fillna(99)
    best = j.sort_values("rank").drop_duplicates("fac_id").set_index("fac_id")["zone_class"]
    return best.fillna("UNMAPPED").reindex(points["fac_id"]).fillna("UNMAPPED")


def _counts(fac: pd.DataFrame, cls: pd.Series) -> dict:
    d = fac[["fac_id", "ftype"]].copy()
    d["zone_class"] = cls.reindex(d["fac_id"]).to_numpy()
    out = {}
    for t, g in d.groupby("ftype"):
        out[t] = {"sfha": int((g.zone_class == "SFHA").sum()), "x_02pct": int((g.zone_class == "X_0.2PCT").sum())}
    return out


def crosscheck(con, manifest: dict, crs: str, search_m: int = 1000, near=(100, 250)) -> dict:
    """Recompute the per-type SFHA and 0.2% counts with geopandas.sjoin, two ways.

    clean_tables: the cleaned polygons/points exported from DuckDB -> tests the join
                  and zone-ranking logic with an independent engine (GEOS STRtree).
    raw_files:    the published NFHL shapefiles re-read, reprojected with pyproj and
                  repaired with shapely.make_valid; facility points rebuilt from their
                  WGS84 lon/lat -> also tests reprojection and repair.
    """
    sql_counts = {r[0]: {"sfha": r[1], "x_02pct": r[2]} for r in con.execute(
        "SELECT ftype, count(*) FILTER (WHERE in_sfha), count(*) FILTER (WHERE in_02pct) "
        "FROM facility_zone GROUP BY ftype").fetchall()}

    fac = _gdf(con, "SELECT fac_id, ftype, lon, lat, ST_AsWKB(geom) AS wkb FROM facilities", crs)
    zones = _gdf(con, "SELECT zone_id, zone_class, ST_AsWKB(geom) AS wkb FROM flood_zones", crs)
    from_clean = _counts(fac, _best_class(fac, zones))

    parts = []
    for n in manifest["nfhl"]:
        if n.get("available"):
            g = gpd.read_file(n["flood_shp"], columns=["FLD_ZONE", "ZONE_SUBTY"])
            parts.append(g[g.geometry.notna() & ~g.geometry.is_empty])
    raw = pd.concat(parts, ignore_index=True).to_crs(crs)
    raw["geometry"] = shapely.make_valid(raw.geometry.values)
    raw["zone_class"] = classify(raw["FLD_ZONE"], raw["ZONE_SUBTY"])
    pts = gpd.GeoDataFrame(fac[["fac_id", "ftype"]], geometry=gpd.points_from_xy(fac.lon, fac.lat), crs="EPSG:4326").to_crs(crs)
    from_raw = _counts(pts, _best_class(pts, raw))

    def agree(other):
        return all(other.get(t) == v for t, v in sql_counts.items()) and set(other) == set(sql_counts)

    dist = distance_check(con, crs, search_m, near)
    return {"sql": sql_counts, "geopandas_from_clean_tables": from_clean, "geopandas_from_raw_files": from_raw,
            "agree_clean": agree(from_clean), "agree_raw": agree(from_raw),
            "distance_check": dist, "agree_distance": dist["agree"],
            "n_facilities": int(len(fac)), "n_polygons_clean": int(len(zones)), "n_polygons_raw": int(len(raw))}


def distance_check(con, crs: str, search_m: int, near=(100, 250), tol_m: float = 0.01) -> dict:
    """Nearest-SFHA distance for facilities outside the SFHA, recomputed with geopandas.sjoin_nearest."""
    fac = _gdf(con, "SELECT fac_id, dist_sfha_m, near_sfha_100m, near_sfha_250m, ST_AsWKB(geom) AS wkb "
                    "FROM facility_exposure WHERE NOT in_sfha", crs)
    sfha = _gdf(con, "SELECT zone_id, ST_AsWKB(geom) AS wkb FROM flood_sfha", crs)
    j = gpd.sjoin_nearest(fac[["fac_id", "geometry"]], sfha, how="left", max_distance=search_m, distance_col="d")
    gp = j.groupby("fac_id")["d"].min().reindex(fac.fac_id)
    sq = fac.set_index("fac_id")["dist_sfha_m"]
    both = sq.notna() & gp.notna()
    diff = (sq[both] - gp[both]).abs()
    flags_agree = all(((gp.fillna(np.inf) <= n).to_numpy() == fac[col].to_numpy()).all()
                      for n, col in ((near[0], "near_sfha_100m"), (near[1], "near_sfha_250m")))
    found_agree = bool((sq.notna() == gp.notna()).all())
    max_diff = float(diff.max()) if len(diff) else 0.0
    return {"n_outside_sfha": int(len(fac)), "n_with_sfha_within_search": int(both.sum()),
            "max_abs_diff_m": round(max_diff, 6), "same_facilities_found": found_agree,
            "threshold_flags_agree": bool(flags_agree), "tolerance_m": tol_m,
            "agree": bool(found_agree and flags_agree and max_diff <= tol_m)}


def export(con, results: Path, data_out: Path, crs: str = "EPSG:3083"):
    results.mkdir(parents=True, exist_ok=True)
    data_out.mkdir(parents=True, exist_ok=True)
    r, d = results.as_posix(), data_out.as_posix()
    con.execute(f"COPY exposure_summary TO '{r}/exposure_summary.csv' (HEADER)")
    con.execute(f"COPY nfhl_metadata TO '{r}/nfhl_metadata.csv' (HEADER)")
    # all screened facilities (no geometry column needed: lon/lat are kept)
    con.execute(f"COPY (SELECT * EXCLUDE (geom) FROM facility_exposure ORDER BY fac_id) "
                f"TO '{r}/facility_exposure.parquet' (FORMAT parquet, COMPRESSION zstd)")
    # small GeoJSON of exposed facilities, WGS84 points
    con.execute(f"""COPY (SELECT * EXCLUDE (lon, lat), ST_Point(lon, lat) AS geom FROM exposed_facilities ORDER BY fac_id)
                    TO '{r}/exposed_facilities.geojson' (FORMAT gdal, DRIVER 'GeoJSON',
                    LAYER_CREATION_OPTIONS ('COORDINATE_PRECISION=6', 'RFC7946=YES'))""")
    # full-resolution layers for GIS use stay in the git-ignored data/ folder
    gpkg = data_out / "facility_exposure.gpkg"
    gpkg.unlink(missing_ok=True)
    con.execute(f"COPY (SELECT * FROM facility_exposure) TO '{gpkg.as_posix()}' "
                f"(FORMAT gdal, DRIVER 'GPKG', SRS '{crs}')")
    con.execute(f"COPY (SELECT zone_id, dfirm_id, fld_zone, zone_subty, zone_class, geom FROM flood_zones) "
                f"TO '{d}/flood_zones.parquet' (FORMAT parquet, COMPRESSION zstd)")


def qa_dict(con) -> dict:
    rows = con.execute("SELECT layer, check_name, ftype, n FROM qa_log").fetchall()
    out: dict = {"flood": {}, "facility": {}, "exposure": {}}
    for layer, check, ftype, n in rows:
        if ftype is None:
            out[layer][check] = int(n)
        else:
            out[layer].setdefault(check, {})[ftype] = int(n)
    return out


def metrics(con, manifest: dict, crs: str, cross: dict, timings: dict, args: dict) -> dict:
    summ = con.execute("SELECT * FROM exposure_summary").df()
    by_type = summ[summ.county == "All"].drop(columns="county").set_index("facility_type")
    by_cell = summ[(summ.county != "All") & (summ.facility_type != "All")]

    def clean(v):
        if isinstance(v, (np.integer,)):
            return int(v)
        if isinstance(v, (np.floating, float)):
            return None if np.isnan(v) else (int(v) if float(v).is_integer() else float(v))
        if v is pd.NA or v is None:
            return None
        return v

    nfhl_meta = con.execute("SELECT * FROM nfhl_metadata").df()
    nfhl_meta = [{k: (str(v)[:10] if k.endswith("effective") else clean(v)) for k, v in r.items()}
                 for r in nfhl_meta.to_dict("records")]
    sources = {
        "counties": {k: manifest["counties"][k] for k in ("url", "retrieved", "publisher", "license")},
        "nfhl": [{k: n.get(k) for k in ("county_fips", "available", "dfirm_id", "url", "file", "size",
                                         "msc_effective_date", "retrieved", "publisher", "license")}
                 for n in manifest["nfhl"]],
        "facilities": [{k: f.get(k) for k in ("ftype", "url", "where", "records", "retrieved", "data_last_edit",
                                              "publisher", "license", "credits")} for f in manifest["facilities"]],
    }
    return {
        "question": "Which critical facilities in five Texas Gulf Coast counties sit in FEMA-mapped flood hazard areas?",
        "run": args,
        "crs": crs,
        "by_facility_type": {t: {k: clean(v) for k, v in row.items()} for t, row in by_type.iterrows()},
        "by_county": {r["county"]: {k: clean(v) for k, v in r.items() if k not in ("county", "facility_type")
                                     and not k.startswith("capacity")}
                      for r in summ[(summ.county != "All") & (summ.facility_type == "All")].to_dict("records")},
        "by_county_and_type": [{k: clean(v) for k, v in r.items()} for r in by_cell.to_dict("records")],
        "flood_polygons_by_class": {k: int(n) for k, n in con.execute(
            "SELECT zone_class || coalesce(' | ' || zone_subty, ''), count(*) FROM flood_zones "
            "WHERE zone_class LIKE 'X%' GROUP BY 1 UNION ALL "
            "SELECT zone_class, count(*) FROM flood_zones WHERE zone_class NOT LIKE 'X%' GROUP BY 1").fetchall()},
        "largest_hospitals_in_sfha": [dict(zip(("name", "county", "beds", "fld_zone"), r)) for r in con.execute(
            "SELECT name, county_name, capacity, fld_zone FROM facility_exposure WHERE ftype = 'hospital' AND in_sfha "
            "ORDER BY capacity DESC NULLS LAST LIMIT 5").fetchall()],
        "raw_facility_records_total": int(con.execute("SELECT count(*) FROM raw_facilities").fetchone()[0]),
        "nfhl_maps": nfhl_meta,
        "data_quality": qa_dict(con),
        "crosscheck_geopandas": cross,
        "timings_s": timings,
        "sources": sources,
    }


def write_metrics(m: dict, path: Path):
    path.write_text(json.dumps(m, indent=2, default=str))
