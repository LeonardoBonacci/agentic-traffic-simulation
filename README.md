# agentic-traffic-simulation

An agentic traffic simulation built on a real street network (Wellington CBD, New Zealand). Autonomous vehicle agents navigate a PostGIS-stored road graph, making independent routing decisions powered by a local LLM (Ollama qwen3:8b). Built with PostgreSQL/PostGIS and Ollama.

The street network is static — loaded once from OpenStreetMap data. Agents are dynamic — each tick, the LLM decides which road each vehicle takes next.

## Setup

```bash
# 1. Start PostGIS + Dekart map viewer
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

## Architecture

- **Database:** PostGIS with `nodes` (intersections), `edges` (road segments), and `agents` (vehicles)
- **Ingestion:** `scripts/ingest_graphml.py` parses OSMnx GraphML and loads the road network
- **LLM:** Ollama qwen3:8b (local, think-mode disabled) — each vehicle asks the model which road to take given available options
- **Orchestrator:** `scripts/orchestrator.py` runs a tick-based simulation loop with:
  - **LLM-driven navigation** — the model picks which road each vehicle takes based on street name, length, and speed limit
  - **Weighted random fallback** — if the LLM fails, falls back to preferring straight (5x) over U-turns (0.2x)
  - **Give way to left** — when multiple vehicles target the same intersection, approach bearings are computed from PostGIS coordinates; a vehicle yields if another is approaching from its left

## Data Model

| Table | Purpose |
|-------|---------|
| `nodes` | Intersections / junctions (273 nodes) |
| `edges` | Road segments with geometry, speed, length (539 edges) |
| `agents` | Vehicles with current_node, target_node, status |

Vehicles are seeded on first orchestrator run with random start/target positions.