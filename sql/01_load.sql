-- =============================================================================
-- 01_load.sql  Raw tables, loaded as published (no reprojection, no fixes yet).
--
-- Each "-- name:" block is run by floodsql/db.py; load blocks run once per file
-- with ${path} (and ${dfirm_id}) filled in. ST_Read uses GDAL, so the same block
-- reads a shapefile, GeoJSON or file geodatabase layer.
--   NFHL shapefiles : NAD83 geographic (EPSG:4269)
--   Facilities, counties (ArcGIS REST, outSR=4326): WGS84 (EPSG:4326)
-- =============================================================================

-- name: schema
CREATE OR REPLACE TABLE raw_flood (
    dfirm_id    VARCHAR,   -- FEMA DFIRM ID, e.g. 48201C (= county FIPS + 'C')
    fld_ar_id   VARCHAR,   -- polygon ID within the DFIRM
    fld_zone    VARCHAR,   -- A, AE, AO, VE, X, D, OPEN WATER, ...
    zone_subty  VARCHAR,   -- e.g. FLOODWAY, 0.2 PCT ANNUAL CHANCE FLOOD HAZARD
    sfha_tf     VARCHAR,   -- 'T' = Special Flood Hazard Area (1% annual chance)
    static_bfe  DOUBLE,    -- static base flood elevation (-9999 = none)
    source_file VARCHAR,
    geom        GEOMETRY   -- EPSG:4269
);

CREATE OR REPLACE TABLE raw_panels (
    dfirm_id VARCHAR,
    firm_pan VARCHAR,      -- FIRM panel number
    eff_date DATE          -- panel effective date
);

CREATE OR REPLACE TABLE raw_counties (
    geoid VARCHAR,         -- 5-digit county FIPS
    name  VARCHAR,
    geom  GEOMETRY         -- EPSG:4326
);

CREATE OR REPLACE TABLE raw_facilities (
    ftype            VARCHAR,  -- hospital | nursing_home | public_school | fire_ems
    source_id        VARCHAR,  -- the publisher's ID
    name             VARCHAR,
    address          VARCHAR,
    city             VARCHAR,
    county_fips_attr VARCHAR,  -- county FIPS as written in the record (NULL if none)
    status           VARCHAR,  -- OPEN / CLOSED / NOT AVAILABLE (HIFLD)
    capacity         INTEGER,  -- beds or enrolled students; NULL when unknown
    capacity_kind    VARCHAR,  -- 'beds' | 'students' | NULL
    lon_attr         DOUBLE,   -- coordinates as written in the attribute table
    lat_attr         DOUBLE,
    geom             GEOMETRY  -- EPSG:4326, may be NULL
);

-- name: load_nfhl
INSERT INTO raw_flood
SELECT '${dfirm_id}', FLD_AR_ID, FLD_ZONE, ZONE_SUBTY, SFHA_TF,
       TRY_CAST(STATIC_BFE AS DOUBLE), '${path}', geom
FROM ST_Read('${path}');

-- name: load_panels
INSERT INTO raw_panels
SELECT '${dfirm_id}', FIRM_PAN, TRY_CAST(EFF_DATE AS DATE)
FROM ST_Read('${path}');

-- name: load_counties
INSERT INTO raw_counties
SELECT GEOID, NAME, geom
FROM ST_Read('${path}');

-- HIFLD uses -999 for "not available"; keep only positive counts.
-- name: load_hospital
INSERT INTO raw_facilities
SELECT 'hospital', CAST(ID AS VARCHAR), NAME, ADDRESS, CITY, CAST(COUNTYFIPS AS VARCHAR), STATUS,
       CASE WHEN TRY_CAST(BEDS AS INTEGER) > 0 THEN TRY_CAST(BEDS AS INTEGER) END, 'beds',
       TRY_CAST(LONGITUDE AS DOUBLE), TRY_CAST(LATITUDE AS DOUBLE), geom
FROM ST_Read('${path}');

-- name: load_nursing_home
INSERT INTO raw_facilities
SELECT 'nursing_home', CAST(ID AS VARCHAR), NAME, ADDRESS, CITY, CAST(COUNTYFIPS AS VARCHAR), STATUS,
       CASE WHEN TRY_CAST(BEDS AS INTEGER) > 0 THEN TRY_CAST(BEDS AS INTEGER) END, 'beds',
       TRY_CAST(LONGITUDE AS DOUBLE), TRY_CAST(LATITUDE AS DOUBLE), geom
FROM ST_Read('${path}');

-- Schools carry the NCES status code: 1 open, 2 closed, 3 new, 4 added,
-- 5 changed agency, 6 inactive, 7 future, 8 reopened.
-- name: load_public_school
INSERT INTO raw_facilities
SELECT 'public_school', CAST(NCESID AS VARCHAR), NAME, ADDRESS, CITY, CAST(COUNTYFIPS AS VARCHAR),
       CASE CAST(STATUS AS VARCHAR) WHEN '2' THEN 'CLOSED' WHEN '6' THEN 'INACTIVE'
                                    WHEN '7' THEN 'FUTURE' ELSE 'OPEN' END,
       CASE WHEN TRY_CAST(ENROLLMENT AS INTEGER) > 0 THEN TRY_CAST(ENROLLMENT AS INTEGER) END, 'students',
       TRY_CAST(LONGITUDE AS DOUBLE), TRY_CAST(LATITUDE AS DOUBLE), geom
FROM ST_Read('${path}');

-- USGS structures carry no county, status or capacity; coordinates come from the geometry.
-- name: load_fire_ems
INSERT INTO raw_facilities
SELECT 'fire_ems', CAST(permanent_identifier AS VARCHAR), name, address, city, NULL, NULL,
       NULL, NULL,
       CASE WHEN geom IS NOT NULL THEN ST_X(geom) END, CASE WHEN geom IS NOT NULL THEN ST_Y(geom) END, geom
FROM ST_Read('${path}');
