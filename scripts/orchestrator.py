"""
Traffic Simulation Orchestrator

A loop that moves vehicles step-by-step over the road network.
Each tick, every vehicle uses PostGIS spatial intelligence to navigate:
  - pgRouting Dijkstra shortest-path to compute optimal routes
  - ST_Distance / ST_DWithin for congestion detection and avoidance
  - Enriched edge queries with distance-to-target, bearing, and congestion
  - LLM receives full spatial context (not just street names)
  - pgRouting Dijkstra for optimal route planning

Usage:
    python3 scripts/orchestrator.py
"""

import json
import math
import random
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

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
USE_LLM = True  # set False to fall back to spatial-weighted only


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
        "options": {"temperature": 0.4, "num_predict": 8},
    }, timeout=10)
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


# ─── PostGIS Spatial Intelligence ────────────────────────────────────────────

def get_enriched_edges(conn, from_node, target_node):
    """
    Get outgoing edges enriched with PostGIS spatial data:
    - distance from each candidate's endpoint to the agent's target
    - bearing toward target
    - edge bearing (direction of travel)
    - congestion count (vehicles currently on/near the edge)
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT * FROM enriched_outgoing_edges(%s, %s)
        """, (from_node, target_node))
        return cur.fetchall()


def get_shortest_path(conn, from_node, target_node):
    """
    Use pgRouting Dijkstra to compute the shortest path.
    Returns list of (edge_id, node_id, cost) or empty list if no path.
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        try:
            cur.execute("""
                SELECT seq, edge_id, node_id, cost
                FROM shortest_path_edges(%s, %s)
            """, (from_node, target_node))
            return cur.fetchall()
        except Exception:
            conn.rollback()
            return []


def get_shortest_path_cost(conn, from_node, target_node):
    """Get total shortest-path distance in meters via pgRouting."""
    with conn.cursor() as cur:
        try:
            cur.execute("SELECT shortest_path_cost(%s, %s)", (from_node, target_node))
            result = cur.fetchone()
            return result[0] if result and result[0] else None
        except Exception:
            conn.rollback()
            return None


def get_congestion_at_node(conn, node_id, radius_m=100.0):
    """Count vehicles near a node using ST_DWithin (geography)."""
    with conn.cursor() as cur:
        cur.execute("SELECT agents_near_node(%s, %s)", (node_id, radius_m))
        return cur.fetchone()[0]


def get_congestion_on_edge(conn, edge_id):
    """Count vehicles on/near a specific edge using ST_DWithin."""
    with conn.cursor() as cur:
        cur.execute("SELECT agents_on_edge(%s)", (edge_id,))
        return cur.fetchone()[0]


def get_nearby_agents(conn, node_id, radius_m=200.0):
    """Get details of nearby agents within radius (for situational awareness)."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT a.agent_id, a.name, a.current_node, a.status, a.speed_kmh,
                   ST_Distance(a.geom::geography, n.geom::geography) AS distance_m
            FROM agents a, nodes n
            WHERE n.node_id = %s
              AND a.geom IS NOT NULL
              AND ST_DWithin(a.geom::geography, n.geom::geography, %s)
            ORDER BY distance_m
        """, (node_id, radius_m))
        return cur.fetchall()


def get_distance_to_target(conn, from_node, target_node):
    """Straight-line distance (meters) between two nodes using PostGIS geography."""
    with conn.cursor() as cur:
        cur.execute("SELECT node_distance_m(%s, %s)", (from_node, target_node))
        result = cur.fetchone()
        return result[0] if result else None


def get_bearing_to_target(conn, from_node, target_node):
    """Bearing (degrees from north) from from_node to target_node."""
    with conn.cursor() as cur:
        cur.execute("SELECT node_bearing(%s, %s)", (from_node, target_node))
        result = cur.fetchone()
        return result[0] if result else None


def record_trail(conn, agent_id, tick, node_id):
    """Record agent position in the trail table for later analytics."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO agent_trails (agent_id, tick, node_id, geom)
            VALUES (%s, %s, %s, (SELECT geom FROM nodes WHERE node_id = %s))
        """, (agent_id, tick, node_id, node_id))
    conn.commit()


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
    """
    other_from = (other_approach_bearing + 180) % 360
    diff = angle_difference(my_approach_bearing, other_from)
    return -150 <= diff <= -30


# ─── Spatial route planning ──────────────────────────────────────────────────

# Maps agent_id -> previous_node (to compute approach bearing)
agent_prev_node = {}

# Maps agent_id -> cached shortest path (list of node_ids)
agent_planned_route = {}


def compute_route(conn, agent):
    """
    Compute (or re-compute) the shortest path from current_node to target_node
    using pgRouting Dijkstra. Caches the result.
    """
    path = get_shortest_path(conn, agent["current_node"], agent["target_node"])
    if path:
        route_nodes = [step["node_id"] for step in path if step["node_id"] != -1]
        agent_planned_route[agent["agent_id"]] = route_nodes
        return route_nodes
    return []


def score_edge_spatially(edge, current_dist_to_target):
    """
    Score an enriched edge based on spatial properties.
    Higher score = better choice.
    """
    score = 0.0

    # 1. Reward edges that bring us closer to target
    dist_to_target = edge.get("dist_to_target_m")
    if dist_to_target is not None and current_dist_to_target:
        progress = current_dist_to_target - dist_to_target  # positive = getting closer
        score += max(0, progress) * 0.01  # scale factor

    # 2. Penalize congested edges heavily
    congestion = edge.get("congestion", 0)
    score -= congestion * 2.0

    # 3. Prefer faster roads
    speed = edge.get("speed_kph") or 30
    score += speed * 0.05

    # 4. Slight preference for shorter segments (more responsive navigation)
    length = edge.get("length_m") or 100
    if length < 200:
        score += 0.5

    return score


def pick_next_node_spatial(conn, agent, current_node, enriched_edges, current_dist):
    """
    Spatially-intelligent edge selection using PostGIS-computed metrics.
    Considers: distance-to-target progress, congestion, speed, route plan.
    """
    # Check if we have a planned route and the next step is among our options
    route = agent_planned_route.get(agent["agent_id"], [])
    if route:
        # Find the next node in our planned route
        try:
            current_idx = route.index(current_node)
            if current_idx + 1 < len(route):
                next_planned = route[current_idx + 1]
                for e in enriched_edges:
                    if e["target_node_"] == next_planned:
                        return next_planned, e
        except ValueError:
            pass  # current_node not in route (stale), recompute later

    # Score all candidate edges
    scored = []
    for e in enriched_edges:
        s = score_edge_spatially(e, current_dist)
        scored.append((e, s))

    scored.sort(key=lambda x: x[1], reverse=True)

    if scored:
        edge = scored[0][0]
        return edge["target_node_"], edge

    return None, None


def pick_next_node_llm(conn, agent, current_node, enriched_edges, current_dist):
    """
    LLM-powered navigation with FULL spatial context from PostGIS.
    The LLM sees: distance to target, congestion, bearings, route progress.
    """
    # Build rich spatial context for each option
    options = []
    for i, e in enumerate(enriched_edges):
        street = e.get("name") or e.get("highway") or "unnamed road"
        length = e.get("length_m", 0) or 0
        speed = e.get("speed_kph") or "unknown"
        dist_to_target = e.get("dist_to_target_m")
        congestion = e.get("congestion", 0)
        bearing = e.get("edge_bearing")

        parts = [f'{i}: "{street}" ({length:.0f}m, {speed} km/h)']
        if dist_to_target is not None:
            progress = (current_dist - dist_to_target) if current_dist else 0
            parts.append(f"    -> brings you {progress:.0f}m closer to destination" if progress > 0
                        else f"    -> takes you {abs(progress):.0f}m further from destination")
        if congestion > 0:
            parts.append(f"    -> CONGESTION: {congestion} vehicles ahead")
        if bearing is not None:
            parts.append(f"    -> heading {bearing:.0f}°")

        options.append("\n".join(parts))

    options_text = "\n".join(options)

    # Get nearby vehicles for situational awareness
    nearby = get_nearby_agents(conn, current_node, 200.0)
    nearby_self = [a for a in nearby if a["agent_id"] != agent["agent_id"]]
    nearby_text = ""
    if nearby_self:
        nearby_text = f"\nNearby vehicles ({len(nearby_self)} within 200m):"
        for n in nearby_self[:3]:
            nearby_text += f"\n  - {n['name']} ({n['distance_m']:.0f}m away, {n['status']})"

    system_prompt = (
        "You are a vehicle navigation AI optimizing for: "
        "1) reaching the destination quickly, "
        "2) avoiding congested roads, "
        "3) preferring faster roads. "
        "Reply with ONLY the option number (e.g. 0, 1, 2). No explanation."
    )

    bearing_to_target = get_bearing_to_target(conn, current_node, agent["target_node"])
    bearing_text = f"\nBearing to destination: {bearing_to_target:.0f}°" if bearing_to_target else ""

    context_parts = [
        f"Vehicle: {agent['name']}, speed: {agent['speed_kmh']} km/h",
        f"Distance to destination: {current_dist:.0f}m" if current_dist else "",
        bearing_text,
        nearby_text,
        f"\nAvailable roads:\n{options_text}",
        f"\nWhich road? Reply with the number only.",
    ]

    human_prompt = "\n".join(p for p in context_parts if p)

    try:
        choice_text = llm_chat(system_prompt, human_prompt)
        choice_text = re.sub(r"<think>.*?</think>", "", choice_text, flags=re.DOTALL).strip()
        digits = re.search(r"\d+", choice_text)
        if digits:
            choice = int(digits.group())
            if 0 <= choice < len(enriched_edges):
                edge = enriched_edges[choice]
                return edge["target_node_"], edge
    except Exception as e:
        print(f"    [LLM fallback] {e}")

    # Fallback to spatial scoring
    return pick_next_node_spatial(conn, agent, current_node, enriched_edges, current_dist)


def pick_next_node(conn, agent, current_node):
    """
    Pick the next node using PostGIS spatial intelligence.
    1. Get enriched edges (distance, bearing, congestion) from PostGIS
    2. Check if re-routing is needed (congestion ahead)
    3. Use LLM with full spatial context, or spatial scoring as fallback
    """
    # Get spatially-enriched edges from PostGIS
    enriched = get_enriched_edges(conn, current_node, agent["target_node"])

    if not enriched:
        # Try reverse edges for bidirectional roads
        edges = get_incoming_edges(conn, current_node)
        if not edges:
            return None, None
        # Fall back to basic edges without enrichment
        enriched = []
        for e in edges:
            enriched.append({
                "edge_id": e["edge_id"],
                "source_node": e["source_node"],
                "target_node_": e["target_node"],
                "name": e["name"],
                "highway": e["highway"],
                "length_m": e["length_m"],
                "speed_kph": e["speed_kph"],
                "dist_to_target_m": None,
                "bearing_to_target": None,
                "edge_bearing": None,
                "congestion": 0,
            })

    if not enriched:
        return None, None

    # Current straight-line distance to target
    current_dist = get_distance_to_target(conn, current_node, agent["target_node"])

    if USE_LLM:
        return pick_next_node_llm(conn, agent, current_node, enriched, current_dist)
    else:
        return pick_next_node_spatial(conn, agent, current_node, enriched, current_dist)


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


INITIAL_VEHICLES = [(f"car_{i:02d}", random.uniform(30.0, 60.0)) for i in range(1, 11)]


def seed_vehicles(conn):
    """
    Insert initial vehicles with spatially-separated start/target nodes.
    Uses ST_Distance to ensure start and target are meaningfully apart.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agents WHERE agent_type = 'vehicle'")
        count = cur.fetchone()[0]
        if count > 0:
            return  # already seeded

        for name, speed in INITIAL_VEHICLES:
            # Pick start and target that are at least 500m apart
            cur.execute("""
                WITH start_node AS (
                    SELECT node_id, geom FROM nodes ORDER BY random() LIMIT 1
                )
                INSERT INTO agents (name, agent_type, current_node, target_node, speed_kmh, geom)
                SELECT %s, 'vehicle', s.node_id,
                       (SELECT n.node_id FROM nodes n, start_node s2
                        WHERE ST_Distance(n.geom::geography, s2.geom::geography) > 500
                        ORDER BY random() LIMIT 1),
                       %s, s.geom
                FROM start_node s, start_node s2
            """, (name, speed))
    conn.commit()
    print(f"Seeded {len(INITIAL_VEHICLES)} vehicles (min 500m apart from targets).\n")


def step_agents(conn, agents, tick):
    """
    Advance all vehicles by one tick with spatial intelligence and conflict resolution.
    Uses ThreadPoolExecutor to parallelize LLM calls across vehicles.
    """
    # Phase 1: Plan moves for all agents (parallelized)
    active_agents = [a for a in agents if a["status"] != "arrived"]
    arrived_agents = [a for a in agents if a["status"] == "arrived"]

    for agent in arrived_agents:
        print(f"  [{agent['name']}] Already arrived at destination")

    def plan_for_agent(agent):
        """Plan move for a single agent using its own DB connection."""
        thread_conn = get_connection()
        try:
            next_node, edge = plan_move(thread_conn, agent)
            return agent, next_node, edge
        finally:
            thread_conn.close()

    planned_moves = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(plan_for_agent, a): a for a in active_agents}
        for future in as_completed(futures):
            agent, next_node, edge = future.result()
            if next_node is None:
                print(f"  [{agent['name']}] STUCK at node {agent['current_node']} -- no outgoing edges")
            else:
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
        record_trail(conn, agent_id, tick, next_node)

        street = edge.get("name") or edge.get("highway") or "unnamed road"
        length = edge.get("length_m") or 0
        congestion = edge.get("congestion", 0)
        dist_remaining = edge.get("dist_to_target_m")

        extra = ""
        if congestion > 0:
            extra += f" [!{congestion} vehicles]"
        if dist_remaining:
            extra += f" [{dist_remaining:.0f}m to go]"

        print(f"  [{name}] {current} -> {next_node} via \"{street}\" ({length:.0f}m){extra} [{status}]")


def run_simulation():
    """Main orchestrator loop with spatial intelligence."""
    conn = get_connection()

    print("=" * 60)
    print("  TRAFFIC SIMULATION ORCHESTRATOR (PostGIS Spatial AI)")
    print("=" * 60)
    print()

    seed_vehicles(conn)

    agents = load_agents(conn)
    print(f"Loaded {len(agents)} vehicles:\n")
    for a in agents:
        dist = get_distance_to_target(conn, a["current_node"], a["target_node"])
        dist_str = f"{dist:.0f}m" if dist else "?"
        print(f"  - {a['name']} at node {a['current_node']} -> target {a['target_node']} ({dist_str} away)")

    # Compute initial shortest-path routes for all vehicles
    print("\nComputing shortest-path routes (pgRouting Dijkstra)...")
    for a in agents:
        route = compute_route(conn, a)
        hops = len(route) if route else 0
        print(f"  - {a['name']}: {hops} hops planned")
    print()

    for tick in range(1, MAX_TICKS + 1):
        print(f"--- Tick {tick:03d} {'-' * 44}")

        # Reload agents to get updated positions
        agents = load_agents(conn)

        step_agents(conn, agents, tick)

        print()
        time.sleep(TICK_INTERVAL)

    # Final summary
    print("=" * 60)
    print("  SIMULATION COMPLETE")
    print("=" * 60)
    agents = load_agents(conn)
    for a in agents:
        dist = get_distance_to_target(conn, a["current_node"], a["target_node"])
        dist_str = f" ({dist:.0f}m remaining)" if dist and a["status"] != "arrived" else ""
        marker = "[OK]" if a["status"] == "arrived" else "[..]"
        print(f"  {marker} {a['name']}: node {a['current_node']} [{a['status']}]{dist_str}")

    conn.close()


if __name__ == "__main__":
    run_simulation()
