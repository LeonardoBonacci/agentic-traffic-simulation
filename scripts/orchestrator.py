"""
Traffic Simulation Orchestrator

A basic loop that moves vehicles step-by-step over the road network.
Each tick, every vehicle advances to the next node along a random
outgoing edge from its current position.

Usage:
    python3 scripts/orchestrator.py
"""

import random
import time

import psycopg2
from psycopg2.extras import RealDictCursor

# ─── Configuration ───────────────────────────────────────────────────────────

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "traffic_sim",
    "user": "traffic",
    "password": "traffic123",
}

TICK_INTERVAL = 1.0  # seconds between simulation steps
MAX_TICKS = 20       # run for this many steps then stop


# ─── Database helpers ────────────────────────────────────────────────────────

def get_connection():
    return psycopg2.connect(**DB_CONFIG)


def load_agents(conn):
    """Load all vehicle agents from the database."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT agent_id, name, current_node, target_node, speed_kmh, status
            FROM agents
            WHERE agent_type = 'vehicle'
        """)
        return cur.fetchall()


def get_outgoing_edges(conn, node_id):
    """Get all edges leaving from a given node."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT edge_id, target_node, name, highway, length_m, speed_kph
            FROM edges
            WHERE source_node = %s
        """, (node_id,))
        return cur.fetchall()


def get_incoming_edges(conn, node_id):
    """Get edges arriving at a node (for bidirectional roads)."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT edge_id, source_node AS target_node, name, highway, length_m, speed_kph
            FROM edges
            WHERE target_node = %s AND oneway = FALSE
        """, (node_id,))
        return cur.fetchall()


def update_agent_position(conn, agent_id, new_node, status="moving"):
    """Update an agent's current node in the database."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE agents
            SET current_node = %s, status = %s, updated_at = now()
            WHERE agent_id = %s
        """, (new_node, status, agent_id))
    conn.commit()


# ─── Simulation logic ────────────────────────────────────────────────────────

INITIAL_VEHICLES = [
    ("car_alpha",   40.0),
    ("car_bravo",   50.0),
    ("car_charlie", 45.0),
    ("car_delta",   55.0),
    ("car_echo",    35.0),
]


def seed_vehicles(conn):
    """Insert initial vehicles if none exist, using random start/target nodes."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agents WHERE agent_type = 'vehicle'")
        count = cur.fetchone()[0]
        if count > 0:
            return  # already seeded

        for name, speed in INITIAL_VEHICLES:
            cur.execute("""
                INSERT INTO agents (name, agent_type, current_node, target_node, speed_kmh)
                VALUES (%s, 'vehicle',
                        (SELECT node_id FROM nodes ORDER BY random() LIMIT 1),
                        (SELECT node_id FROM nodes ORDER BY random() LIMIT 1),
                        %s)
            """, (name, speed))
    conn.commit()
    print(f"Seeded {len(INITIAL_VEHICLES)} vehicles.\n")


def pick_next_node(conn, current_node):
    """
    Pick the next node for a vehicle to move to.
    Strategy: choose a random outgoing edge. If none, try reverse edges.
    Returns (next_node, edge_info) or (None, None) if stuck.
    """
    edges = get_outgoing_edges(conn, current_node)
    if not edges:
        edges = get_incoming_edges(conn, current_node)
    if not edges:
        return None, None
    edge = random.choice(edges)
    return edge["target_node"], edge


def step_agent(conn, agent):
    """Advance a single vehicle agent by one edge."""
    current = agent["current_node"]
    name = agent["name"]

    next_node, edge = pick_next_node(conn, current)

    if next_node is None:
        print(f"  [{name}] STUCK at node {current} -- no outgoing edges")
        return

    # Check if agent reached its target
    status = "moving"
    if next_node == agent["target_node"]:
        status = "arrived"

    update_agent_position(conn, agent["agent_id"], next_node, status)

    street = edge.get("name") or edge.get("highway") or "unnamed road"
    length = edge.get("length_m", 0)

    print(f"  [{name}] {current} -> {next_node} via \"{street}\" ({length:.0f}m) [{status}]")


def run_simulation():
    """Main orchestrator loop."""
    conn = get_connection()

    print("=" * 60)
    print("  TRAFFIC SIMULATION ORCHESTRATOR")
    print("=" * 60)
    print()

    seed_vehicles(conn)

    agents = load_agents(conn)
    print(f"Loaded {len(agents)} vehicles:\n")
    for a in agents:
        print(f"  - {a['name']} at node {a['current_node']} -> target {a['target_node']}")
    print()

    for tick in range(1, MAX_TICKS + 1):
        print(f"--- Tick {tick:03d} {'-' * 44}")

        # Reload agents to get updated positions
        agents = load_agents(conn)

        for agent in agents:
            if agent["status"] == "arrived":
                print(f"  [{agent['name']}] Already arrived at destination")
                continue
            step_agent(conn, agent)

        print()
        time.sleep(TICK_INTERVAL)

    # Final summary
    print("=" * 60)
    print("  SIMULATION COMPLETE")
    print("=" * 60)
    agents = load_agents(conn)
    for a in agents:
        marker = "[OK]" if a["status"] == "arrived" else "[..]"
        print(f"  {marker} {a['name']}: node {a['current_node']} [{a['status']}]")

    conn.close()


if __name__ == "__main__":
    run_simulation()
