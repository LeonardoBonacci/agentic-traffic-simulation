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
    NULL::integer AS congestion,
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
    NULL AS congestion,
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
    agents_near_node(node_id, 100) AS congestion,
    ST_AsGeoJSON(geom)::json AS geometry
FROM nodes;

-- ============================================================
-- CONGESTION HEATMAP: edges colored by vehicle density
-- ============================================================
SELECT
    edge_id::text AS id,
    name,
    highway,
    speed_kph,
    length_m,
    agents_on_edge(edge_id) AS vehicle_count,
    ST_AsGeoJSON(geom)::json AS geometry
FROM edges
WHERE geom IS NOT NULL
ORDER BY agents_on_edge(edge_id) DESC;

-- ============================================================
-- AGENT TRAILS: animated path history
-- ============================================================
SELECT
    t.agent_id::text || '_' || t.tick::text AS id,
    a.name,
    t.tick,
    t.recorded_at,
    ST_AsGeoJSON(t.geom)::json AS geometry
FROM agent_trails t
JOIN agents a ON a.agent_id = t.agent_id
WHERE t.geom IS NOT NULL
ORDER BY t.agent_id, t.tick;

-- ============================================================
-- SPATIAL AWARENESS: vehicles + their 200m detection radius
-- ============================================================
SELECT
    agent_id::text AS id,
    name,
    status,
    speed_kmh,
    ST_AsGeoJSON(ST_Buffer(geom::geography, 200)::geometry)::json AS geometry
FROM agents
WHERE geom IS NOT NULL;
