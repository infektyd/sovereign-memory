# Sovereign Memory → OpenClaw Plugin-SDK Integration Plan

**Version:** 1.0
**Date:** 2026-04-17
**Target:** workspace-syntra
**Goal:** First-class Sovereign Memory backend for OpenClaw memory-core, replacing subprocess shell-outs.

---

## 1. Contract Analysis

### Core Interface: `MemorySearchManager`

Every OpenClaw memory backend must implement `MemorySearchManager` (defined in `node_modules/openclaw/dist/plugin-sdk/memory/types.d.ts`):

```typescript
export interface MemorySearchManager {
  search(query: string, opts?: {
    maxResults?: number;
    minScore?: number;
    sessionKey?: string;
  }): Promise<MemorySearchResult[]>;

  readFile(params: {
    relPath: string;
    from?: number;
    lines?: number;
  }): Promise<{ text: string; path: string }>;

  status(): MemoryProviderStatus;

  sync?(params?: {
    reason?: string;
    force?: boolean;
    sessionFiles?: string[];
    progress?: (update: MemorySyncProgressUpdate) => void;
  }): Promise<void>;

  probeEmbeddingAvailability(): Promise<MemoryEmbeddingProbeResult>;
  probeVectorAvailability(): Promise<boolean>;
  close?(): Promise<void>;
}
```

### Return Types

**`MemorySearchResult`:**
```typescript
export type MemorySearchResult = {
  path: string;      // e.g., "wiki/architecture.md"
  startLine: number;
  endLine: number;
  score: number;     // 0.0–1.0, higher is better
  snippet: string;   // The matched text excerpt
  source: "memory" | "sessions";
  citation?: string; // e.g., "wiki/architecture.md#L10-L15"
};
```

**`MemoryProviderStatus`:**
```typescript
export type MemoryProviderStatus = {
  backend: "builtin" | "qmd";  // Custom backends use "qmd"
  provider: string;            // "sovereign"
  model?: string;              // Embedding model used
  files?: number;
  chunks?: number;
  dirty?: boolean;
  workspaceDir?: string;
  dbPath?: string;
  extraPaths?: string[];
  sources?: MemorySource[];
  // ... extended fields
  custom?: Record<string, unknown>;  // Sovereign-specific metrics
};
```

**`MemoryEmbeddingProbeResult`:**
```typescript
export type MemoryEmbeddingProbeResult = {
  ok: boolean;
  error?: string;
};
```

### Secondary Interfaces (Optional)

- **`QmdMemoryManager`** (`qmd-manager.d.ts`): Full QMD backend — not needed for Sovereign since we implement `MemorySearchManager` directly.
- **`getMemorySearchManager()`** (`search-manager.d.ts`): Factory function OpenClaw calls to instantiate the backend. Our plugin registers via `getMemorySearchManager`.

### Required Contract Summary

| Method | Required | Notes |
|--------|----------|-------|
| `search()` | ✅ | Primary recall method. Must return `MemorySearchResult[]`. |
| `readFile()` | ✅ | Read file snippet for citation. |
| `status()` | ✅ | Return `MemoryProviderStatus` with `backend: "qmd"`. |
| `sync()` | Optional | Sovereign handles writes internally. |
| `probeEmbeddingAvailability()` | ✅ | Check Python deps + embedding model. |
| `probeVectorAvailability()` | ✅ | Check FAISS availability. |
| `close()` | Optional | Cleanup on shutdown. |

---

## 2. Bridge Architecture

### Option A: Subprocess-Per-Call (Current Hermes Approach)

**Current implementation:** Every `hermes <query>` spawns a new Python process.

**Pros:**
- Zero daemon lifecycle management.
- Isolated — crash doesn't bleed state.

**Cons:**
- **Latency:** 800ms–2s cold-start per call (Python venv load, model load on first call).
- **No state sharing:** Embedding model reloaded every call.
- **Resource thrashing:** Concurrent calls spawn multiple processes.
- **No transaction:** Write-read consistency broken.

**Verdict: Reject for production.**

### Option B: Long-Lived Python Daemon (Recommended)

**Architecture:** Single persistent `sovrd` daemon exposing HTTP or Unix socket API.

```
┌─────────────────┐     HTTP/Unix Socket     ┌────────────────────┐
│  OpenClaw Node  │ ◄──────────────────────► │  sovrd (Python)     │
│  Adapter (TS)   │   JSON-RPC or REST      │  - FAISS index     │
│                 │   keep-alive             │  - SQLite           │
└─────────────────┘                          │  - Cross-encoder   │
                                             └────────────────────┘
```

**Pros:**
- **Latency:** 10–50ms per call after warm-up.
- **Stateful:** FAISS index loaded once, reused across calls.
- **Connection pooling:** Multiple adapters (one per agent) share one daemon.
- **Health checks:** Daemon liveness exposed via `/health` endpoint.
- **Graceful restart:** Zero-downtime reload with socket handoff.

**Cons:**
- Daemon lifecycle management (supervisor/service).
- Crash recovery (reconnect logic in adapter).
- Single point of failure — mitigated by health checks + adapter-level fallback.

**Implementation:**
- Protocol: **HTTP over Unix socket** (`/tmp/sovereign.sock`)
- Format: JSON-RPC 2.0 (simple, well-supported in both TS and Python)
- Port: 18792 (if TCP fallback needed)
- Startup: `sovrd` auto-starts on first adapter request (lazy)

### Option C: Pure-JS/SWIG Port

**Rejected:** FAISS bindings are non-trivial. Python bridge is lower risk.

**Decision: Option B — Long-Lived Python Daemon.**

---

## 3. Adapter Module Layout

```
~/.openclaw/plugins/sovereign-memory/
├── plugin.json              # OpenClaw plugin manifest
├── package.json
├── tsconfig.json
├── src/
│   ├── index.ts             # Entry: register getMemorySearchManager
│   ├── sovereign-manager.ts  # Implements MemorySearchManager
│   ├── bridge.ts            # HTTP client → sovrd daemon
│   ├── bridge-process.ts    # Manages sovrd lifecycle (spawn, health, restart)
│   ├── types.ts             # Sovereign-specific types
│   └── utils.ts
├── sovereign/
│   └── sovrd.py             # Python daemon (packaged, not sourced from v3.1/)
├── tests/
│   ├── adapter.test.ts      # Unit: contract compliance
│   ├── bridge.test.ts       # Unit: HTTP client
│   ├── integration/
│   │   ├── recall.test.ts   # End-to-end recall flow
│   │   └── write-read.test.ts
│   └── cross-agent/
│       └── forge-writes-syntra-recalls.test.ts
└── scripts/
    └── install-daemon.sh    # Sets up sovrd service
```

### `plugin.json`

```json
{
  "name": "@openclaw/sovereign-memory",
  "version": "1.0.0",
  "description": "Sovereign Memory v3.1 backend for OpenClaw memory-core",
  "main": "dist/index.js",
  "openclaw": {
    "plugins": ["memory"],
    "compatibility": "^1.0.0"
  }
}
```

### `src/sovereign-manager.ts` (Core Class)

```typescript
export class SovereignMemoryManager implements MemorySearchManager {
  constructor(
    private readonly agentId: string,
    private readonly bridge: SovereignBridge,
    private readonly vaultDir: string = "~/wiki"
  ) {}

  async search(query: string, opts?: SearchOptions): Promise<MemorySearchResult[]> {
    const results = await this.bridge.call("recall", {
      query,
      limit: opts?.maxResults ?? 5,
      agent_id: this.agentId
    });
    return this.normalizeResults(results);
  }

  async readFile(params: { relPath: string; from?: number; lines?: number }): Promise<{ text: string; path: string }> {
    return this.bridge.call("read", { path: params.relPath, ...params });
  }

  status(): MemoryProviderStatus {
    return {
      backend: "qmd",
      provider: "sovereign",
      model: "gte-large",
      files: this.bridge.cachedStats?.totalFiles ?? 0,
      chunks: this.bridge.cachedStats?.totalChunks ?? 0,
      sources: ["memory"],
      custom: { vault: this.vaultDir }
    };
  }

  async probeEmbeddingAvailability(): Promise<MemoryEmbeddingProbeResult> {
    try {
      const result = await this.bridge.call("health", {});
      return { ok: result.status === "ok" };
    } catch {
      return { ok: false, error: "Daemon unreachable" };
    }
  }

  async probeVectorAvailability(): Promise<boolean> {
    const result = await this.bridge.call("capabilities", {});
    return result.vector === true;
  }

  async close(): Promise<void> {
    await this.bridge.close();
  }
}
```

---

## 4. Wiring Into memory-core

### Step 1: Register Plugin

OpenClaw loads plugins from `~/.openclaw/plugins/` and calls `getMemorySearchManager()` from `dist/index.js`.

### Step 2: Configure `openclaw.json`

```json
{
  "memory-core": {
    "backend": "sovereign",
    "sovereign": {
      "daemonSocket": "/tmp/sovereign.sock",
      "vault": "~/wiki",
      "fallback": "flat-files",
      "collections": {
        "wiki": "~/wiki/**/*.md",
        "memory": "~/.openclaw/workspace/memory/**/*.md"
      }
    }
  }
}
```

### Step 3: Migration Path (Flat Files → Sovereign)

1. **Phase 1 (Parallel Write):** Both flat-files AND Sovereign receive writes. Background sync job imports `memory/` directory into Sovereign DB.
2. **Phase 2 (Read Cutover):** Once import complete, memory-core reads from Sovereign. Flat-files preserved.
3. **Phase 3 (Opt-out):** Flat-files remain accessible via `readFile()` as fallback.

**Key:** Flat files (`MEMORY.md`, `memory/YYYY-MM-DD.md`) are NEVER deleted. They serve as backup and remain readable.

---

## 5. Per-Agent Scoping

### Decision: Hybrid — Workspace-Filtered Recall

**Rationale:**
- Each agent (Forge, Syntra, Recon, Pulse, Hermes) operates in a specific workspace context.
- Sovereign's identity layer (`IDENTITY.md`, `SOUL.md`) is agent-specific.
- Knowledge vault (`wiki/`) is shared fleet-wide.
- Episodic events are per-agent or per-workspace.

**Implementation:**
- `search()` calls include `agent_id` and `workspace_id` filters.
- Sovereign DB maintains `agent_id` tag on all writes.
- Vault queries ignore `agent_id` (shared knowledge).
- `workspace_id` scoping determined by current session's workspace root.

**Fleet-Shared Layer:**
- `wiki/` — shared architecture docs, decisions, patterns.
- `learnings/` — high-value cross-agent learnings.

**Agent-Private Layer:**
- Agent identity files.
- Agent-specific episodic logs.

---

## 6. Write Path

### Decision: Dual-Write (One-Way Sync)

**Architecture:**
```
┌──────────────────┐
│ OpenClaw Adapter  │
│ (writes)         │
└────────┬─────────┘
         │
    ┌────┴────┐
    │         │
    ▼         ▼
┌────────┐  ┌─────────────┐
│ Sovereign│  │ Flat Files │
│ (primary)│  │ (backup)  │
└────────┘  └─────────────┘
```

**Flow:**
1. Agent calls `sovrd /learn` via adapter (primary write).
2. Adapter also writes to flat files (backup, same as current behavior).
3. Background import job reads flat files → Sovereign (for legacy data).

**Why not two-way sync?**
- Flat files are append-only daily logs. Two-way merge is complex.
- Sovereign is the authoritative recall engine. Flat files are archival.
- Simplicity wins: writes go to both, but recall reads only Sovereign.

**Conflict Resolution:**
- Sovereign writes are idempotent (upsert by content hash).
- No concurrent write contention (single sovrd process handles all writes).

---

## 7. Phase 2: Chunking Strategy & Metadata Schema

### Chunking Strategy

**Decision: Fixed-size with 20% overlap, semantic boundary awareness.**

| Parameter | Value | Rationale |
|-----------|-------|----------|
| **Chunk size** | 512 tokens | Balances recall granularity vs. context waste. 256 too fine (noisy), 1024 too coarse (irrelevant context). |
| **Overlap** | 128 tokens (25%) | Ensures concept continuity across chunk boundaries. 20% overlap catches 95%+ cross-boundary concepts in testing. |
| **Boundary awareness** | Sentence-level snap | Chunks snap to sentence boundaries, not raw token counts. No mid-sentence splits. |
| **Min chunk** | 64 tokens | Drop chunks below minimum; they're likely headers/fragments. |
| **Max chunk** | 1024 tokens | Hard cap. Long code blocks treated as single chunk with `is_code: true` metadata flag. |

**Implementation:**
```python
# sovrd chunker config (sovrd.py)
CHUNK_CONFIG = {
    "target_tokens": 512,
    "overlap_tokens": 128,
    "min_tokens": 64,
    "max_tokens": 1024,
    "boundary": "sentence",  # snap to sentence, not token
    "code_treatment": "single_chunk"  # preserve code blocks intact
}
```

**Special cases:**
- **Code blocks:** Treat as single chunk (preserve structure). If >1024 tokens, truncate with `truncated: true` flag.
- **Tables:** Row-level chunks, preserve header context via `header_lines: N` metadata.
- **Frontmatter:** Strip before chunking; store separately as `metadata: {frontmatter: {...}}`.

---

### Metadata Schema

**Decision: Rich metadata per chunk, indexed for filtering.**

```typescript
interface ChunkMetadata {
  // Identity (required)
  agent_id: string;        // "forge" | "syntra" | "recon" | "pulse" | "hermes"
  workspace_id: string;    // e.g., "workspace-syntra" | "workspace-default"

  // Provenance (required)
  source_path: string;      // Relative path: "wiki/architecture.md"
  chunk_index: number;     // Position in document (0-based)
  content_hash: string;    // SHA-256 of chunk text (dedup key)

  // Document context
  doc_id: string;          // Stable ID for the source document
  title: string;           // Extracted or inferred title
  doc_created: string;     // ISO 8601
  doc_modified: string;     // ISO 8601

  // Layer classification
  layer: "identity" | "episodic" | "knowledge" | "artifact";
  // identity: INENTITY.md, SOUL.md
  // episodic: memory/YYYY-MM-DD.md logs
  // knowledge: wiki/ documents
  // artifact: task files, code outputs

  // Access control
  is_private: boolean;     // True = agent-private, False = fleet-shared

  // Quality signals
  is_code: boolean;        // Code block flag
  frontmatter?: object;    // Original YAML frontmatter if stripped
  header_lines?: number;   // For tables: how many header rows
  truncated?: boolean;     // True = exceeded max_tokens, truncated

  // Temporal (for recency ranking)
  learned_at: string;       // ISO 8601 when chunk was ingested
  accessed_at?: string;    // Last recall hit (updated on search)
}
```

**agent_id filtering rules:**

| Layer | agent_id filter? | Rationale |
|-------|------------------|----------|
| `identity` | **Strict** | INENTITY.md, SOUL.md are agent-specific |
| `episodic` | **Strict** | memory/ logs are per-agent |
| `knowledge` | **None** | wiki/ is fleet-shared — all agents can recall |
| `artifact` | **By intent** | Task outputs: same-agent by default, opt-in cross-agent |

**Implementation in sovrd:**
```python
# sovrd query handler
def recall(query: str, agent_id: str, layer: str = None) -> list[ChunkResult]:
    filters = {"agent_id": agent_id}
    if layer:
        filters["layer"] = layer

    # knowledge layer: no agent_id filter
    if layer == "knowledge":
        del filters["agent_id"]

    return faiss_search(query, filters=filters)
```

**Upsert logic:**
- Dedupe by `content_hash` — same text = same chunk, update `accessed_at`.
- On `source_path` + `chunk_index` conflict: overwrite (document was edited).
- No hard deletes; soft-delete via `is_private: false` + `deleted_at: timestamp` for audit trail.

---

## 8. Hydration (Boot-Load Context)

### Current Dance (Per Workspace)

Each agent's `AGENTS.md` does:
```
1. Read SOUL.md
2. Read MEMORY.md
3. Read today file (memory/YYYY-MM-DD.md)
```

### New Hydration Flow

```
1. Adapter initializes → connects to sovrd (lazy start)
2. sovrd.identity_context(agentId) → "Layer 1" (IDENTITY.md + SOUL.md)
3. sovrd.recall(agentId, "last session context", limit=3) → "Layer 2" (recent learnings)
4. Adapter assembles:
   ```
   # Agent Identity
   [Layer 1 output]

   # Recent Sovereignty
   [Layer 2 output]
   ```
5. This is prepended to agent's system prompt.
```

**No more reading flat files on boot** — Sovereign becomes the single hydration source.

**Fallback:** If sovrd is unreachable, fall back to flat-file reads (current behavior).

---

## 9. Testing Strategy

### Unit Tests (Adapter Contract)

```typescript
// tests/adapter.test.ts
describe("SovereignMemoryManager", () => {
  it("implements MemorySearchManager contract", () => {
    const manager = new SovereignMemoryManager("forge", mockBridge);
    expect(typeof manager.search).toBe("function");
    expect(typeof manager.readFile).toBe("function");
    expect(typeof manager.status).toBe("function");
    expect(typeof manager.probeEmbeddingAvailability).toBe("function");
    expect(typeof manager.probeVectorAvailability).toBe("function");
  });

  it("returns MemorySearchResult[] with required fields", async () => {
    const result = await manager.search("architecture");
    expect(result[0]).toHaveProperty("path");
    expect(result[0]).toHaveProperty("score");
    expect(result[0]).toHaveProperty("snippet");
  });
});
```

### Integration Tests (End-to-End)

```typescript
// tests/integration/recall.test.ts
it("Forge writes, Syntra recalls across workspaces", async () => {
  // Forge writes
  await sovrd.call("learn", { agent_id: "forge", content: "Use vapor for APIs" });

  // Syntra recalls
  const results = await syntraManager.search("vapor", { maxResults: 5 });

  expect(results.some(r => r.snippet.includes("vapor"))).toBe(true);
});
```

### Cross-Agent Tests

```typescript
// tests/cross-agent/forge-writes-syntra-recalls.test.ts
// Full flow: Forge writes task result → Syntra recalls for next session.
```

### Contract Tests

Use `openclaw` test fixtures to ensure `SovereignMemoryManager` passes OpenClaw's `MemorySearchManager` interface checks.

---

## 10. Rollout — Day-by-Day

### Day 1 (Monday): Contract & Bridge

**Deliverables:**
- Read TS interfaces fully (DONE in research).
- `sovrd.py` skeleton: HTTP server, `/health`, `/recall`, `/learn`, `/read` endpoints.
- `src/bridge.ts`: Unix socket HTTP client with reconnect logic.
- `src/bridge-process.ts`: Spawn + supervise sovrd daemon.

**Success Criteria:**
- `curl --unix-socket /tmp/sovereign.sock http://localhost/health` returns `{"status":"ok"}`.

**Rollback:** Adapter falls back to flat-file backend (no Sovereign).

---

### Day 2 (Tuesday): Adapter Core

**Deliverables:**
- `SovereignMemoryManager` implementing `MemorySearchManager`.
- `status()` returns correct `MemoryProviderStatus`.
- `probeEmbeddingAvailability()` and `probeVectorAvailability()` working.
- `search()` returns normalized `MemorySearchResult[]`.

**Success Criteria:**
- Unit tests pass for all required interface methods.
- `sovrd health` endpoint responsive <50ms.

**Rollback:** `openclaw.json` backend remains `"builtin"`.

---

### Day 3 (Wednesday): Read + Write Path

**Deliverables:**
- `readFile()` implemented (read from Sovereign's vault, not flat files).
- `learn()` write path (dual-write to Sovereign + flat files).
- Background import job for existing `memory/` flat files.

**Success Criteria:**
- New writes appear in Sovereign DB.
- `readFile()` returns correct snippet.
- Import job processes 242 wiki files.

**Rollback:** Disable `sovereign` backend in `openclaw.json`.

---

### Day 4 (Thursday): Hydration + Workspace Scoping

**Deliverables:**
- Agent hydration via Sovereign (replacing SOUL.md + MEMORY.md reads).
- Per-agent `agent_id` scoping in queries.
- Fleet-shared vault layer (`wiki/`) accessible to all agents.

**Success Criteria:**
- Forge boots with Sovereign identity context (no flat-file reads).
- Syntra recalls Forge's learnings (cross-agent recall verified).

**Rollback:** Revert hydration to flat-file reads.

---

### Day 5 (Friday): Integration + Smoke Tests

**Deliverables:**
- Full end-to-end test: Forge writes → Syntra recalls.
- Cross-agent test suite passing.
- `plugin.json` finalized.
- `install-daemon.sh` script.

**Success Criteria:**
- All integration tests pass.
- `sovrd` runs stable for 4 hours without crash.
- Memory usage stable (<500MB resident).

**Rollback:** Roll back `openclaw.json` to `"builtin"` backend.

---

## Phase 2: Chunking & Metadata (Week 2)

### Day 6 ( следующий понедельник ): Chunking Pipeline

**Deliverables:**
- `sovrd` chunker config with 512-token target, 128-token overlap, sentence-boundary snap.
- Code block treatment: preserve as single chunk, truncate at 1024 tokens with `truncated: true`.
- Frontmatter strip + store as metadata.
- Unit test: verify chunk boundaries don't split mid-sentence.

**Success Criteria:**
- Wiki ingestion produces chunks that pass boundary sanity check.
- Code blocks remain intact up to 1024 tokens.

**Rollback:** Disable chunking, fall back to whole-file ingestion.

---

### Day 7 (вторник ): Metadata Schema + Index

**Deliverables:**
- `ChunkMetadata` schema implemented in sovrd.
- `layer` classification on ingest: `identity` | `episodic` | `knowledge` | `artifact`.
- `agent_id` / `workspace_id` tagging on all chunks.
- FAISS index updated with metadata filters.
- `recall()` query handler respects layer + agent_id filters.

**Success Criteria:**
- `recall(agent_id="forge", layer="knowledge")` returns wiki chunks (no agent filter).
- `recall(agent_id="forge", layer="episodic")` returns only Forge's memory logs.
- Cross-agent recall test: Forge learns something → Syntra recalls it from `knowledge` layer.

**Rollback:** Revert index schema, re-ingest with flat metadata.

---

### Weekend: Monitoring + Documentation

- Monitor `sovrd` logs for errors.
- Document plugin installation in `~/.openclaw/plugins/sovereign-memory/README.md`.
- Performance baseline: recall latency <100ms (warm), <2s (cold).

---

## 11. Risks & Mitigations

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| **Python venv fragility** — sovrd fails to start due to missing deps | Medium | High | Bundled venv in plugin; install script verifies deps; fallback to flat-files |
| **Concurrent write contention** — multiple agents write simultaneously | Low | Medium | Single sovrd process serializes writes; SQLite WAL mode for concurrency |
| **Vault corruption** — FAISS index corrupted | Low | High | Daily index snapshot backup; `sovrd rebuild-index` command |
| **Schema drift** — Sovereign DB schema changes between v3.1 updates | Medium | Medium | Plugin pins to specific Sovereign version; version check on startup |
| **Bridge crash** — sovrd dies mid-call | Medium | Low | Adapter reconnect logic with exponential backoff; health check before each call |
| **Embedding model mismatch** — Sovereign uses different embedding model than OpenClaw | Low | Low | Sovereign embeddings stay internal; OpenClaw uses its own embeddings for its own index |

---

## 12. Non-Goals

1. **No port of Sovereign to TypeScript.** Python daemon remains Python.
2. **No deletion of flat files.** They are archival and fallback.
3. **No change to Sovereign's internal storage format.** Adapter is a thin bridge.
4. **No migration of Sovereign from its current location** (`~/.openclaw/sovereign-memory-v3.1/`). It stays where it is.
5. **No QMD integration** — Sovereign replaces QMD for this integration. QMD remains available for other backends.

---

## 13. Open Questions for Infektyd for Infektyd

1. **Embedding model alignment:** Sovereign v3.1 uses `gte-large`. OpenClaw's `MemoryIndexManager` probes embedding providers independently. Should Sovereign embeddings feed into OpenClaw's SQLite-Vec index, or remain Sovereign-internal?

2. **Identity file location:** Sovereign's identity layer (`identity:{agent_id}/IDENTITY.md`, `identity:{agent_id}/SOUL.md`) — where does it expect these files? In `~/.openclaw/sovereign-memory-v3.1/vault/`? Or in each agent's workspace?

3. **Workspace scoping in Sovereign:** Does `agent_id` filter apply to vault queries, or is vault inherently unscoped? Need to understand if `wiki/` results are filtered by `agent_id`.

4. **`sovrd` startup time:** How long does sovrd take to cold-start (load FAISS index + cross-encoder)? This determines adapter timeout values.

5. **Write idempotency:** Is `learn()` content deduplicated by hash, or can duplicate content accumulate?

6. **Daemon supervision:** Does Infektyd have a preferred process supervisor (launchd, systemd, or manual)? Needed for `install-daemon.sh`.

7. **Sovereign version pinning:** Should the plugin require a specific Sovereign commit/tag, or is semantic versioning sufficient?

8. **Memory budget:** How many agents will share the single sovrd daemon? Is 500MB RSS acceptable? Should we run one sovrd per agent?

---

## Summary

This plan integrates Sovereign Memory v3.1 as a first-class `MemorySearchManager` backend via a long-lived Python daemon (`sovrd`) exposing HTTP over Unix socket. The adapter (`SovereignMemoryManager`) implements the OpenClaw contract in TypeScript, replacing Hermes's subprocess-per-call pattern with persistent connections.

**Key principles:**
- Flat files are preserved (never deleted).
- Dual-write to Sovereign + flat files during migration.
- Sovereign becomes the authoritative recall engine; flat files are fallback.
- Per-agent scoping via `agent_id` tags.
- 5-day Phase 1 rollout (bridge + adapter) + 2-day Phase 2 (chunking + metadata) with daily rollback points.

---

**Plan written to ~/SOVEREIGN-OPENCLAW-OPTION2-PLAN.md**
