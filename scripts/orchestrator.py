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

import random
import time

from config import MAX_TICKS, TICK_INTERVAL
from db import get_connection, load_agents, record_trail, update_agent_position
from radio import VehicleRadio
from agent import run_vehicle_agent

# ─── Orchestrator (thin tick-loop) ───────────────────────────────────────────

INITIAL_VEHICLES = [(f"car_{i:02d}", random.uniform(30.0, 60.0)) for i in range(1, 6)]


def seed_vehicles(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agents WHERE agent_type = 'vehicle'")
        if cur.fetchone()[0] > 0:
            return
        for name, speed in INITIAL_VEHICLES:
            cur.execute("""
                WITH reachable AS (
                    SELECT DISTINCT source_node AS node_id FROM edges
                ),
                start_node AS (
                    SELECT n.node_id, n.geom
                    FROM nodes n JOIN reachable r ON r.node_id = n.node_id
                    ORDER BY random() LIMIT 1
                ),
                end_node AS (
                    SELECT n.node_id
                    FROM nodes n JOIN reachable r ON r.node_id = n.node_id, start_node s
                    WHERE ST_Distance(n.geom::geography, s.geom::geography) > 300
                    ORDER BY random() LIMIT 1
                )
                INSERT INTO agents (name, agent_type, current_node, target_node, speed_kmh, geom)
                SELECT %s, 'vehicle', s.node_id, e.node_id, %s, s.geom
                FROM start_node s, end_node e
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
