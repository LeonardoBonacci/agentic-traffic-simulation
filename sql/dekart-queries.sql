-- ============================================================
-- DEKART MAP QUERIES
-- Paste these into Dekart (http://localhost:8080) to visualize
-- the traffic simulation on a Kepler.gl map.
-- ============================================================

-- Combined: street network + agents + intersections in one query
SELECT
    'edge' AS layer,
    edge_id::text AS id,
    name,
    highway AS type,
    speed_kph AS speed,
    length_m,
    NULL::double precision AS heading,
    NULL AS status,
    NULL::integer AS street_count,
    ST_AsGeoJSON(geom)::json AS geometry
FROM edges
WHERE geom IS NOT NULL

UNION ALL

SELECT
    'agent' AS layer,
    agent_id::text AS id,
    name,
    agent_type AS type,
    speed_kmh AS speed,
    NULL AS length_m,
    heading,
    status,
    NULL AS street_count,
    ST_AsGeoJSON(geom)::json AS geometry
FROM agents
WHERE geom IS NOT NULL

UNION ALL

SELECT
    'node' AS layer,
    node_id::text AS id,
    osm_highway AS name,
    'intersection' AS type,
    NULL AS speed,
    NULL AS length_m,
    NULL AS heading,
    NULL AS status,
    street_count,
    ST_AsGeoJSON(geom)::json AS geometry
FROM nodes;
