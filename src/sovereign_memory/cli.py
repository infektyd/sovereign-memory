#!/usr/bin/env python3
"""
Sovereign Memory V3.1 — Command Line Interface.

Usage:
    sovereign-memory index [--verbose]          # Index everything (vault + wiki)
    sovereign-memory index --vault-only         # Index only the Obsidian vault
    sovereign-memory index --wiki-only          # Index only wiki directories
    sovereign-memory query "query text"         # Query the memory
    sovereign-memory context <agent_id>         # Get agent startup context
    sovereign-memory learn <agent> <content>    # Store a learning
    sovereign-memory learnings <query>           # Search learnings
    sovereign-memory decay                       # Run memory decay pass
    sovereign-memory graph                       # Export knowledge graph
    sovereign-memory extract <path>              # Extract memories with a local model bridge
    sovereign-memory watch                       # Start file watcher
    sovereign-memory stats                       # Show system stats
"""

import argparse
import json
import logging
import os
import sys


def setup_logging(verbose=False):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )


def cmd_index(args, config, db):
    """Run all indexers (vault + wiki)."""
    from sovereign_memory.sources.index_all import index_all
    result = index_all(config, vault=not args.wiki_only, wiki=not args.vault_only, verbose=args.verbose)
    print(json.dumps(result, indent=2))


def cmd_query(args, config, db):
    """Query the memory with hybrid retrieval."""
    from sovereign_memory.agents.agent_api import SovereignAgent
    agent = SovereignAgent(args.agent, config)
    query = " ".join(args.query) if isinstance(args.query, list) else args.query
    print(agent.recall(query, limit=args.limit))
    agent.close()


def cmd_context(args, config, db):
    """Get agent startup context."""
    from sovereign_memory.agents.agent_api import SovereignAgent
    agent = SovereignAgent(args.agent_id, config)
    print(agent.startup_context(limit=args.limit))
    agent.close()


def cmd_learn(args, config, db):
    """Store a new learning."""
    from sovereign_memory.agents.agent_api import SovereignAgent
    agent = SovereignAgent(args.agent_id, config)
    content = " ".join(args.content) if isinstance(args.content, list) else args.content
    lid = agent.learn(content, category=args.category)
    print(f"Stored learning #{lid} [{args.category}]")
    agent.close()


def cmd_learnings(args, config, db):
    """Search learnings."""
    from sovereign_memory.core.writeback import WriteBackMemory
    wb = WriteBackMemory(db, config)
    query = " ".join(args.query) if isinstance(args.query, list) else args.query
    results = wb.recall_learnings(query, agent_id=args.agent, category=args.category)
    for r in results:
        print(f"  [{r['category']}] {r['content'][:120]} (by {r['agent_id']})")
    if not results:
        print("No learnings found.")


def cmd_decay(args, config, db):
    """Run memory decay pass."""
    from sovereign_memory.core.decay import MemoryDecay
    decay = MemoryDecay(db, config)
    stats = decay.run_decay()
    print(json.dumps(stats, indent=2))
    report = decay.get_decay_report()
    print(json.dumps(report, indent=2))


def cmd_graph(args, config, db):
    """Export knowledge graph."""
    from sovereign_memory.core.graph_export import GraphExporter
    exporter = GraphExporter(db, config)
    path = exporter.export_to_file(agent_filter=args.agent)
    print(f"Exported: {path}")


def cmd_extract(args, config, db):
    """Extract memory candidates from a text file with a local model bridge."""
    from sovereign_memory.agents.agent_api import SovereignAgent
    from sovereign_memory.extraction import MemoryExtractor

    extractor = MemoryExtractor()
    result = extractor.extract_file(args.path)

    stored = []
    if args.learn_agent:
        agent = SovereignAgent(args.learn_agent, config=config, db=db)
        try:
            for memory in result.memories:
                if args.durable_only and memory.durability != "durable":
                    continue
                learning_id = agent.learn(
                    memory.claim,
                    category=memory.category,
                    confidence=memory.confidence,
                    source_query=f"extracted from {args.path}",
                )
                stored.append({
                    "learning_id": learning_id,
                    "claim": memory.claim,
                    "category": memory.category,
                })
        finally:
            agent.close()

    output = result.as_dict()
    if stored:
        output["stored"] = stored
    print(json.dumps(output, indent=2))


def cmd_watch(args, config, db):
    """Start file watcher."""
    from sovereign_memory.core.indexer import VaultIndexer
    indexer = VaultIndexer(db, config)
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


def cmd_stats(args, config, db):
    """Show overall system stats."""
    from sovereign_memory.core.writeback import WriteBackMemory
    from sovereign_memory.core.faiss_index import FAISSIndex

    with db.cursor() as c:
        c.execute("SELECT COUNT(*) as n FROM documents")
        doc_count = c.fetchone()["n"]

        c.execute("SELECT COUNT(*) as n FROM chunk_embeddings")
        chunk_count = c.fetchone()["n"]

        c.execute("SELECT COUNT(*) as n FROM episodic_events")
        event_count = c.fetchone()["n"]

        c.execute("SELECT COUNT(*) as n FROM threads")
        thread_count = c.fetchone()["n"]

    wb = WriteBackMemory(db, config)
    wb_stats = wb.get_stats()

    faiss = FAISSIndex(config)

    print(json.dumps({
        "documents": doc_count,
        "chunks": chunk_count,
        "episodic_events": event_count,
        "threads": thread_count,
        "learnings": wb_stats,
        "embedding_dim": config.embedding_dim,
        "raw_embedding_bytes": chunk_count * config.embedding_dim * 4,
        "faiss_index_type": f"auto (flat → HNSW at {config.hnsw_threshold})",
    }, indent=2))


def main():
    """Parse arguments and dispatch to the appropriate command handler."""
    parser = argparse.ArgumentParser(
        description="Sovereign Memory V3.1 — Intelligent memory system for AI agents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable verbose/debug logging",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Index
    idx_parser = subparsers.add_parser("index", help="Index vault and/or wiki")
    idx_parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    idx_parser.add_argument("--vault-only", action="store_true", help="Index vault only")
    idx_parser.add_argument("--wiki-only", action="store_true", help="Index wiki only")

    # Query
    query_parser = subparsers.add_parser("query", help="Query memory with hybrid retrieval")
    query_parser.add_argument("query", help="Search query text", nargs="*")
    query_parser.add_argument("--agent", default="main", help="Agent ID")
    query_parser.add_argument("--limit", type=int, default=5, help="Max results")

    # Context
    ctx_parser = subparsers.add_parser("context", help="Get agent startup context")
    ctx_parser.add_argument("agent_id", help="Agent ID")
    ctx_parser.add_argument("--limit", type=int, default=5, help="Max results")

    # Learn
    learn_parser = subparsers.add_parser("learn", help="Store a new learning")
    learn_parser.add_argument("agent_id", help="Agent ID")
    learn_parser.add_argument("content", help="Learning content", nargs="*")
    learn_parser.add_argument("--category", default="general", help="Learning category")

    # Learnings
    learnings_parser = subparsers.add_parser("learnings", help="Search learnings")
    learnings_parser.add_argument("query", help="Search query text", nargs="*")
    learnings_parser.add_argument("--agent", help="Filter by agent ID")
    learnings_parser.add_argument("--category", help="Filter by category")

    # Decay
    subparsers.add_parser("decay", help="Run memory decay pass")

    # Graph
    graph_parser = subparsers.add_parser("graph", help="Export knowledge graph")
    graph_parser.add_argument("--agent", help="Filter by agent")

    # Extract
    extract_parser = subparsers.add_parser("extract", help="Extract memory candidates from a text file")
    extract_parser.add_argument("path", help="Text or markdown file to extract from")
    extract_parser.add_argument(
        "--learn-agent",
        help="If set, store extracted durable memories as learnings for this agent",
    )
    extract_parser.add_argument(
        "--durable-only",
        action="store_true",
        help="When writing learnings, skip extracted entries marked ephemeral",
    )

    # Watch
    subparsers.add_parser("watch", help="Start file watcher")

    # Stats
    subparsers.add_parser("stats", help="Show system stats")

    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Lazy import config
    from sovereign_memory.core.config import SovereignConfig, DEFAULT_CONFIG
    from sovereign_memory.core.db import SovereignDB

    config = DEFAULT_CONFIG
    db = SovereignDB(config)

    commands = {
        "index": cmd_index,
        "query": cmd_query,
        "context": cmd_context,
        "learn": cmd_learn,
        "learnings": cmd_learnings,
        "decay": cmd_decay,
        "graph": cmd_graph,
        "extract": cmd_extract,
        "watch": cmd_watch,
        "stats": cmd_stats,
    }

    try:
        handler = commands[args.command]
        handler(args, config, db)
    except KeyError:
        print(f"Unknown command: {args.command}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
