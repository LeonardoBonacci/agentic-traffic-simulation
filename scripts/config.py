"""Shared configuration constants for the traffic simulation."""

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "traffic_sim",
    "user": "traffic",
    "password": "traffic123",
}

TICK_INTERVAL = 0.5   # seconds between simulation steps
MAX_TICKS = 10        # run for this many steps then stop
MAX_TOOL_CALLS = 6    # cap tool calls per agent per tick
OLLAMA_TIMEOUT = 120  # seconds — Ollama queues requests serially

OLLAMA_MODEL = "qwen3:8b"
OLLAMA_URL = "http://localhost:11434/api/chat"
