"""Tool definitions and execution for vehicle agents."""

import json

from psycopg2.extras import RealDictCursor

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
        sql = args.get("sql", "").strip().rstrip(";")
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
