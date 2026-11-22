"""Offline tests: tiny synthetic inputs through the real sql/ files in an in-memory DuckDB.

Geometry is laid out in metres in EPSG:3083 around a Houston origin, then
written as lon/lat GeoJSON with the same field names as the published data, so
01_load.sql -> 05_summary.sql run unchanged.

    zones (x by y, metres):   AE [0,1000]x[0,1000] (+ an exact duplicate)
                              X 0.2% [1000,2000]x[0,1000]   X minimal [2000,4000]x[0,1000]
                              D [0,1000]x[2000,3000]        VE bow-tie (invalid) at x 5000-6000
"""
import json
import os

os.environ.setdefault("MKL_THREADING_LAYER", "SEQUENTIAL")

import pytest  # noqa: E402
from pyproj import Transformer  # noqa: E402

import floodsql  # noqa: E402,F401
from floodsql import db, report  # noqa: E402

CRS = "EPSG:3083"
TO_LL = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
TO_XY = Transformer.from_crs("EPSG:4326", CRS, always_xy=True)
OX, OY = TO_XY.transform(-95.40, 29.70)


def ll(x, y):
    return list(TO_LL.transform(OX + x, OY + y))


def poly(coords):
    ring = [ll(x, y) for x, y in coords]
    return {"type": "Polygon", "coordinates": [ring + [ring[0]]]}


def rect(x0, y0, x1, y1):
    return poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def fc(features):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": p, "geometry": g} for p, g in features]}


def pt(x, y):
    return {"type": "Point", "coordinates": ll(x, y)}


def zone(i, z, sub=None, sfha="F"):
    return {"FLD_AR_ID": f"Z{i}", "FLD_ZONE": z, "ZONE_SUBTY": sub, "SFHA_TF": sfha, "STATIC_BFE": -9999.0}


def hosp(i, name, x=None, y=None, status="OPEN", beds=100, county="48201", geom=None, address="1 Main"):
    lon, lat = ll(x, y) if x is not None else (0.0, 0.0)
    props = {"ID": i, "NAME": name, "ADDRESS": address, "CITY": "Houston", "COUNTYFIPS": county,
             "STATUS": status, "BEDS": beds, "LONGITUDE": lon, "LATITUDE": lat}
    return props, (geom if geom is not None else (pt(x, y) if x is not None else None))


def school(i, name, x, y, enrollment=500, county="48201", status=1):
    lon, lat = ll(x, y)
    return ({"NCESID": i, "NAME": name, "ADDRESS": "2 Main", "CITY": "Houston", "COUNTYFIPS": county,
             "STATUS": status, "ENROLLMENT": enrollment, "LONGITUDE": lon, "LATITUDE": lat}, pt(x, y))


def fire(i, name, geom):
    return {"permanent_identifier": i, "name": name, "address": "3 Main", "city": "Houston"}, geom


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    d = tmp_path_factory.mktemp("synthetic")
    zones = [
        (zone(1, "AE", sfha="T"), rect(0, 0, 1000, 1000)),
        (zone(1, "AE", sfha="T"), rect(0, 0, 1000, 1000)),               # exact duplicate
        (zone(2, "X", "0.2 PCT ANNUAL CHANCE FLOOD HAZARD"), rect(1000, 0, 2000, 1000)),
        (zone(3, "X", "AREA OF MINIMAL FLOOD HAZARD"), rect(2000, 0, 4000, 1000)),
        (zone(4, "D"), rect(0, 2000, 1000, 3000)),
        (zone(5, "VE", sfha="T"), poly([(5000, 0), (6000, 1000), (6000, 0), (5000, 1000)])),  # bow-tie
    ]
    files = {
        "flood": fc(zones),
        "panels": fc([({"FIRM_PAN": "48201C0001L", "EFF_DATE": "2019-11-15"}, rect(0, 0, 10, 10))]),
        "counties": fc([({"GEOID": "48201", "NAME": "Harris County"}, rect(-3000, -3000, 8000, 4000))]),
        "hospital": fc([
            hosp("H1", "Bayou Hospital", 500, 500, beds=200),          # SFHA
            hosp("H2", "Prairie Hospital", 1500, 500, beds=50),        # 0.2%
            hosp("H3", "Edge Hospital", 1000, 500, beds=-999),         # on SFHA / 0.2% edge -> SFHA, beds unknown
            hosp("H4", "Closed Hospital", 500, 400, status="CLOSED"),  # removed
            hosp("H5", "Far Hospital", 20000, 500),                    # outside the study county
            hosp("H6", "No Coords Hospital"),                          # missing geometry
        ]),
        "nursing_home": fc([
            hosp("N1", "Oak Care", 500, 1080, beds=60),                # 80 m from SFHA, unmapped
            hosp("N2", "Oak Care", 505, 1085, beds=60),                # same name within 50 m -> duplicate
            hosp("N3", "Pine Care", 500, 2500, beds=40),               # zone D
            hosp("N4", "Oak Care", 530, 1080, beds=90, address="9 Side St"),                # same name 30 m away, other beds -> kept, flagged
        ]),
        "public_school": fc([
            school("S1", "Elm Elementary", 500, 1200),                 # 200 m from SFHA
            school("S2", "Ash Middle", 500, 1600),                     # 600 m from SFHA
            school("S3", "Wrong County High", 20000, 900, county="48201"),  # attribute says Harris
            school("S4", "Future School", 600, 600, status=7),         # NCES status 7 = future -> removed
            school("S5", "Elm Elementary", 510, 1200),                 # same name, different NCESID -> kept
            school("S1", "Elm Elementary dup", 520, 1200),             # same NCESID as S1 -> removed
        ]),
        "fire_ems": fc([
            fire("F1", "Station 1", pt(3000, 500)),                    # X minimal, 2000 m from SFHA
            fire("F2", "Station 2", None),                             # missing
            fire("F3", "Station 3", {"type": "Point", "coordinates": [0.0, 0.0]}),  # zero
            fire("F4", "Station 4", pt(5800, 300)),                    # inside the repaired bow-tie (VE)
        ]),
    }
    paths = {}
    for k, v in files.items():
        p = d / f"{k}.geojson"
        p.write_text(json.dumps(v))
        paths[k] = str(p)
    manifest = {
        "counties": {"file": paths["counties"]},
        "nfhl": [{"available": True, "dfirm_id": "48201C", "flood_shp": paths["flood"], "panel_shp": paths["panels"]},
                 {"available": False, "county_fips": "48245"}],
        "facilities": [{"ftype": t, "file": paths[t]} for t in ("hospital", "nursing_home", "public_school", "fire_ems")],
    }
    con = db.connect()
    db.build(con, manifest, CRS)
    yield con, manifest
    con.close()


def fz(con):
    return {r[0]: r[1:] for r in con.execute(
        "SELECT name, zone_class, in_sfha, in_02pct, dist_sfha_m, near_sfha_100m, near_sfha_250m "
        "FROM facility_exposure").fetchall()}


def qa(con, check, ftype=None):
    sql = "SELECT coalesce(sum(n), 0) FROM qa_log WHERE check_name = ?" + (" AND ftype = ?" if ftype else "")
    return con.execute(sql, [check, ftype] if ftype else [check]).fetchone()[0]


def test_zone_classification(run):
    con, _ = run
    r = fz(con)
    assert r["Bayou Hospital"][:3] == ("SFHA", True, False)
    assert r["Prairie Hospital"][:3] == ("X_0.2PCT", False, True)
    assert r["Edge Hospital"][0] == "SFHA"            # higher hazard wins on a shared edge
    assert r["Pine Care"][0] == "D"
    assert r["Oak Care"][0] == "UNMAPPED"
    assert r["Station 1"][0] == "X_MINIMAL"
    assert r["Station 4"][0] == "SFHA"                # inside the repaired VE bow-tie


def test_distance_thresholds(run):
    con, _ = run
    r = fz(con)
    assert r["Bayou Hospital"][3] == 0.0
    assert r["Oak Care"][3] == pytest.approx(80, abs=0.5) and r["Oak Care"][4:] == (True, True)
    assert r["Elm Elementary"][3] == pytest.approx(200, abs=0.5) and r["Elm Elementary"][4:] == (False, True)
    assert r["Ash Middle"][3] == pytest.approx(600, abs=0.5) and r["Ash Middle"][4:] == (False, False)
    assert r["Pine Care"][3] is None                  # nearest SFHA is 1000 m+ away: beyond the search radius
    # Prairie Hospital is in the 0.2% zone, 500 m from the AE edge
    assert r["Prairie Hospital"][3] == pytest.approx(500, abs=0.5)


def test_cleaning_and_dedupe(run):
    con, _ = run
    assert qa(con, "invalid_geometry_as_published") == 1
    assert qa(con, "still_invalid_after_fix") == 0
    assert qa(con, "exact_duplicates_removed") == 1
    assert qa(con, "duplicates_removed", "nursing_home") == 1
    assert qa(con, "coords_missing") == 2
    assert qa(con, "coords_zero") == 1
    assert qa(con, "not_operating_removed", "hospital") == 1
    assert qa(con, "not_operating_removed", "public_school") == 1
    assert qa(con, "outside_study_counties") == 2
    assert qa(con, "point_outside_attributed_county", "public_school") == 1
    names = [r[0] for r in con.execute("SELECT name FROM facilities").fetchall()]
    assert names.count("Oak Care") == 2 and "Closed Hospital" not in names       # N2 removed, N4 kept
    assert names.count("Elm Elementary") == 2 and "Elm Elementary dup" not in names
    assert qa(con, "duplicates_removed", "public_school") == 1
    assert qa(con, "possible_duplicate_kept", "nursing_home") == 1
    assert qa(con, "possible_duplicate_kept", "public_school") == 1


def test_summary_and_capacity(run):
    con, _ = run
    row = con.execute("SELECT n_facilities, n_sfha, n_02pct, capacity_total, capacity_sfha, capacity_missing "
                      "FROM exposure_summary WHERE county = 'Harris' AND facility_type = 'hospital'").fetchone()
    assert row == (3, 2, 1, 250, 200, 1)               # Edge Hospital's -999 beds -> unknown
    tot = con.execute("SELECT n_facilities, n_sfha, n_outside_within_100m, n_outside_within_250m "
                      "FROM exposure_summary WHERE county = 'All' AND facility_type = 'All'").fetchone()
    assert tot == (11, 3, 2, 4)
    fire_cap = con.execute("SELECT capacity_total FROM exposure_summary "
                           "WHERE county = 'Harris' AND facility_type = 'fire_ems'").fetchone()[0]
    assert fire_cap is None


def test_band_sensitivity(run):
    con, _ = run
    rows = {(b, t): (n, s) for b, t, n, s in con.execute(
        "SELECT band_m, facility_type, n_outside_within_band, n_sfha_or_within_band FROM band_sensitivity").fetchall()}
    assert sorted({b for b, _ in rows}) == [50, 100, 250, 500]
    tot = con.execute("SELECT n_sfha, n_outside_within_100m, n_outside_within_250m "
                      "FROM exposure_summary WHERE county = 'All' AND facility_type = 'All'").fetchone()
    assert rows[(100, "All")] == (tot[1], tot[0] + tot[1])    # same as the 100 m and 250 m flags
    assert rows[(250, "All")] == (tot[2], tot[0] + tot[2])
    assert rows[(50, "All")][0] == 0                         # nearest outside point is Oak Care at 80 m
    alls = [rows[(b, "All")][0] for b in (50, 100, 250, 500)]
    assert alls == sorted(alls)                              # wider bands never count fewer
    with pytest.raises(ValueError):
        db.build(db.connect(), {"nfhl": [], "facilities": []}, CRS, bands_m=(50, 2000))


def test_rtree_used_in_join(run):
    con, _ = run
    plan = con.execute("EXPLAIN SELECT count(*) FROM facilities f JOIN flood_zones z "
                       "ON ST_Intersects(f.geom, z.geom)").fetchall()[0][1]
    assert "SPATIAL_JOIN" in plan


def test_geopandas_crosscheck(run):
    con, manifest = run
    c = report.crosscheck(con, manifest, CRS)
    assert c["agree_clean"] and c["agree_raw"] and c["agree_distance"], c
    assert c["sql"]["hospital"] == {"sfha": 2, "x_02pct": 1}


def test_sql_blocks_and_params():
    b = db.blocks(db.SQL_DIR / "01_load.sql")
    assert {"schema", "load_nfhl", "load_hospital", "load_fire_ems"} <= set(b)
    with pytest.raises(KeyError):
        db.render("SELECT ${missing}", {})
    assert db.lit("C:\\a\\it's") == "C:/a/it''s"
