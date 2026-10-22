-- =============================================================================
-- 05_summary.sql  Counts by county x facility type x zone, plus beds/students.
--
-- GROUPING SETS produces the county x type cells, the per-type and per-county
-- subtotals and the grand total in one pass; 'All' marks a rolled-up level.
-- =============================================================================

CREATE OR REPLACE TABLE exposure_summary AS
SELECT
    CASE WHEN grouping(county_name) = 1 THEN 'All' ELSE county_name END AS county,
    CASE WHEN grouping(ftype) = 1       THEN 'All' ELSE ftype END       AS facility_type,
    count(*)                                              AS n_facilities,
    count(*) FILTER (WHERE in_sfha)                       AS n_sfha,
    count(*) FILTER (WHERE in_02pct)                      AS n_02pct,
    count(*) FILTER (WHERE zone_class = 'X_LEVEE')        AS n_levee,
    count(*) FILTER (WHERE zone_class = 'X_MINIMAL')      AS n_minimal,
    count(*) FILTER (WHERE zone_class = 'D')              AS n_zone_d,
    count(*) FILTER (WHERE zone_class IN ('UNMAPPED','OPEN_WATER','NOT_INCLUDED','OTHER')) AS n_unmapped_other,
    count(*) FILTER (WHERE near_sfha_100m)                AS n_outside_within_100m,
    count(*) FILTER (WHERE near_sfha_250m)                AS n_outside_within_250m,
    -- the 250 m band overlaps the 0.2% zone, so this is the de-duplicated total
    count(*) FILTER (WHERE near_sfha_250m AND in_02pct)   AS n_02pct_and_within_250m,
    count(*) FILTER (WHERE in_sfha OR in_02pct OR near_sfha_250m) AS n_sfha_02pct_or_250m,
    round(100.0 * count(*) FILTER (WHERE in_sfha)  / count(*), 1)                  AS pct_sfha,
    round(100.0 * count(*) FILTER (WHERE in_02pct) / count(*), 1)                  AS pct_02pct,
    round(100.0 * count(*) FILTER (WHERE in_sfha OR near_sfha_250m) / count(*), 1) AS pct_sfha_or_250m,
    round(100.0 * count(*) FILTER (WHERE in_sfha OR in_02pct OR near_sfha_250m) / count(*), 1) AS pct_sfha_02pct_or_250m,
    -- capacity only means something within one facility type (beds vs students)
    CASE WHEN grouping(ftype) = 0 THEN any_value(capacity_kind) END   AS capacity_kind,
    -- (NULL for fire/EMS stations, which have no capacity attribute)
    CASE WHEN grouping(ftype) = 0 AND any_value(capacity_kind) IS NOT NULL THEN sum(capacity) END AS capacity_total,
    CASE WHEN grouping(ftype) = 0 AND any_value(capacity_kind) IS NOT NULL
         THEN coalesce(sum(capacity) FILTER (WHERE in_sfha), 0) END  AS capacity_sfha,
    CASE WHEN grouping(ftype) = 0 AND any_value(capacity_kind) IS NOT NULL
         THEN coalesce(sum(capacity) FILTER (WHERE in_02pct), 0) END AS capacity_02pct,
    CASE WHEN grouping(ftype) = 0 AND any_value(capacity_kind) IS NOT NULL
         THEN count(*) FILTER (WHERE capacity IS NULL) END           AS capacity_missing
FROM facility_exposure
GROUP BY GROUPING SETS ((county_name, ftype), (ftype), (county_name), ())
ORDER BY county = 'All', county, facility_type = 'All', facility_type;

-- Which map versions are we screening against? Panel effective dates per DFIRM.
CREATE OR REPLACE TABLE nfhl_metadata AS
SELECT p.dfirm_id, c.county_name,
       count(*) AS n_panels,
       min(p.eff_date) AS earliest_panel_effective,
       max(p.eff_date) AS latest_panel_effective,
       (SELECT count(*) FROM flood_zones z WHERE z.dfirm_id = p.dfirm_id) AS n_polygons,
       (SELECT round(sum(area_m2) / 1e6, 1) FROM flood_zones z
         WHERE z.dfirm_id = p.dfirm_id AND z.zone_class = 'SFHA') AS sfha_km2
FROM raw_panels p
LEFT JOIN counties c ON c.county_fips = left(p.dfirm_id, 5)
GROUP BY ALL
ORDER BY p.dfirm_id;

-- Facilities to publish on the map: in SFHA, in the 0.2% zone, or within 250 m.
CREATE OR REPLACE VIEW exposed_facilities AS
SELECT fac_id, ftype, name, city, county_name, capacity, capacity_kind,
       zone_class, fld_zone, zone_subty, round(dist_sfha_m, 1) AS dist_sfha_m, sfha_proximity, lon, lat
FROM facility_exposure
WHERE in_sfha OR in_02pct OR near_sfha_250m;
