-- =============================================================================
-- 06_band_sensitivity.sql  How much do the "near the SFHA" counts depend on the
-- band width?
--
-- The 100 m and 250 m bands in 04_proximity.sql are screening choices, not
-- standards. This re-counts, for each band width in ${band_values} (metres),
-- the facilities outside the SFHA whose nearest SFHA polygon is within that
-- distance. It reads dist_sfha_m from facility_exposure, so no new spatial join
-- is run. Band widths must not exceed the ${search_m} m search radius, because
-- distances beyond it are NULL.
-- =============================================================================

CREATE OR REPLACE TABLE band_sensitivity AS
WITH bands(band_m) AS (VALUES ${band_values})
SELECT
    b.band_m,
    CASE WHEN grouping(f.ftype) = 1 THEN 'All' ELSE f.ftype END             AS facility_type,
    count(*)                                                                AS n_facilities,
    count(*) FILTER (WHERE NOT f.in_sfha AND f.dist_sfha_m <= b.band_m)     AS n_outside_within_band,
    count(*) FILTER (WHERE f.in_sfha OR f.dist_sfha_m <= b.band_m)          AS n_sfha_or_within_band,
    round(100.0 * count(*) FILTER (WHERE f.in_sfha OR f.dist_sfha_m <= b.band_m) / count(*), 1)
                                                                            AS pct_sfha_or_within_band
FROM bands b
CROSS JOIN facility_exposure f
GROUP BY GROUPING SETS ((b.band_m, f.ftype), (b.band_m))
ORDER BY b.band_m, facility_type = 'All', facility_type;
