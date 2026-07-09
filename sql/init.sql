-- Enable PostGIS and pgRouting extensions
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgrouting;

-- ============================================================
-- STATIC STREET MAP TABLES (read once at simulation start)
-- ============================================================

-- Intersections / junctions (graph nodes)
CREATE TABLE nodes (
    node_id       BIGINT PRIMARY KEY,
    osm_highway   TEXT,          -- e.g. traffic_signals, crossing, motorway_junction
    osm_junction  TEXT,          -- e.g. 'yes' for roundabouts
    street_count  INTEGER,       -- number of streets meeting at this node
    geom          GEOMETRY(Point, 4326) NOT NULL
);

CREATE INDEX idx_nodes_geom ON nodes USING GIST (geom);

-- Road segments (graph edges)
CREATE TABLE edges (
    edge_id       SERIAL PRIMARY KEY,
    source_node   BIGINT NOT NULL REFERENCES nodes(node_id),
    target_node   BIGINT NOT NULL REFERENCES nodes(node_id),
    osm_id        TEXT,          -- can be a list for merged edges
    highway       TEXT,          -- road classification: trunk, secondary, tertiary, etc.
    name          TEXT,          -- street name
    oneway        BOOLEAN DEFAULT FALSE,
    reversed      BOOLEAN DEFAULT FALSE,
    length_m      DOUBLE PRECISION,  -- segment length in meters
    speed_kph     DOUBLE PRECISION,  -- speed in km/h
    travel_time_s DOUBLE PRECISION,  -- traversal time in seconds
    maxspeed      TEXT,
    lanes         TEXT,          -- can be a list for merged edges
    ref           TEXT,          -- route reference (e.g. SH 1)
    access        TEXT,
    bridge        TEXT,
    width         TEXT,
    tunnel        TEXT,
    junction      TEXT,
    geom          GEOMETRY(LineString, 4326)  -- full geometry of the segment
);

CREATE INDEX idx_edges_geom ON edges USING GIST (geom);
CREATE INDEX idx_edges_source ON edges (source_node);
CREATE INDEX idx_edges_target ON edges (target_node);

-- ============================================================
-- DYNAMIC AGENT TABLE (agents update their own location)
-- ============================================================

CREATE TABLE agents (
    agent_id      SERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    agent_type    TEXT NOT NULL DEFAULT 'vehicle',  -- vehicle, pedestrian, cyclist
    current_node  BIGINT NOT NULL REFERENCES nodes(node_id),
    target_node   BIGINT REFERENCES nodes(node_id),
    current_edge  INTEGER REFERENCES edges(edge_id),
    position_on_edge DOUBLE PRECISION DEFAULT 0.0,  -- 0.0 to 1.0 fraction along edge
    speed_kmh     DOUBLE PRECISION DEFAULT 50.0,
    heading       DOUBLE PRECISION DEFAULT 0.0,     -- degrees from north
    geom          GEOMETRY(Point, 4326),            -- current location
    status        TEXT DEFAULT 'idle',              -- idle, moving, arrived
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_agents_geom ON agents USING GIST (geom);
CREATE INDEX idx_agents_edge ON agents (current_edge);
CREATE INDEX idx_agents_node ON agents (current_node);

-- ============================================================
-- AGENT TRAIL TABLE (historical positions for analytics)
-- ============================================================

CREATE TABLE agent_trails (
    trail_id      SERIAL PRIMARY KEY,
    agent_id      INTEGER NOT NULL REFERENCES agents(agent_id),
    tick          INTEGER NOT NULL,
    node_id       BIGINT NOT NULL REFERENCES nodes(node_id),
    geom          GEOMETRY(Point, 4326),
    recorded_at   TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_trails_agent ON agent_trails (agent_id);
CREATE INDEX idx_trails_geom ON agent_trails USING GIST (geom);

-- ============================================================
-- SPATIAL HELPER FUNCTIONS
-- ============================================================

-- Distance (meters) between two nodes
CREATE OR REPLACE FUNCTION node_distance_m(node_a BIGINT, node_b BIGINT)
RETURNS DOUBLE PRECISION AS $$
    SELECT ST_Distance(
        (SELECT geom::geography FROM nodes WHERE node_id = node_a),
        (SELECT geom::geography FROM nodes WHERE node_id = node_b)
    );
$$ LANGUAGE SQL STABLE;

-- Bearing (degrees) from node A to node B
CREATE OR REPLACE FUNCTION node_bearing(node_a BIGINT, node_b BIGINT)
RETURNS DOUBLE PRECISION AS $$
    SELECT degrees(ST_Azimuth(
        (SELECT geom FROM nodes WHERE node_id = node_a),
        (SELECT geom FROM nodes WHERE node_id = node_b)
    ));
$$ LANGUAGE SQL STABLE;

-- Count agents within a radius (meters) of a node
CREATE OR REPLACE FUNCTION agents_near_node(nid BIGINT, radius_m DOUBLE PRECISION)
RETURNS INTEGER AS $$
    SELECT count(*)::integer
    FROM agents a, nodes n
    WHERE n.node_id = nid
      AND a.geom IS NOT NULL
      AND ST_DWithin(a.geom::geography, n.geom::geography, radius_m);
$$ LANGUAGE SQL STABLE;

-- Count agents on a specific edge (within corridor buffer)
CREATE OR REPLACE FUNCTION agents_on_edge(eid INTEGER, buffer_m DOUBLE PRECISION DEFAULT 15.0)
RETURNS INTEGER AS $$
    SELECT count(*)::integer
    FROM agents a, edges e
    WHERE e.edge_id = eid
      AND a.geom IS NOT NULL
      AND e.geom IS NOT NULL
      AND ST_DWithin(a.geom::geography, e.geom::geography, buffer_m);
$$ LANGUAGE SQL STABLE;

-- Get candidate edges with spatial enrichment (distance to target, congestion)
CREATE OR REPLACE FUNCTION enriched_outgoing_edges(
    from_node BIGINT,
    target_node BIGINT
)
RETURNS TABLE (
    edge_id       INTEGER,
    source_node   BIGINT,
    target_node_  BIGINT,
    name          TEXT,
    highway       TEXT,
    length_m      DOUBLE PRECISION,
    speed_kph     DOUBLE PRECISION,
    dist_to_target_m DOUBLE PRECISION,
    bearing_to_target DOUBLE PRECISION,
    edge_bearing  DOUBLE PRECISION,
    congestion    INTEGER
) AS $$
    SELECT
        e.edge_id,
        e.source_node,
        e.target_node,
        e.name,
        e.highway,
        e.length_m,
        e.speed_kph,
        -- How far is this edge's target from our destination?
        ST_Distance(
            (SELECT geom::geography FROM nodes WHERE node_id = e.target_node),
            (SELECT geom::geography FROM nodes WHERE node_id = target_node)
        ) AS dist_to_target_m,
        -- Bearing from edge target toward our destination
        degrees(ST_Azimuth(
            (SELECT geom FROM nodes WHERE node_id = e.target_node),
            (SELECT geom FROM nodes WHERE node_id = target_node)
        )) AS bearing_to_target,
        -- Bearing of this edge segment
        degrees(ST_Azimuth(
            (SELECT geom FROM nodes WHERE node_id = e.source_node),
            (SELECT geom FROM nodes WHERE node_id = e.target_node)
        )) AS edge_bearing,
        -- Number of vehicles currently on/near this edge
        (SELECT count(*)::integer FROM agents a
         WHERE a.geom IS NOT NULL AND e.geom IS NOT NULL
         AND ST_DWithin(a.geom::geography, e.geom::geography, 20.0)
        ) AS congestion
    FROM edges e
    WHERE e.source_node = from_node;
$$ LANGUAGE SQL STABLE;

-- Shortest-path cost (total meters) using Dijkstra
CREATE OR REPLACE FUNCTION shortest_path_cost(
    start_node BIGINT,
    end_node BIGINT
)
RETURNS DOUBLE PRECISION AS $$
DECLARE
    total_cost DOUBLE PRECISION;
BEGIN
    SELECT sum(cost) INTO total_cost
    FROM pgr_dijkstra(
        'SELECT edge_id AS id, source_node AS source, target_node AS target, length_m AS cost, CASE WHEN oneway THEN -1 ELSE length_m END AS reverse_cost FROM edges',
        start_node,
        end_node,
        directed := true
    );
    RETURN total_cost;
END;
$$ LANGUAGE plpgsql STABLE;

-- Shortest-path sequence (list of edge IDs)
CREATE OR REPLACE FUNCTION shortest_path_edges(
    start_node BIGINT,
    end_node BIGINT
)
RETURNS TABLE (seq INTEGER, edge_id BIGINT, node_id BIGINT, cost DOUBLE PRECISION) AS $$
    SELECT seq::integer, edge AS edge_id, node AS node_id, cost
    FROM pgr_dijkstra(
        'SELECT edge_id AS id, source_node AS source, target_node AS target, length_m AS cost, CASE WHEN oneway THEN -1 ELSE length_m END AS reverse_cost FROM edges',
        start_node,
        end_node,
        directed := true
    )
    ORDER BY seq;
$$ LANGUAGE SQL STABLE;
