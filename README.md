# Sovereign Memory

**Two-layer agent memory: identity-first hydration + hybrid RAG retrieval**

Sovereign Memory gives AI agents persistent, structured memory with a critical architectural decision: **identity loads whole, knowledge loads chunked**. An agent knows WHO it is before any retrieved knowledge shapes its context.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    AGENT BOOT                            │
│                                                         │
│  Layer 1: IDENTITY (whole document)                     │
│  ┌──────────────────┐  ┌──────────────────┐            │
│  │   IDENTITY.md    │  │     SOUL.md      │            │
│  │  Who I am, what  │  │  Moral weights,  │            │
│  │  I do, my role   │  │  cognitive style │            │
│  └──────────────────┘  └──────────────────┘            │
│           ↓ Never chunked, always complete              │
│                                                         │
│  Layer 2: KNOWLEDGE (chunked RAG)                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐             │
│  │   Wiki   │  │  Vault   │  │ Learnings │             │
│  │  (52pg)  │  │  (67pg)  │  │ (write-back)│           │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘             │
│       ↓              ↓              ↓                    │
│  ┌─────────────────────────────────────────┐            │
│  │      Hybrid Retrieval (FAISS + FTS5)    │            │
│  │      Cross-encoder re-ranking           │            │
│  └─────────────────────────────────────────┘            │
│           ↓ Chunked, budget-controlled                  │
│                                                         │
│  Output: System prompt context (~5200 chars)            │
└─────────────────────────────────────────────────────────┘
```

## Why Two Layers?

Chunking identity is a hallucination vector. "You never question the architecture" without the WHO saying it is dangerous. Identity must load as a complete document so the agent has its own lens before interpreting retrieved knowledge.

## Features

- **Two-layer hydration** — identity (whole) before knowledge (chunked)
- **Hybrid retrieval** — FAISS semantic + SQLite FTS5 keyword, fused via Reciprocal Rank Fusion
- **Cross-encoder re-ranking** — second-pass relevance scoring
- **Write-back memory** — agents store learnings that surface in future context
- **Episodic events** — task tracking, conversation threads, temporal awareness
- **Memory decay** — "slime mold" logic fades low-relevance knowledge over time
- **Markdown-aware chunking** — respects headings, code blocks, list blocks
- **Wiki + Obsidian ingestion** — structured knowledge from multiple sources
- **Knowledge graph export** — visualize the agent's memory as a graph

## Quick Start

```bash
pip install sovereign-memory
```

```python
from sovereign_memory import SovereignAgent, SovereignConfig

# Initialize an agent
agent = SovereignAgent("hermes")

# Layer 1: Identity (whole document)
identity = agent.identity_context()

# Layer 2: Knowledge (chunked RAG)
context = agent.startup_context(limit=10)

# Runtime recall
results = agent.recall("websocket architecture")

# Store a learning
agent.learn("User prefers dark mode UI", category="preference")

# Clean up
agent.close()
```

## CLI

```bash
# Index all knowledge sources
sovereign-memory index

# Query the memory
sovereign-memory query "agent swarm topology" --agent hermes

# Get agent startup context
sovereign-memory context hermes

# Store a learning
sovereign-memory learn hermes "Prefers trailing commas in Swift"

# Run memory decay
sovereign-memory decay

# Export knowledge graph
sovereign-memory graph --agent hermes

# Show system stats
sovereign-memory stats
```

## Configuration

All paths default from `SOVEREIGN_HOME` (resolves in order):
1. `SOVEREIGN_HOME` env var (explicit)
2. `~/.sovereign/` (new installs)
3. `~/.openclaw/` (backwards compat)

Individual overrides via env vars: `SOVEREIGN_DB_PATH`, `SOVEREIGN_FAISS_PATH`, `SOVEREIGN_VAULT_PATH`, etc.

## Agent Identity System

Each agent gets an identity directory with two files:

| File | Purpose |
|------|---------|
| `IDENTITY.md` | Role, capabilities, constraints, communication style |
| `SOUL.md` | Moral weights, cognitive architecture mapping, core directives |

Identity files are indexed with `whole_document=1` — they are never chunked and always retrieved in full during Layer 1 hydration.

## Development

```bash
git clone https://github.com/Infektyd/sovereign-memory.git
cd sovereign-memory
python -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# Run tests
pytest tests/ -v
```

## License

MIT
