# agentic-traffic-simulation

**What if every car in your city had a brain the size of an 8-billion-parameter language model — and absolutely zero driving experience?**

Welcome to the world's most over-engineered traffic simulation: each vehicle is a fully autonomous LLM agent that talks to a spatial database, overthinks every intersection, and occasionally gets lost despite having access to Dijkstra's shortest path. It's like giving a philosophy PhD student a GPS and asking them to deliver pizza.

Vehicles navigate a real street network (Wellington CBD, New Zealand) stored in PostGIS, using Ollama's native tool-calling to query roads, check congestion, compute routes, and make decisions. There is zero human-written navigation logic — the LLM figures it out (or spectacularly doesn't).

Each tick, every vehicle runs a ReAct-style tool-calling loop: the LLM decides what to investigate, reasons about the results, and commits a move. The orchestrator is a thin tick-clock that only steps in when two vehicles try to occupy the same intersection (because even AI cars road-rage).

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

# 5. Run the simulation (5 autonomous vehicles, 10 ticks of chaos)
python3 scripts/orchestrator.py

# 6. Open Dekart to watch the mayhem unfold on a map
open http://localhost:8080
```

## Monitoring (Dekart)

[Dekart](https://github.com/dekart-xyz/dekart) provides a Kepler.gl map UI connected directly to PostGIS. Open **http://localhost:8080**, create a new report, and paste queries from `sql/dekart-queries.sql` to watch your AI drivers bumble around Wellington:

- **Street network** — all road segments colored by speed or type
- **Live agents** — vehicle positions updated each tick (refresh to see them move!)
- **Intersections** — node layer showing where decisions happen (and are regretted)
- **Congestion heatmap** — edges colored by vehicle density (spoiler: it's always the same road)
- **Agent trails** — animated breadcrumbs showing every wrong turn
- **Detection radii** — 200m awareness bubbles (they can see each other, they just don't care)

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

| Tool | What the AI thinks it does |
|------|---------------------------|
| `get_roads_from_here()` | "Let me see my options" (lists adjacent roads with name, length, speed) |
| `check_congestion(edge_id)` | "Is that road busy?" (counts vehicles on a road segment) |
| `get_nearby_vehicles(radius_m)` | "Who else is out here?" (situational awareness, rarely used wisely) |
| `get_distance_to_destination(from_node)` | "Am I getting closer?" (straight-line distance reality check) |
| `compute_shortest_route()` | "Just tell me the optimal path" (Dijkstra — then ignores it half the time) |
| `query_db(sql)` | "I know SQL, watch this" (raw read-only PostGIS queries, what could go wrong) |
| `move_to(node_id)` | "I choose this road" (commits the move, no takebacks) |

## How It Works

1. **Agent spawns** — dropped at a random intersection with a destination >500m away (good luck, buddy)
2. **Each tick** — the LLM receives its identity and must use tools to figure out where it is and where to go next
3. **Autonomous reasoning** — the agent decides what to query: check congestion? compute a route? run raw SQL like some kind of database cowboy? It's its call
4. **Conflict resolution** — if two vehicles target the same node, first come first served. The loser sits there for a tick contemplating existence
5. **Fallback** — if the LLM panics, hallucinates a non-existent road, or times out, the agent shrugs and picks the first available road
6. **Position updates** — after each move, everything is persisted to PostGIS for the map to pick up

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

---

*"We gave 8 billion parameters a steering wheel and access to a spatial database. What could possibly go wrong?"*