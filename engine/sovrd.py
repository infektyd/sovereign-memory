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
* ``search(query, agent_id?, limit?, depth?, budget_tokens?)`` — Hybrid FAISS + FTS5 search.
  depth: headline | snippet (default) | chunk | document — progressive disclosure tiers.
  budget_tokens: if set, applies MMR-diverse token-budget packing.
* ``expand(result_id, depth?)``          — Re-fetch a result at a deeper depth tier.
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
import re
import signal
import socket
import sys
import time
import threading
import importlib.util
import uuid
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


def _trace_ring():
    """Load engine/trace.py without colliding with Python's stdlib trace module."""
    module_name = "_sovereign_trace"
    if module_name in sys.modules:
        return sys.modules[module_name].GLOBAL_TRACE_RING
    trace_path = _ENGINE_DIR / "trace.py"
    spec = importlib.util.spec_from_file_location(module_name, trace_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load trace module from {trace_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.GLOBAL_TRACE_RING

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


_HANDOFF_KINDS = frozenset({"handoff", "candidate_learning", "request", "answer"})
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(api[_-]?key|password|secret|credential|private[_ -]?key)\b\s*[:=]\s*([^\s,;<>\"']+)"),
    re.compile(r"(?i)\b(bearer|access[_-]?token|refresh[_-]?token|token)\b\s*[:=]\s*([^\s,;<>\"']+)"),
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
]
_LOCAL_PATH_PATTERN = re.compile(r"(?<!\w)(?:/Users/[^ \n\r\t\"'<>]+|/Volumes/[^ \n\r\t\"'<>]+|/private/[^ \n\r\t\"'<>]+)")


def _redact_text(text: str) -> tuple[str, bool]:
    redacted = text
    changed = False
    for pattern in _SECRET_PATTERNS:
        if pattern.search(redacted):
            if pattern.groups >= 2:
                redacted = pattern.sub(lambda m: f"{m.group(1)}=[REDACTED]", redacted)
            else:
                redacted = pattern.sub("[REDACTED]", redacted)
            changed = True
    if _LOCAL_PATH_PATTERN.search(redacted):
        redacted = _LOCAL_PATH_PATTERN.sub("[REDACTED_PATH]", redacted)
        changed = True
    return redacted, changed


def _redact_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        items = []
        changed = False
        for item in value:
            redacted, item_changed = _redact_value(item)
            items.append(redacted)
            changed = changed or item_changed
        return items, changed
    if isinstance(value, dict):
        obj = {}
        changed = False
        for key, item in value.items():
            redacted, item_changed = _redact_value(item)
            obj[key] = redacted
            changed = changed or item_changed
        return obj, changed
    return value, False


def _agent_env_key(agent_id: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", agent_id.upper()).strip("_")


def _default_agent_vault(agent_id: str) -> Path:
    aliases = {
        "claude-code": "claudecode",
        "claudecode": "claudecode",
        "codex": "codex",
        "hermes": "hermes",
        "openclaw": "openclaw",
    }
    slug = aliases.get(agent_id, re.sub(r"[^a-z0-9]+", "", agent_id.lower()) or "agent")
    return Path.home() / ".sovereign-memory" / f"{slug}-vault"


def _agent_vault(agent_id: str) -> tuple[Path, bool]:
    mapping_raw = os.environ.get("SOVEREIGN_AGENT_VAULTS", "")
    if mapping_raw:
        try:
            mapping = json.loads(mapping_raw)
            if isinstance(mapping, dict) and agent_id in mapping:
                return Path(os.path.expanduser(str(mapping[agent_id]))), True
        except json.JSONDecodeError:
            logger.warning("Invalid SOVEREIGN_AGENT_VAULTS JSON; using defaults")

    env_key = f"SOVEREIGN_{_agent_env_key(agent_id)}_VAULT_PATH"
    if os.environ.get(env_key):
        return Path(os.path.expanduser(os.environ[env_key])), True
    return _default_agent_vault(agent_id), False


def _ensure_handoff_vault(vault_path: Path) -> None:
    for rel in (
        "raw",
        "wiki",
        "wiki/handoffs",
        "schema",
        "logs",
        "inbox",
        "outbox",
    ):
        (vault_path / rel).mkdir(parents=True, exist_ok=True)
    log_path = vault_path / "log.md"
    if not log_path.exists():
        log_path.write_text("# Sovereign Memory Log\n\n", encoding="utf-8")
    index_path = vault_path / "index.md"
    if not index_path.exists():
        index_path.write_text("# Sovereign Memory Index\n\n", encoding="utf-8")


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:80] or "handoff"


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_handoff_audit(vault_path: Path, tool: str, summary: str, details: dict) -> None:
    _ensure_handoff_vault(vault_path)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"## [{ts}] {tool} | {summary}\n\n```json\n{json.dumps(details, indent=2, sort_keys=True)}\n```\n\n"
    for path in (vault_path / "log.md", vault_path / "logs" / f"{ts[:10]}.md"):
        if not path.exists():
            path.write_text(f"# {ts[:10]} Sovereign Memory Audit\n\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)


def _validate_handoff_packet(from_agent: str, to_agent: str, packet: Any) -> tuple[Optional[dict], Optional[str]]:
    if not isinstance(packet, dict):
        return None, "packet must be an object"
    normalized = dict(packet)
    normalized.setdefault("from_agent", from_agent)
    normalized.setdefault("to_agent", to_agent)
    normalized.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    normalized.setdefault("trace_id", str(uuid.uuid4()))

    required = {
        "from_agent": str,
        "to_agent": str,
        "kind": str,
        "task": str,
        "envelope": str,
        "wikilink_refs": list,
        "trace_id": str,
        "created_at": str,
    }
    for key, typ in required.items():
        if key not in normalized:
            return None, f"{key} is required"
        if not isinstance(normalized[key], typ):
            return None, f"{key} must be {typ.__name__}"
        if typ is str and not normalized[key].strip():
            return None, f"{key} must not be empty"
    if normalized["from_agent"] != from_agent:
        return None, "from_agent must match params.from_agent"
    if normalized["to_agent"] != to_agent:
        return None, "to_agent must match params.to_agent"
    if normalized["kind"] not in _HANDOFF_KINDS:
        return None, f"kind must be one of {sorted(_HANDOFF_KINDS)}"
    if not all(isinstance(ref, str) and ref.strip() for ref in normalized["wikilink_refs"]):
        return None, "wikilink_refs must be a list of strings"
    if "expires_at" in normalized and normalized["expires_at"] is not None and not isinstance(normalized["expires_at"], str):
        return None, "expires_at must be a string"
    return normalized, None


def _compile_handoff_page(sender_vault: Path, packet: dict, stamp: str) -> Optional[Path]:
    if packet.get("kind") != "handoff":
        return None
    title = packet["task"].strip()
    slug = _slugify(f"{packet['from_agent']}-to-{packet['to_agent']}-{title}")
    page_path = sender_vault / "wiki" / "handoffs" / f"{stamp[:8]}-{slug}.md"
    refs = "\n".join(f"- [[{ref.removesuffix('.md')}]]" for ref in packet.get("wikilink_refs", []))
    page = (
        "---\n"
        f"title: {title}\n"
        "type: handoff\n"
        "status: accepted\n"
        "privacy: safe\n"
        f"from_agent: {packet['from_agent']}\n"
        f"to_agent: {packet['to_agent']}\n"
        f"trace_id: {packet['trace_id']}\n"
        f"created: {packet['created_at']}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"From: `{packet['from_agent']}`\n\n"
        f"To: `{packet['to_agent']}`\n\n"
        "## Envelope\n\n"
        f"```xml\n{packet['envelope']}\n```\n\n"
        "## Wikilink References\n\n"
        f"{refs or '- None'}\n"
    )
    page_path.write_text(page, encoding="utf-8")
    return page_path


def _handle_daemon_handoff(params: dict, request_id: Any) -> dict:
    """Validate, redact, and mediate an agent-to-agent inbox handoff."""
    global _request_count
    _request_count += 1

    from_agent = str(params.get("from_agent", "")).strip()
    to_agent = str(params.get("to_agent", "")).strip()
    if not from_agent or not to_agent:
        return _make_error(-32602, "from_agent and to_agent are required", request_id)

    packet, error = _validate_handoff_packet(from_agent, to_agent, params.get("packet"))
    if error:
        return _make_error(-32602, error, request_id)

    redacted_packet, redacted = _redact_value(packet)
    sender_vault, sender_explicit = _agent_vault(from_agent)
    recipient_vault, recipient_explicit = _agent_vault(to_agent)
    create_missing = os.environ.get("SOVEREIGN_HANDOFF_CREATE_MISSING_VAULTS", "1").lower() not in {"0", "false", "off"}

    if not recipient_explicit and not recipient_vault.exists() and not create_missing:
        if create_missing or sender_explicit:
            _ensure_handoff_vault(sender_vault)
        return _make_response({
            "status": "degraded",
            "delivered": False,
            "reason": f"destination vault not found for {to_agent}",
            "to_agent": to_agent,
            "recipient_vault": str(recipient_vault),
        }, request_id)

    try:
        _ensure_handoff_vault(sender_vault)
        _ensure_handoff_vault(recipient_vault)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        suffix = f"{stamp}-{_slugify(redacted_packet['task'])}-{redacted_packet['trace_id'][:8]}.json"
        outbox_path = sender_vault / "outbox" / suffix
        inbox_path = recipient_vault / "inbox" / suffix
        _write_json(outbox_path, redacted_packet)
        _write_json(inbox_path, redacted_packet)
        page_path = _compile_handoff_page(sender_vault, redacted_packet, stamp)
        audit_details = {
            "trace_id": redacted_packet["trace_id"],
            "from_agent": from_agent,
            "to_agent": to_agent,
            "kind": redacted_packet["kind"],
            "task": redacted_packet["task"],
            "inbox_path": str(inbox_path),
            "outbox_path": str(outbox_path),
            "handoff_page": str(page_path) if page_path else None,
            "redacted": redacted,
        }
        _append_handoff_audit(sender_vault, "handoff_sent", redacted_packet["task"], audit_details)
        _append_handoff_audit(recipient_vault, "handoff_received", redacted_packet["task"], audit_details)
        return _make_response({
            "status": "ok",
            "delivered": True,
            "redacted": redacted,
            "inbox_path": str(inbox_path),
            "outbox_path": str(outbox_path),
            "handoff_page": str(page_path) if page_path else None,
            "trace_id": redacted_packet["trace_id"],
        }, request_id)
    except Exception as exc:
        logger.exception("daemon.handoff failed")
        return _make_response({
            "status": "degraded",
            "delivered": False,
            "reason": str(exc),
            "to_agent": to_agent,
        }, request_id)


def _handle_ping(params: dict, request_id: Any) -> dict:
    return _make_response("pong", request_id)


def _resolve_backend(backend_param, config=None):
    """
    Resolve the backend parameter for a search request.

    backend_param may be:
      None / "auto"      → use config.vector_backends (default: ["faiss-disk"])
      "faiss-disk"       → single backend by name
      "faiss-mem"        → single backend by name
      ["faiss-disk", X]  → fan-out via MultiBackend

    Returns a resolved backend string or list for passing to retrieval.
    For single-backend default mode, returns None (retrieval uses its own FAISS).
    """
    from config import DEFAULT_CONFIG
    cfg = config or DEFAULT_CONFIG

    if backend_param is None or backend_param == "auto":
        # Auto-resolution: try each configured backend in order
        # For now return the first backend in config (full cascade in §2.3)
        backends = cfg.vector_backends
        if not backends or backends == ["faiss-disk"]:
            return None  # Default path — use existing FAISSIndex in retrieval
        return backends
    return backend_param


def _handle_search(params: dict, request_id: Any) -> dict:
    """Search Sovereign Memory via hybrid retrieval.

    Accepts optional ``depth`` parameter for progressive disclosure:
      headline  — wikilink, title, score, confidence, age_days (~30 tokens/result)
      snippet   — + text (≤280 chars) (~120 tokens/result) [DEFAULT]
      chunk     — + full chunk text, heading context, provenance (~500 tokens)
      document  — + full source document (whole_document=1 rows only)

    Accepts optional ``budget_tokens`` for MMR-diverse token-budgeted packing.
    When provided, selects a diverse subset fitting within the token budget.
    ``depth="auto"`` with ``budget_tokens`` uses "snippet" as the base tier.

    Accepts optional ``backend`` parameter:
      "auto" (default)   — use config.vector_backends priority cascade
      "faiss-disk"       — force specific backend
      ["faiss-disk", X]  — fan-out via multi-backend
    """
    global _request_count
    _request_count += 1

    query = params.get("query", "")
    if not query:
        return _make_error(-32602, "query is required", request_id)

    agent_id = params.get("agent_id", "main")
    limit = min(int(params.get("limit", 5)), 20)
    depth = str(params.get("depth", "snippet"))
    budget_tokens_param = params.get("budget_tokens")
    backend_param = params.get("backend", "auto")
    layers = params.get("layers")
    sort = str(params.get("sort", "semantic"))
    start_date = params.get("start_date")
    end_date = params.get("end_date")
    expand = params.get("expand", True)
    summarize_neighborhood = bool(params.get("summarize_neighborhood", False))

    # depth="auto" means "pick snippet and apply MMR budget packing"
    if depth == "auto":
        depth = "snippet"

    # Resolve backend — None means use the default FAISSIndex path (bit-identical)
    _resolved_backend = _resolve_backend(backend_param)

    try:
        engine = _lazy_retrieval()
        results = engine.retrieve(
            query=query,
            agent_id=agent_id,
            limit=limit,
            depth=depth,
            backend=_resolved_backend,
            layers=layers,
            sort=sort,
            start_date=start_date,
            end_date=end_date,
            expand=expand,
            summarize_neighborhood=summarize_neighborhood,
        )

        # Apply MMR token-budget packing if requested
        if budget_tokens_param is not None:
            try:
                budget = int(budget_tokens_param)
                from tokens import pack_results
                results = pack_results(results, budget_tokens=budget, depth=depth)
            except Exception as pack_exc:
                logger.warning("pack_results failed: %s — returning unbudgeted results", pack_exc)

        return _make_response({
            "query": query,
            "agent_id": agent_id,
            "depth": depth,
            "count": len(results),
            "trace_id": getattr(engine, "last_trace_id", None),
            "query_variants": (
                results[0].get("query_variants", [query])
                if results else [query]
            ),
            "results": results,
        }, request_id)
    except Exception as exc:
        logger.exception("search failed")
        return _make_error(-32000, f"Search error: {exc}", request_id)


def _handle_feedback(params: dict, request_id: Any) -> dict:
    """Store useful/not-useful feedback for a prior search result."""
    global _request_count
    _request_count += 1

    query = params.get("query", "")
    result_id = params.get("result_id")
    if not query:
        return _make_error(-32602, "query is required", request_id)
    if result_id is None:
        return _make_error(-32602, "result_id is required", request_id)

    try:
        result_id = int(result_id)
    except (TypeError, ValueError):
        return _make_error(-32602, "result_id must be an integer", request_id)

    useful = bool(params.get("useful", False))
    agent_id = params.get("agent_id", "main")

    try:
        result = _lazy_retrieval().record_feedback(
            query=query,
            result_id=result_id,
            useful=useful,
            agent_id=agent_id,
        )
        return _make_response(result, request_id)
    except Exception as exc:
        logger.exception("feedback failed")
        return _make_error(-32000, f"Feedback error: {exc}", request_id)


def _handle_trace(params: dict, request_id: Any) -> dict:
    """Return a process-local trace entry by id."""
    global _request_count
    _request_count += 1

    trace_id = params.get("trace_id")
    if not trace_id:
        return _make_error(-32602, "trace_id is required", request_id)

    try:
        trace = _trace_ring().get(str(trace_id))
        if trace is None:
            return _make_response({
                "trace_id": trace_id,
                "trace": None,
                "status": "not_found",
                "ephemeral": True,
            }, request_id)
        return _make_response({
            "trace_id": trace_id,
            "trace": trace,
            "status": "ok",
            "ephemeral": True,
        }, request_id)
    except Exception as exc:
        logger.warning("trace lookup failed: %s", exc)
        return _make_response({
            "trace_id": trace_id,
            "trace": {
                "trace_id": trace_id,
                "degraded": True,
                "reason": str(exc),
            },
            "status": "degraded",
            "ephemeral": True,
        }, request_id)


def _handle_expand(params: dict, request_id: Any) -> dict:
    """Re-fetch a specific result at a deeper depth tier.

    Accepts:
      result_id  — chunk_id or doc_id from a prior search result (required)
      depth      — target depth tier: 'chunk' or 'document' (default: 'chunk')
    """
    global _request_count
    _request_count += 1

    result_id = params.get("result_id")
    if result_id is None:
        return _make_error(-32602, "result_id is required", request_id)

    try:
        result_id = int(result_id)
    except (TypeError, ValueError):
        return _make_error(-32602, "result_id must be an integer", request_id)

    depth = str(params.get("depth", "chunk"))

    try:
        engine = _lazy_retrieval()
        result = engine.expand_result(result_id=result_id, depth=depth)
        if result is None:
            return _make_error(-32000, f"No result found for result_id={result_id}", request_id)
        return _make_response({
            "result_id": result_id,
            "depth": depth,
            "result": result,
        }, request_id)
    except Exception as exc:
        logger.exception("expand failed")
        return _make_error(-32000, f"Expand error: {exc}", request_id)


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
    """Store a learning with optional dual-write to flat-file.

    PR-6 behavior change: if contradictions are detected and ``force`` is not
    True (default False), returns::

        {"status": "contradiction", "candidates": [...]}

    without writing anything.  The caller must either resubmit with
    ``force=true`` to bypass detection, or supply a ``contradicts_id`` to
    explicitly record which prior learning is contradicted.

    On success the return shape is unchanged::

        {"status": "ok", "learning_id": <int>, "agent_id": ..., "category": ..., "dual_write": <bool>}

    Backward compatibility: existing calls without the new fields (assertion,
    applies_when, evidence_doc_ids, contradicts_id, force) continue to work
    exactly as before — the default of force=False is the only new behavior.
    """
    global _request_count, _dual_write_enabled
    _request_count += 1

    content = params.get("content", "")
    if not content:
        return _make_error(-32602, "content is required", request_id)

    agent_id = params.get("agent_id", "hermes")
    category = params.get("category", "general")
    force = bool(params.get("force", False))

    # PR-6 structured fields (all optional)
    assertion = params.get("assertion")        # explicit structured assertion
    applies_when = params.get("applies_when")  # scoping condition
    evidence_doc_ids = params.get("evidence_doc_ids")  # list of doc ids
    contradicts_id = params.get("contradicts_id")      # explicit contradiction

    try:
        wb = _lazy_writeback()

        # ── PR-6: Contradiction Detection ────────────────────────────────────
        # Skip detection when the caller explicitly forces the write or already
        # acknowledges a specific contradicts_id.
        if not force and contradicts_id is None:
            # Use the explicit assertion if provided, otherwise extract the
            # first sentence of content (or content itself if short).
            check_text = assertion or _extract_assertion(content)
            try:
                candidates = wb.detect_contradictions(
                    content_or_assertion=check_text,
                    agent_id=None,  # cross-agent — global check
                )
            except Exception as exc:
                logger.warning(
                    "learn: contradiction detection failed (%s) — proceeding with write",
                    exc,
                )
                candidates = []

            if candidates:
                return _make_response({
                    "status": "contradiction",
                    "candidates": candidates,
                }, request_id)

        # ── Store the learning ───────────────────────────────────────────────
        import sqlite3 as _sqlite3, time as _time, json as _json
        import numpy as _np

        now = _time.time()
        doc_ids_json = _json.dumps(evidence_doc_ids) if evidence_doc_ids else None

        # Embed for semantic search (and future contradiction detection)
        emb_bytes = None
        if wb.model:
            try:
                emb = wb.model.encode(content).astype(_np.float32)
                emb_bytes = emb.tobytes()
            except Exception as exc:
                logger.warning("learn: embedding failed (%s) — storing without embedding", exc)

        # Resolve category
        from writeback import CATEGORIES
        if category not in CATEGORIES:
            category = "general"

        with wb.db.cursor() as c:
            c.execute("""
                INSERT INTO learnings
                (agent_id, category, content, source_doc_ids, source_query,
                 confidence, embedding, created_at,
                 assertion, applies_when, evidence_doc_ids, contradicts_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                agent_id, category, content,
                None,              # source_doc_ids (legacy field, not the new evidence_doc_ids)
                params.get("source_query"),
                params.get("confidence", 1.0),
                emb_bytes, now,
                assertion, applies_when, doc_ids_json,
                contradicts_id,
                "active",
            ))
            lid = c.lastrowid

            # If caller supplies supersedes, mark the old learning
            supersedes = params.get("supersedes")
            if supersedes:
                c.execute(
                    "UPDATE learnings SET superseded_by = ? WHERE learning_id = ?",
                    (lid, supersedes),
                )

        logger.info(
            "Stored learning #%d [%s/%s]: %.60s…",
            lid, agent_id, category, content,
        )

        # Write to disk as markdown (Obsidian integration — existing behaviour)
        if wb.config.writeback_enabled:
            wb._write_to_disk(lid, agent_id, category, content, now)

        dw_ok = False
        if _dual_write_enabled:
            dw_ok = _flatfile_append(content, category)

        return _make_response({
            "status": "ok",
            "learning_id": lid,
            "agent_id": agent_id,
            "category": category,
            "dual_write": dw_ok,
        }, request_id)
    except Exception as exc:
        logger.exception("learn failed")
        return _make_error(-32000, f"Learn error: {exc}", request_id)


def _extract_assertion(content: str) -> str:
    """
    Extract a short assertion from content for contradiction checking.

    Uses the first sentence if content is longer than 120 characters,
    otherwise uses content as-is.
    """
    content = content.strip()
    if len(content) <= 120:
        return content
    # Split on common sentence terminators followed by whitespace
    import re
    m = re.search(r'[.!?]\s', content)
    if m:
        return content[:m.start() + 1].strip()
    return content[:120].strip()


def _handle_resolve_contradiction(params: dict, request_id: Any) -> dict:
    """Write a new learning and atomically supersede a list of prior learnings.

    Params:
        new_content (str, required): Content of the new learning.
        supersede_ids (list[int], required): IDs of learnings to supersede.
        agent_id (str, optional): Agent writing this resolution.
        category (str, optional): Category for the new learning.
        assertion (str, optional): Structured assertion for the new learning.
        applies_when (str, optional): Scoping condition.
        evidence_doc_ids (list, optional): Supporting document IDs.

    Returns on success::

        {"status": "ok", "new_learning_id": <int>, "superseded": [<int>, ...]}

    The operation is a single SQLite transaction: either the new learning is
    written AND all supersede_ids are updated, or nothing changes.
    """
    global _request_count
    _request_count += 1

    new_content = params.get("new_content", "")
    if not new_content:
        return _make_error(-32602, "new_content is required", request_id)

    supersede_ids = params.get("supersede_ids", [])
    if not isinstance(supersede_ids, list):
        return _make_error(-32602, "supersede_ids must be a list", request_id)

    agent_id = params.get("agent_id", "hermes")
    category = params.get("category", "general")
    assertion = params.get("assertion")
    applies_when = params.get("applies_when")
    evidence_doc_ids = params.get("evidence_doc_ids")

    try:
        import time as _time, json as _json
        import numpy as _np

        wb = _lazy_writeback()

        from writeback import CATEGORIES
        if category not in CATEGORIES:
            category = "general"

        now = _time.time()
        doc_ids_json = _json.dumps(evidence_doc_ids) if evidence_doc_ids else None

        emb_bytes = None
        if wb.model:
            try:
                emb = wb.model.encode(new_content).astype(_np.float32)
                emb_bytes = emb.tobytes()
            except Exception as exc:
                logger.warning(
                    "resolve_contradiction: embedding failed (%s) — storing without embedding",
                    exc,
                )

        # Single transaction: write new + supersede old
        with wb.db.transaction() as c:
            c.execute("""
                INSERT INTO learnings
                (agent_id, category, content, source_doc_ids, source_query,
                 confidence, embedding, created_at,
                 assertion, applies_when, evidence_doc_ids, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                agent_id, category, new_content,
                None, None,
                params.get("confidence", 1.0),
                emb_bytes, now,
                assertion, applies_when, doc_ids_json,
                "active",
            ))
            new_lid = c.lastrowid

            for sid in supersede_ids:
                c.execute(
                    """UPDATE learnings
                       SET superseded_by = ?, status = 'superseded'
                       WHERE learning_id = ?""",
                    (new_lid, sid),
                )

        logger.info(
            "Resolved contradiction: new learning #%d supersedes %s",
            new_lid, supersede_ids,
        )

        # Write to disk as markdown
        if wb.config.writeback_enabled:
            wb._write_to_disk(new_lid, agent_id, category, new_content, now)

        return _make_response({
            "status": "ok",
            "new_learning_id": new_lid,
            "superseded": supersede_ids,
        }, request_id)
    except Exception as exc:
        logger.exception("resolve_contradiction failed")
        return _make_error(-32000, f"Resolve contradiction error: {exc}", request_id)


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
    "ping":                   _handle_ping,
    "search":                 _handle_search,
    "feedback":               _handle_feedback,
    "trace":                  _handle_trace,
    "expand":                 _handle_expand,
    "read":                   _handle_read,
    "learn":                  _handle_learn,
    "resolve_contradiction":  _handle_resolve_contradiction,
    "log_event":              _handle_log_event,
    "daemon.handoff":         _handle_daemon_handoff,
    "handoff":                _handle_daemon_handoff,
    "status":                 _handle_status,
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
