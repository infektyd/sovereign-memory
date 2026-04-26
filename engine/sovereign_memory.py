#!/usr/bin/env python3
"""
Sovereign Memory V3.1 — Main Entry Point.

Usage:
    # Index everything (vault + wiki)
    python sovereign_memory.py index [--verbose]

    # Index only vault or only wiki
    python sovereign_memory.py index --vault-only
    python sovereign_memory.py index --wiki-only

    # Query
    python sovereign_memory.py query "websocket architecture" [--agent forge] [--limit 5]

    # Agent context
    python sovereign_memory.py context forge [--limit 5]

    # Store a learning
    python sovereign_memory.py learn forge "WebSocket needs 500ms backoff" [--category fix]

    # Search learnings
    python sovereign_memory.py learnings "websocket" [--agent forge]

    # Run decay pass
    python sovereign_memory.py decay

    # Export graph
    python sovereign_memory.py graph [--agent forge]

    # Start file watcher
    python sovereign_memory.py watch

    # Show stats
    python sovereign_memory.py stats
"""

import sys
import json
import logging

from config import DEFAULT_CONFIG
from db import SovereignDB


def cmd_index(args):
    from index_all import index_all
    verbose = "--verbose" in args or "-v" in args
    vault_only = "--vault-only" in args
    wiki_only = "--wiki-only" in args
    do_vault = not wiki_only
    do_wiki = not vault_only
    result = index_all(vault=do_vault, wiki=do_wiki, verbose=verbose)
    print(json.dumps(result, indent=2))


def cmd_query(args):
    from agent_api import SovereignAgent
    if not args:
        print("Usage: sovereign_memory.py query <query> [--agent <id>] [--limit <n>]")
        sys.exit(1)

    agent_id = "main"
    limit = 5
    query_parts = []
    i = 0
    while i < len(args):
        if args[i] == "--agent" and i + 1 < len(args):
            agent_id = args[i + 1]
            i += 2
        elif args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        else:
            query_parts.append(args[i])
            i += 1

    query = " ".join(query_parts)
    agent = SovereignAgent(agent_id)
    print(agent.recall(query, limit=limit))
    agent.close()


def cmd_context(args):
    from agent_api import SovereignAgent
    agent_id = args[0] if args else "main"
    limit = 5
    if "--limit" in args:
        idx = args.index("--limit")
        if idx + 1 < len(args):
            limit = int(args[idx + 1])

    agent = SovereignAgent(agent_id)
    print(agent.startup_context(limit=limit))
    agent.close()


def cmd_learn(args):
    from agent_api import SovereignAgent
    if len(args) < 2:
        print("Usage: sovereign_memory.py learn <agent_id> <content> [--category <cat>]")
        sys.exit(1)

    agent_id = args[0]
    category = "general"
    content_parts = []
    i = 1
    while i < len(args):
        if args[i] == "--category" and i + 1 < len(args):
            category = args[i + 1]
            i += 2
        else:
            content_parts.append(args[i])
            i += 1

    content = " ".join(content_parts)
    agent = SovereignAgent(agent_id)
    lid = agent.learn(content, category=category)
    print(f"Stored learning #{lid} [{category}]")
    agent.close()


def cmd_learnings(args):
    from writeback import WriteBackMemory
    if not args:
        print("Usage: sovereign_memory.py learnings <query> [--agent <id>]")
        sys.exit(1)

    agent_id = None
    query_parts = []
    i = 0
    while i < len(args):
        if args[i] == "--agent" and i + 1 < len(args):
            agent_id = args[i + 1]
            i += 2
        else:
            query_parts.append(args[i])
            i += 1

    query = " ".join(query_parts)
    db = SovereignDB()
    wb = WriteBackMemory(db)
    results = wb.recall_learnings(query, agent_id=agent_id)
    for r in results:
        print(f"  [{r['category']}] {r['content'][:120]} (by {r['agent_id']})")
    if not results:
        print("No learnings found.")
    db.close()


def cmd_decay(args):
    from decay import MemoryDecay
    db = SovereignDB()
    decay = MemoryDecay(db)
    stats = decay.run_decay()
    print(json.dumps(stats, indent=2))
    report = decay.get_decay_report()
    print(json.dumps(report, indent=2))
    db.close()


def cmd_graph(args):
    from graph_export import GraphExporter
    agent_filter = None
    if "--agent" in args:
        idx = args.index("--agent")
        if idx + 1 < len(args):
            agent_filter = args[idx + 1]

    db = SovereignDB()
    exporter = GraphExporter(db)
    path = exporter.export_to_file(agent_filter=agent_filter)
    print(f"Exported: {path}")
    db.close()


def cmd_watch(args):
    from indexer import VaultIndexer
    db = SovereignDB()
    indexer = VaultIndexer(db)
    result = indexer.index_vault(verbose=True)
    print(json.dumps(result, indent=2))
    observer = indexer.start_watcher()
    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        observer.join()
    db.close()


def cmd_stats(args):
    """Show overall system stats."""
    from writeback import WriteBackMemory
    from faiss_index import FAISSIndex

    db = SovereignDB()

    with db.cursor() as c:
        c.execute("SELECT COUNT(*) as n FROM documents")
        doc_count = c.fetchone()["n"]

        c.execute("SELECT COUNT(*) as n FROM chunk_embeddings")
        chunk_count = c.fetchone()["n"]

        c.execute("SELECT COUNT(*) as n FROM episodic_events")
        event_count = c.fetchone()["n"]

        c.execute("SELECT COUNT(*) as n FROM threads")
        thread_count = c.fetchone()["n"]

    wb = WriteBackMemory(db)
    wb_stats = wb.get_stats()

    faiss = FAISSIndex()

    print(json.dumps({
        "documents": doc_count,
        "chunks": chunk_count,
        "episodic_events": event_count,
        "threads": thread_count,
        "learnings": wb_stats,
        "embedding_dim": DEFAULT_CONFIG.embedding_dim,
        "raw_embedding_bytes": chunk_count * DEFAULT_CONFIG.embedding_dim * 4,
        "faiss_index_type": "auto (flat → HNSW at {})".format(DEFAULT_CONFIG.hnsw_threshold),
    }, indent=2))
    db.close()


def cmd_faiss(args):
    """
    FAISS index management.

    Usage:
        python sovereign_memory.py faiss --rebuild   Force rebuild and save to disk.
        python sovereign_memory.py faiss --status    Show cache status.
    """
    import time as _time
    import numpy as np

    if "--rebuild" in args or len(args) == 0:
        # Force rebuild from DB then save to disk
        print("Rebuilding FAISS index from DB...")
        t0 = _time.time()

        db = SovereignDB()
        from faiss_index import FAISSIndex
        from faiss_persist import compute_db_checksum

        faiss_idx = FAISSIndex(DEFAULT_CONFIG)

        chunk_ids = []
        embeddings = []
        with db.cursor() as c:
            c.execute("SELECT chunk_id, embedding FROM chunk_embeddings")
            for row in c.fetchall():
                vec = np.frombuffer(row["embedding"], dtype=np.float32)
                if vec.shape[0] == DEFAULT_CONFIG.embedding_dim:
                    chunk_ids.append(row["chunk_id"])
                    embeddings.append(vec)

        if chunk_ids:
            all_vecs = np.array(embeddings, dtype=np.float32)
            faiss_idx.build_from_vectors(chunk_ids, all_vecs)
            conn = db._get_conn()
            saved = faiss_idx.save_to_disk(db_conn=conn)
            elapsed = _time.time() - t0
            print(json.dumps({
                "status": "rebuilt" if saved else "rebuilt_no_save",
                "vectors": len(chunk_ids),
                "elapsed_ms": round(elapsed * 1000, 1),
                "index_type": faiss_idx._current_type,
            }, indent=2))
        else:
            print(json.dumps({"status": "empty", "vectors": 0}, indent=2))

        db.close()

    elif "--status" in args:
        import os
        from faiss_persist import _faiss_dir_for_db, compute_db_checksum
        import sqlite3 as _sqlite3

        faiss_dir = _faiss_dir_for_db(DEFAULT_CONFIG.db_path)
        manifest_path = os.path.join(faiss_dir, "index.manifest.json")
        faiss_path = manifest_path.replace(".manifest.json", ".faiss")

        result = {
            "manifest_path": manifest_path,
            "faiss_path": faiss_path,
            "manifest_exists": os.path.exists(manifest_path),
            "faiss_exists": os.path.exists(faiss_path),
        }

        if os.path.exists(manifest_path):
            import json as _json
            with open(manifest_path) as f:
                result["manifest"] = _json.load(f)

        try:
            conn = _sqlite3.connect(DEFAULT_CONFIG.db_path)
            result["current_checksum"] = compute_db_checksum(conn)
            conn.close()
        except Exception as e:
            result["checksum_error"] = str(e)

        print(json.dumps(result, indent=2))
    else:
        print("Usage: sovereign_memory.py faiss [--rebuild | --status]")
        sys.exit(1)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    command = sys.argv[1]
    remaining = sys.argv[2:]

    commands = {
        "index": cmd_index,
        "query": cmd_query,
        "context": cmd_context,
        "learn": cmd_learn,
        "learnings": cmd_learnings,
        "decay": cmd_decay,
        "graph": cmd_graph,
        "watch": cmd_watch,
        "stats": cmd_stats,
        "faiss": cmd_faiss,
    }

    if command in commands:
        commands[command](remaining)
    else:
        print(f"Unknown command: {command}")
        print(f"Available: {', '.join(commands.keys())}")
        sys.exit(1)


if __name__ == "__main__":
    main()
