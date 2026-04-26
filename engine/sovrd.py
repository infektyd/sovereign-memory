#!/usr/bin/env python3
"""sovrd — Sovereign Memory Daemon (Layer 2 IPC Service).

A lightweight daemon that exposes Sovereign Memory (FAISS + SQLite) over a
Unix domain socket using JSON-RPC 2.0.  Enables Hermes Agent and other local
consumers to execute search, read, and status requests without spawning a new
Python interpreter or reloading heavy MLX / sentence-transformer weights every
call.

Features
--------
* Unix domain socket IPC (JSON-RPC 2.0) for sub-millisecond latency.
* Per-agent scoping — every request carries an optional ``agent_id`` tag.
* Dual-write support — writes can optionally also go to the flat-file
  ``~/.openclaw/MEMORY.md`` that the builtin provider uses.
* Hot-reloadable config via SIGHUP.
* Health / status endpoint.
* Graceful shutdown via SIGTERM / SIGINT.

JSON-RPC Methods
----------------
* ``search(query, agent_id?, limit?)``  — Hybrid FAISS + FTS5 search.
* ``read(agent_id?, limit?)``            — Agent startup context (recall).
* ``learn(content, agent_id?, category?)`` — Write a learning (dual-write).
* ``log_event(event_type, content, agent_id?)`` — Episodic event.
* ``status()``                           — Daemon + engine health.
* ``ping()``                             — Liveness probe.

Usage
-----
    python sovrd.py                    # default socket: /tmp/sovrd.sock
    python sovrd.py --socket /path/s   # custom socket
    python sovrd.py --port 9900        # HTTP fallback (optional)
    python sovrd.py --dual-write       # enable dual-write to flat-file memory

Client Quick-Start (Python)
---------------------------
    import socket, json
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect("/tmp/sovrd.sock")
    def rpc(method, params=None):
        msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params or {}}) + "\\n"
        s.sendall(msg.encode())
        resp = json.loads(s.recv(1 << 20))
        return resp.get("result")

    rpc("search", {"query": "websocket architecture"})
    rpc("read",   {"agent_id": "hermes"})
    rpc("status")
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import sys
import time
import threading
from pathlib import Path
from typing import Any, Dict, Optional

# ── Logging ───────────────────────────────────────────────────────────────

logger = logging.getLogger("sovrd")

# ── Sovereign engine imports ─────────────────────────────────────────────
# The daemon lives alongside the Sovereign engine so we can import its
# modules directly.  We add the engine directory to sys.path if needed.

_ENGINE_DIR = Path(__file__).resolve().parent
if str(_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(_ENGINE_DIR))

from config import DEFAULT_CONFIG, SovereignConfig          # noqa: E402
from db import SovereignDB                                  # noqa: E402

# ── Lazy imports (heavy ML deps) ──────────────────────────────────────────
_retrieval = None
_episodic = None
_writeback = None


def _lazy_retrieval():
    """Lazy-load RetrievalEngine to defer MLX weight loading."""
    global _retrieval
    if _retrieval is None:
        from retrieval import RetrievalEngine
        _retrieval = RetrievalEngine(SovereignDB(), DEFAULT_CONFIG)
    return _retrieval


def _lazy_writeback():
    """Lazy-load WriteBackMemory."""
    global _writeback
    if _writeback is None:
        from writeback import WriteBackMemory
        _writeback = WriteBackMemory(SovereignDB(), DEFAULT_CONFIG)
    return _writeback


def _lazy_episodic():
    """Lazy-load EpisodicMemory."""
    global _episodic
    if _episodic is None:
        from episodic import EpisodicMemory
        _episodic = EpisodicMemory(SovereignDB(), DEFAULT_CONFIG)
    return _episodic

# ── Dual-write helper (flat-file MEMORY.md) ──────────────────────────────

_OPENCLAW_DIR = Path.home() / ".openclaw"
_MEMORY_MD = _OPENCLAW_DIR / "MEMORY.md"
_MEMORY_LOCK = threading.Lock()


def _flatfile_append(entry: str, category: str = "learn") -> bool:
    """Append a formatted entry to ~/.openclaw/MEMORY.md.

    Format:
        ## [category] content  (2025-04-18 20:09)

    Returns True on success.
    """
    try:
        ts = time.strftime("%Y-%m-%d %H:%M")
        line = f"- [{category}] {entry} ({ts})\n"
        with _MEMORY_LOCK:
            if _MEMORY_MD.exists():
                text = _MEMORY_MD.read_text()
                if line.strip() not in text:
                    _MEMORY_MD.write_text(text + line)
            else:
                _MEMORY_MD.parent.mkdir(parents=True, exist_ok=True)
                _MEMORY_MD.write_text(f"# Hermes Memory\n\n{line}")
        return True
    except Exception as exc:
        logger.warning("Dual-write to MEMORY.md failed: %s", exc)
        return False


# ── JSON-RPC 2.0 server ──────────────────────────────────────────────────

VERSION = "0.1.0"
_start_time = 0.0
_request_count = 0
_dual_write_enabled = False


def _make_response(result: Any, request_id: Any = None) -> dict:
    """Build a JSON-RPC success response."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _make_error(code: int, message: str, request_id: Any = None) -> dict:
    """Build a JSON-RPC error response."""
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def _handle_ping(params: dict, request_id: Any) -> dict:
    return _make_response("pong", request_id)


def _handle_search(params: dict, request_id: Any) -> dict:
    """Search Sovereign Memory via hybrid retrieval."""
    global _request_count
    _request_count += 1

    query = params.get("query", "")
    if not query:
        return _make_error(-32602, "query is required", request_id)

    agent_id = params.get("agent_id", "main")
    limit = min(int(params.get("limit", 5)), 20)

    try:
        engine = _lazy_retrieval()
        results = engine.retrieve(query=query, agent_id=agent_id, limit=limit)
        formatted = []
        for r in results:
            formatted.append({
                "text": r.get("chunk_text", ""),
                "source": r.get("filename", ""),
                "heading": r.get("heading_context", ""),
                "score": round(r.get("score", 0), 4),
            })
        return _make_response({
            "query": query,
            "agent_id": agent_id,
            "count": len(formatted),
            "results": formatted,
        }, request_id)
    except Exception as exc:
        logger.exception("search failed")
        return _make_error(-32000, f"Search error: {exc}", request_id)


def _handle_read(params: dict, request_id: Any) -> dict:
    """Read agent startup context (identity + knowledge + learnings)."""
    global _request_count
    _request_count += 1

    agent_id = params.get("agent_id", "hermes")
    limit = min(int(params.get("limit", 5)), 20)

    try:
        db = SovereignDB()
        lines = []

        # Prior context
        with db.cursor() as c:
            c.execute("""
                SELECT d.doc_id, d.path, d.agent, d.sigil,
                       d.access_count, d.decay_score
                FROM documents d
                WHERE (d.agent = ? OR d.agent = 'unknown'
                       OR d.agent LIKE 'wiki:%')
                  AND d.whole_document = 0
                ORDER BY d.decay_score * d.access_count DESC,
                         d.last_accessed DESC NULLS LAST
                LIMIT ?
            """, (agent_id, limit))
            rows = c.fetchall()
            if rows:
                lines.append(f"## Prior Context ({agent_id})")
                for row in rows:
                    fname = os.path.basename(row["path"])
                    line = (
                        f"  - **{fname}** ({row['sigil']}) "
                        f"[{row['agent']}] "
                        f"accessed {row['access_count']}x, "
                        f"decay={row['decay_score']:.2f}"
                    )
                    lines.append(line)

        # Recent learnings
        with db.cursor() as c:
            c.execute("""
                SELECT learning_id, category, content, confidence, created_at
                FROM learnings
                WHERE agent_id = ? AND superseded_by IS NULL
                ORDER BY created_at DESC
                LIMIT 10
            """, (agent_id,))
            rows = c.fetchall()
            if rows:
                lines.append(f"\n## Learnings ({agent_id})")
                for row in rows:
                    lines.append(
                        f"  - [{row['category']}] {row['content'][:150]} "
                        f"(conf={row['confidence']:.1f})"
                    )

        # Recent episodic events
        with db.cursor() as c:
            c.execute("""
                SELECT event_type, content, created_at
                FROM episodic_events
                WHERE agent_id = ?
                ORDER BY created_at DESC
                LIMIT 5
            """, (agent_id,))
            rows = c.fetchall()
            if rows:
                lines.append(f"\n## Recent Activity ({agent_id})")
                for row in rows:
                    ts = time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(row["created_at"])
                    )
                    lines.append(
                        f"  - [{row['event_type']}] "
                        f"{row['content'][:120]} ({ts})"
                    )
        db.close()

        return _make_response({
            "agent_id": agent_id,
            "context": "\n".join(lines) if lines else f"No context for '{agent_id}'.",
        }, request_id)
    except Exception as exc:
        logger.exception("read failed")
        return _make_error(-32000, f"Read error: {exc}", request_id)


def _handle_learn(params: dict, request_id: Any) -> dict:
    """Store a learning with optional dual-write to flat-file."""
    global _request_count, _dual_write_enabled
    _request_count += 1

    content = params.get("content", "")
    if not content:
        return _make_error(-32602, "content is required", request_id)

    agent_id = params.get("agent_id", "hermes")
    category = params.get("category", "general")

    try:
        wb = _lazy_writeback()
        lid = wb.store_learning(
            agent_id=agent_id,
            content=content,
            category=category,
            confidence=params.get("confidence", 1.0),
        )

        dw_ok = False
        if _dual_write_enabled:
            dw_ok = _flatfile_append(content, category)

        return _make_response({
            "learning_id": lid,
            "agent_id": agent_id,
            "category": category,
            "dual_write": dw_ok,
        }, request_id)
    except Exception as exc:
        logger.exception("learn failed")
        return _make_error(-32000, f"Learn error: {exc}", request_id)


def _handle_log_event(params: dict, request_id: Any) -> dict:
    """Log an episodic event."""
    global _request_count
    _request_count += 1

    event_type = params.get("event_type", "")
    content = params.get("content", "")
    if not event_type or not content:
        return _make_error(-32602, "event_type and content are required",
                           request_id)

    agent_id = params.get("agent_id", "hermes")
    task_id = params.get("task_id")
    thread_id = params.get("thread_id")

    try:
        ep = _lazy_episodic()
        eid = ep.log_event(
            agent_id=agent_id,
            event_type=event_type,
            content=content,
            task_id=task_id,
            thread_id=thread_id,
        )
        return _make_response({"event_id": eid, "agent_id": agent_id},
                              request_id)
    except Exception as exc:
        logger.exception("log_event failed")
        return _make_error(-32000, f"Log event error: {exc}", request_id)


def _handle_status(params: dict, request_id: Any) -> dict:
    """Return daemon and engine status."""
    global _request_count
    _request_count += 1

    db_ok = False
    db_stats = {}
    try:
        db = SovereignDB()
        with db.cursor() as c:
            c.execute("SELECT COUNT(*) as n FROM documents")
            db_stats["documents"] = c.fetchone()["n"]
            c.execute("SELECT COUNT(*) as n FROM chunk_embeddings")
            db_stats["chunks"] = c.fetchone()["n"]
            c.execute("SELECT COUNT(*) as n FROM learnings")
            db_stats["learnings"] = c.fetchone()["n"]
            c.execute("SELECT COUNT(*) as n FROM episodic_events")
            db_stats["events"] = c.fetchone()["n"]
        db.close()
        db_ok = True
    except Exception:
        pass

    faiss_ok = False
    faiss_path = Path(DEFAULT_CONFIG.faiss_index_path)
    if faiss_path.exists():
        faiss_size = faiss_path.stat().st_size
        faiss_ok = faiss_size > 0

    uptime = time.time() - _start_time
    return _make_response({
        "daemon": {
            "version": VERSION,
            "uptime_seconds": round(uptime, 1),
            "requests_served": _request_count,
            "socket_path": str(_unix_socket_path),
            "dual_write": _dual_write_enabled,
        },
        "engine": {
            "db_ok": db_ok,
            "db_path": DEFAULT_CONFIG.db_path,
            "faiss_ok": faiss_ok,
            "faiss_path": DEFAULT_CONFIG.faiss_index_path,
            "stats": db_stats,
        },
    }, request_id)


# Method registry
_METHODS: Dict[str, callable] = {
    "ping":       _handle_ping,
    "search":     _handle_search,
    "read":       _handle_read,
    "learn":      _handle_learn,
    "log_event":  _handle_log_event,
    "status":     _handle_status,
}

# ── Unix socket server ───────────────────────────────────────────────────

_unix_socket_path: Path = Path("/tmp/sovrd.sock")
_running = False
_server: Optional[asyncio.AbstractServer] = None


def _parse_request(data: bytes) -> Optional[dict]:
    """Parse a JSON-RPC request from raw bytes."""
    try:
        text = data.decode("utf-8").strip()
        if not text:
            return None
        return json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Invalid request: %s", exc)
        return None


def _dispatch(request: dict) -> dict:
    """Route a JSON-RPC request to the correct handler."""
    method_name = request.get("method", "")
    request_id = request.get("id")
    params = request.get("params", {}) or {}

    handler = _METHODS.get(method_name)
    if handler is None:
        return _make_error(-32601, f"Method not found: {method_name}",
                           request_id)
    return handler(params, request_id)


async def _handle_client(reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter):
    """Handle a single client connection."""
    try:
        while True:
            # Read until newline (JSON-RPC over line-delimited protocol)
            data = await reader.readuntil(b"\n")
            if not data:
                break

            request = _parse_request(data)
            if request is None:
                response = _make_error(-32700, "Parse error")
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
                continue

            response = _dispatch(request)
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
    except asyncio.IncompleteReadError:
        pass  # Client disconnected
    except Exception:
        logger.exception("Client handler error")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _serve_unix_socket(path: Path):
    """Start the Unix socket server."""
    global _server, _running

    # Remove stale socket
    if path.exists():
        path.unlink()

    _server = await asyncio.start_unix_server(_handle_client, path=str(path))

    # Set socket permissions (owner read/write only)
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass

    logger.info("Listening on %s", path)
    _running = True

    try:
        async with _server:
            await _server.serve_forever()
    except asyncio.CancelledError:
        pass


# ── HTTP fallback server (optional) ──────────────────────────────────────

async def _serve_http(host: str = "127.0.0.1", port: int = 9900):
    """Minimal HTTP server for environments without Unix socket support."""

    async def handler(reader, writer):
        try:
            # Read HTTP request
            request_line = await reader.readline()
            headers = {}
            while True:
                line = await reader.readline()
                if line.strip() == b"":
                    break
                if b":" in line:
                    key, val = line.split(b":", 1)
                    headers[key.decode().strip().lower()] = val.decode().strip()

            content_length = int(headers.get("content-length", 0))
            body = b""
            if content_length:
                body = await reader.readexactly(content_length)

            if not body:
                writer.write(
                    b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n"
                )
                await writer.drain()
                return

            request = _parse_request(body)
            if request is None:
                resp = json.dumps(_make_error(-32700, "Parse error"))
            else:
                resp = json.dumps(_dispatch(request))

            resp_bytes = resp.encode()
            writer.write(
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(resp_bytes)}\r\n"
                f"\r\n".encode() + resp_bytes
            )
            await writer.drain()
        except Exception:
            logger.exception("HTTP handler error")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handler, host, port)
    logger.info("HTTP fallback listening on %s:%d", host, port)

    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass


# ── Entry point ──────────────────────────────────────────────────────────

def main():
    global _start_time, _unix_socket_path, _dual_write_enabled

    _start_time = time.time()

    parser = argparse.ArgumentParser(
        description="sovrd — Sovereign Memory Daemon",
    )
    parser.add_argument(
        "--socket", "-s",
        default="/tmp/sovrd.sock",
        help="Unix socket path (default: /tmp/sovrd.sock)",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=0,
        help="HTTP fallback port (0 = disabled, default: 0)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="HTTP bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--dual-write",
        action="store_true",
        default=False,
        help="Enable dual-write to ~/.openclaw/MEMORY.md",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args()

    # Logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [sovrd] %(levelname)s: %(message)s",
    )

    _unix_socket_path = Path(args.socket)
    _dual_write_enabled = args.dual_write

    logger.info("sovrd v%s starting", VERSION)
    logger.info("Engine dir: %s", _ENGINE_DIR)
    logger.info("Socket:    %s", args.socket)
    if args.port:
        logger.info("HTTP:      %s:%d", args.host, args.port)
    logger.info("Dual-write: %s", _dual_write_enabled)

    loop = asyncio.new_event_loop()
    main_task = loop.create_task(_serve_unix_socket(_unix_socket_path))

    http_task = None
    if args.port:
        http_task = loop.create_task(_serve_http(args.host, args.port))

    def _shutdown(signum, frame):
        nonlocal _running
        sig_name = signal.Signals(signum).name
        logger.info("Received %s, shutting down…", sig_name)
        _running = False

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    def _reload(signum, frame):
        logger.info("SIGHUP received — config reload (no-op for now)")
    signal.signal(signal.SIGHUP, _reload)

    try:
        loop.run_until_complete(main_task)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down…")
    finally:
        _running = False
        # Clean up socket
        if _unix_socket_path.exists():
            _unix_socket_path.unlink()
        loop.close()
        logger.info("sovrd stopped.")


if __name__ == "__main__":
    main()
