# agentic-traffic-simulation

An agentic traffic simulation built on a real street network (Wellington CBD, New Zealand). Autonomous agents navigate a PostGIS-stored road graph, making independent decisions about routing and movement. Built with PostgreSQL/PostGIS, LangChain, and Dekart for visualization.

The street network is static — loaded once from OpenStreetMap data. Agents are dynamic — they update their own position along edges of the graph as the simulation runs.

## Setup

```bash
# 1. Start PostGIS
docker compose up -d

# 2. Install Python deps
pip3 install -r requirements.txt

# 3. Load the street network into the database
python3 scripts/ingest_graphml.py

# 4. Run the simulation
python3 scripts/orchestrator.py
```

## Architecture

- **Database:** PostGIS with `nodes` (intersections), `edges` (road segments), and `agents` (vehicles)
- **Ingestion:** `scripts/ingest_graphml.py` parses OSMnx GraphML and loads the road network
- **Orchestrator:** `scripts/orchestrator.py` runs a tick-based simulation loop with:
  - **Weighted random turns** — vehicles prefer going straight (weight 5) over slight turns (3.5), moderate turns (2), sharp turns (1), and U-turns (0.2)
  - **Give way to left** — when multiple vehicles target the same intersection, approach bearings are computed from PostGIS coordinates; a vehicle yields if another is approaching from its left

## Data Model

| Table | Purpose |
|-------|---------|
| `nodes` | Intersections / junctions (273 nodes) |
| `edges` | Road segments with geometry, speed, length (539 edges) |
| `agents` | Vehicles with current_node, target_node, status |

Vehicles are seeded on first orchestrator run with random start/target positions.