# Sovereign Memory

Local-first memory for AI agents. SQLite + FAISS + sentence-transformers — no cloud, no API keys, no vendor lock-in.

The core idea: **identity loads whole, knowledge loads chunked.** An agent knows WHO it is before any retrieved knowledge shapes its context. This prevents the failure mode where chunked identity text gets mixed with retrieved knowledge and the agent loses coherence about its own role.

## How It Works

Sovereign Memory runs a 5-stage retrieval pipeline when an agent asks for context:

```
Vault / Wiki docs
       ↓
  1. FTS5 keyword search (SQLite full-text)
  2. FAISS semantic search (sentence-transformers embeddings)
  3. Reciprocal Rank Fusion (weighted merge)
  4. Cross-encoder re-ranking (second-pass relevance scoring)
  5. Context budgeting (fit results into a token budget, not just top-K)
       ↓
  Ranked chunks, ready for injection into a system prompt
```

But before any of that runs, Layer 1 loads the agent's identity files — `IDENTITY.md` and `SOUL.md` — as complete documents. These are never chunked. The agent gets its full sense of self before the retrieval pipeline adds knowledge.

## What It's Built On

| Component | Role |
|-----------|------|
| **SQLite + WAL mode** | Document store, metadata, episodic events, task logs, thread tracking |
| **FTS5** | Full-text keyword search with triggers for automatic index updates |
| **FAISS** | Vector similarity search (flat index for small collections, HNSW for large) |
| **all-MiniLM-L6-v2** | Embedding model (384-dim, runs locally via sentence-transformers) |
| **cross-encoder/ms-marco-MiniLM-L-6-v2** | Re-ranker for second-pass scoring |
| **tiktoken** | Token counting for context budget enforcement |

Everything runs locally. No external API calls. First run downloads the two models (~80MB each) from Hugging Face.

## Setup

```bash
pip install sovereign-memory
```

This pulls in `sentence-transformers` (which includes PyTorch — ~2GB total). If you already have PyTorch installed, it'll use your existing installation.

### Directory Structure

Sovereign Memory resolves its home directory in this order:

1. `SOVEREIGN_HOME` env var (if set explicitly)
2. `~/.sovereign/` (default for new installs)
3. `~/.openclaw/` (backwards compatibility)

Inside that home directory, it expects:

```
~/.sovereign/
├── sovereign.db          # SQLite database (auto-created)
├── sovereign_faiss.index # FAISS index (auto-created)
├── vault/                # Your Obsidian vault or markdown knowledge base
│   ├── agents/
│   │   └── hermes/
│   │       ├── IDENTITY.md   # Agent identity (loaded whole)
│   │       └── SOUL.md       # Agent soul doc (loaded whole)
│   ├── docs/
│   │   └── *.md              # Knowledge docs (chunked for retrieval)
│   └── learnings/            # Write-back learnings from agents
└── wiki/                     # Optional wiki source (also indexed)
```

Override any path with env vars: `SOVEREIGN_DB_PATH`, `SOVEREIGN_FAISS_PATH`, `SOVEREIGN_VAULT_PATH`.

### Create an Agent Identity

Copy the example template and fill it in:

```bash
cp -r ~/.sovereign/vault/agents/_example ~/.sovereign/vault/agents/hermes
```

Edit `IDENTITY.md` — who the agent is, what it does, its constraints and communication style. Edit `SOUL.md` — cognitive architecture mapping, core directives, moral weights when values conflict.

These files are indexed with `whole_document=1` and always returned in full during Layer 1 hydration.

### Index Your Knowledge

```bash
# Index everything (vault + wiki sources)
sovereign-memory index

# Or start a file watcher that re-indexes on changes
sovereign-memory watch
```

The indexer walks your vault, parses markdown frontmatter (looks for `agent:` and `sigil:` tags), splits documents into chunks respecting heading boundaries and code blocks, embeds each chunk, and stores everything in SQLite + FAISS.

## Python API

```python
from sovereign_memory import SovereignAgent

agent = SovereignAgent("hermes")

# Layer 1 — identity (whole document, never chunked)
identity = agent.identity_context()

# Layer 2 — knowledge (chunked RAG via the 5-stage pipeline)
context = agent.startup_context(limit=10)

# Runtime recall — returns ranked results as a list of dicts
results = agent.recall("websocket architecture", limit=5)
for r in results:
    print(f"{r['filename']} (score={r['score']:.3f}): {r['chunk_text'][:80]}")

# Write-back memory — agent stores a learning for future retrieval
agent.learn("User prefers dark mode UI", category="preference")

# Episodic tracking
agent.log("Completed onboarding flow review", event_type="task")
task_id = agent.start_task("Refactor auth module")
agent.end_task(task_id, status="completed", result="Moved to JWT-based auth")

# Conversation threads
thread_id = agent.create_thread("Architecture discussion")
thread = agent.get_thread(thread_id)

# Knowledge graph export
graph = agent.export_graph(limit=50)

agent.close()
```

## CLI Reference

```bash
# Index all knowledge sources (vault + wiki)
sovereign-memory index

# Query memory with hybrid retrieval
sovereign-memory query "agent swarm topology" --agent hermes --limit 5

# Get an agent's full startup context (identity + knowledge)
sovereign-memory context hermes

# Store a learning for an agent
sovereign-memory learn hermes "Prefers trailing commas in Swift"

# Search stored learnings
sovereign-memory learnings hermes "code style"

# Run memory decay pass (fades low-relevance docs, 7-day half-life)
sovereign-memory decay

# Export knowledge graph as JSON
sovereign-memory graph --agent hermes

# Extract memory candidates through a local model bridge
sovereign-memory extract ./session.md
sovereign-memory extract ./session.md --learn-agent hermes --durable-only

# Start file watcher for live re-indexing
sovereign-memory watch

# Show database stats
sovereign-memory stats
```

## Integrations

The optional OpenClaw bridge lives in
`integrations/openclaw-extension/`. It exposes Sovereign Memory through a local
Unix-socket daemon and a TypeScript memory manager. See
`docs/runtime-integration.md` for the repository boundary between the packaged
core, OpenClaw integration code, and local model services such as an Apple
Foundation Models bridge.

## Key Concepts

### Two-Layer Hydration

Standard RAG chunks everything, including identity documents. This means an agent's sense of self competes with retrieved knowledge for context window space, and partial identity chunks can cause incoherent behavior.

Sovereign Memory separates identity from knowledge:
- **Layer 1 (Identity):** `IDENTITY.md` and `SOUL.md` load as complete documents. Always present, never chunked, never displaced by retrieval results.
- **Layer 2 (Knowledge):** Everything else goes through the chunked retrieval pipeline with scoring and budget control.

### Markdown-Aware Chunking

The chunker respects document structure — it splits on headings, preserves code blocks as atomic units, keeps list items together, and prepends heading breadcrumbs to each chunk so retrieved fragments retain their structural context.

### Write-Back Learnings

Agents can store learnings via `agent.learn()`. These are persisted in SQLite with category tags, versioned (new learnings can supersede old ones), and written back to disk as markdown files in the vault's `learnings/` directory — which means they get re-indexed and become retrievable in future sessions.

### Memory Decay

A configurable decay function (7-day half-life by default) reduces the relevance score of documents that haven't been accessed recently. Documents that are frequently retrieved get access-count reinforcement. This prevents the knowledge base from growing stale without hard cutoffs.

### Episodic Memory

Beyond static knowledge, agents can log events, track tasks (start/complete with duration), and maintain conversation threads. Threads auto-bind to semantically related documents. This gives agents temporal awareness — not just what they know, but what they've done.

### Local Extraction

The `sovereign_memory.extraction` module turns long text or session exports into
structured memory candidates using a local OpenAI-compatible chat endpoint. It
defaults to `http://127.0.0.1:11437/v1/chat/completions`, which matches a local
Apple Foundation Models bridge, and can be pointed elsewhere with
`SOVEREIGN_EXTRACTOR_URL` and `SOVEREIGN_EXTRACTOR_MODEL`. The CLI can print the
extractions or store durable entries directly as agent learnings.

## Configuration

All settings are in `SovereignConfig` with sensible defaults. Override via env vars or pass a custom config:

```python
from sovereign_memory import SovereignAgent, SovereignConfig

config = SovereignConfig(
    vault_path="~/my-vault",
    chunk_size=384,              # target tokens per chunk
    chunk_overlap=0.15,          # 15% overlap between chunks
    fts_weight=0.35,             # FTS5 weight in RRF fusion (vs 0.65 semantic)
    context_budget_tokens=4096,  # max tokens returned per recall
    decay_half_life_days=7.0,    # memory decay half-life
    faiss_hnsw_threshold=10000,  # switch from flat to HNSW at this vector count
)

agent = SovereignAgent("hermes", config=config)
```

## Development

```bash
git clone https://github.com/infektyd/sovereign-memory.git
cd sovereign-memory
python -m venv venv
source venv/bin/activate
pip install -e ".[dev]"

# Run tests (37 tests — import checks + integration tests)
pytest tests/ -v
```

## License

MIT
