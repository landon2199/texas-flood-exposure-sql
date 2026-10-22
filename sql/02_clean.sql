-- =============================================================================
-- 02_clean.sql  Project to ${crs}, repair, classify, de-duplicate, index.
--
-- Every check writes a row to qa_log so the data-quality numbers in
-- results/metrics.json come straight from SQL.
-- =============================================================================

CREATE OR REPLACE TABLE qa_log (layer VARCHAR, check_name VARCHAR, ftype VARCHAR, n BIGINT);

-- -----------------------------------------------------------------------------
-- Flood hazard polygons
-- -----------------------------------------------------------------------------
INSERT INTO qa_log SELECT 'flood', 'raw_polygons', NULL, count(*) FROM raw_flood;
INSERT INTO qa_log SELECT 'flood', 'null_or_empty_geometry', NULL, count(*)
FROM raw_flood WHERE geom IS NULL OR ST_IsEmpty(geom);
INSERT INTO qa_log SELECT 'flood', 'invalid_geometry_as_published', NULL, count(*)
FROM raw_flood WHERE geom IS NOT NULL AND NOT ST_IsValid(geom);
-- SFHA_TF should agree with the zone letter; disagreements are logged, the zone letter wins.
INSERT INTO qa_log SELECT 'flood', 'sfha_flag_disagrees_with_zone', NULL, count(*)
FROM raw_flood
WHERE (fld_zone IN ('A','AE','AH','AO','AR','A99','V','VE')) <> (coalesce(sfha_tf, 'F') = 'T');

-- Project (always_xy: the source is lon/lat order), then repair. ST_MakeValid can
-- return a collection with slivers as lines/points; keep only the polygon parts.
CREATE OR REPLACE TEMP TABLE flood_projected AS
SELECT row_number() OVER () AS raw_id, dfirm_id, fld_ar_id, fld_zone, zone_subty, sfha_tf, static_bfe,
       ST_Transform(geom, 'EPSG:4269', '${crs}', always_xy := true) AS geom
FROM raw_flood
WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom);

CREATE OR REPLACE TEMP TABLE flood_fixed AS
SELECT * REPLACE (
         CASE WHEN ST_IsValid(geom) THEN geom
              ELSE ST_CollectionExtract(ST_MakeValid(geom), 3) END AS geom),
       NOT ST_IsValid(geom) AS was_fixed
FROM flood_projected;

INSERT INTO qa_log SELECT 'flood', 'invalid_after_projection_fixed', NULL, count(*) FILTER (WHERE was_fixed) FROM flood_fixed;
INSERT INTO qa_log SELECT 'flood', 'still_invalid_after_fix', NULL, count(*) FILTER (WHERE NOT ST_IsValid(geom)) FROM flood_fixed;
INSERT INTO qa_log SELECT 'flood', 'empty_after_fix_dropped', NULL, count(*) FILTER (WHERE ST_IsEmpty(geom)) FROM flood_fixed;

-- Exact duplicates: same DFIRM, same zone attributes and byte-identical geometry.
CREATE OR REPLACE TEMP TABLE flood_dedup AS
SELECT * EXCLUDE (dup_rank)
FROM (
    SELECT *, row_number() OVER (
               PARTITION BY dfirm_id, fld_zone, coalesce(zone_subty, ''), md5(ST_AsHEXWKB(geom))
               ORDER BY raw_id) AS dup_rank
    FROM flood_fixed
    WHERE NOT ST_IsEmpty(geom)
)
WHERE dup_rank = 1;

INSERT INTO qa_log SELECT 'flood', 'exact_duplicates_removed', NULL,
       (SELECT count(*) FROM flood_fixed WHERE NOT ST_IsEmpty(geom)) - (SELECT count(*) FROM flood_dedup);

-- Zone classes, ranked so a point on a shared edge takes the higher hazard.
--   SFHA      1% annual chance (A*, V*)               -> mandatory flood insurance area
--   X_0.2PCT  shaded X, 0.2% annual chance ("500-year")
--   X_LEVEE   shaded X, reduced risk due to levee
--   X_MINIMAL unshaded X
--   D         possible but undetermined hazard (not studied)
CREATE OR REPLACE TABLE flood_zones (
    zone_id    BIGINT PRIMARY KEY,
    dfirm_id   VARCHAR,
    county_fips VARCHAR,
    fld_ar_id  VARCHAR,
    fld_zone   VARCHAR,
    zone_subty VARCHAR,
    zone_class VARCHAR,
    zone_rank  INTEGER,
    static_bfe DOUBLE,
    was_fixed  BOOLEAN,
    area_m2    DOUBLE,
    geom       GEOMETRY('${crs}')
);

INSERT INTO flood_zones
SELECT raw_id, dfirm_id, left(dfirm_id, 5), fld_ar_id, fld_zone, zone_subty, zone_class,
       CASE zone_class WHEN 'SFHA' THEN 1 WHEN 'X_0.2PCT' THEN 2 WHEN 'X_LEVEE' THEN 3
                       WHEN 'X_MINIMAL' THEN 4 WHEN 'OPEN_WATER' THEN 5 WHEN 'D' THEN 6
                       WHEN 'NOT_INCLUDED' THEN 7 ELSE 8 END,
       CASE WHEN static_bfe > -9000 THEN static_bfe END,
       was_fixed, ST_Area(geom), geom
FROM (
    SELECT *,
           CASE
             WHEN fld_zone IN ('A','AE','AH','AO','AR','A99','V','VE')                 THEN 'SFHA'
             WHEN fld_zone = 'X' AND zone_subty LIKE '0.2 PCT ANNUAL CHANCE%'          THEN 'X_0.2PCT'
             WHEN fld_zone = 'X' AND zone_subty LIKE '%REDUCED FLOOD RISK DUE TO LEVEE%' THEN 'X_LEVEE'
             WHEN fld_zone = 'X'                                                       THEN 'X_MINIMAL'
             WHEN fld_zone = 'D'                                                       THEN 'D'
             WHEN fld_zone = 'OPEN WATER'                                              THEN 'OPEN_WATER'
             WHEN fld_zone = 'AREA NOT INCLUDED'                                       THEN 'NOT_INCLUDED'
             ELSE 'OTHER'
           END AS zone_class
    FROM flood_dedup
);

-- R-tree on the polygon bounding boxes. EXPLAIN shows DuckDB using it both for
-- constant-geometry filters and inside the SPATIAL_JOIN of 03/04
-- ("RTREE_INDEX_SCAN ... Bounds: deferred (from join filter)").
CREATE INDEX flood_zones_rtree ON flood_zones USING RTREE (geom);

-- SFHA polygons only, for the distance step.
CREATE OR REPLACE TABLE flood_sfha AS
SELECT zone_id, county_fips, fld_zone, geom FROM flood_zones WHERE zone_class = 'SFHA';
CREATE INDEX flood_sfha_rtree ON flood_sfha USING RTREE (geom);

INSERT INTO qa_log SELECT 'flood', 'clean_polygons', NULL, count(*) FROM flood_zones;
INSERT INTO qa_log SELECT 'flood', 'zone_other_class', NULL, count(*) FROM flood_zones WHERE zone_class = 'OTHER';

-- -----------------------------------------------------------------------------
-- Counties
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE counties (
    county_fips VARCHAR PRIMARY KEY,
    county_name VARCHAR,
    geom        GEOMETRY('${crs}')
);
INSERT INTO counties
SELECT geoid, replace(name, ' County', ''),
       ST_Transform(ST_MakeValid(geom), 'EPSG:4326', '${crs}', always_xy := true)
FROM raw_counties;

-- -----------------------------------------------------------------------------
-- Facilities
-- -----------------------------------------------------------------------------
INSERT INTO qa_log SELECT 'facility', 'raw_records', ftype, count(*) FROM raw_facilities GROUP BY ftype;

-- Coordinates: missing geometry, a (0,0) point, or outside Texas' bounding box.
CREATE OR REPLACE TEMP TABLE fac_flagged AS
SELECT row_number() OVER (ORDER BY ftype, source_id, name) AS fac_id, *,
       CASE
         WHEN geom IS NULL OR ST_IsEmpty(geom)                         THEN 'missing'
         WHEN abs(ST_X(geom)) < 1e-6 OR abs(ST_Y(geom)) < 1e-6          THEN 'zero'
         WHEN ST_X(geom) NOT BETWEEN -106.7 AND -93.5
           OR ST_Y(geom) NOT BETWEEN 25.8 AND 36.6                      THEN 'outside_texas'
         ELSE 'ok'
       END AS coord_flag,
       upper(coalesce(status, '')) IN ('CLOSED', 'INACTIVE', 'FUTURE') AS is_closed  -- not operating
FROM raw_facilities;

INSERT INTO qa_log SELECT 'facility', 'coords_' || coord_flag, ftype, count(*)
FROM fac_flagged WHERE coord_flag <> 'ok' GROUP BY ALL;
INSERT INTO qa_log SELECT 'facility', 'not_operating_removed', ftype, count(*)
FROM fac_flagged WHERE is_closed GROUP BY ALL;

-- Assign each usable point to a study county by geometry, not by its attribute.
CREATE OR REPLACE TEMP TABLE fac_located AS
SELECT f.fac_id, f.ftype, f.source_id, f.name, f.address, f.city, f.county_fips_attr,
       f.capacity, f.capacity_kind, c.county_fips, c.county_name, f.geom AS geom_wgs84, p.geom
FROM fac_flagged f
CROSS JOIN LATERAL (SELECT ST_Transform(f.geom, 'EPSG:4326', '${crs}', always_xy := true) AS geom) p
LEFT JOIN counties c ON ST_Intersects(c.geom, p.geom)
WHERE f.coord_flag = 'ok' AND NOT f.is_closed
-- a point exactly on a shared county line would match twice; keep one
QUALIFY row_number() OVER (PARTITION BY f.fac_id ORDER BY c.county_fips) = 1;

-- A record whose county attribute names a study county but whose point falls
-- outside that county: a geocoding or attribute error worth flagging.
INSERT INTO qa_log SELECT 'facility', 'point_outside_attributed_county', ftype, count(*)
FROM fac_located
WHERE county_fips_attr IN (SELECT county_fips FROM counties)
  AND county_fips_attr IS DISTINCT FROM county_fips
GROUP BY ALL;
-- Points in a study county whose attribute says another county.
INSERT INTO qa_log SELECT 'facility', 'attribute_names_other_county', ftype, count(*)
FROM fac_located
WHERE county_fips IS NOT NULL AND county_fips_attr IS NOT NULL AND county_fips_attr <> county_fips
GROUP BY ALL;

-- Duplicates within the study area. The publisher ID comes first:
--   (a) the same publisher ID twice -> duplicate (all types; NCESID for schools).
--   (b) hospitals and nursing homes only (HIFLD IDs are not unique per campus):
--       same normalised name AND same capacity AND (within 20 m OR same
--       normalised address) -> duplicate. In the HIFLD nursing-home layer these are
--       mostly the same licence listed from two Texas HHS source lists. Separately
--       licensed units at one address (e.g. a 100-bed nursing home and a 16-bed
--       assisted-living wing with the same name) differ in beds and are kept.
--   Same name within 50 m that is not removed by (a)/(b) is kept and only
--   logged as 'possible_duplicate_kept'.
CREATE OR REPLACE TEMP TABLE fac_study AS
SELECT *,
       upper(trim(regexp_replace(coalesce(name, ''), '[^A-Za-z0-9]+', ' ', 'g')))    AS name_norm,
       upper(trim(regexp_replace(coalesce(address, ''), '[^A-Za-z0-9]+', ' ', 'g'))) AS addr_norm
FROM fac_located WHERE county_fips IS NOT NULL;

CREATE OR REPLACE TEMP TABLE fac_dupes AS
SELECT fac_id, 'same_source_id' AS reason FROM (
    SELECT fac_id, row_number() OVER (PARTITION BY ftype, source_id ORDER BY fac_id) AS rk
    FROM fac_study WHERE source_id IS NOT NULL
) WHERE rk > 1
UNION
SELECT b.fac_id, 'same_name_and_capacity_or_address'
FROM fac_study a
JOIN fac_study b
  ON a.ftype = b.ftype AND a.ftype IN ('hospital', 'nursing_home')
 AND a.name_norm = b.name_norm AND a.name_norm <> '' AND a.fac_id < b.fac_id
 AND a.capacity IS NOT DISTINCT FROM b.capacity
 AND ( ST_DWithin(a.geom, b.geom, 20) OR (a.addr_norm = b.addr_norm AND a.addr_norm <> '') );

INSERT INTO qa_log SELECT 'facility', 'duplicates_removed', s.ftype, count(DISTINCT d.fac_id)
FROM fac_dupes d JOIN fac_study s USING (fac_id) GROUP BY ALL;

INSERT INTO qa_log SELECT 'facility', 'possible_duplicate_kept', b.ftype, count(DISTINCT b.fac_id)
FROM fac_study a
JOIN fac_study b
  ON a.ftype = b.ftype AND a.name_norm = b.name_norm AND a.name_norm <> ''
 AND a.fac_id < b.fac_id AND ST_DWithin(a.geom, b.geom, 50)
WHERE b.fac_id NOT IN (SELECT fac_id FROM fac_dupes)
  AND a.fac_id NOT IN (SELECT fac_id FROM fac_dupes)
GROUP BY ALL;

CREATE OR REPLACE TABLE facilities (
    fac_id        BIGINT PRIMARY KEY,
    ftype         VARCHAR,
    source_id     VARCHAR,
    name          VARCHAR,
    address       VARCHAR,
    city          VARCHAR,
    county_fips   VARCHAR,
    county_name   VARCHAR,
    capacity      INTEGER,
    capacity_kind VARCHAR,
    lon           DOUBLE,
    lat           DOUBLE,
    geom          GEOMETRY('${crs}')
);
INSERT INTO facilities
SELECT fac_id, ftype, source_id, name, address, city, county_fips, county_name, capacity, capacity_kind,
       ST_X(geom_wgs84), ST_Y(geom_wgs84), geom
FROM fac_study
WHERE fac_id NOT IN (SELECT fac_id FROM fac_dupes);
CREATE INDEX facilities_rtree ON facilities USING RTREE (geom);

INSERT INTO qa_log SELECT 'facility', 'outside_study_counties', ftype, count(*)
FROM fac_located WHERE county_fips IS NULL GROUP BY ALL;
INSERT INTO qa_log SELECT 'facility', 'clean_in_study_area', ftype, count(*) FROM facilities GROUP BY ALL;
