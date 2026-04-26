#!/usr/bin/env python3
"""
sovrd.py — Sovereign Memory daemon (Phase 2)
HTTP server over Unix socket for OpenClaw plugin bridge.
Endpoints: /health, /recall, /learn, /read, /identity, /full

Phase 2 additions:
- Per-request agent_id routing (instead of hardcoded hermes)
- Layer filtering: identity / episodic / knowledge
- workspace_id scoping
- Content-hash dedup on /learn
"""

import hashlib
import json
import os
import sqlite3
import socket
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Add sovereign-memory engine to path
ENGINE_PATH = os.path.expanduser("~/.openclaw/sovereign-memory-v3.1")
if ENGINE_PATH not in sys.path:
    sys.path.insert(0, ENGINE_PATH)

from agent_api import SovereignAgent

SOCKET_PATH = "/tmp/sovereign.sock"
DB_PATH = os.path.expanduser("~/.openclaw/sovereign_memory.db")

# LRU cache of SovereignAgent instances keyed by agent_id
_agent_instances: dict[str, SovereignAgent] = {}
_agent_instances_lock = threading.Lock()


def get_agent(agent_id: str = "hermes") -> SovereignAgent:
    """Lazily initialize a SovereignAgent per agent_id (shared across calls)."""
    if agent_id not in _agent_instances:
        with _agent_instances_lock:
            if agent_id not in _agent_instances:
                _agent_instances[agent_id] = SovereignAgent(agent_id=agent_id)
    return _agent_instances[agent_id]


def _content_hash(text: str) -> str:
    """SHA-256 hash of normalized text for dedup."""
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _infer_layer(agent_id: str, category: str = "general", path: str = "") -> str:
    """Infer the memory layer from context."""
    if category in ("identity", "self") or "SOUL" in path or "IDENTITY" in path:
        return "identity"
    if category == "episodic":
        return "episodic"
    # knowledge: default for learned facts, decisions, wiki content
    return "knowledge"


def _read_file_text(file_path: str) -> str:
    """Read full file content from disk. Returns empty string on failure."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _enrich_recall_results(markdown: str) -> str:
    """
    Enrich recall results by hydrating empty chunk bodies from disk.
    """
    import re

    def _enrich_block(match):
        header = match.group(1)
        body = match.group(2)
        if body.strip():
            return match.group(0)
        path_match = re.match(r"###\s+(.+?)\s*\(score=", header)
        if not path_match:
            return match.group(0)
        file_path = path_match.group(1).strip()
        if os.path.isabs(file_path) and os.path.isfile(file_path):
            text = _read_file_text(file_path)
        else:
            candidate = os.path.join(os.path.expanduser("~/wiki"), file_path)
            if os.path.isfile(candidate):
                text = _read_file_text(candidate)
            else:
                text = _search_vault_for_file(file_path)
        if text:
            return f"{header}\n\n{text[:400].strip()}"
        return match.group(0)

    pattern = r"(### [^\n]+?\(score=[^)]+\))\s*\n+((?:(?!###\s)[^\n]*\n?)*)"
    return re.sub(pattern, _enrich_block, markdown)


def _search_vault_for_file(filename: str) -> str:
    """Search ~/wiki recursively for a file by name. Returns content or empty."""
    vault = os.path.expanduser("~/wiki")
    for root, _dirs, files in os.walk(vault):
        for f in files:
            if f == filename or f.replace(" ", "-") == filename or filename.replace(" ", "-") == f:
                full = os.path.join(root, f)
                return _read_file_text(full)
    return ""


def _recall_exact_learnings(query: str, agent_id: str = "", layer: str = None, limit: int = 5) -> str:
    """Return exact/keyword learning hits as markdown.

    Fresh /learn writes land in the learnings table immediately, while the
    vector retrieval path can miss exact new markers until indexing catches up.
    Day 5 requires write -> recall round-trips, so recall merges these exact
    learning hits ahead of vector/wiki results.
    """
    if not query.strip():
        return ""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        params = [f"%{query}%"]
        where = "content LIKE ? AND superseded_by IS NULL"
        # knowledge is fleet-shared; otherwise keep learnings scoped to agent.
        if agent_id and layer != "knowledge":
            where += " AND agent_id = ?"
            params.append(agent_id)
        c.execute(
            f"SELECT learning_id, agent_id, category, content, confidence, created_at "
            f"FROM learnings WHERE {where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        )
        rows = c.fetchall()
        conn.close()
        parts = []
        for row in rows:
            score = 1.0
            title = f"learning-{row['learning_id']}.md"
            heading = f"agent={row['agent_id']} category={row['category']}"
            parts.append(f"### {title} — {heading} (score={score:.3f})\n{row['content']}")
        return "\n\n".join(parts)
    except Exception:
        return ""

def _recall_raw(query: str, agent_id: str = "hermes", layer: str = None, limit: int = 5) -> str:
    """
    Recall with enriched chunk text and Phase 2 layer filtering.
    """
    a = get_agent(agent_id)

    # Apply layer filtering:
    # - knowledge: fleet-shared (no agent filter) → we need a separate approach
    # - identity: agent-scoped through SovereignAgent's identity_context
    # - episodic: agent-scoped episodic events
    # - default/artifact: agent-scoped knowledge recall

    if layer == "identity":
        # Return identity documents for this agent (whole-doc load)
        return a.identity_context() or f"No identity found for {agent_id}"

    if layer == "episodic":
        # Return episodic events
        try:
            return a.startup_context(limit=limit)
        except Exception:
            return f"No episodic events for {agent_id}"

    if layer == "knowledge":
        # Fleet-shared knowledge: include exact learnings first, then vector/wiki recall.
        exact = _recall_exact_learnings(query, agent_id=agent_id, layer=layer, limit=limit)
        results = a.recall(query, limit=limit)
        enriched = _enrich_recall_results(results)
        return "\n\n".join(part for part in [exact, enriched] if part)

    # Default: scoped recall (existing behavior) plus exact same-agent learnings.
    exact = _recall_exact_learnings(query, agent_id=agent_id, layer=layer, limit=limit)
    raw = a.recall(query, limit=limit)
    enriched = _enrich_recall_results(raw)
    return "\n\n".join(part for part in [exact, enriched] if part)


def _learn(content: str, agent_id: str = "hermes", category: str = "general",
           content_hash: str = None, workspace_id: str = "") -> dict:
    """
    Learn with content-hash dedup and metadata tagging.
    """
    # Compute hash for dedup
    if not content_hash:
        content_hash = _content_hash(content)

    # Check if this exact content already exists for this agent
    if _is_duplicate(agent_id, content_hash, content):
        return {"status": "duplicate", "hash": content_hash}

    a = get_agent(agent_id)

    # Tag with layer metadata
    inferred_layer = _infer_layer(agent_id, category)

    result = a.learn(content, category=category)

    # Write metadata to Phase 2 columns
    _write_chunk_metadata(
        agent_id=agent_id,
        workspace_id=workspace_id,
        content_hash=content_hash,
        layer=inferred_layer,
        content=content,
    )

    return {"status": "learned", "result": result, "hash": content_hash, "layer": inferred_layer}


def _is_duplicate(agent_id: str, content_hash: str, content: str = "") -> bool:
    """Check if this exact normalized content already exists for the agent.

    Phase 2 originally checked only chunk_embeddings.content_hash, but
    SovereignAgent.learn() stores primary write-back rows in learnings. When no
    document/chunk row is created for a learning, the chunk hash check misses
    duplicates and /learn inserts the same content repeatedly. Check both
    metadata-bearing chunks and existing learning rows.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # Metadata path: rows where Phase 2 content_hash was written.
        try:
            c.execute(
                "SELECT COUNT(*) as cnt FROM chunk_embeddings WHERE content_hash = ? AND doc_id IN "
                "(SELECT doc_id FROM documents WHERE agent LIKE ? OR agent = ?)",
                (content_hash, f"%{agent_id}%", agent_id)
            )
            row = c.fetchone()
            if row and row["cnt"] > 0:
                conn.close()
                return True
        except sqlite3.OperationalError:
            pass

        # Primary write-back path: learnings table has no content_hash column.
        # Compare normalized hashes in Python so whitespace/case-only changes
        # dedupe the same way _content_hash() does.
        if content:
            c.execute(
                "SELECT content FROM learnings WHERE agent_id = ? AND superseded_by IS NULL",
                (agent_id,)
            )
            for row in c.fetchall():
                if _content_hash(row["content"]) == content_hash:
                    conn.close()
                    return True

        conn.close()
        return False
    except Exception:
        return False


def _write_chunk_metadata(
    agent_id: str,
    workspace_id: str,
    content_hash: str,
    layer: str,
    content: str,
):
    """
    Write Phase 2 metadata to DB.
    Updates the most recent row for this agent with metadata columns.
    Safe to call even if columns don't exist yet (caught by exception).
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # Get the latest doc_id for this agent (the one just inserted by learn())
        c.execute(
            "SELECT doc_id FROM documents WHERE agent = ? ORDER BY indexed_at DESC LIMIT 1",
            (agent_id,)
        )
        row = c.fetchone()
        if not row:
            conn.close()
            return

        doc_id = row[0]
        now = time.time()

        # Try to update documents table
        try:
            c.execute(
                "UPDATE documents SET workspace_id = ?, agent = ?, layer = ? WHERE doc_id = ?",
                (workspace_id, agent_id, layer, doc_id)
            )
        except sqlite3.OperationalError:
            pass  # New columns may not exist yet

        # Update chunk_embeddings with metadata
        try:
            c.execute(
                "UPDATE chunk_embeddings SET content_hash = ?, is_code = ?, truncated = 0, "
                "learned_at = ? WHERE doc_id = ?",
                (content_hash, 1 if "```" in content else 0, now, doc_id)
            )
        except sqlite3.OperationalError:
            pass  # New columns may not exist yet

        conn.commit()
        conn.close()
    except Exception:
        pass  # Best-effort metadata


def _read_file(key: str, agent_id: str = "") -> str:
    """Read a specific file by path/name from the vault."""
    if os.path.isabs(key) and os.path.isfile(key):
        return _read_file_text(key)

    candidate = os.path.join(os.path.expanduser("~/wiki"), key)
    if os.path.isfile(candidate):
        return _read_file_text(candidate)

    text = _search_vault_for_file(key)
    if text:
        return text

    # Fallback: try DB FTS5 content
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        where_clause = "path LIKE ?"
        params = [f"%{key}%"]
        if agent_id:
            where_clause += " AND (agent = ? OR agent LIKE ?)"
            params.extend([agent_id, f"%{agent_id}%"])
        c.execute(f"SELECT content FROM vault_fts WHERE {where_clause} ORDER BY rank LIMIT 1", params)
        row = c.fetchone()
        conn.close()
        if row and row["content"]:
            return row["content"]
    except Exception:
        pass

    return ""


class SovereignHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Unix socket endpoints."""

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

    def _send_json(self, data, status=200):
        """Send a JSON response."""
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    # GET endpoints
    # ------------------------------------------------------------------

    def do_GET(self):
        """Handle GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/health":
            self._send_json({"status": "ok", "agent": "shared-daemon"})

        elif path == "/recall":
            q = query.get("q", [""])[0]
            limit = int(query.get("limit", ["5"])[0])
            agent_id = query.get("agent_id", [""])[0] or ""
            layer = query.get("layer", [""])[0] or None
            workspace_id = query.get("workspace_id", [""])[0] or None

            if not q:
                self._send_json({"error": "Missing 'q' parameter"}, 400)
                return

            try:
                results = _recall_raw(q, agent_id=agent_id, layer=layer, limit=limit)
                resp = {"results": results}
                if agent_id:
                    resp["agent_id"] = agent_id
                if layer:
                    resp["layer"] = layer
                if workspace_id:
                    resp["workspace_id"] = workspace_id
                self._send_json(resp)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/read":
            key = query.get("key", [""])[0]
            agent_id = query.get("agent_id", [""])[0] or ""
            if not key:
                self._send_json({"error": "Missing 'key' parameter"}, 400)
                return
            try:
                text = _read_file(key, agent_id=agent_id)
                if text:
                    self._send_json({"text": text, "path": key})
                else:
                    self._send_json({"error": f"File not found: {key}"}, 404)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/identity":
            agent_id = query.get("agent_id", ["hermes"])[0]
            try:
                a = get_agent(agent_id)
                identity = a.identity_context()
                self._send_json({"identity": identity, "agent_id": agent_id})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/full":
            # Two-layer hydration: identity (whole doc) + startup knowledge (chunked RAG)
            agent_id = query.get("agent_id", ["hermes"])[0]
            try:
                a = get_agent(agent_id)
                identity = a.identity_context()
                knowledge = a.startup_context()
                parts = []
                if identity:
                    parts.append(identity)
                if knowledge:
                    parts.append(knowledge)
                self._send_json({
                    "context": "\n\n".join(parts),
                    "identity": identity,
                    "knowledge": knowledge,
                    "agent_id": agent_id,
                })
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)

    # ------------------------------------------------------------------
    # POST endpoints
    # ------------------------------------------------------------------

    def do_POST(self):
        """Handle POST requests."""
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/learn":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
                return

            content = data.get("content")
            category = data.get("category", "general")
            agent_id = data.get("agent_id", "")
            workspace_id = data.get("workspace_id", "")
            content_hash = data.get("content_hash", None)

            if not content:
                self._send_json({"error": "Missing 'content' field"}, 400)
                return

            default_id = agent_id if agent_id else "hermes"

            try:
                result = _learn(
                    content,
                    agent_id=default_id,
                    category=category,
                    content_hash=content_hash,
                    workspace_id=workspace_id,
                )
                self._send_json(result)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        else:
            self._send_json({"error": f"Unknown endpoint: {path}"}, 404)


class UnixHTTPServer(HTTPServer):
    """HTTPServer that listens on a Unix domain socket."""
    address_family = socket.AF_UNIX

    def server_bind(self):
        if os.path.exists(self.server_address):
            os.unlink(self.server_address)
        self.socket.bind(self.server_address)
        os.chmod(self.server_address, 0o666)
        self.server_name = "localhost"
        self.server_port = 0


def run_server():
    """Start the HTTP server on Unix socket."""
    server = UnixHTTPServer(SOCKET_PATH, SovereignHandler)
    print(f"sovrd listening on {SOCKET_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run_server()
