"""Database connection and helper functions."""

import psycopg2
from psycopg2.extras import RealDictCursor

from config import DB_CONFIG


def get_connection():
    return psycopg2.connect(**DB_CONFIG)


def load_agents(conn):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT agent_id, name, current_node, target_node, speed_kmh, status
            FROM agents WHERE agent_type = 'vehicle'
        """)
        return cur.fetchall()


def record_trail(conn, agent_id, tick, node_id):
    with conn.cursor() as cur:
        # Build the waypoint JSON object, then upsert into the JSONB array
        cur.execute("""
            WITH wp AS (
                SELECT jsonb_build_object(
                    'tick', %s::int,
                    'node_id', %s::bigint,
                    'lat', ST_Y(n.geom),
                    'lng', ST_X(n.geom),
                    'ts', now()
                ) AS point
                FROM nodes n WHERE n.node_id = %s
            )
            INSERT INTO agent_trails (agent_id, waypoints)
            SELECT %s, jsonb_build_array(wp.point) FROM wp
            ON CONFLICT (agent_id) DO UPDATE
            SET waypoints = agent_trails.waypoints || (SELECT jsonb_build_array(wp.point) FROM wp)
        """, (tick, node_id, node_id, agent_id))
    conn.commit()


def update_agent_position(conn, agent_id, new_node, status="moving"):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE agents
            SET current_node = %s, status = %s,
                geom = (SELECT geom FROM nodes WHERE node_id = %s),
                updated_at = now()
            WHERE agent_id = %s
        """, (new_node, status, new_node, agent_id))
    conn.commit()
