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
-- PUB/SUB: auto-broadcast vehicle movements via PG NOTIFY
-- ============================================================

-- Trigger function: fires NOTIFY when a vehicle changes current_node
CREATE OR REPLACE FUNCTION notify_vehicle_move() RETURNS trigger AS $$
DECLARE
    road_name TEXT;
BEGIN
    -- Only fire if current_node actually changed
    IF OLD.current_node IS DISTINCT FROM NEW.current_node THEN
        -- Look up the road name between old and new node
        SELECT COALESCE(e.name, e.highway, 'unnamed road') INTO road_name
        FROM edges e
        WHERE (e.source_node = OLD.current_node AND e.target_node = NEW.current_node)
           OR (e.target_node = OLD.current_node AND e.source_node = NEW.current_node AND e.oneway = FALSE)
        LIMIT 1;

        PERFORM pg_notify('vehicle_moves', json_build_object(
            'vehicle', NEW.name,
            'from', OLD.current_node,
            'to', NEW.current_node,
            'road', COALESCE(road_name, 'unnamed road'),
            'status', NEW.status
        )::text);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_vehicle_move
    AFTER UPDATE ON agents
    FOR EACH ROW
    WHEN (OLD.current_node IS DISTINCT FROM NEW.current_node)
    EXECUTE FUNCTION notify_vehicle_move();

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
