"""Pub/Sub: listen for vehicle movement broadcasts via PG LISTEN/NOTIFY."""

import json
import select
import threading

import psycopg2

from config import DB_CONFIG


class VehicleRadio:
    """
    Background listener for PG NOTIFY on the 'vehicle_moves' channel.
    Collects broadcasts; the orchestrator drains and prints them each tick.
    """

    def __init__(self):
        self._messages = []
        self._lock = threading.Lock()
        self._conn = None
        self._running = False
        self._thread = None

    def start(self):
        self._conn = psycopg2.connect(**DB_CONFIG)
        self._conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        with self._conn.cursor() as cur:
            cur.execute("LISTEN vehicle_moves")
        self._running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()

    def _listen_loop(self):
        while self._running:
            if select.select([self._conn], [], [], 0.2) != ([], [], []):
                self._conn.poll()
                while self._conn.notifies:
                    notify = self._conn.notifies.pop(0)
                    try:
                        data = json.loads(notify.payload)
                    except (json.JSONDecodeError, TypeError):
                        data = {"raw": notify.payload}
                    with self._lock:
                        self._messages.append(data)

    def drain(self):
        """Return and clear all accumulated messages."""
        with self._lock:
            msgs = self._messages[:]
            self._messages.clear()
        return msgs

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)
        if self._conn:
            self._conn.close()
