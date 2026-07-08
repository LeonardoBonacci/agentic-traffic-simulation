# agentic-traffic-simulation

## Setup

```bash
# 1. Start PostGIS
docker compose up -d

# 2. Install Python deps
pip3 install -r requirements.txt

# 3. Load the street network into the database
python3 scripts/ingest_graphml.py

# 4. Query the database
docker exec traffic-sim-db psql -U traffic -d traffic_sim -c "SELECT name, highway, length_m FROM edges LIMIT 5;"
```