"""Vehicle agent: LLM tool-calling loop via Ollama."""

import json

import requests

from config import MAX_TOOL_CALLS, OLLAMA_MODEL, OLLAMA_TIMEOUT, OLLAMA_URL
from db import get_connection
from tools import TOOLS, execute_tool


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
