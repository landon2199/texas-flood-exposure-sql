"""Fetch the public inputs: FEMA NFHL county packages, facility points, county outlines.

Every request is logged (URL, retrieval time, source edit/effective dates) to
data/raw/manifest.json so the README can cite exactly what was used. Files that
already exist are reused; pass ``refresh=True`` to download again.
"""
from __future__ import annotations

import datetime as dt
import json
import time
import zipfile
from pathlib import Path

import requests

UA = {"User-Agent": "texas-flood-exposure-sql/0.1 (public-data research; GitHub portfolio)"}
PAUSE_S = 1.0  # polite pause between paged requests

MSC_SEARCH = "https://msc.fema.gov/portal/advanceSearch"
NFHL_COUNTY_ZIP = "https://hazards.fema.gov/nfhlv2/output/County/{file}"
TIGER_COUNTIES = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/1"

# Facility layers that are reachable without login (checked 2026-10-22). HIFLD Open
# was retired in 2025; FEMA's Resilience Analysis and Planning Tool (RAPT) hosts
# the HIFLD hospital, nursing-home and public-school layers as public feature
# services. Fire/EMS stations come from the USGS National Map Structures dataset.
FACILITY_SOURCES = {
    "hospital": {
        "url": "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/Hospitals_RAPT/FeatureServer/6",
        "where": "STATE = 'TX'",
        "order": "FID",
        "publisher": "HIFLD Hospitals, hosted by FEMA RAPT (ArcGIS Online item c4b621eb03ca4f78a05d9063629e98bf)",
        "license": "HIFLD Open: public use, no restrictions stated ('None (Public Use)')",
    },
    "nursing_home": {
        "url": "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/Nursing_Homes_RAPT/FeatureServer/7",
        "where": "STATE = 'TX'",
        "order": "FID",
        "publisher": "HIFLD Nursing Homes / Assisted Care, hosted by FEMA RAPT (item 47c1893618ad48ac8d0cf199593b0c9a)",
        "license": "HIFLD Open: public use",
    },
    "public_school": {
        "url": "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/Public_Schools/FeatureServer/3",
        "where": "STATE = 'TX'",
        "order": "FID",
        "publisher": "HIFLD Public Schools (from NCES Common Core of Data), hosted by FEMA RAPT (item 963a2d204098495a8a80134ed9dc95ae)",
        "license": "HIFLD Open: public use ('None (Public Use)')",
    },
    "fire_ems": {
        "url": "https://carto.nationalmap.gov/arcgis/rest/services/structures/MapServer/16",
        "where": "STATE = 'TX'",
        "order": "OBJECTID",
        "publisher": "USGS National Map Structures: Fire Stations/EMS Stations",
        "license": "USGS: public domain",
    },
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _get(url: str, params: dict | None = None, timeout: int = 180, tries: int = 4, stream=False):
    """GET with retries; FEMA's servers are slow and occasionally drop the TLS handshake."""
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=timeout, stream=stream)
            r.raise_for_status()
            return r
        except (requests.RequestException,) as e:  # noqa: PERF203
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {url} ({last})")


def _arcgis_paged(url: str, where: str, order: str, geometry: str | None = None,
                  page: int = 1000) -> dict:
    """Page an ArcGIS REST layer with resultOffset and return one GeoJSON FeatureCollection."""
    feats, offset = [], 0
    while True:
        params = {"where": where, "outFields": "*", "outSR": 4326, "f": "geojson",
                  "orderByFields": order, "resultOffset": offset, "resultRecordCount": page}
        if geometry:
            params.update(geometry=geometry, geometryType="esriGeometryEnvelope", inSR=4326,
                          spatialRel="esriSpatialRelIntersects")
        js = _get(f"{url}/query", params).json()
        if "error" in js:
            raise RuntimeError(f"{url}: {js['error']}")
        batch = js.get("features", [])
        feats.extend(batch)
        more = js.get("exceededTransferLimit") or js.get("properties", {}).get("exceededTransferLimit")
        if len(batch) < page and not more:
            break
        offset += len(batch)
        time.sleep(PAUSE_S)
    return {"type": "FeatureCollection", "features": feats}


def _layer_info(url: str) -> dict:
    js = _get(url, {"f": "json"}).json()
    edit = (js.get("editingInfo") or {}).get("dataLastEditDate") or (js.get("editingInfo") or {}).get("lastEditDate")
    return {"name": js.get("name"),
            "data_last_edit": dt.datetime.fromtimestamp(edit / 1000, dt.timezone.utc).date().isoformat() if edit else None,
            "credits": (js.get("copyrightText") or "").strip()}


def fetch_counties(fips: list[str], out: Path, refresh=False) -> dict:
    out.parent.mkdir(parents=True, exist_ok=True)
    if refresh or not out.exists():
        codes = ",".join(f"'{f[2:]}'" for f in fips)
        fc = _arcgis_paged(TIGER_COUNTIES, f"STATE = '48' AND COUNTY IN ({codes})", "OBJECTID")
        out.write_text(json.dumps(fc))
    retrieved = dt.datetime.fromtimestamp(out.stat().st_mtime, dt.timezone.utc).replace(microsecond=0).isoformat()
    return {"kind": "counties", "url": TIGER_COUNTIES, "file": str(out), "retrieved": retrieved,
            "publisher": "U.S. Census Bureau TIGERweb (current vintage)", "license": "U.S. Government work, public domain"}


def lookup_nfhl(fips: str) -> dict | None:
    """Ask the FEMA Map Service Center which NFHL county package is current for a county."""
    params = {"affiliate": "fema", "query": "", "selstate": fips[:2], "selcounty": fips,
              "selcommunity": f"{fips}C", "searchedCid": f"{fips}C", "method": "search"}
    for _ in range(4):
        try:
            js = _get(MSC_SEARCH, params, timeout=120).json()
            break
        except ValueError:  # empty body: the portal sometimes answers 200 with nothing
            time.sleep(3)
    else:
        raise RuntimeError(f"MSC search returned no JSON for {fips}")
    for p in js.get("EFFECTIVE", {}).get("NFHL_COUNTY_DATA", []) or []:
        if p.get("product_NAME") == f"NFHL_{fips}C":
            return {"dfirm_id": f"{fips}C", "file": p["product_FILE_PATH"], "size": p.get("product_FILE_SIZE"),
                    "msc_effective_date": p.get("product_EFFECTIVE_DATE_STRING")}
    return None


def fetch_nfhl(fips: str, raw: Path, refresh=False) -> dict:
    """Download one county NFHL package and extract the flood-zone and FIRM-panel shapefiles."""
    info = lookup_nfhl(fips)
    if info is None:
        return {"kind": "nfhl", "county_fips": fips, "available": False}
    raw.mkdir(parents=True, exist_ok=True)
    zpath = raw / info["file"]
    url = NFHL_COUNTY_ZIP.format(file=info["file"])
    if refresh or not zpath.exists():
        tmp = zpath.with_suffix(".part")
        with _get(url, timeout=900, stream=True) as r, open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
        tmp.replace(zpath)
    folder = raw / info["dfirm_id"]
    if refresh or not (folder / "S_FLD_HAZ_AR.shp").exists():
        folder.mkdir(exist_ok=True)
        with zipfile.ZipFile(zpath) as z:
            for name in z.namelist():
                if Path(name).stem in ("S_FLD_HAZ_AR", "S_FIRM_PAN") and "/" not in name:
                    z.extract(name, folder)
    return {"kind": "nfhl", "county_fips": fips, "available": True, "url": url,
            "retrieved": dt.datetime.fromtimestamp(zpath.stat().st_mtime, dt.timezone.utc).replace(microsecond=0).isoformat(),
            "flood_shp": str(folder / "S_FLD_HAZ_AR.shp"), "panel_shp": str(folder / "S_FIRM_PAN.shp"),
            "publisher": "FEMA National Flood Hazard Layer, county package via the FEMA Map Service Center",
            "license": "U.S. Government work, public domain", **info}


def fetch_facilities(ftype: str, out: Path, refresh=False) -> dict:
    src = FACILITY_SOURCES[ftype]
    out.parent.mkdir(parents=True, exist_ok=True)
    if refresh or not out.exists():
        fc = _arcgis_paged(src["url"], src["where"], src["order"])
        out.write_text(json.dumps(fc))
        time.sleep(PAUSE_S)
    n = len(json.loads(out.read_text())["features"])
    meta = _layer_info(src["url"])
    return {"kind": "facility", "ftype": ftype, "url": src["url"], "where": src["where"], "file": str(out),
            "records": n, "retrieved": dt.datetime.fromtimestamp(out.stat().st_mtime, dt.timezone.utc).replace(microsecond=0).isoformat(),
            "publisher": src["publisher"], "license": src["license"], **meta}


def fetch_all(fips: list[str], ftypes: list[str], data_dir: Path, refresh=False) -> dict:
    raw = data_dir / "raw"
    manifest = {"counties": fetch_counties(fips, raw / "counties.geojson", refresh)}
    manifest["nfhl"] = []
    for f in fips:
        print(f"  NFHL {f} ...", flush=True)
        manifest["nfhl"].append(fetch_nfhl(f, raw / "nfhl", refresh))
    manifest["facilities"] = []
    for t in ftypes:
        print(f"  facilities {t} ...", flush=True)
        manifest["facilities"].append(fetch_facilities(t, raw / "facilities" / f"{t}.geojson", refresh))
    (raw / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest
