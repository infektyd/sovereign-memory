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

PR-1b adds progressive disclosure depth tiers to retrieve():
  headline — wikilink, title, score, confidence, age_days (~30 tokens/result)
  snippet  — + text (≤280 chars) (~120 tokens/result)  [DEFAULT — zero change]
  chunk    — + full chunk text, heading_context, full provenance (~500 tokens)
  document — + full source document (whole_document=1 rows only)
"""

import time
import math
import logging
from typing import List, Dict, Literal, Optional

import numpy as np

from config import SovereignConfig, DEFAULT_CONFIG
from db import SovereignDB
from faiss_index import FAISSIndex

logger = logging.getLogger("sovereign.retrieval")

# Valid depth tiers for progressive disclosure.
DepthTier = Literal["headline", "snippet", "chunk", "document"]
_VALID_DEPTHS = {"headline", "snippet", "chunk", "document"}
_SNIPPET_MAX_CHARS = 280


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
        """Return the process-wide embedding model singleton."""
        from models import get_embedder
        return get_embedder()

    @property
    def reranker(self):
        """Return the process-wide cross-encoder singleton."""
        from models import get_cross_encoder
        return get_cross_encoder()

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
            placeholders = ",".join("?" * len(chunk_ids))
            c.execute(f"""
                SELECT ce.chunk_id, ce.doc_id, ce.chunk_text, ce.heading_context,
                       d.path, d.agent, d.sigil, d.decay_score
                FROM chunk_embeddings ce
                JOIN documents d ON d.doc_id = ce.doc_id
                WHERE ce.chunk_id IN ({placeholders})
            """, chunk_ids)

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

    # ── Depth-tier field filtering ─────────────────────────────

    def _apply_depth(self, result: Dict, depth: str) -> Dict:
        """
        Filter a result dict to only the fields appropriate for *depth*.

        headline  — wikilink, title, score, confidence, age_days
        snippet   — + text (≤280 chars) [DEFAULT — matches current callers]
        chunk     — + full chunk text, heading_context, provenance
        document  — chunk + full_document_text (whole_document rows only)
        """
        if depth not in _VALID_DEPTHS:
            depth = "snippet"

        if depth == "headline":
            return {
                "source": result.get("source", ""),
                "filename": result.get("filename", ""),
                "score": result.get("score", 0),
                "doc_id": result.get("doc_id"),
                # confidence and age_days will be populated in PR-2; carry nulls now
                "confidence": result.get("confidence"),
                "age_days": result.get("age_days"),
                "depth": "headline",
            }

        if depth == "snippet":
            text = result.get("chunk_text", "")
            if len(text) > _SNIPPET_MAX_CHARS:
                text = text[:_SNIPPET_MAX_CHARS] + "…"
            out = {
                "text": text,
                "source": result.get("source", ""),
                "filename": result.get("filename", ""),
                "heading": result.get("heading_context", ""),
                "score": result.get("score", 0),
                "doc_id": result.get("doc_id"),
                "depth": "snippet",
            }
            # Keep token_count if already computed
            if "token_count" in result:
                out["token_count"] = result["token_count"]
            return out

        if depth in ("chunk", "document"):
            out = {
                "text": result.get("chunk_text", ""),
                "source": result.get("source", ""),
                "filename": result.get("filename", ""),
                "heading": result.get("heading_context", ""),
                "score": result.get("score", 0),
                "doc_id": result.get("doc_id"),
                "chunk_id": result.get("chunk_id"),
                "agent": result.get("agent", ""),
                "sigil": result.get("sigil", ""),
                "fts_rank": result.get("fts_rank"),
                "sem_rank": result.get("sem_rank"),
                "provenance": {
                    "fts_rank": result.get("fts_rank"),
                    "semantic_rank": result.get("sem_rank"),
                    "rrf_score": result.get("rrf_score"),
                    "cross_encoder_score": result.get("rerank_score"),
                    "decay_factor": result.get("decay_score"),
                    "doc_id": result.get("doc_id"),
                    "chunk_id": result.get("chunk_id"),
                    "agent_origin": result.get("agent", ""),
                    "backend": "faiss-disk",
                },
                "depth": depth,
            }
            if "token_count" in result:
                out["token_count"] = result["token_count"]
            # document tier: add full_document_text if available in result
            if depth == "document" and "full_document_text" in result:
                out["full_document_text"] = result["full_document_text"]
            return out

        # Fallback: snippet
        return self._apply_depth(result, "snippet")

    def _fetch_full_document(self, doc_id: int) -> Optional[str]:
        """Fetch the full concatenated text for a whole_document row."""
        with self.db.cursor() as c:
            c.execute("""
                SELECT chunk_text FROM chunk_embeddings
                WHERE doc_id = ?
                ORDER BY chunk_id
            """, (doc_id,))
            rows = c.fetchall()
        if not rows:
            return None
        return "\n".join(row["chunk_text"] for row in rows)

    # ── Public API ─────────────────────────────────────────────

    def retrieve(
        self,
        query: str,
        limit: int = 5,
        agent_id: Optional[str] = None,
        update_access: bool = True,
        budget_tokens: bool = True,
        depth: str = "snippet",
    ) -> List[Dict]:
        """
        Hybrid retrieval: FTS5 + FAISS semantic, RRF fusion, cross-encoder re-rank,
        context budgeting, with progressive disclosure depth tiers.

        Pipeline:
        1. FTS5 keyword search → top candidates
        2. FAISS semantic search → top candidates
        3. RRF fusion → merged rankings
        4. Cross-encoder re-rank → precision refinement
        5. Context budgeting → fit within token limit
        6. Depth-tier field filtering → progressive disclosure

        Args:
            query: Search query string.
            limit: Maximum results to return (default 5, max 20).
            agent_id: If set, filter results to this agent's documents.
            update_access: Whether to bump access_count in the DB.
            budget_tokens: Whether to apply context window budgeting.
            depth: Progressive disclosure tier.
                   "headline" — minimal fields (~30 tokens/result)
                   "snippet"  — + text (≤280 chars) (~120 tokens) [DEFAULT]
                   "chunk"    — + full chunk text, heading, provenance (~500 tokens)
                   "document" — + full source document (whole_document=1 only)

        Returns list of ranked results filtered to the requested depth tier.
        Existing callers that pass no depth receive identical results (snippet).
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

        # Validate depth tier; default to snippet (zero change for existing callers)
        if depth not in _VALID_DEPTHS:
            logger.warning("Unknown depth=%r, falling back to 'snippet'", depth)
            depth = "snippet"

        # Format output and update access counts
        import os
        results = []
        for r in merged:
            # Build a rich intermediate dict with all raw fields available.
            # _apply_depth will project it down to the requested tier.
            score = round(r.get("rerank_score", r.get("final_score", 0)), 4)
            raw = {
                "doc_id": r["doc_id"],
                "chunk_id": r.get("chunk_id"),
                "path": r["path"],
                "source": r.get("path", ""),
                "filename": os.path.basename(r["path"]),
                "agent": r["agent"],
                "sigil": r["sigil"],
                "score": score,
                "fts_rank": r.get("fts_rank"),
                "sem_rank": r.get("sem_rank"),
                "rrf_score": r.get("rrf_score"),
                "rerank_score": r.get("rerank_score"),
                "decay_score": r.get("decay_score"),
                "chunk_text": r.get("chunk_text", ""),
                "heading_context": r.get("heading_context", ""),
                "token_count": r.get("token_count", 0),
                # Phase 1.2 fields: not yet computed; carry as null for graceful upgrade
                "confidence": None,
                "age_days": None,
            }

            # For document depth, attach full document text if available
            if depth == "document":
                raw["full_document_text"] = self._fetch_full_document(r["doc_id"])

            results.append(self._apply_depth(raw, depth))

            if update_access:
                with self.db.cursor() as c:
                    c.execute(
                        """UPDATE documents
                           SET access_count = access_count + 1, last_accessed = ?
                           WHERE doc_id = ?""",
                        (time.time(), r["doc_id"]),
                    )

        return results

    def expand_result(
        self,
        result_id: int,
        depth: str = "chunk",
        update_access: bool = True,
    ) -> Optional[Dict]:
        """
        Re-fetch a specific result at a deeper depth tier.

        *result_id* may be either a chunk_id or a doc_id; this method tries
        chunk_id first, then falls back to doc_id.

        Args:
            result_id: chunk_id or doc_id from a prior search result.
            depth: Target depth tier ('chunk' or 'document'). Defaults to 'chunk'.
            update_access: Whether to bump access_count on the document.

        Returns:
            A result dict at the requested depth, or None if not found.
        """
        if depth not in _VALID_DEPTHS:
            depth = "chunk"

        import os

        row = None
        with self.db.cursor() as c:
            # Try chunk_id first
            c.execute("""
                SELECT ce.chunk_id, ce.doc_id, ce.chunk_text, ce.heading_context,
                       d.path, d.agent, d.sigil, d.decay_score
                FROM chunk_embeddings ce
                JOIN documents d ON d.doc_id = ce.doc_id
                WHERE ce.chunk_id = ?
            """, (result_id,))
            row = c.fetchone()

        if row is None:
            # Fall back to doc_id: get the best chunk for this document
            with self.db.cursor() as c:
                c.execute("""
                    SELECT ce.chunk_id, ce.doc_id, ce.chunk_text, ce.heading_context,
                           d.path, d.agent, d.sigil, d.decay_score
                    FROM chunk_embeddings ce
                    JOIN documents d ON d.doc_id = ce.doc_id
                    WHERE ce.doc_id = ?
                    ORDER BY ce.chunk_id
                    LIMIT 1
                """, (result_id,))
                row = c.fetchone()

        if row is None:
            return None

        raw = {
            "doc_id": row["doc_id"],
            "chunk_id": row["chunk_id"],
            "path": row["path"],
            "source": row["path"],
            "filename": os.path.basename(row["path"]),
            "agent": row["agent"],
            "sigil": row["sigil"],
            "score": 0.0,
            "fts_rank": None,
            "sem_rank": None,
            "rrf_score": None,
            "rerank_score": None,
            "decay_score": row["decay_score"],
            "chunk_text": row["chunk_text"],
            "heading_context": row["heading_context"] or "",
            "token_count": 0,
            "confidence": None,
            "age_days": None,
        }

        if depth == "document":
            raw["full_document_text"] = self._fetch_full_document(row["doc_id"])

        if update_access:
            with self.db.cursor() as c:
                c.execute(
                    """UPDATE documents
                       SET access_count = access_count + 1, last_accessed = ?
                       WHERE doc_id = ?""",
                    (time.time(), row["doc_id"]),
                )

        return self._apply_depth(raw, depth)

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
