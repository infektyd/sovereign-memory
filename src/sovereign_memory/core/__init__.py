"""Sovereign Memory V3.1 — Core modules.

Imports are lazy to avoid requiring numpy/faiss at package-discovery time.
Access them as attributes: sovereign_memory.core.FAISSIndex, etc.
"""

from .config import SovereignConfig, DEFAULT_CONFIG

__all__ = [
    "SovereignConfig",
    "DEFAULT_CONFIG",
    "SovereignDB",
    "MarkdownChunker",
    "Chunk",
    "FAISSIndex",
    "RetrievalEngine",
    "VaultIndexer",
    "MemoryDecay",
    "EpisodicMemory",
    "WriteBackMemory",
    "GraphExporter",
]


def __getattr__(name):
    """Lazy-load heavy modules only when accessed."""
    _lazy = {
        "SovereignDB": ".db",
        "MarkdownChunker": ".chunker",
        "Chunk": ".chunker",
        "FAISSIndex": ".faiss_index",
        "RetrievalEngine": ".retrieval",
        "VaultIndexer": ".indexer",
        "MemoryDecay": ".decay",
        "EpisodicMemory": ".episodic",
        "WriteBackMemory": ".writeback",
        "GraphExporter": ".graph_export",
    }
    if name in _lazy:
        import importlib
        mod = importlib.import_module(_lazy[name], __name__)
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
