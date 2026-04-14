"""
Sovereign Memory V3.1 — Centralized Configuration.

V3.1 changes:
- Removed all compression config (TurboQuant stripped entirely)
- Added FAISS index type config (flat → HNSW auto-switch)
- Added cross-encoder re-ranking config
- Added write-back memory config
- Added context window budgeting config
- Added markdown-aware chunking config
- SOVEREIGN_HOME env var for portable data directory
"""

import os
from dataclasses import dataclass, field
from typing import Dict, Optional


def _sovereign_home() -> str:
    """Resolve the Sovereign Memory data directory.

    Priority:
    1. SOVEREIGN_HOME env var (explicit override)
    2. ~/.sovereign/ (default for new installs)
    3. ~/.openclaw/ (backwards compat with existing setups)
    """
    explicit = os.environ.get("SOVEREIGN_HOME")
    if explicit:
        return explicit

    default_home = os.path.expanduser("~/.sovereign")
    if os.path.isdir(default_home):
        return default_home

    legacy_home = os.path.expanduser("~/.openclaw")
    if os.path.isdir(legacy_home):
        return legacy_home

    # Neither exists yet — use the new default
    return default_home


@dataclass
class SovereignConfig:
    """All configuration in one place, overridable via env vars or constructor."""

    # Paths — all derive from SOVEREIGN_HOME unless explicitly overridden
    vault_path: str = os.environ.get(
        "SOVEREIGN_VAULT_PATH",
        os.path.expanduser("~/obsidian/openClaw/")
    )
    db_path: str = os.environ.get(
        "SOVEREIGN_DB_PATH",
        os.path.join(_sovereign_home(), "sovereign_memory.db")
    )
    graph_export_dir: str = os.environ.get(
        "SOVEREIGN_GRAPH_DIR",
        os.path.join(_sovereign_home(), "graphs/")
    )
    faiss_index_path: str = os.environ.get(
        "SOVEREIGN_FAISS_PATH",
        os.path.join(_sovereign_home(), "sovereign_faiss.index")
    )

    # Multiple wiki paths to index alongside the vault
    wiki_paths: list = field(default_factory=lambda: [
        os.path.expanduser("~/wiki"),
    ])

    # Embedding model
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dim: int = 384

    # FAISS indexing
    # "flat" = brute-force (exact), "hnsw" = approximate (fast at scale)
    # "auto" = flat until hnsw_threshold vectors, then rebuild as HNSW
    faiss_index_type: str = "auto"
    hnsw_threshold: int = 50_000           # Switch from flat → HNSW at this count
    hnsw_m: int = 32                       # HNSW connections per node (higher = more accurate, more RAM)
    hnsw_ef_construction: int = 200        # HNSW build-time search width
    hnsw_ef_search: int = 128              # HNSW query-time search width

    # Retrieval
    fts_weight: float = 0.35              # RRF constant for FTS5 rank
    semantic_weight: float = 0.65         # RRF constant for semantic rank
    rrf_k: int = 60                       # Reciprocal Rank Fusion constant

    # Cross-encoder re-ranking
    reranker_enabled: bool = True
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    reranker_top_k: int = 20              # Re-rank top-K candidates from first pass
    reranker_final_k: int = 5             # Return top-K after re-ranking

    # Chunking (markdown-aware)
    chunk_size: int = 384                 # Target tokens per chunk
    chunk_overlap: int = 64              # Overlap tokens between chunks
    chunk_strategy: str = "markdown"     # "markdown" (header-aware) or "sliding" (V3 behavior)

    # Write-back memory
    writeback_enabled: bool = True
    writeback_path: str = os.environ.get(
        "SOVEREIGN_WRITEBACK_PATH",
        os.path.join(_sovereign_home(), "learnings/")
    )

    # Context window budgeting
    context_budget_tokens: int = 4096     # Max tokens to return in a single recall
    token_model: str = "cl100k_base"      # tiktoken encoding for counting

    # Memory decay (Phase 8)
    decay_half_life_days: float = 7.0
    decay_min_score: float = 0.05
    decay_cron_hour: int = 4

    # Thread propagation
    thread_bind_threshold: float = 0.55

    # Agent colors (for visualization)
    agent_colors: Dict[str, str] = field(default_factory=lambda: {
        "forge": "#00D4FF",
        "recon": "#6B5BFF",
        "heartbeat_router": "#FF00FF",
        "syntra": "#00FF88",
        "hermes": "#FF8800",
        "unknown": "#808080",
    })

    def ensure_dirs(self):
        """Create directories if they don't exist."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        os.makedirs(self.graph_export_dir, exist_ok=True)
        if self.writeback_enabled:
            os.makedirs(self.writeback_path, exist_ok=True)


# Global default config — importable everywhere
DEFAULT_CONFIG = SovereignConfig()
