-- =============================================================================
-- 03_exposure.sql  Point-in-polygon: which flood zone is each facility in?
--
-- ST_Intersects (not ST_Within) so a point exactly on a polygon edge counts as
-- inside. Where a point touches several polygons (shared edges, overlapping
-- revisions) the highest-hazard class wins via zone_rank.
-- A facility that touches no NFHL polygon at all is UNMAPPED.
-- =============================================================================

CREATE OR REPLACE TEMP TABLE zone_hits AS
SELECT f.fac_id, z.zone_id, z.zone_class, z.zone_rank, z.fld_zone, z.zone_subty, z.dfirm_id
FROM facilities f
JOIN flood_zones z ON ST_Intersects(f.geom, z.geom);

CREATE OR REPLACE TABLE facility_zone AS
WITH best AS (
    SELECT fac_id,
           min(zone_rank)                   AS zone_rank,
           arg_min(zone_class, zone_rank)   AS zone_class,
           arg_min(fld_zone, zone_rank)     AS fld_zone,
           arg_min(zone_subty, zone_rank)   AS zone_subty,
           count(*)                         AS n_polygons,
           count(DISTINCT zone_class)       AS n_classes
    FROM zone_hits
    GROUP BY fac_id
)
SELECT f.fac_id, f.ftype, f.source_id, f.name, f.address, f.city, f.county_fips, f.county_name,
       f.capacity, f.capacity_kind, f.lon, f.lat,
       coalesce(b.zone_class, 'UNMAPPED')  AS zone_class,
       b.fld_zone, b.zone_subty,
       coalesce(b.n_polygons, 0)           AS n_polygons,
       coalesce(b.n_classes, 0)            AS n_classes,
       coalesce(b.zone_class = 'SFHA', false)     AS in_sfha,
       coalesce(b.zone_class = 'X_0.2PCT', false) AS in_02pct,
       f.geom
FROM facilities f
LEFT JOIN best b USING (fac_id);

INSERT INTO qa_log SELECT 'exposure', 'facilities_on_class_boundary', ftype, count(*)
FROM facility_zone WHERE n_classes > 1 GROUP BY ALL;
-- Facilities with no regulatory zone: no polygon at all, or an 'AREA NOT INCLUDED'
-- (land mapped on another community's FIRM), open water or an unknown zone.
INSERT INTO qa_log SELECT 'exposure', 'zone_' || lower(zone_class), ftype, count(*)
FROM facility_zone WHERE zone_class IN ('UNMAPPED', 'NOT_INCLUDED', 'OPEN_WATER', 'OTHER') GROUP BY ALL;
