"""
Sovereign Memory V3.1 — Retrieval Engine.

V3.1 changes over V3:
1. FAISS-backed semantic search (not raw numpy loops)
2. Cross-encoder re-ranking: first-pass retrieval is fast but approximate;
   second-pass re-ranking with a cross-encoder scores (query, passage) pairs
   directly for much higher precision
3. Context window budgeting: returns chunks up to a token budget, not just top-K
4. Heading context enrichment: chunks carry their heading breadcrumb
5. No compression anywhere — all vectors are raw float32[384]
"""

import time
import math
import logging
from typing import List, Dict, Optional

import numpy as np

from sovereign_memory.core.config import SovereignConfig, DEFAULT_CONFIG
from sovereign_memory.core.db import SovereignDB
from sovereign_memory.core.faiss_index import FAISSIndex

logger = logging.getLogger("sovereign.retrieval")


class RetrievalEngine:
    """Hybrid FTS5 + FAISS semantic retrieval with cross-encoder re-ranking."""

    def __init__(
        self,
        db: SovereignDB,
        config: SovereignConfig = DEFAULT_CONFIG,
        faiss_index: Optional[FAISSIndex] = None,
    ):
        self.db = db
        self.config = config
        self.faiss_index = faiss_index or FAISSIndex(config)
        self._model = None
        self._reranker = None
        self._tokenizer = None

    @property
    def model(self):
        """Lazy-load embedding model."""
        if self._model is None:
            import os
            os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self.config.embedding_model)
            except ImportError:
                self._model = False
        return self._model if self._model is not False else None

    @property
    def reranker(self):
        """Lazy-load cross-encoder re-ranker."""
        if self._reranker is None and self.config.reranker_enabled:
            try:
                from sentence_transformers import CrossEncoder
                self._reranker = CrossEncoder(self.config.reranker_model)
                logger.info("Cross-encoder loaded: %s", self.config.reranker_model)
            except ImportError:
                logger.warning("sentence-transformers CrossEncoder not available — skipping re-ranking")
                self._reranker = False
            except Exception as e:
                logger.warning("Failed to load cross-encoder: %s", e)
                self._reranker = False
        return self._reranker if self._reranker is not False else None

    @property
    def tokenizer(self):
        """Lazy-load tiktoken tokenizer for context budgeting."""
        if self._tokenizer is None:
            try:
                import tiktoken
                self._tokenizer = tiktoken.get_encoding(self.config.token_model)
            except ImportError:
                logger.warning("tiktoken not installed — using word-count approximation")
                self._tokenizer = False
        return self._tokenizer if self._tokenizer is not False else None

    def _count_tokens(self, text: str) -> int:
        """Count tokens in text."""
        if self.tokenizer:
            return len(self.tokenizer.encode(text))
        # Fallback: rough word-count approximation (1 token ≈ 0.75 words)
        return int(len(text.split()) / 0.75)

    # ── FTS5 Search ────────────────────────────────────────────

    def _fts_search(self, query: str, limit: int) -> List[Dict]:
        """FTS5 search using BM25 ranking."""
        results = []
        safe_query = self._sanitize_fts_query(query)
        if not safe_query:
            return results

        with self.db.cursor() as c:
            c.execute("""
                SELECT f.doc_id, d.path, d.agent, d.sigil,
                       rank AS bm25_rank, d.decay_score
                FROM vault_fts f
                JOIN documents d ON d.doc_id = f.doc_id
                WHERE vault_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (safe_query, limit * 3))

            for row in c.fetchall():
                results.append({
                    "doc_id": row["doc_id"],
                    "path": row["path"],
                    "agent": row["agent"],
                    "sigil": row["sigil"],
                    "bm25_rank": row["bm25_rank"],
                    "decay_score": row["decay_score"] or 1.0,
                })

        return results

    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        """Sanitize query for FTS5 MATCH syntax."""
        import re
        # Replace hyphens with spaces (FTS5 treats - as NOT operator)
        cleaned = re.sub(r'[^\w\s]', ' ', query)
        words = cleaned.split()
        if not words:
            return ""
        return " ".join(words)

    # ── FAISS Semantic Search ─────────────────────────────────

    def _semantic_search(self, query: str, limit: int) -> List[Dict]:
        """
        Semantic search via FAISS index.
        Returns best-chunk-per-doc with chunk text and heading context.
        """
        if not self.model:
            return []

        query_emb = self.model.encode(query).astype(np.float32)

        # Search FAISS for top candidates
        search_limit = limit * 5  # Over-fetch for doc dedup
        faiss_results = self.faiss_index.search(query_emb, top_k=search_limit)

        if not faiss_results:
            # Fallback: build index from DB if empty
            self._ensure_faiss_loaded()
            faiss_results = self.faiss_index.search(query_emb, top_k=search_limit)

        if not faiss_results:
            return []

        # Fetch chunk metadata and deduplicate by doc_id (best chunk per doc)
        chunk_ids = [cid for cid, _ in faiss_results]
        score_map = {cid: score for cid, score in faiss_results}

        doc_best: Dict[int, Dict] = {}  # doc_id → best result

        with self.db.cursor() as c:
            placeholders = ",".join("?" for _ in chunk_ids)
            c.execute(
                "SELECT ce.chunk_id, ce.doc_id, ce.chunk_text, ce.heading_context,"
                "       d.path, d.agent, d.sigil, d.decay_score"
                " FROM chunk_embeddings ce"
                " JOIN documents d ON d.doc_id = ce.doc_id"
                " WHERE ce.chunk_id IN ({})".format(placeholders),
                chunk_ids,
            )

            for row in c.fetchall():
                cid = row["chunk_id"]
                did = row["doc_id"]
                sim = score_map.get(cid, 0.0)

                if did not in doc_best or sim > doc_best[did]["similarity"]:
                    doc_best[did] = {
                        "doc_id": did,
                        "chunk_id": cid,
                        "path": row["path"],
                        "agent": row["agent"],
                        "sigil": row["sigil"],
                        "similarity": sim,
                        "chunk_text": row["chunk_text"],
                        "heading_context": row["heading_context"] or "",
                        "decay_score": row["decay_score"] or 1.0,
                    }

        results = sorted(doc_best.values(), key=lambda x: x["similarity"], reverse=True)
        return results[:limit * 3]

    def _ensure_faiss_loaded(self) -> None:
        """Load FAISS index from DB if not already loaded."""
        if self.faiss_index.count > 0:
            return

        chunk_ids = []
        embeddings = []

        with self.db.cursor() as c:
            c.execute("SELECT chunk_id, embedding FROM chunk_embeddings")
            for row in c.fetchall():
                vec = np.frombuffer(row["embedding"], dtype=np.float32)
                if vec.shape[0] == self.config.embedding_dim:
                    chunk_ids.append(row["chunk_id"])
                    embeddings.append(vec)

        if chunk_ids:
            all_vecs = np.array(embeddings, dtype=np.float32)
            self.faiss_index.build_from_vectors(chunk_ids, all_vecs)
            logger.info("FAISS index loaded from DB: %d vectors", len(chunk_ids))

    # ── Cross-Encoder Re-Ranking ──────────────────────────────

    def _rerank(self, query: str, candidates: List[Dict]) -> List[Dict]:
        """
        Re-rank candidates using a cross-encoder.

        Cross-encoders score (query, passage) pairs directly — much more
        accurate than bi-encoder similarity, but slower. We use it as a
        second pass on the top candidates from the fast first pass.
        """
        if not self.reranker or not candidates:
            return candidates

        # Prepare pairs for the cross-encoder
        pairs = []
        for c in candidates:
            passage = c.get("chunk_text", "")
            heading = c.get("heading_context", "")
            # Prepend heading context so the re-ranker knows the section
            if heading:
                passage = f"[{heading}] {passage}"
            pairs.append([query, passage])

        try:
            scores = self.reranker.predict(pairs)

            for i, c in enumerate(candidates):
                c["rerank_score"] = float(scores[i])

            # Sort by cross-encoder score
            candidates.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
        except Exception as e:
            logger.warning("Re-ranking failed: %s — falling back to RRF scores", e)

        return candidates

    # ── Reciprocal Rank Fusion ─────────────────────────────────

    def _rrf_merge(
        self,
        fts_results: List[Dict],
        semantic_results: List[Dict],
        limit: int,
    ) -> List[Dict]:
        """
        Reciprocal Rank Fusion: merge FTS5 and semantic results.
        RRF(d) = Σ  1 / (k + rank_i(d))
        """
        k = self.config.rrf_k
        doc_scores: Dict[int, Dict] = {}

        for rank, r in enumerate(fts_results, start=1):
            did = r["doc_id"]
            if did not in doc_scores:
                doc_scores[did] = {
                    "doc_id": did,
                    "path": r["path"],
                    "agent": r["agent"],
                    "sigil": r["sigil"],
                    "rrf_score": 0.0,
                    "decay_score": r.get("decay_score", 1.0),
                    "fts_rank": rank,
                    "sem_rank": None,
                    "chunk_text": r.get("chunk_text", ""),
                    "heading_context": r.get("heading_context", ""),
                }
            doc_scores[did]["rrf_score"] += self.config.fts_weight / (k + rank)

        for rank, r in enumerate(semantic_results, start=1):
            did = r["doc_id"]
            if did not in doc_scores:
                doc_scores[did] = {
                    "doc_id": did,
                    "path": r["path"],
                    "agent": r["agent"],
                    "sigil": r["sigil"],
                    "rrf_score": 0.0,
                    "decay_score": r.get("decay_score", 1.0),
                    "fts_rank": None,
                    "sem_rank": rank,
                    "chunk_text": r.get("chunk_text", ""),
                    "heading_context": r.get("heading_context", ""),
                }
            doc_scores[did]["rrf_score"] += self.config.semantic_weight / (k + rank)
            if doc_scores[did]["sem_rank"] is None:
                doc_scores[did]["sem_rank"] = rank
            # Carry forward chunk_text from semantic results (FTS doesn't have it)
            if r.get("chunk_text") and not doc_scores[did].get("chunk_text"):
                doc_scores[did]["chunk_text"] = r["chunk_text"]
                doc_scores[did]["heading_context"] = r.get("heading_context", "")

        for d in doc_scores.values():
            d["final_score"] = d["rrf_score"] * d["decay_score"]

        ranked = sorted(doc_scores.values(), key=lambda x: x["final_score"], reverse=True)
        return ranked[:limit]

    # ── Context Window Budgeting ──────────────────────────────

    def _budget_results(self, results: List[Dict], query: str) -> List[Dict]:
        """
        Trim results to fit within context_budget_tokens.
        Returns as many results as fit within the token budget.
        """
        budget = self.config.context_budget_tokens
        if budget <= 0:
            return results

        budgeted = []
        total_tokens = 0

        for r in results:
            chunk_text = r.get("chunk_text", "")
            heading = r.get("heading_context", "")
            # Estimate tokens for this result's contribution to context
            entry_text = f"{heading}: {chunk_text}" if heading else chunk_text
            entry_tokens = self._count_tokens(entry_text)

            if total_tokens + entry_tokens > budget and budgeted:
                # Would exceed budget — stop
                break

            total_tokens += entry_tokens
            r["token_count"] = entry_tokens
            budgeted.append(r)

        return budgeted

    # ── Public API ─────────────────────────────────────────────

    def retrieve(
        self,
        query: str,
        limit: int = 5,
        agent_id: Optional[str] = None,
        update_access: bool = True,
        budget_tokens: bool = True,
    ) -> List[Dict]:
        """
        Hybrid retrieval: FTS5 + FAISS semantic, RRF fusion, cross-encoder re-rank,
        context budgeting.

        Pipeline:
        1. FTS5 keyword search → top candidates
        2. FAISS semantic search → top candidates
        3. RRF fusion → merged rankings
        4. Cross-encoder re-rank → precision refinement
        5. Context budgeting → fit within token limit

        Returns list of ranked results with scores and chunk text.
        """
        rerank_k = self.config.reranker_top_k if self.config.reranker_enabled else limit

        # Step 1-2: Dual retrieval
        fts_results = self._fts_search(query, rerank_k)
        semantic_results = self._semantic_search(query, rerank_k)

        # Step 3: RRF merge
        merged = self._rrf_merge(fts_results, semantic_results, rerank_k)

        # Step 4: Cross-encoder re-rank
        if self.config.reranker_enabled and self.reranker:
            merged = self._rerank(query, merged)
            merged = merged[:self.config.reranker_final_k]
        else:
            merged = merged[:limit]

        # Optional agent filter
        if agent_id:
            merged = [r for r in merged if r["agent"] == agent_id or r["agent"] == "unknown"]

        # Step 5: Context budgeting
        if budget_tokens:
            merged = self._budget_results(merged, query)

        # Format output and update access counts
        import os
        results = []
        for r in merged:
            results.append({
                "doc_id": r["doc_id"],
                "path": r["path"],
                "filename": os.path.basename(r["path"]),
                "agent": r["agent"],
                "sigil": r["sigil"],
                "score": round(r.get("rerank_score", r.get("final_score", 0)), 4),
                "fts_rank": r.get("fts_rank"),
                "sem_rank": r.get("sem_rank"),
                "chunk_text": r.get("chunk_text", ""),
                "heading_context": r.get("heading_context", ""),
                "token_count": r.get("token_count", 0),
            })

            if update_access:
                with self.db.cursor() as c:
                    c.execute(
                        """UPDATE documents
                           SET access_count = access_count + 1, last_accessed = ?
                           WHERE doc_id = ?""",
                        (time.time(), r["doc_id"]),
                    )

        return results

    def search_episodic(
        self,
        query: str,
        agent_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict]:
        """Search episodic events via FTS5."""
        safe_q = self._sanitize_fts_query(query)
        if not safe_q:
            return []

        results = []
        with self.db.cursor() as c:
            if agent_id:
                c.execute("""
                    SELECT ef.event_id, ef.agent_id, ef.content, e.event_type,
                           e.task_id, e.thread_id, e.created_at
                    FROM episodic_fts ef
                    JOIN episodic_events e ON e.event_id = ef.event_id
                    WHERE episodic_fts MATCH ? AND ef.agent_id = ?
                    ORDER BY rank
                    LIMIT ?
                """, (safe_q, agent_id, limit))
            else:
                c.execute("""
                    SELECT ef.event_id, ef.agent_id, ef.content, e.event_type,
                           e.task_id, e.thread_id, e.created_at
                    FROM episodic_fts ef
                    JOIN episodic_events e ON e.event_id = ef.event_id
                    WHERE episodic_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (safe_q, limit))

            for row in c.fetchall():
                results.append(dict(row))

        return results

    def search_learnings(
        self,
        query: str,
        agent_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict]:
        """Search write-back learnings via FTS5."""
        safe_q = self._sanitize_fts_query(query)
        if not safe_q:
            return []

        results = []
        with self.db.cursor() as c:
            if agent_id:
                c.execute("""
                    SELECT lf.learning_id, lf.agent_id, lf.content, lf.category,
                           l.confidence, l.created_at, l.access_count
                    FROM learnings_fts lf
                    JOIN learnings l ON l.learning_id = lf.learning_id
                    WHERE learnings_fts MATCH ? AND lf.agent_id = ?
                          AND l.superseded_by IS NULL
                    ORDER BY rank
                    LIMIT ?
                """, (safe_q, agent_id, limit))
            else:
                c.execute("""
                    SELECT lf.learning_id, lf.agent_id, lf.content, lf.category,
                           l.confidence, l.created_at, l.access_count
                    FROM learnings_fts lf
                    JOIN learnings l ON l.learning_id = lf.learning_id
                    WHERE learnings_fts MATCH ?
                          AND l.superseded_by IS NULL
                    ORDER BY rank
                    LIMIT ?
                """, (safe_q, limit))

            for row in c.fetchall():
                results.append(dict(row))

        return results
