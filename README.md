# agentic-traffic-simulation

An agentic traffic simulation built on a real street network (Wellington CBD, New Zealand). Autonomous vehicle agents navigate a PostGIS-stored road graph using **spatial intelligence** — pgRouting shortest-path planning, real-time congestion detection via `ST_DWithin`, and dynamic re-routing. A local LLM (Ollama qwen3:8b) receives full geospatial context from PostGIS to make informed routing decisions.

The street network is static — loaded once from OpenStreetMap data. Agents are dynamic — each tick they perceive their spatial environment (distance to target, congestion ahead, nearby vehicles) and make intelligent routing choices.

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

# 5. Run the simulation
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
┌────────────────────────────────────────────────────────────────┐
│                    Orchestrator (Python)                        │
├────────────────────────────────────────────────────────────────┤
│  Per tick, per vehicle:                                        │
│  1. Query PostGIS: enriched_outgoing_edges(node, target)       │
│     → distance-to-target, edge bearing, congestion count       │
│  2. Check planned route from pgRouting Dijkstra cache          │
│  3. Detect congestion ahead (ST_DWithin lookahead)             │
│  4. If congested → re-compute route via pgr_dijkstra           │
│  5. Present spatial context to LLM for final decision          │
│  6. Resolve give-way-to-left at contested intersections        │
│  7. Execute move, record trail                                 │
└────────────────────────────────────────────────────────────────┘
         │                              │
         ▼                              ▼
┌─────────────────────┐     ┌──────────────────────┐
│  PostGIS + pgRouting │     │  Ollama (qwen3:8b)   │
│  ─────────────────── │     │  ──────────────────── │
│  • ST_Distance       │     │  Receives per-road:  │
│  • ST_DWithin        │     │  • distance progress │
│  • ST_Azimuth        │     │  • congestion count  │
│  • pgr_dijkstra      │     │  • bearing to target │
│  • enriched_edges()  │     │  • nearby vehicles   │
│  • agents_near_node()│     │                      │
│  • agents_on_edge()  │     │                      │
└─────────────────────┘     └──────────────────────┘
```

## PostGIS Spatial Intelligence

| Function | Purpose |
|----------|---------|
| `enriched_outgoing_edges(node, target)` | Returns candidate roads with distance-to-target, bearing, congestion count |
| `shortest_path_edges(start, end)` | pgRouting Dijkstra — optimal route as edge sequence |
| `shortest_path_cost(start, end)` | Total route distance in meters |
| `agents_near_node(node, radius_m)` | Count vehicles within radius using `ST_DWithin` (geography) |
| `agents_on_edge(edge_id, buffer_m)` | Count vehicles on/near an edge corridor |
| `node_distance_m(a, b)` | Straight-line distance between two nodes (meters) |
| `node_bearing(a, b)` | Azimuth bearing from node A to B |

## Agent Decision Pipeline

1. **Route planning** — At spawn, each vehicle computes a full shortest-path via `pgr_dijkstra`
2. **Spatial perception** — Each tick, the agent queries its enriched outgoing edges (PostGIS computes distance-to-target, congestion, bearing for each option)
3. **Congestion detection** — If vehicles are clustered on upcoming route segments (`ST_DWithin` lookahead), trigger re-routing
4. **LLM decision** — The model sees: distance progress per road, congestion warnings, bearing to target, nearby vehicles
5. **Conflict resolution** — Give-way-to-left rule at contested intersections using `ST_Azimuth` bearings
6. **Fallback** — If LLM fails, spatial scoring selects the road with best progress-to-target minus congestion penalty

## Data Model

| Table | Purpose |
|-------|---------|
| `nodes` | Intersections / junctions (273 nodes) |
| `edges` | Road segments with geometry, speed, length (539 edges) |
| `agents` | Vehicles with current_node, target_node, status |
| `agent_trails` | Historical position log (tick, node, geom) for replay/analytics |

Vehicles are seeded with `ST_Distance > 500m` between start and target to ensure meaningful routes.