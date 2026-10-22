-- =============================================================================
-- 04_proximity.sql  How close are facilities outside the SFHA to its edge?
--
-- A facility 30 m outside a mapped SFHA is, for planning purposes, very
-- different from one 3 km away. ST_DWithin limits the search to ${search_m} m
-- so the distance is only computed against nearby SFHA polygons; beyond that
-- the distance is reported as NULL ("more than ${search_m} m").
-- Units are metres because geometries are in the projected CRS (${crs}).
-- =============================================================================

CREATE OR REPLACE TEMP TABLE sfha_near AS
SELECT f.fac_id, min(ST_Distance(f.geom, s.geom)) AS dist_m
FROM facility_zone f
JOIN flood_sfha s ON ST_DWithin(f.geom, s.geom, ${search_m})
WHERE NOT f.in_sfha
GROUP BY f.fac_id;

CREATE OR REPLACE TABLE facility_exposure AS
SELECT z.* EXCLUDE (geom),
       CASE WHEN z.in_sfha THEN 0.0 ELSE n.dist_m END             AS dist_sfha_m,
       (NOT z.in_sfha AND n.dist_m <= ${near1_m})                  AS near_sfha_100m,
       (NOT z.in_sfha AND n.dist_m <= ${near2_m})                  AS near_sfha_250m,
       CASE WHEN z.in_sfha              THEN 'in SFHA'
            WHEN n.dist_m <= ${near1_m} THEN 'within ${near1_m} m'
            WHEN n.dist_m <= ${near2_m} THEN 'within ${near2_m} m'
            WHEN n.dist_m IS NOT NULL   THEN 'within ${search_m} m'
            ELSE 'over ${search_m} m' END                          AS sfha_proximity,
       z.geom
FROM facility_zone z
LEFT JOIN sfha_near n USING (fac_id);
-- near_sfha_* is NULL (not false) when no SFHA lies within the search radius;
-- normalise to false so the flags are two-valued.
UPDATE facility_exposure SET near_sfha_100m = coalesce(near_sfha_100m, false),
                             near_sfha_250m = coalesce(near_sfha_250m, false);
