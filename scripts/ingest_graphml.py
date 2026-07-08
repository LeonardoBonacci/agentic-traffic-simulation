"""
Ingest Wellington CBD street network from GraphML into PostGIS.

Reads the OSMnx-generated GraphML file, parses nodes (intersections) and
edges (road segments), and inserts them into the traffic_sim database.

Usage:
    pip install -r requirements.txt
    docker compose up -d
    python scripts/ingest_graphml.py
"""

import xml.etree.ElementTree as ET
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values
from shapely.geometry import Point, LineString
from shapely import wkt

# ─── Configuration ───────────────────────────────────────────────────────────

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "traffic_sim",
    "user": "traffic",
    "password": "traffic123",
}

GRAPHML_PATH = Path(__file__).resolve().parent.parent / "data" / "wellington_cbd.graphml"

# GraphML namespace
NS = {"gml": "http://graphml.graphdrawing.org/xmlns"}

# Key ID → semantic meaning (from the <key> declarations in the file)
NODE_KEYS = {
    "d4": "y",
    "d5": "x",
    "d6": "street_count",
    "d7": "highway",
    "d8": "junction",
}

EDGE_KEYS = {
    "d9": "osmid",
    "d10": "highway",
    "d11": "maxspeed",
    "d12": "name",
    "d13": "oneway",
    "d14": "reversed",
    "d15": "length",
    "d16": "speed_kph",
    "d17": "travel_time",
    "d18": "geometry",
    "d19": "lanes",
    "d20": "ref",
    "d21": "access",
    "d22": "bridge",
    "d23": "width",
    "d24": "tunnel",
    "d25": "junction",
}


def parse_graphml(filepath: Path):
    """Parse the GraphML file and return lists of node/edge dicts."""
    tree = ET.parse(filepath)
    root = tree.getroot()
    graph = root.find("gml:graph", NS)

    nodes = []
    for node_el in graph.findall("gml:node", NS):
        node_id = int(node_el.get("id"))
        attrs = {}
        for data_el in node_el.findall("gml:data", NS):
            key = data_el.get("key")
            if key in NODE_KEYS:
                attrs[NODE_KEYS[key]] = data_el.text
        nodes.append({"node_id": node_id, **attrs})

    edges = []
    for edge_el in graph.findall("gml:edge", NS):
        source = int(edge_el.get("source"))
        target = int(edge_el.get("target"))
        attrs = {"source": source, "target": target}
        for data_el in edge_el.findall("gml:data", NS):
            key = data_el.get("key")
            if key in EDGE_KEYS:
                attrs[EDGE_KEYS[key]] = data_el.text
        edges.append(attrs)

    return nodes, edges


def to_bool(val: str | None) -> bool:
    """Convert string boolean to Python bool."""
    if val is None:
        return False
    return val.strip().lower() == "true"


def to_float(val: str | None) -> float | None:
    """Convert string to float, returning None on failure."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def to_int(val: str | None) -> int | None:
    """Convert string to int, returning None on failure."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def build_edge_geom(edge: dict, node_lookup: dict) -> str | None:
    """
    Build WKT geometry for an edge.
    If the edge has an explicit geometry field, use it.
    Otherwise, create a straight line between source and target nodes.
    """
    if "geometry" in edge and edge["geometry"]:
        # Already a WKT LINESTRING
        return edge["geometry"]

    # Fall back to straight line between source/target
    src = node_lookup.get(edge["source"])
    tgt = node_lookup.get(edge["target"])
    if src and tgt:
        line = LineString([(float(src["x"]), float(src["y"])),
                           (float(tgt["x"]), float(tgt["y"]))])
        return line.wkt
    return None


def insert_nodes(conn, nodes: list[dict]):
    """Insert all nodes into the database."""
    sql = """
        INSERT INTO nodes (node_id, osm_highway, osm_junction, street_count, geom)
        VALUES %s
        ON CONFLICT (node_id) DO NOTHING
    """
    rows = []
    for n in nodes:
        x = float(n["x"])
        y = float(n["y"])
        point_wkt = f"SRID=4326;POINT({x} {y})"
        rows.append((
            n["node_id"],
            n.get("highway"),
            n.get("junction"),
            to_int(n.get("street_count")),
            point_wkt,
        ))

    with conn.cursor() as cur:
        execute_values(
            cur, sql, rows,
            template="(%s, %s, %s, %s, ST_GeomFromEWKT(%s))",
            page_size=100,
        )
    conn.commit()
    print(f"  Inserted {len(rows)} nodes.")


def insert_edges(conn, edges: list[dict], node_lookup: dict):
    """Insert all edges into the database."""
    sql = """
        INSERT INTO edges (
            source_node, target_node, osm_id, highway, name,
            oneway, reversed, length_m, speed_kph, travel_time_s,
            maxspeed, lanes, ref, access, bridge, width, tunnel, junction, geom
        ) VALUES %s
    """
    rows = []
    for e in edges:
        geom_wkt = build_edge_geom(e, node_lookup)
        geom_ewkt = f"SRID=4326;{geom_wkt}" if geom_wkt else None

        rows.append((
            e["source"],
            e["target"],
            e.get("osmid"),
            e.get("highway"),
            e.get("name"),
            to_bool(e.get("oneway")),
            to_bool(e.get("reversed")),
            to_float(e.get("length")),
            to_float(e.get("speed_kph")),
            to_float(e.get("travel_time")),
            e.get("maxspeed"),
            e.get("lanes"),
            e.get("ref"),
            e.get("access"),
            e.get("bridge"),
            e.get("width"),
            e.get("tunnel"),
            e.get("junction"),
            geom_ewkt,
        ))

    with conn.cursor() as cur:
        execute_values(
            cur, sql, rows,
            template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, ST_GeomFromEWKT(%s))",
            page_size=100,
        )
    conn.commit()
    print(f"  Inserted {len(rows)} edges.")


def main():
    print(f"Parsing {GRAPHML_PATH} ...")
    nodes, edges = parse_graphml(GRAPHML_PATH)
    print(f"  Found {len(nodes)} nodes, {len(edges)} edges.")

    # Build lookup for fallback geometry construction
    node_lookup = {n["node_id"]: n for n in nodes}

    print("Connecting to PostGIS ...")
    conn = psycopg2.connect(**DB_CONFIG)

    try:
        print("Inserting nodes ...")
        insert_nodes(conn, nodes)

        print("Inserting edges ...")
        insert_edges(conn, edges, node_lookup)

        # Verify
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM nodes")
            n_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM edges")
            e_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM edges WHERE geom IS NOT NULL")
            geom_count = cur.fetchone()[0]

        print(f"\nDone! Database contains:")
        print(f"  {n_count} nodes (intersections)")
        print(f"  {e_count} edges (road segments)")
        print(f"  {geom_count} edges with geometry")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
