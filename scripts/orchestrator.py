"""
Traffic Simulation Orchestrator — Autonomous Vehicle Agents

Each vehicle is a fully autonomous LLM agent with direct database access.
Per tick, the agent runs a ReAct tool-calling loop via Ollama:
  - It can query PostGIS/pgRouting however it likes (read-only SQL)
  - It decides when it has enough information
  - It commits a move by calling move_to(node_id)

The orchestrator is a thin tick-clock + conflict resolver.

Usage:
    python3 scripts/orchestrator.py
"""

import json
import random
import select
import threading
import time

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

TICK_INTERVAL = 0.5   # seconds between simulation steps
MAX_TICKS = 20        # run for this many steps then stop
MAX_TOOL_CALLS = 6    # cap tool calls per agent per tick
OLLAMA_TIMEOUT = 120  # seconds — Ollama queues requests serially

OLLAMA_MODEL = "qwen3:8b"
OLLAMA_URL = "http://localhost:11434/api/chat"

# ─── Tool definitions (Ollama function-calling schema) ───────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_roads_from_here",
            "description": "Get all roads leaving from your current intersection. Returns road name, length in meters, speed limit, and the node_id at the other end.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_congestion",
            "description": "Check how many vehicles are currently on or near a specific road (edge_id). Returns a count.",
            "parameters": {
                "type": "object",
                "properties": {
                    "edge_id": {"type": "integer", "description": "The edge_id of the road to check."},
                },
                "required": ["edge_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_nearby_vehicles",
            "description": "Get vehicles near your current position within a radius. Returns their names, distances, and statuses.",
            "parameters": {
                "type": "object",
                "properties": {
                    "radius_m": {"type": "number", "description": "Search radius in meters (default 200)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_distance_to_destination",
            "description": "Get the straight-line distance in meters from a given node to your destination.",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_node": {"type": "integer", "description": "Node to measure from. Omit to use your current node."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compute_shortest_route",
            "description": "Compute the shortest path (Dijkstra) from your current node to your destination. Returns a list of node_ids and total distance in meters.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_db",
            "description": "Execute a read-only SQL query against the traffic database. Tables: nodes(node_id, geom), edges(edge_id, source_node, target_node, name, highway, length_m, speed_kph, oneway, geom), agents(agent_id, name, current_node, target_node, speed_kmh, status, geom). PostGIS and pgRouting functions available. MAX 5 rows returned.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "A SELECT query to run."},
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_to",
            "description": "Move your vehicle to an adjacent node. This ends your turn. The node MUST be directly connected to your current node by a road.",
            "parameters": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "integer", "description": "The node_id to move to (must be adjacent)."},
                },
                "required": ["node_id"],
            },
        },
    },
]


# ─── Database helpers ────────────────────────────────────────────────────────

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
        cur.execute("""
            INSERT INTO agent_trails (agent_id, tick, node_id, geom)
            VALUES (%s, %s, %s, (SELECT geom FROM nodes WHERE node_id = %s))
        """, (agent_id, tick, node_id, node_id))
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


# ─── Pub/Sub: listen for vehicle movement broadcasts via PG LISTEN ───────────

class VehicleRadio:
    """
    Background listener for PG NOTIFY on the 'vehicle_moves' channel.
    Collects broadcasts; the orchestrator drains and prints them each tick.
    """

    def __init__(self):
        self._messages = []
        self._lock = threading.Lock()
        self._conn = None
        self._running = False
        self._thread = None

    def start(self):
        self._conn = psycopg2.connect(**DB_CONFIG)
        self._conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        with self._conn.cursor() as cur:
            cur.execute("LISTEN vehicle_moves")
        self._running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()

    def _listen_loop(self):
        while self._running:
            if select.select([self._conn], [], [], 0.2) != ([], [], []):
                self._conn.poll()
                while self._conn.notifies:
                    notify = self._conn.notifies.pop(0)
                    try:
                        data = json.loads(notify.payload)
                    except (json.JSONDecodeError, TypeError):
                        data = {"raw": notify.payload}
                    with self._lock:
                        self._messages.append(data)

    def drain(self):
        """Return and clear all accumulated messages."""
        with self._lock:
            msgs = self._messages[:]
            self._messages.clear()
        return msgs

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)
        if self._conn:
            self._conn.close()


# ─── Tool execution ──────────────────────────────────────────────────────────

def execute_tool(conn, agent, tool_name, args):
    """
    Execute a tool call on behalf of a vehicle agent.
    Returns (result_string, is_move, move_node_id).
    """
    current_node = agent["current_node"]
    target_node = agent["target_node"]

    if tool_name == "get_roads_from_here":
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT edge_id, target_node, name, highway, length_m, speed_kph
                FROM edges WHERE source_node = %s
                UNION ALL
                SELECT edge_id, source_node AS target_node, name, highway, length_m, speed_kph
                FROM edges WHERE target_node = %s AND oneway = FALSE
            """, (current_node, current_node))
            rows = cur.fetchall()
        if not rows:
            return "No roads leaving your current intersection.", False, None
        lines = []
        for r in rows:
            name = r["name"] or r["highway"] or "unnamed"
            lines.append(f"- edge_id={r['edge_id']}, to node {r['target_node']}, "
                        f"\"{name}\", {r['length_m']:.0f}m, {r['speed_kph']} km/h")
        return "\n".join(lines), False, None

    elif tool_name == "check_congestion":
        edge_id = args.get("edge_id")
        if edge_id is None:
            return "Error: edge_id required.", False, None
        with conn.cursor() as cur:
            cur.execute("SELECT agents_on_edge(%s)", (int(edge_id),))
            count = cur.fetchone()[0]
        return f"{count} vehicle(s) on/near edge {edge_id}.", False, None

    elif tool_name == "get_nearby_vehicles":
        radius = args.get("radius_m", 200)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT a.name, a.status, a.speed_kmh,
                       ST_Distance(a.geom::geography, n.geom::geography)::int AS distance_m
                FROM agents a, nodes n
                WHERE n.node_id = %s AND a.geom IS NOT NULL
                  AND a.agent_id != %s
                  AND ST_DWithin(a.geom::geography, n.geom::geography, %s)
                ORDER BY distance_m LIMIT 5
            """, (current_node, agent["agent_id"], float(radius)))
            rows = cur.fetchall()
        if not rows:
            return "No other vehicles nearby.", False, None
        lines = [f"- {r['name']}: {r['distance_m']}m away, {r['status']}, {r['speed_kmh']} km/h"
                 for r in rows]
        return "\n".join(lines), False, None

    elif tool_name == "get_distance_to_destination":
        from_node = args.get("from_node", current_node)
        with conn.cursor() as cur:
            cur.execute("SELECT node_distance_m(%s, %s)", (int(from_node), target_node))
            dist = cur.fetchone()[0]
        if dist is None:
            return "Could not compute distance.", False, None
        return f"{dist:.0f} meters from node {from_node} to your destination.", False, None

    elif tool_name == "compute_shortest_route":
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                cur.execute("""
                    SELECT node AS node_id, cost FROM pgr_dijkstra(
                        'SELECT edge_id AS id, source_node AS source, target_node AS target,
                         length_m AS cost, CASE WHEN oneway THEN -1 ELSE length_m END AS reverse_cost
                         FROM edges',
                        %s, %s, directed := true
                    ) ORDER BY seq
                """, (current_node, target_node))
                rows = cur.fetchall()
            except Exception:
                conn.rollback()
                return "No route found to destination.", False, None
        if not rows:
            return "No route found to destination.", False, None
        node_ids = [r["node_id"] for r in rows if r["node_id"] != -1]
        total_cost = sum(r["cost"] for r in rows if r["cost"] > 0)
        return (f"Shortest route ({total_cost:.0f}m, {len(node_ids)} hops): "
                f"{' -> '.join(str(n) for n in node_ids[:10])}"
                f"{'...' if len(node_ids) > 10 else ''}"), False, None

    elif tool_name == "query_db":
        sql = args.get("sql", "").strip()
        if not sql:
            return "Error: sql parameter required.", False, None
        # Security: only allow SELECT/WITH
        sql_upper = sql.upper().lstrip()
        if not (sql_upper.startswith("SELECT") or sql_upper.startswith("WITH")):
            return "Error: only SELECT queries allowed.", False, None
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                cur.execute(sql + " LIMIT 5" if "LIMIT" not in sql.upper() else sql)
                rows = cur.fetchall()
            except Exception as e:
                conn.rollback()
                return f"SQL error: {e}", False, None
        if not rows:
            return "Query returned no results.", False, None
        return json.dumps([dict(r) for r in rows], default=str), False, None

    elif tool_name == "move_to":
        node_id = args.get("node_id")
        if node_id is None:
            return "Error: node_id required.", False, None
        node_id = int(node_id)
        # Validate adjacency
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM edges
                WHERE (source_node = %s AND target_node = %s)
                   OR (target_node = %s AND source_node = %s AND oneway = FALSE)
                LIMIT 1
            """, (current_node, node_id, current_node, node_id))
            if not cur.fetchone():
                return (f"Error: node {node_id} is not adjacent to your current node "
                        f"{current_node}. Use get_roads_from_here to see valid moves."), False, None
        return f"Moving to node {node_id}.", True, node_id

    return f"Unknown tool: {tool_name}", False, None


# ─── Vehicle Agent (LLM tool-calling loop) ───────────────────────────────────

def build_system_prompt(agent):
    """Build the system prompt that gives the vehicle its identity and goals."""
    return (
        f"You are {agent['name']}, an autonomous vehicle navigating Wellington CBD.\n"
        f"Your current position: node {agent['current_node']}\n"
        f"Your destination: node {agent['target_node']}\n"
        f"Your speed: {agent['speed_kmh']} km/h\n\n"
        "You have tools to query the road network database (PostGIS + pgRouting).\n"
        "Use them to understand your surroundings, check for congestion, and plan your route.\n"
        "When you've decided, call move_to(node_id) to move to an adjacent intersection.\n\n"
        "Strategy: reach your destination efficiently while avoiding congested roads.\n"
        "You MUST call move_to exactly once to end your turn. Be decisive — don't over-analyze."
    )


def run_vehicle_agent(agent, tick):
    """
    Run one tick of an autonomous vehicle agent.
    Returns (chosen_node_id, reasoning_log) or (None, error_msg).
    """
    conn = get_connection()
    reasoning_log = []
    move_node = None

    try:
        messages = [
            {"role": "system", "content": build_system_prompt(agent)},
            {"role": "user", "content": f"Tick {tick}. Decide your next move."},
        ]

        for step in range(MAX_TOOL_CALLS):
            # Call Ollama with tools
            resp = requests.post(OLLAMA_URL, json={
                "model": OLLAMA_MODEL,
                "messages": messages,
                "tools": TOOLS,
                "stream": False,
                "think": False,
                "options": {"temperature": 0.3, "num_predict": 256},
            }, timeout=OLLAMA_TIMEOUT)
            resp.raise_for_status()
            result = resp.json()

            msg = result["message"]
            messages.append(msg)

            # If no tool calls, the LLM is done (maybe gave a text response)
            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                reasoning_log.append(f"[text] {msg.get('content', '')[:100]}")
                break

            # Execute each tool call
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                fn_args = tc["function"].get("arguments", {})
                reasoning_log.append(f"[call] {fn_name}({json.dumps(fn_args)})")

                result_str, is_move, node_id = execute_tool(conn, agent, fn_name, fn_args)
                reasoning_log.append(f"[result] {result_str[:120]}")

                # Append tool result back to conversation
                messages.append({
                    "role": "tool",
                    "content": result_str,
                })

                if is_move:
                    move_node = node_id
                    break

            if move_node is not None:
                break

    except Exception as e:
        reasoning_log.append(f"[error] {e}")

    # Fallback: if LLM never called move_to, pick first available road
    if move_node is None:
        reasoning_log.append("[fallback] picking first available road")
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT target_node FROM edges WHERE source_node = %s LIMIT 1
                """, (agent["current_node"],))
                row = cur.fetchone()
                if row:
                    move_node = row[0]
        except Exception:
            pass

    conn.close()
    return move_node, reasoning_log


# ─── Orchestrator (thin tick-loop) ───────────────────────────────────────────

INITIAL_VEHICLES = [(f"car_{i:02d}", random.uniform(30.0, 60.0)) for i in range(1, 6)]


def seed_vehicles(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agents WHERE agent_type = 'vehicle'")
        if cur.fetchone()[0] > 0:
            return
        for name, speed in INITIAL_VEHICLES:
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
    print(f"Seeded {len(INITIAL_VEHICLES)} vehicles.\n")


def resolve_conflicts(planned_moves):
    """
    If multiple vehicles want the same node, only the first one gets it.
    Others are blocked this tick. (Simple conflict resolution.)
    """
    targets_seen = {}
    blocked = set()
    for agent_id, node_id in planned_moves.items():
        if node_id is None:
            continue
        if node_id in targets_seen:
            blocked.add(agent_id)  # second vehicle loses
        else:
            targets_seen[node_id] = agent_id
    return blocked


def run_simulation():
    """Main orchestrator: thin tick-loop with parallel autonomous agents."""
    conn = get_connection()

    # Start the pub/sub radio listener
    radio = VehicleRadio()
    radio.start()
    print("📻 Vehicle radio listening on channel 'vehicle_moves'...")
    print()

    print("=" * 60)
    print("  AUTONOMOUS VEHICLE SIMULATION (Tool-Calling Agents)")
    print("=" * 60)
    print()

    seed_vehicles(conn)
    agents = load_agents(conn)
    print(f"Loaded {len(agents)} autonomous vehicles:\n")
    for a in agents:
        with conn.cursor() as cur:
            cur.execute("SELECT node_distance_m(%s, %s)", (a["current_node"], a["target_node"]))
            dist = cur.fetchone()[0]
        dist_str = f"{dist:.0f}m" if dist else "?"
        print(f"  - {a['name']}: node {a['current_node']} -> {a['target_node']} ({dist_str})")
    print()

    for tick in range(1, MAX_TICKS + 1):
        print(f"{'─' * 60}")
        print(f"  TICK {tick:03d}")
        print(f"{'─' * 60}")

        agents = load_agents(conn)
        active = [a for a in agents if a["status"] != "arrived"]

        if not active:
            print("  All vehicles arrived!")
            break

        # Run agents sequentially (Ollama serves one request at a time)
        planned_moves = {}
        agent_logs = {}

        for agent in active:
            node_id, log = run_vehicle_agent(agent, tick)
            planned_moves[agent["agent_id"]] = node_id
            agent_logs[agent["agent_id"]] = (agent, log)

        # Resolve conflicts (two vehicles targeting same node)
        blocked = resolve_conflicts(planned_moves)

        # Execute moves
        for agent_id, node_id in planned_moves.items():
            agent, log = agent_logs[agent_id]
            name = agent["name"]

            if node_id is None:
                print(f"  [{name}] STUCK — no valid move found")
                for entry in log:
                    print(f"    {entry}")
                continue

            if agent_id in blocked:
                print(f"  [{name}] BLOCKED — another vehicle claimed node {node_id}")
                continue

            status = "arrived" if node_id == agent["target_node"] else "moving"
            update_agent_position(conn, agent_id, node_id, status)
            record_trail(conn, agent_id, tick, node_id)

            # Print reasoning summary
            tool_calls = [e for e in log if e.startswith("[call]")]
            summary = ", ".join(t.replace("[call] ", "") for t in tool_calls[:3])
            print(f"  [{name}] -> node {node_id} [{status}]")
            print(f"    reasoning: {summary or 'direct move'}")

        # Drain and print radio broadcasts received this tick
        time.sleep(0.1)  # small pause to let notifications arrive
        broadcasts = radio.drain()
        if broadcasts:
            print(f"  {'·' * 40}")
            print(f"  📻 Radio chatter this tick:")
            for msg in broadcasts:
                print(f"     {msg['vehicle']} entered \"{msg['road']}\" "
                      f"(node {msg['from']} → {msg['to']})")

        print()
        time.sleep(TICK_INTERVAL)

    # Final summary
    print("=" * 60)
    print("  SIMULATION COMPLETE")
    print("=" * 60)
    agents = load_agents(conn)
    for a in agents:
        with conn.cursor() as cur:
            cur.execute("SELECT node_distance_m(%s, %s)", (a["current_node"], a["target_node"]))
            dist = cur.fetchone()[0]
        dist_str = f" ({dist:.0f}m remaining)" if dist and a["status"] != "arrived" else ""
        marker = "OK" if a["status"] == "arrived" else ".."
        print(f"  [{marker}] {a['name']}: node {a['current_node']} [{a['status']}]{dist_str}")

    radio.stop()
    conn.close()


if __name__ == "__main__":
    run_simulation()
