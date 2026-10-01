#!/usr/bin/env python3
"""
Codebase Memory Graph Viewer
=============================
Serves a D3.js force-directed graph visualization of all indexed projects'
codebase-memory graphs. Basic auth enabled. Listens on port 8888.

Usage:
    python3 graph_viewer.py

Then open http://<host>:8888 in a browser.
"""

import json
import sqlite3
import base64
import os
import hashlib
import mimetypes
import re
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────────────

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8888

# Basic auth credentials: username:password (bcrypt-like – we use SHA-256 hash)
# Default:  admin : admin  (CHANGE THIS!)
AUTH_USERNAME = os.environ.get("GRAPH_VIEWER_USER", "admin")
AUTH_PASSWORD_HASH = os.environ.get(
    "GRAPH_VIEWER_PASS_HASH",
    # SHA-256 of "admin" – replace with your own:  python3 -c "import hashlib; print(hashlib.sha256(b'YOURPASS').hexdigest())"
    "8c6976e5b5410415bde908bd4dee15dfb167a9c873fc4bb8a81f6f2ab448a918",
)

BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "dbs"
REPO_NAME = "memory_map"

# ── Helpers ──────────────────────────────────────────────────────────────────

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_auth(headers) -> bool:
    auth = headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        user, password = decoded.split(":", 1)
        pwd_hash = sha256_hex(password.encode("utf-8"))
        return user == AUTH_USERNAME and pwd_hash == AUTH_PASSWORD_HASH
    except Exception:
        return False


def require_auth(handler):
    """Return 401 if auth fails; otherwise proceed."""
    if not check_auth(handler.headers):
        handler.send_response(401)
        handler.send_header("WWW-Authenticate", 'Basic realm="Codebase Memory Graph Viewer"')
        handler.send_header("Content-Type", "text/html")
        handler.end_headers()
        handler.wfile.write(
            b"<html><body><h1>401 Unauthorized</h1>"
            b"<p>Please authenticate to access this graph viewer.</p></body></html>"
        )
        return False
    return True


# ── Database access ──────────────────────────────────────────────────────────

def get_project_list():
    """Scan DB_DIR for project subdirectories containing artifact.json."""
    projects = []
    if not DB_DIR.exists():
        return projects
    for proj_dir in sorted(DB_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        artifact_file = proj_dir / "artifact.json"
        if not artifact_file.exists():
            continue
        try:
            with open(artifact_file) as f:
                meta = json.load(f)
            proj_name = proj_dir.name
            # Find the DB file in this directory
            db_files = list(proj_dir.glob("*.db"))
            if not db_files:
                print(f"WARN: no .db file in {proj_dir}", file=sys.stderr)
                continue
            db_path = db_files[0]  # Take the first .db file
            conn = sqlite3.connect(str(db_path))
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM nodes")
            node_count = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM edges")
            edge_count = c.fetchone()[0]
            conn.close()
            meta["db_path"] = str(db_path)
            meta["db_exists"] = True
            meta["display_name"] = proj_name
            meta["node_count"] = node_count
            meta["edge_count"] = edge_count
            # Store the actual DB filename for later lookup
            meta["db_filename"] = db_path.name
            projects.append(meta)
        except Exception as e:
            print(f"WARN: skipping {proj_dir}: {e}", file=sys.stderr)
    return projects


def get_db_path(project_name: str) -> str:
    """Find the DB file for a project inside DB_DIR/<project_name>/."""
    proj_dir = DB_DIR / project_name
    if not proj_dir.exists():
        raise FileNotFoundError(f"No directory for project '{project_name}'")
    db_files = list(proj_dir.glob("*.db"))
    if not db_files:
        raise FileNotFoundError(f"No .db file found for project '{project_name}' in {proj_dir}")
    return str(db_files[0])


def query_nodes(conn, search=None, label_filter=None, limit=5000):
    """Return list of node dicts."""
    c = conn.cursor()
    # Use FTS for search when provided; otherwise scan nodes table
    if search:
        c.execute("""
            SELECT n.id, n.project, n.label, n.name, n.qualified_name,
                   n.file_path, n.start_line, n.end_line, n.properties
            FROM nodes n
            JOIN nodes_fts fts ON n.id = fts.id
            WHERE nodes_fts MATCH ?
            LIMIT ?
        """, (search, limit))
    elif label_filter:
        c.execute("""
            SELECT id, project, label, name, qualified_name,
                   file_path, start_line, end_line, properties
            FROM nodes
            WHERE project = ? AND label = ?
            LIMIT ?
        """, (project_name, label_filter, limit))
    else:
        c.execute("""
            SELECT id, project, label, name, qualified_name,
                   file_path, start_line, end_line, properties
            FROM nodes
            LIMIT ?
        """, (limit,))
    rows = c.fetchall()
    result = []
    for row in rows:
        props = json.loads(row[8]) if row[8] else {}
        result.append({
            "id": row[0],
            "project": row[1],
            "label": row[2],
            "name": row[3],
            "qualified_name": row[4],
            "file_path": row[5],
            "start_line": row[6],
            "end_line": row[7],
            "properties": props,
        })
    return result


def query_edges(conn, project_name=None, limit=20000):
    """Return list of edge dicts with source/target node info."""
    c = conn.cursor()
    if project_name:
        c.execute("""
            SELECT e.id, e.source_id, e.target_id, e.type, e.properties,
                   s.label as src_label, s.name as src_name,
                   s.qualified_name as src_qname, s.file_path as src_file,
                   t.label as tgt_label, t.name as tgt_name,
                   t.qualified_name as tgt_qname, t.file_path as tgt_file
            FROM edges e
            JOIN nodes s ON e.source_id = s.id
            JOIN nodes t ON e.target_id = t.id
            WHERE (s.project = ? OR t.project = ?)
            LIMIT ?
        """, (project_name, project_name, limit))
    else:
        c.execute("""
            SELECT e.id, e.source_id, e.target_id, e.type, e.properties,
                   s.label as src_label, s.name as src_name,
                   s.qualified_name as src_qname, s.file_path as src_file,
                   t.label as tgt_label, t.name as tgt_name,
                   t.qualified_name as tgt_qname, t.file_path as tgt_file
            FROM edges e
            JOIN nodes s ON e.source_id = s.id
            JOIN nodes t ON e.target_id = t.id
            LIMIT ?
        """, (limit,))
    rows = c.fetchall()
    result = []
    for row in rows:
        props = json.loads(row[4]) if row[4] else {}
        result.append({
            "id": row[0],
            "source": row[1],
            "target": row[2],
            "type": row[3],
            "properties": props,
            "source_node": {
                "id": row[1],
                "label": row[5],
                "name": row[6],
                "qualified_name": row[7],
                "file_path": row[8],
            },
            "target_node": {
                "id": row[2],
                "label": row[9],
                "name": row[10],
                "qualified_name": row[11],
                "file_path": row[12],
            },
        })
    return result


def get_node_details(conn, node_id: int):
    """Return full node details including edges."""
    c = conn.cursor()
    c.execute("""
        SELECT id, project, label, name, qualified_name,
               file_path, start_line, end_line, properties
        FROM nodes WHERE id = ?
    """, (node_id,))
    row = c.fetchone()
    if not row:
        return None
    node = {
        "id": row[0],
        "project": row[1],
        "label": row[2],
        "name": row[3],
        "qualified_name": row[4],
        "file_path": row[5],
        "start_line": row[6],
        "end_line": row[7],
        "properties": json.loads(row[8]) if row[8] else {},
    }
    # Outgoing edges
    c.execute("""
        SELECT e.id, e.source_id, e.target_id, e.type, e.properties,
               t.label as tgt_label, t.name as tgt_name,
               t.qualified_name as tgt_qname, t.file_path as tgt_file
        FROM edges e
        JOIN nodes t ON e.target_id = t.id
        WHERE e.source_id = ?
        LIMIT 200
    """, (node_id,))
    node["outgoing"] = []
    for r in c.fetchall():
        node["outgoing"].append({
            "id": r[0], "source": r[1], "target": r[2], "type": r[3],
            "properties": json.loads(r[4]) if r[4] else {},
            "target_node": {"label": r[5], "name": r[6], "qualified_name": r[7], "file_path": r[8]},
        })
    # Incoming edges
    c.execute("""
        SELECT e.id, e.source_id, e.target_id, e.type, e.properties,
               s.label as src_label, s.name as src_name,
               s.qualified_name as src_qname, s.file_path as src_file
        FROM edges e
        JOIN nodes s ON e.source_id = s.id
        WHERE e.target_id = ?
        LIMIT 200
    """, (node_id,))
    node["incoming"] = []
    for r in c.fetchall():
        node["incoming"].append({
            "id": r[0], "source": r[1], "target": r[2], "type": r[3],
            "properties": json.loads(r[4]) if r[4] else {},
            "source_node": {"label": r[5], "name": r[6], "qualified_name": r[7], "file_path": r[8]},
        })
    return node


def get_file_content(repo_dir: str, file_path: str) -> str:
    """Read a file from the original repo directory."""
    # The repo path is stored in the project's properties
    return None  # We'll handle this via the DB's file_hashes or direct fs access


# ── Color scheme for node labels ─────────────────────────────────────────────

NODE_COLORS = {
    "Project":        "#4fc3f7",
    "Branch":         "#81c784",
    "File":           "#aed581",
    "Folder":         "#dce775",
    "Module":         "#fff176",
    "Class":          "#ffcc80",
    "Method":         "#ffab91",
    "Function":       "#ff8a65",
    "Variable":       "#f06292",
    "EnvVar":         "#e57373",
    "Package":        "#ba68c8",
    "Section":        "#9575cd",
    "Decorator":      "#7986cb",
    "Route":          "#4db6ac",
    "Property":       "#64b5f6",
    "Interface":      "#ce93d8",
    "TypeAlias":      "#f48fb1",
    "Enum":           "#a1887f",
    "EnumMember":     "#8d6e63",
    "Import":         "#90a4ae",
    "Export":         "#78909c",
    "Generic":        "#bcaaa4",
    "Default":        "#bdbdbd",
}


def node_color(label: str) -> str:
    return NODE_COLORS.get(label, NODE_COLORS["Default"])


# ── HTTP Handler ─────────────────────────────────────────────────────────────

class GraphViewerHandler(SimpleHTTPRequestHandler):
    """Serve the graph viewer UI + API endpoints."""

    def __init__(self, *args, **kwargs):
        # Point HTTP server at the base dir for static files
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        query = parse_qs(parsed.query)

        # ── API: list projects ──────────────────────────────────────────────
        if path == "/api/projects":
            if not require_auth(self):
                return
            projects = get_project_list()
            self.send_json(200, projects)
            return

        # ── API: get graph data (nodes + edges) ─────────────────────────────
        if path == "/api/graph":
            if not require_auth(self):
                return
            project = query.get("project", [None])[0]
            if not project:
                self.send_json(400, {"error": "Missing 'project' query parameter"})
                return
            db_path = get_db_path(project)
            if not os.path.exists(db_path):
                self.send_json(404, {"error": f"Database not found for project '{project}'"})
                return
            try:
                conn = sqlite3.connect(db_path)
                # Look up the actual project name stored in the DB
                c = conn.cursor()
                c.execute("SELECT name FROM projects LIMIT 1")
                row = c.fetchone()
                actual_project = row[0] if row else project
                nodes = query_nodes(conn, limit=20000)
                edges = query_edges(conn, project_name=actual_project, limit=30000)
                conn.close()
                # Trim to only nodes that appear in edges (for graph visibility)
                connected_ids = set()
                for e in edges:
                    connected_ids.add(e["source"])
                    connected_ids.add(e["target"])
                # Add hub nodes too (projects, branches)
                for n in nodes:
                    if n["label"] in ("Project", "Branch"):
                        connected_ids.add(n["id"])
                filtered_nodes = [n for n in nodes if n["id"] in connected_ids]
                node_map = {n["id"]: n for n in filtered_nodes}
                filtered_edges = [e for e in edges if e["source"] in node_map and e["target"] in node_map]
                self.send_json(200, {
                    "project": project,
                    "nodes": filtered_nodes,
                    "edges": filtered_edges,
                    "node_count": len(filtered_nodes),
                    "edge_count": len(filtered_edges),
                })
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── API: get node details ────────────────────────────────────────────
        if path.startswith("/api/node/"):
            if not require_auth(self):
                return
            node_id_str = path.split("/")[-1]
            try:
                node_id = int(node_id_str)
            except ValueError:
                self.send_json(400, {"error": "Invalid node ID"})
                return
            project = query.get("project", [None])[0]
            if not project:
                self.send_json(400, {"error": "Missing 'project' query parameter"})
                return
            db_path = get_db_path(project)
            if not os.path.exists(db_path):
                self.send_json(404, {"error": f"Database not found"})
                return
            try:
                conn = sqlite3.connect(db_path)
                node = get_node_details(conn, node_id)
                conn.close()
                if node:
                    self.send_json(200, node)
                else:
                    self.send_json(404, {"error": "Node not found"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── API: search nodes ────────────────────────────────────────────────
        if path == "/api/search":
            if not require_auth(self):
                return
            project = query.get("project", [None])[0]
            search = query.get("q", [""])[0]
            if not project or not search:
                self.send_json(400, {"error": "Missing 'project' or 'q' parameter"})
                return
            db_path = get_db_path(project)
            if not os.path.exists(db_path):
                self.send_json(404, {"error": "Database not found"})
                return
            try:
                conn = sqlite3.connect(db_path)
                nodes = query_nodes(conn, search=search, limit=100)
                conn.close()
                self.send_json(200, {"project": project, "query": search, "results": nodes})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── Root: serve the index.html ───────────────────────────────────────
        if path == "" or path == "/":
            # Set cache-control to no-cache so updates are visible
            self.send_response(302)
            self.send_header("Location", "/index.html")
            self.end_headers()
            return

        # ── Default: serve static files ──────────────────────────────────────
        super().do_GET()

    def send_json(self, status, data):
        body = json.dumps(data, indent=2 if status < 400 else None).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    # Ensure DB directory exists
    DB_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*70}")
    print(f"  Codebase Memory Graph Viewer")
    print(f"  Listening on http://{LISTEN_HOST}:{LISTEN_PORT}")
    print(f"  Username: {AUTH_USERNAME}")
    print(f"  DB directory: {DB_DIR}")
    print(f"{'='*70}\n")

    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), GraphViewerHandler)
    print("  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
