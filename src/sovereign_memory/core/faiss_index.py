"""
Sovereign Memory V3.1 — FAISS Index Manager.

V3 used raw numpy loops to compute cosine similarity against all chunks.
That works fine at small scale but becomes a bottleneck at 200K+ vectors.

V3.1 uses a proper FAISS index with auto-scaling:
- Flat (exact) index under hnsw_threshold vectors
- HNSW (approximate) index above threshold
- Automatic rebuild when threshold is crossed
- Persistent to disk — survives restarts
- All vectors are full-fidelity float32[384]
"""

import os
import logging
from typing import Dict, List, Optional, Tuple

import numpy as np

from sovereign_memory.core.config import SovereignConfig, DEFAULT_CONFIG

logger = logging.getLogger("sovereign.faiss")


class FAISSIndex:
    """
    FAISS index manager with auto-scaling between Flat and HNSW.

    Usage:
        idx = FAISSIndex(config)
        idx.add(chunk_id=42, embedding=np.array([...]))
        results = idx.search(query_embedding, top_k=20)
        # results: [(chunk_id, distance), ...]
    """

    def __init__(self, config: SovereignConfig = DEFAULT_CONFIG):
        self.config = config
        self.dim = config.embedding_dim
        self._index = None
        self._id_map: Dict[int, int] = {}     # internal_idx → chunk_id
        self._reverse_map: Dict[int, int] = {} # chunk_id → internal_idx
        self._vectors: List[np.ndarray] = []    # Keep raw vectors for rebuild
        self._chunk_ids: List[int] = []
        self._current_type = "flat"
        self._faiss = None

        self._load_faiss()

    def _load_faiss(self):
        """Import faiss with graceful fallback."""
        try:
            import faiss
            self._faiss = faiss
        except ImportError:
            logger.warning(
                "faiss-cpu not installed. Install with: pip install faiss-cpu. "
                "Falling back to numpy brute-force search."
            )
            self._faiss = None

    @property
    def count(self) -> int:
        """Number of vectors in the index."""
        return len(self._chunk_ids)

    def build_from_vectors(
        self,
        chunk_ids: List[int],
        embeddings: np.ndarray,
    ) -> None:
        """
        Build (or rebuild) the index from a batch of vectors.

        Args:
            chunk_ids: List of chunk_id integers
            embeddings: numpy array of shape (N, dim), float32
        """
        if len(chunk_ids) == 0:
            return

        self._chunk_ids = list(chunk_ids)
        self._vectors = [embeddings[i] for i in range(len(chunk_ids))]
        self._id_map = {i: cid for i, cid in enumerate(chunk_ids)}
        self._reverse_map = {cid: i for i, cid in enumerate(chunk_ids)}

        n = len(chunk_ids)

        if self._faiss is None:
            # No FAISS — will use numpy fallback in search()
            self._current_type = "numpy"
            logger.info("Built numpy fallback index: %d vectors", n)
            return

        faiss = self._faiss

        # Normalize for cosine similarity (FAISS inner product on normalized = cosine)
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = np.where(norms < 1e-8, 1.0, norms)
        normalized = (embeddings / norms).astype(np.float32)

        should_hnsw = (
            self.config.faiss_index_type == "hnsw"
            or (self.config.faiss_index_type == "auto" and n >= self.config.hnsw_threshold)
        )

        if should_hnsw and n >= 1000:
            # HNSW index — use inner product metric for cosine similarity on normalized vectors
            index = faiss.IndexHNSWFlat(self.dim, self.config.hnsw_m, faiss.METRIC_INNER_PRODUCT)
            index.hnsw.efConstruction = self.config.hnsw_ef_construction
            index.hnsw.efSearch = self.config.hnsw_ef_search
            index.add(normalized)
            self._index = index
            self._current_type = "hnsw"
            logger.info("Built HNSW index: %d vectors (M=%d, ef=%d)",
                        n, self.config.hnsw_m, self.config.hnsw_ef_construction)
        else:
            # Flat index (exact search)
            index = faiss.IndexFlatIP(self.dim)  # Inner product on normalized = cosine
            index.add(normalized)
            self._index = index
            self._current_type = "flat"
            logger.info("Built Flat index: %d vectors", n)

    def add(self, chunk_id: int, embedding: np.ndarray) -> None:
        """
        Add a single vector to the index.
        For bulk operations, use build_from_vectors() instead.
        """
        self._chunk_ids.append(chunk_id)
        self._vectors.append(embedding.copy())
        idx = len(self._chunk_ids) - 1
        self._id_map[idx] = chunk_id
        self._reverse_map[chunk_id] = idx

        if self._faiss and self._index is not None:
            vec = embedding.astype(np.float32).reshape(1, -1)
            norm = np.linalg.norm(vec)
            if norm > 1e-8:
                vec = vec / norm
            self._index.add(vec)

            # Check if we need to upgrade to HNSW
            if (self._current_type == "flat"
                    and self.config.faiss_index_type == "auto"
                    and self.count >= self.config.hnsw_threshold):
                logger.info("Threshold crossed (%d vectors), rebuilding as HNSW...",
                            self.count)
                all_vecs = np.array(self._vectors, dtype=np.float32)
                self.build_from_vectors(self._chunk_ids, all_vecs)

    def remove(self, chunk_id: int) -> None:
        """
        Remove a vector by chunk_id.
        Marks it for exclusion — actual removal happens on next rebuild.
        """
        if chunk_id in self._reverse_map:
            idx = self._reverse_map.pop(chunk_id)
            self._id_map.pop(idx, None)
            # Note: FAISS doesn't support true deletion for HNSW.
            # We filter results in search() instead. Periodic rebuild cleans up.

    def search(
        self,
        query_embedding: np.ndarray,
        top_k: int = 20,
    ) -> List[Tuple[int, float]]:
        """
        Search for nearest neighbors.

        Args:
            query_embedding: float32 array of shape (dim,)
            top_k: Number of results

        Returns:
            List of (chunk_id, similarity_score) sorted by score descending.
        """
        if self.count == 0:
            return []

        query = query_embedding.astype(np.float32).reshape(1, -1)
        norm = np.linalg.norm(query)
        if norm > 1e-8:
            query = query / norm

        if self._faiss and self._index is not None:
            # FAISS search
            # Request extra results to account for removed vectors
            search_k = min(top_k * 2, self.count)
            scores, indices = self._index.search(query, search_k)

            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx < 0:  # FAISS returns -1 for missing
                    continue
                chunk_id = self._id_map.get(int(idx))
                if chunk_id is not None and chunk_id in self._reverse_map:
                    results.append((chunk_id, float(score)))
                if len(results) >= top_k:
                    break

            return results

        else:
            # Numpy fallback (brute-force cosine similarity)
            if not self._vectors:
                return []

            all_vecs = np.array(self._vectors, dtype=np.float32)
            norms = np.linalg.norm(all_vecs, axis=1, keepdims=True)
            norms = np.where(norms < 1e-8, 1.0, norms)
            normalized = all_vecs / norms

            sims = (normalized @ query.T).flatten()
            top_indices = np.argsort(sims)[::-1][:top_k * 2]

            results = []
            for idx in top_indices:
                chunk_id = self._id_map.get(int(idx))
                if chunk_id is not None and chunk_id in self._reverse_map:
                    results.append((chunk_id, float(sims[idx])))
                if len(results) >= top_k:
                    break

            return results

    def rebuild(self) -> None:
        """Force a full rebuild from stored vectors (cleans up deletions)."""
        if self._chunk_ids and self._vectors:
            # Filter out removed
            live_ids = []
            live_vecs = []
            for i, cid in enumerate(self._chunk_ids):
                if cid in self._reverse_map:
                    live_ids.append(cid)
                    live_vecs.append(self._vectors[i])

            if live_vecs:
                all_vecs = np.array(live_vecs, dtype=np.float32)
                self.build_from_vectors(live_ids, all_vecs)
            else:
                self._chunk_ids = []
                self._vectors = []
                self._id_map = {}
                self._reverse_map = {}
                self._index = None

    def save(self, path: Optional[str] = None) -> None:
        """Save index to disk for persistence across restarts."""
        path = path or self.config.faiss_index_path
        if not self._chunk_ids or not self._vectors:
            logger.debug("No vectors to save")
            return

        import json as _json
        meta = {
            "chunk_ids": self._chunk_ids,
            "index_type": self._current_type,
        }
        vecs = np.array(self._vectors, dtype=np.float32)

        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        np.save(path + ".vectors.npy", vecs)
        with open(path + ".meta.json", "w") as f:
            _json.dump(meta, f)
        logger.info("Saved FAISS index to %s (%d vectors)", path, len(self._chunk_ids))

    def load(self, path: Optional[str] = None) -> bool:
        """Load index from disk. Returns True if loaded successfully."""
        path = path or self.config.faiss_index_path
        vec_path = path + ".vectors.npy"
        meta_path = path + ".meta.json"

        if not os.path.exists(vec_path) or not os.path.exists(meta_path):
            return False

        try:
            import json as _json
            vecs = np.load(vec_path)
            with open(meta_path) as f:
                meta = _json.load(f)
            chunk_ids = meta["chunk_ids"]
            self.build_from_vectors(chunk_ids, vecs)
            logger.info("Loaded FAISS index from %s (%d vectors)", path, len(chunk_ids))
            return True
        except Exception as e:
            logger.warning("Failed to load FAISS index from %s: %s", path, e)
            return False

    def get_stats(self) -> Dict:
        """Return index statistics."""
        return {
            "total_vectors": self.count,
            "index_type": self._current_type,
            "dimension": self.dim,
            "hnsw_threshold": self.config.hnsw_threshold,
            "memory_bytes": self.count * self.dim * 4,  # float32
        }
