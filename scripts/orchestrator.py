"""
Traffic Simulation Orchestrator

A loop that moves vehicles step-by-step over the road network.
Each tick, every vehicle advances to the next node along a weighted-random
outgoing edge from its current position.  Vehicles must give way to
traffic approaching an intersection from their left side.

Usage:
    python3 scripts/orchestrator.py
"""

import json
import math
import random
import re
import time
from collections import defaultdict

import psycopg2
import requests
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

OLLAMA_MODEL = "qwen3:8b"
OLLAMA_URL = "http://localhost:11434/api/chat"
USE_LLM = True  # set False to fall back to weighted-random only


# ─── LLM helper ──────────────────────────────────────────────────────────────

def llm_chat(system_prompt, user_prompt):
    """Call Ollama chat API directly (bypasses langchain think-mode bug)."""
    resp = requests.post(OLLAMA_URL, json={
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.4, "num_predict": 32},
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()["message"]["content"].strip()


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
            SELECT edge_id, source_node, target_node, name, highway, length_m, speed_kph
            FROM edges
            WHERE source_node = %s
        """, (node_id,))
        return cur.fetchall()


def get_incoming_edges(conn, node_id):
    """Get edges arriving at a node (for bidirectional roads)."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT edge_id, source_node, target_node AS target_node, name, highway, length_m, speed_kph
            FROM edges
            WHERE target_node = %s AND oneway = FALSE
        """, (node_id,))
        return cur.fetchall()


def get_node_coords(conn, node_id):
    """Get (lon, lat) for a node from its PostGIS geometry."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ST_X(geom), ST_Y(geom) FROM nodes WHERE node_id = %s
        """, (node_id,))
        row = cur.fetchone()
        if row:
            return row[0], row[1]
        return None, None


def update_agent_position(conn, agent_id, new_node, status="moving"):
    """Update an agent's current node and geom in the database."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE agents
            SET current_node = %s,
                status = %s,
                geom = (SELECT geom FROM nodes WHERE node_id = %s),
                updated_at = now()
            WHERE agent_id = %s
        """, (new_node, status, new_node, agent_id))
    conn.commit()


# ─── Simulation logic ────────────────────────────────────────────────────────

# ─── Geometry / bearing helpers ──────────────────────────────────────────────

def compute_bearing(lon1, lat1, lon2, lat2):
    """
    Compute initial bearing (0-360°, clockwise from north) from point 1 to point 2.
    """
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    x = math.sin(dlon) * math.cos(lat2_r)
    y = math.cos(lat1_r) * math.sin(lat2_r) - \
        math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon)
    bearing = math.degrees(math.atan2(x, y))
    return bearing % 360


def angle_difference(bearing1, bearing2):
    """
    Signed angle from bearing1 to bearing2 (-180 to +180).
    Positive = bearing2 is clockwise (to the right) of bearing1.
    """
    diff = (bearing2 - bearing1 + 180) % 360 - 180
    return diff


def is_approaching_from_left(my_approach_bearing, other_approach_bearing):
    """
    Determine if another vehicle is approaching the intersection from my left.
    my_approach_bearing: the direction I'm travelling INTO the intersection.
    other_approach_bearing: the direction they're travelling INTO the intersection.

    "From my left" means the other vehicle's arrival direction is roughly
    to the left of my direction of travel (between -30° and -150° relative).
    """
    # Reverse the other's bearing to get direction FROM which they arrive
    other_from = (other_approach_bearing + 180) % 360
    diff = angle_difference(my_approach_bearing, other_from)
    # Negative diff = to my left
    return -150 <= diff <= -30


# ─── Turn weighting ─────────────────────────────────────────────────────────

def weight_edges_by_turn(conn, from_node, edges):
    """
    Weight outgoing edges based on how much turning is required.
    Strongly prefers going straight, discourages U-turns.
    Returns list of (edge, weight) tuples.
    """
    if not edges:
        return []

    from_lon, from_lat = get_node_coords(conn, from_node)
    if from_lon is None:
        # Can't compute geometry — equal weights
        return [(e, 1.0) for e in edges]

    # Our approach bearing into from_node is unknown without previous node,
    # so we weight relative to each other (uniform if no geometry)
    # We'll compute target bearings from from_node to each edge target
    weighted = []
    for e in edges:
        target = e["target_node"]
        t_lon, t_lat = get_node_coords(conn, target)
        if t_lon is None:
            weighted.append((e, 1.0))
        else:
            weighted.append((e, 1.0))  # base weight

    return weighted


def weight_edges_with_history(conn, prev_node, current_node, edges):
    """
    Weight edges considering the direction we came from (prev_node -> current_node).
    Prefers continuing straight, penalises U-turns.
    """
    if not edges or prev_node is None:
        return [(e, 1.0) for e in edges]

    prev_lon, prev_lat = get_node_coords(conn, prev_node)
    cur_lon, cur_lat = get_node_coords(conn, current_node)

    if prev_lon is None or cur_lon is None:
        return [(e, 1.0) for e in edges]

    # Bearing we arrived from
    arrival_bearing = compute_bearing(prev_lon, prev_lat, cur_lon, cur_lat)

    weighted = []
    for e in edges:
        target = e["target_node"]
        t_lon, t_lat = get_node_coords(conn, target)
        if t_lon is None:
            weighted.append((e, 1.0))
            continue

        exit_bearing = compute_bearing(cur_lon, cur_lat, t_lon, t_lat)
        turn_angle = abs(angle_difference(arrival_bearing, exit_bearing))

        # Weight: straight (0°) = 5, slight turn (45°) = 4, right angle (90°) = 2, U-turn (180°) = 0.2
        if turn_angle <= 30:
            w = 5.0   # straight ahead
        elif turn_angle <= 60:
            w = 3.5   # slight turn
        elif turn_angle <= 100:
            w = 2.0   # moderate turn
        elif turn_angle <= 140:
            w = 1.0   # sharp turn
        else:
            w = 0.2   # U-turn (very unlikely)

        weighted.append((e, w))

    return weighted


# ─── Vehicle state (previous node tracking) ─────────────────────────────────

# Maps agent_id -> previous_node (to compute approach bearing)
agent_prev_node = {}


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


def pick_next_node_random(conn, agent, current_node, edges):
    """
    Fallback: weighted-random edge selection based on turn angle.
    """
    prev_node = agent_prev_node.get(agent["agent_id"])
    weighted = weight_edges_with_history(conn, prev_node, current_node, edges)

    total = sum(w for _, w in weighted)
    if total == 0:
        edge = random.choice(edges)
    else:
        r = random.uniform(0, total)
        cumulative = 0
        edge = weighted[0][0]
        for e, w in weighted:
            cumulative += w
            if r <= cumulative:
                edge = e
                break

    return edge["target_node"], edge


def pick_next_node_llm(conn, agent, current_node, edges):
    """
    Use LLM to decide which edge the vehicle should take.
    Presents road options and asks the model to choose.
    """
    prev_node = agent_prev_node.get(agent["agent_id"])

    # Build option descriptions
    options = []
    for i, e in enumerate(edges):
        street = e.get("name") or e.get("highway") or "unnamed road"
        length = e.get("length_m", 0)
        speed = e.get("speed_kph", "unknown")
        options.append(f"  {i}: \"{street}\" ({length:.0f}m, speed limit {speed} km/h)")

    options_text = "\n".join(options)

    system_prompt = (
        "You are a vehicle navigation AI. Pick which road to take next. "
        "Reply with ONLY the option number (e.g. 0, 1, 2). No explanation."
    )

    context_parts = [f"Vehicle: {agent['name']}, speed: {agent['speed_kmh']} km/h"]
    if prev_node:
        context_parts.append(f"Came from node {prev_node}")
    context_parts.append(f"Currently at node {current_node}")
    context_parts.append(f"Target destination: node {agent['target_node']}")
    context_parts.append(f"\nAvailable roads:\n{options_text}")
    context_parts.append(f"\nWhich road? Reply with the number only.")

    human_prompt = "\n".join(context_parts)

    try:
        choice_text = llm_chat(system_prompt, human_prompt)
        # Strip any <think>...</think> tags qwen3 might emit
        choice_text = re.sub(r"<think>.*?</think>", "", choice_text, flags=re.DOTALL).strip()
        # Extract first number from response
        digits = re.search(r"\d+", choice_text)
        if digits:
            choice = int(digits.group())
            if 0 <= choice < len(edges):
                edge = edges[choice]
                return edge["target_node"], edge
    except Exception as e:
        print(f"    [LLM fallback] {e}")

    # Fallback to weighted random
    return pick_next_node_random(conn, agent, current_node, edges)


def pick_next_node(conn, agent, current_node):
    """
    Pick the next node for a vehicle to move to.
    Uses LLM decision-making when enabled, otherwise weighted-random.
    Returns (next_node, edge_info) or (None, None) if stuck.
    """
    edges = get_outgoing_edges(conn, current_node)
    if not edges:
        edges = get_incoming_edges(conn, current_node)
    if not edges:
        return None, None

    if USE_LLM:
        return pick_next_node_llm(conn, agent, current_node, edges)
    else:
        return pick_next_node_random(conn, agent, current_node, edges)


def plan_move(conn, agent):
    """
    Plan the next move for a vehicle. Returns (next_node, edge) or (None, None).
    Does NOT commit the move — used for conflict detection.
    """
    current = agent["current_node"]
    if agent["status"] == "arrived":
        return None, None
    return pick_next_node(conn, agent, current)


def get_approach_bearing(conn, agent):
    """Get the bearing at which an agent is approaching its current node."""
    prev = agent_prev_node.get(agent["agent_id"])
    if prev is None:
        return None
    p_lon, p_lat = get_node_coords(conn, prev)
    c_lon, c_lat = get_node_coords(conn, agent["current_node"])
    if p_lon is None or c_lon is None:
        return None
    return compute_bearing(p_lon, p_lat, c_lon, c_lat)


def resolve_give_way(conn, moves):
    """
    Given a dict of {agent_id: (agent, next_node, edge)}, resolve conflicts
    at intersections using the "give way to left" rule.

    When multiple vehicles want to enter the same node, a vehicle must
    yield if another vehicle is approaching that intersection from its left.

    Returns set of agent_ids that are BLOCKED this tick.
    """
    # Group by target node
    by_target = defaultdict(list)
    for agent_id, (agent, next_node, edge) in moves.items():
        if next_node is not None:
            by_target[next_node].append((agent_id, agent, edge))

    blocked = set()

    for target_node, contenders in by_target.items():
        if len(contenders) < 2:
            continue  # no conflict

        # Compute approach bearings for each contender
        bearings = {}
        for agent_id, agent, edge in contenders:
            bearing = get_approach_bearing(conn, agent)
            if bearing is not None:
                bearings[agent_id] = bearing

        # For each pair, check give-way-to-left
        for agent_id, agent, edge in contenders:
            if agent_id not in bearings:
                continue
            my_bearing = bearings[agent_id]

            for other_id, other_agent, other_edge in contenders:
                if other_id == agent_id or other_id not in bearings:
                    continue
                other_bearing = bearings[other_id]

                # If the other vehicle is approaching from MY left, I must give way
                if is_approaching_from_left(my_bearing, other_bearing):
                    blocked.add(agent_id)
                    break

    return blocked


def step_agents(conn, agents):
    """
    Advance all vehicles by one tick with give-way-to-left conflict resolution.
    """
    # Phase 1: Plan moves for all agents
    planned_moves = {}
    for agent in agents:
        if agent["status"] == "arrived":
            print(f"  [{agent['name']}] Already arrived at destination")
            continue
        next_node, edge = plan_move(conn, agent)
        if next_node is None:
            print(f"  [{agent['name']}] STUCK at node {agent['current_node']} -- no outgoing edges")
            continue
        planned_moves[agent["agent_id"]] = (agent, next_node, edge)

    # Phase 2: Resolve give-way conflicts
    blocked = resolve_give_way(conn, planned_moves)

    # Phase 3: Execute non-blocked moves
    for agent_id, (agent, next_node, edge) in planned_moves.items():
        name = agent["name"]
        current = agent["current_node"]

        if agent_id in blocked:
            print(f"  [{name}] YIELDING at node {current} (give way to left)")
            continue

        # Track previous node for bearing calculations
        agent_prev_node[agent_id] = current

        status = "moving"
        if next_node == agent["target_node"]:
            status = "arrived"

        update_agent_position(conn, agent_id, next_node, status)

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

        step_agents(conn, agents)

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
