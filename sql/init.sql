-- Enable PostGIS and pgRouting extensions
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgrouting;

-- ============================================================
-- STATIC STREET MAP TABLES (read once at simulation start)
-- ============================================================

-- Intersections / junctions (graph nodes)
CREATE TABLE nodes (
    node_id       BIGINT PRIMARY KEY,
    geom          GEOMETRY(Point, 4326) NOT NULL
);

CREATE INDEX idx_nodes_geom ON nodes USING GIST (geom);

-- Road segments (graph edges)
CREATE TABLE edges (
    edge_id       SERIAL PRIMARY KEY,
    source_node   BIGINT NOT NULL REFERENCES nodes(node_id),
    target_node   BIGINT NOT NULL REFERENCES nodes(node_id),
    highway       TEXT,          -- road classification: trunk, secondary, tertiary, etc.
    name          TEXT,          -- street name
    oneway        BOOLEAN DEFAULT FALSE,
    length_m      DOUBLE PRECISION,  -- segment length in meters
    speed_kph     DOUBLE PRECISION,  -- speed in km/h
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
    agent_type    TEXT NOT NULL DEFAULT 'vehicle',
    current_node  BIGINT NOT NULL REFERENCES nodes(node_id),
    target_node   BIGINT REFERENCES nodes(node_id),
    speed_kmh     DOUBLE PRECISION DEFAULT 50.0,
    geom          GEOMETRY(Point, 4326),            -- current location
    status        TEXT DEFAULT 'idle',              -- idle, moving, arrived
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_agents_geom ON agents USING GIST (geom);
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
