# agentic-traffic-simulation

An agentic traffic simulation where each vehicle is a **fully autonomous LLM agent** with direct database access. Vehicles navigate a real street network (Wellington CBD, New Zealand) stored in PostGIS, using Ollama's native tool-calling to query roads, check congestion, compute routes, and decide their next move — all without human-written navigation logic.

Each tick, every vehicle runs a ReAct-style tool-calling loop: the LLM decides what to investigate (available roads, congestion levels, shortest path), reasons about the results, and commits a move. The orchestrator is a thin tick-clock that only resolves physical conflicts (two vehicles targeting the same intersection).

## Setup

```bash
# 1. Start PostGIS+pgRouting + Dekart map viewer
docker compose up -d

# 2. Install Python deps
pip3 install -r requirements.txt

# 3. Ensure Ollama is running with qwen3:8b
ollama pull qwen3:8b

# 4. Load the street network into the database
python3 scripts/ingest_graphml.py

# 5. Run the simulation (5 autonomous vehicles, 20 ticks)
python3 scripts/orchestrator.py

# 6. Open Dekart to view the map
open http://localhost:8080
```

## Monitoring (Dekart)

[Dekart](https://github.com/dekart-xyz/dekart) provides a Kepler.gl map UI connected directly to the PostGIS database. Open **http://localhost:8080**, create a new report, and paste queries from `sql/dekart-queries.sql` to visualize:

- **Street network** — all road segments colored by speed or type
- **Live agents** — vehicle positions updated each simulation tick
- **Intersections** — node layer showing junctions and traffic signals
- **Congestion heatmap** — edges colored by vehicle density (via `agents_on_edge()`)
- **Agent trails** — animated path history showing where vehicles have been
- **Detection radii** — 200m awareness buffers around each vehicle

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Orchestrator (thin tick-loop)                              │
│  • Manages tick clock                                       │
│  • Spawns agent invocations sequentially                    │
│  • Resolves physical conflicts (two vehicles → same node)   │
│  • Records trails, updates positions                        │
└──────────────────────────┬──────────────────────────────────┘
                           │ per vehicle, per tick
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Vehicle Agent (LLM + tools)                                │
│                                                             │
│  System prompt: "You are car_03, navigating Wellington CBD. │
│  Your destination is node X. Use tools to understand your   │
│  surroundings and decide your next move."                   │
│                                                             │
│  Tools available:                                           │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ get_roads_from_here()    — adjacent roads + metadata  │  │
│  │ check_congestion(edge)   — vehicles on a road         │  │
│  │ get_nearby_vehicles(r)   — situational awareness      │  │
│  │ get_distance_to_destination(node) — progress check    │  │
│  │ compute_shortest_route() — pgRouting Dijkstra         │  │
│  │ query_db(sql)            — raw PostGIS/SQL access     │  │
│  │ move_to(node_id)         — commit move (ends turn)    │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  The LLM reasons freely and calls tools in any order.       │
│  It MUST call move_to() exactly once to end its turn.       │
└─────────────────────────────────────────────────────────────┘
         │                              │
         ▼                              ▼
┌─────────────────────┐     ┌──────────────────────┐
│  PostGIS + pgRouting │     │  Ollama (qwen3:8b)   │
│  ─────────────────── │     │  ──────────────────── │
│  • ST_Distance       │     │  Native tool-calling │
│  • ST_DWithin        │     │  (function calling)  │
│  • ST_Azimuth        │     │                      │
│  • pgr_dijkstra      │     │  Agent decides:      │
│  • enriched_edges()  │     │  • what to query     │
│  • agents_near_node()│     │  • when to move      │
│  • agents_on_edge()  │     │  • which road to take│
└─────────────────────┘     └──────────────────────┘
```

## Agent Tools

| Tool | Purpose |
|------|---------|
| `get_roads_from_here()` | List all roads leaving current intersection (name, length, speed, target node) |
| `check_congestion(edge_id)` | Count vehicles on/near a specific road segment |
| `get_nearby_vehicles(radius_m)` | Situational awareness — names, distances, statuses of nearby vehicles |
| `get_distance_to_destination(from_node)` | Straight-line distance to destination from any node |
| `compute_shortest_route()` | Full Dijkstra shortest path from current position to destination |
| `query_db(sql)` | Arbitrary read-only SQL against the spatial database (PostGIS + pgRouting) |
| `move_to(node_id)` | Commit a move to an adjacent node — ends the agent's turn |

## How It Works

1. **Agent spawns** — assigned a random start node and a destination >500m away
2. **Each tick** — the LLM receives its identity (name, position, destination, speed) and must call tools to gather info and then `move_to()` to commit a decision
3. **Autonomous reasoning** — the agent decides what to query; it might check congestion, compute a route, or run a custom spatial query via `query_db()`
4. **Conflict resolution** — if two vehicles target the same node, only the first one proceeds
5. **Fallback** — if the LLM fails or times out, the agent picks the first available road
6. **Position updates** — after each move, the agent's position is persisted to PostGIS (`agents` table, `agent_trails` table)

## PostGIS Functions

| Function | Purpose |
|----------|---------|
| `enriched_outgoing_edges(node, target)` | Candidate roads with distance-to-target, bearing, congestion |
| `shortest_path_edges(start, end)` | pgRouting Dijkstra — optimal route as edge sequence |
| `shortest_path_cost(start, end)` | Total route distance in meters |
| `agents_near_node(node, radius_m)` | Count vehicles within radius (`ST_DWithin` geography) |
| `agents_on_edge(edge_id, buffer_m)` | Count vehicles on/near an edge corridor |
| `node_distance_m(a, b)` | Straight-line distance between two nodes (meters) |
| `node_bearing(a, b)` | Azimuth bearing from node A to B |

## Data Model

| Table | Purpose |
|-------|---------|
| `nodes` | Intersections / junctions with PostGIS Point geometry |
| `edges` | Road segments with LineString geometry, speed, length, one-way info |
| `agents` | Vehicles with current_node, target_node, geom, status (updated each tick) |
| `agent_trails` | Historical position log (tick, node, geom) for replay/analytics |

Vehicles are seeded with `ST_Distance > 500m` between start and target to ensure meaningful routes.