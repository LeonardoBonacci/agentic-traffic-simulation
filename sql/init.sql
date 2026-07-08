-- Enable PostGIS extension
CREATE EXTENSION IF NOT EXISTS postgis;

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
