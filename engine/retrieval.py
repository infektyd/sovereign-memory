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
from typing import List, Dict, Literal, Optional, Sequence

import numpy as np

from config import SovereignConfig, DEFAULT_CONFIG
from db import SovereignDB
from faiss_index import FAISSIndex

logger = logging.getLogger("sovereign.retrieval")

# Valid depth tiers for progressive disclosure.
DepthTier = Literal["headline", "snippet", "chunk", "document"]
_VALID_DEPTHS = {"headline", "snippet", "chunk", "document"}
_SNIPPET_MAX_CHARS = 280

# Page type → source authority mapping
_TYPE_TO_AUTHORITY = {
    "schema": "schema",
    "handoff": "handoff",
    "decision": "decision",
    "session": "session",
    "concept": "concept",
    "procedure": "procedure",
    "artifact": "artifact",
    "entity": "vault",
    "synthesis": "concept",
}


def _page_type_to_authority(page_type: Optional[str], agent: str) -> Optional[str]:
    """Map a page_type string to a source_authority value."""
    if page_type:
        return _TYPE_TO_AUTHORITY.get(page_type.lower(), "vault")
    if agent.startswith("wiki:"):
        sub = agent[5:]
        return _TYPE_TO_AUTHORITY.get(sub, "vault")
    if agent.startswith("identity:"):
        return "schema"
    return "vault"


def _path_to_wikilink(path: str) -> Optional[str]:
    """Convert an absolute path to a [[wikilink]] style reference."""
    if not path:
        return None
    import os
    # Strip extension and produce a relative-ish wiki link
    name = os.path.splitext(os.path.basename(path))[0]
    # Try to extract a wiki-relative path
    for marker in ("/wiki/", "/raw/", "/schema/"):
        idx = path.find(marker)
        if idx >= 0:
            rel = path[idx + 1:]  # e.g. wiki/concepts/foo
            rel_noext = os.path.splitext(rel)[0]
            return f"[[{rel_noext}]]"
    return f"[[{name}]]"


def _parse_evidence_refs(raw) -> Optional[list]:
    """Parse evidence_refs field (stored as JSON string or None)."""
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw
    import json
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass
    # Try comma-split as fallback
    if isinstance(raw, str) and raw.strip():
        return [s.strip() for s in raw.split(",") if s.strip()]
    return None


def _recommended_action(
    status: Optional[str],
    instruction_like: Optional[bool],
    confidence: Optional[float],
) -> str:
    """Heuristic recommended action for the consuming agent."""
    if instruction_like:
        return "escalate"
    if status in ("superseded", "rejected", "expired"):
        return "ignore"
    if status == "draft":
        return "follow_up"
    if confidence is not None and confidence < 0.2:
        return "follow_up"
    return "cite"


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
        if self._reranker is not None:
            return self._reranker
        from models import get_cross_encoder
        self._reranker = get_cross_encoder()
        return self._reranker

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
                       rank AS bm25_rank, d.decay_score,
                       d.page_status, d.privacy_level, d.page_type,
                       d.evidence_refs, d.indexed_at, d.layer
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
                    "page_status": row["page_status"] or "candidate",
                    "privacy_level": row["privacy_level"] or "safe",
                    "page_type": row["page_type"],
                    "evidence_refs": row["evidence_refs"],
                    "indexed_at": row["indexed_at"],
                    "layer": row["layer"] or "knowledge",
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
                       d.path, d.agent, d.sigil, d.decay_score,
                       d.page_status, d.privacy_level, d.page_type,
                       d.evidence_refs, d.indexed_at,
                       COALESCE(ce.layer, d.layer, 'knowledge') AS layer
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
                        "page_status": row["page_status"] or "candidate",
                        "privacy_level": row["privacy_level"] or "safe",
                        "page_type": row["page_type"],
                        "evidence_refs": row["evidence_refs"],
                        "indexed_at": row["indexed_at"],
                        "layer": row["layer"] or "knowledge",
                    }

        results = sorted(doc_best.values(), key=lambda x: x["similarity"], reverse=True)
        return results[:limit * 3]

    def _ensure_faiss_loaded(self) -> None:
        """
        Load FAISS index from DB if not already loaded.

        PR-2: Attempt disk cache first (cold-start <500ms).
        On miss, rebuild from DB then save to disk.
        """
        if self.faiss_index.count > 0:
            return

        # PR-2: Try disk cache first
        try:
            conn = self.db._get_conn()
            if self.faiss_index.try_load_from_disk(db_conn=conn):
                return
        except Exception as e:
            logger.debug("Disk cache load failed (non-fatal): %s", e)

        # Rebuild from DB
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

            # PR-2: Save to disk for next cold start
            try:
                conn = self.db._get_conn()
                self.faiss_index.save_to_disk(db_conn=conn)
            except Exception as e:
                logger.debug("FAISS disk save failed (non-fatal): %s", e)

    # ── Cross-Encoder Re-Ranking ──────────────────────────────

    def _rerank(self, query: str, candidates: List[Dict]) -> List[Dict]:
        """
        Re-rank candidates using a cross-encoder.

        Cross-encoders score (query, passage) pairs directly — much more
        accurate than bi-encoder similarity, but slower. We use it as a
        second pass on the top candidates from the fast first pass.
        """
        reranker = self.reranker
        if not reranker or not candidates:
            return candidates

        model_name, model_version = self._reranker_identity(reranker)
        try:
            from rerank_cache import GLOBAL_RERANK_CACHE
            cache = GLOBAL_RERANK_CACHE
        except Exception:
            cache = None

        missing = []
        missing_indexes = []
        all_scores = [None] * len(candidates)
        if cache is not None:
            for i, c in enumerate(candidates):
                chunk_id = c.get("chunk_id")
                if chunk_id is None:
                    missing.append(c)
                    missing_indexes.append(i)
                    continue
                cached = cache.get(model_name, model_version, query, int(chunk_id))
                if cached is None:
                    missing.append(c)
                    missing_indexes.append(i)
                else:
                    all_scores[i] = cached
        else:
            missing = candidates
            missing_indexes = list(range(len(candidates)))

        if not missing:
            for i, c in enumerate(candidates):
                c["rerank_score"] = float(all_scores[i])
            candidates.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
            return candidates

        # Prepare pairs for the cross-encoder
        pairs = []
        for c in missing:
            passage = c.get("chunk_text", "")
            heading = c.get("heading_context", "")
            # Prepend heading context so the re-ranker knows the section
            if heading:
                passage = f"[{heading}] {passage}"
            pairs.append([query, passage])

        try:
            scores = reranker.predict(pairs)

            for score_index, c, score_value in zip(missing_indexes, missing, scores):
                score = float(score_value)
                all_scores[score_index] = score
                chunk_id = c.get("chunk_id")
                if cache is not None and chunk_id is not None:
                    cache.set(model_name, model_version, query, int(chunk_id), score)

            for i, c in enumerate(candidates):
                c["rerank_score"] = float(all_scores[i] or 0.0)

            # Sort by cross-encoder score
            candidates.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
        except Exception as e:
            logger.warning("Re-ranking failed: %s — falling back to RRF scores", e)

        return candidates

    def _reranker_identity(self, reranker) -> tuple[str, str]:
        model_name = getattr(self.config, "reranker_model", None) or "unknown"
        for attr in ("model_name", "name"):
            value = getattr(reranker, attr, None)
            if value:
                model_name = str(value)
                break
        model_version = "unknown"
        for attr in ("model_version", "version", "revision"):
            value = getattr(reranker, attr, None)
            if value:
                model_version = str(value)
                break
        return model_name, model_version

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
                    "page_status": r.get("page_status", "candidate"),
                    "privacy_level": r.get("privacy_level", "safe"),
                    "page_type": r.get("page_type"),
                    "evidence_refs": r.get("evidence_refs"),
                    "indexed_at": r.get("indexed_at"),
                    "layer": r.get("layer", "knowledge"),
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
                    "page_status": r.get("page_status", "candidate"),
                    "privacy_level": r.get("privacy_level", "safe"),
                    "page_type": r.get("page_type"),
                    "evidence_refs": r.get("evidence_refs"),
                    "indexed_at": r.get("indexed_at"),
                    "layer": r.get("layer", "knowledge"),
                }
            doc_scores[did]["rrf_score"] += self.config.semantic_weight / (k + rank)
            if doc_scores[did]["sem_rank"] is None:
                doc_scores[did]["sem_rank"] = rank
            # Carry forward chunk_text from semantic results (FTS doesn't have it)
            if r.get("chunk_text") and not doc_scores[did].get("chunk_text"):
                doc_scores[did]["chunk_text"] = r["chunk_text"]
                doc_scores[did]["heading_context"] = r.get("heading_context", "")
            # Carry forward page metadata from semantic if FTS didn't provide it
            if not doc_scores[did].get("page_type") and r.get("page_type"):
                doc_scores[did]["page_type"] = r["page_type"]
            if not doc_scores[did].get("evidence_refs") and r.get("evidence_refs"):
                doc_scores[did]["evidence_refs"] = r["evidence_refs"]
            if not doc_scores[did].get("indexed_at") and r.get("indexed_at"):
                doc_scores[did]["indexed_at"] = r["indexed_at"]
            if not doc_scores[did].get("layer") and r.get("layer"):
                doc_scores[did]["layer"] = r["layer"]

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

        # Helper: add all PR-2 envelope fields (additive — present in all tiers)
        # Does NOT override provenance if already built by the caller.
        def _pr2_fields(result: Dict, existing: Optional[Dict] = None) -> Dict:
            fields = {
                "confidence": result.get("confidence"),
                "rationale": result.get("rationale"),
                "privacy_level": result.get("privacy_level"),
                "source_authority": result.get("source_authority"),
                "review_state": result.get("review_state"),
                "instruction_like": result.get("instruction_like"),
                "wikilink": result.get("wikilink"),
                "evidence_refs": result.get("evidence_refs"),
                "recommended_action": result.get("recommended_action"),
                "recommended_wiki_updates": result.get("recommended_wiki_updates") or [],
            }
            # Only include provenance if not already in existing dict
            if existing is None or "provenance" not in existing:
                fields["provenance"] = result.get("provenance")
            return fields

        if depth == "headline":
            out = {
                "source": result.get("source", ""),
                "filename": result.get("filename", ""),
                "score": result.get("score", 0),
                "doc_id": result.get("doc_id"),
                "confidence": result.get("confidence"),
                "age_days": result.get("age_days"),
                "layer": result.get("layer"),
                "depth": "headline",
            }
            out.update(_pr2_fields(result, out))
            return out

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
                "layer": result.get("layer"),
                "depth": "snippet",
            }
            # Keep token_count if already computed
            if "token_count" in result:
                out["token_count"] = result["token_count"]
            out.update(_pr2_fields(result, out))
            return out

        if depth in ("chunk", "document"):
            built_prov = result.get("provenance") or {
                "fts_rank": result.get("fts_rank"),
                "semantic_rank": result.get("sem_rank"),
                "rrf_score": result.get("rrf_score"),
                "cross_encoder_score": result.get("rerank_score"),
                "decay_factor": result.get("decay_score"),
                "doc_id": result.get("doc_id"),
                "chunk_id": result.get("chunk_id"),
                "agent_origin": result.get("agent", ""),
                "backend": "faiss-disk",
            }
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
                "layer": result.get("layer"),
                "fts_rank": result.get("fts_rank"),
                "sem_rank": result.get("sem_rank"),
                "provenance": built_prov,
                "depth": depth,
            }
            if "token_count" in result:
                out["token_count"] = result["token_count"]
            out.update(_pr2_fields(result, out))
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

    def _resolve_backends(self, backend_names: list) -> list:
        """
        Resolve a list of backend name strings to VectorBackend instances.

        Currently supports: "faiss-disk", "faiss-mem".
        Stubs ("qdrant", "lance") raise ImportError at construction.
        """
        resolved = []
        for name in backend_names:
            if name == "faiss-disk":
                from backends.faiss_disk import FaissDiskBackend
                b = FaissDiskBackend(self.config, self.db)
                resolved.append(b)
            elif name == "faiss-mem":
                from backends.faiss_mem import FaissMemBackend
                b = FaissMemBackend(self.config)
                resolved.append(b)
            elif name == "qdrant":
                from backends.qdrant import QdrantBackend
                b = QdrantBackend(self.config)  # raises ImportError if not installed
                resolved.append(b)
            elif name == "lance":
                from backends.lance import LanceBackend
                b = LanceBackend(self.config)  # raises ImportError if not installed
                resolved.append(b)
            else:
                logger.warning("Unknown backend %r — skipping", name)
        return resolved

    def _hits_to_dicts(self, hits, query_emb: np.ndarray) -> List[Dict]:
        """
        Convert VectorHit list to the dict format used by _rrf_merge.

        Fetches chunk metadata from SQLite to fill path, agent, etc.
        """
        from vector_backend import VectorHit
        if not hits:
            return []

        chunk_ids = [h.chunk_id for h in hits]
        score_map = {h.chunk_id: (h.score, h.backend) for h in hits}
        doc_best: Dict[int, Dict] = {}

        with self.db.cursor() as c:
            placeholders = ",".join("?" * len(chunk_ids))
            c.execute(f"""
                SELECT ce.chunk_id, ce.doc_id, ce.chunk_text, ce.heading_context,
                       d.path, d.agent, d.sigil, d.decay_score,
                       d.page_status, d.privacy_level, d.page_type,
                       d.evidence_refs, d.indexed_at,
                       COALESCE(ce.layer, d.layer, 'knowledge') AS layer
                FROM chunk_embeddings ce
                JOIN documents d ON d.doc_id = ce.doc_id
                WHERE ce.chunk_id IN ({placeholders})
            """, chunk_ids)

            for row in c.fetchall():
                cid = row["chunk_id"]
                did = row["doc_id"]
                sim, bname = score_map.get(cid, (0.0, "unknown"))

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
                        "page_status": row["page_status"] or "candidate",
                        "privacy_level": row["privacy_level"] or "safe",
                        "page_type": row["page_type"],
                        "evidence_refs": row["evidence_refs"],
                        "indexed_at": row["indexed_at"],
                        "layer": row["layer"] or "knowledge",
                        "backend_name": bname,
                    }

        return sorted(doc_best.values(), key=lambda x: x["similarity"], reverse=True)

    def _backend_search(
        self,
        query_emb: np.ndarray,
        limit: int,
        backend,
    ) -> List[Dict]:
        """
        Search using an explicit VectorBackend instance (PR-3 multi-backend path).

        Returns a list of dicts in the same format as _semantic_search(),
        with the 'backend_name' field populated from hit.backend.
        """
        search_k = limit * 5
        hits = backend.search(query_emb, k=search_k, filter=None)

        if not hits:
            return []

        chunk_ids = [h.chunk_id for h in hits]
        score_map = {h.chunk_id: (h.score, h.backend) for h in hits}

        doc_best: Dict[int, Dict] = {}

        with self.db.cursor() as c:
            placeholders = ",".join("?" * len(chunk_ids))
            c.execute(f"""
                SELECT ce.chunk_id, ce.doc_id, ce.chunk_text, ce.heading_context,
                       d.path, d.agent, d.sigil, d.decay_score,
                       d.page_status, d.privacy_level, d.page_type,
                       d.evidence_refs, d.indexed_at,
                       COALESCE(ce.layer, d.layer, 'knowledge') AS layer
                FROM chunk_embeddings ce
                JOIN documents d ON d.doc_id = ce.doc_id
                WHERE ce.chunk_id IN ({placeholders})
            """, chunk_ids)

            for row in c.fetchall():
                cid = row["chunk_id"]
                did = row["doc_id"]
                sim, backend_name = score_map.get(cid, (0.0, "unknown"))

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
                        "page_status": row["page_status"] or "candidate",
                        "privacy_level": row["privacy_level"] or "safe",
                        "page_type": row["page_type"],
                        "evidence_refs": row["evidence_refs"],
                        "indexed_at": row["indexed_at"],
                        "layer": row["layer"] or "knowledge",
                        "backend_name": backend_name,
                    }

        results = sorted(doc_best.values(), key=lambda x: x["similarity"], reverse=True)
        return results[:limit * 3]

    def _rrf_merge_multi(
        self,
        fts_results: List[Dict],
        semantic_results: List[Dict],
        extra_backend_results: List[List[Dict]],
        limit: int,
    ) -> List[Dict]:
        """
        RRF merge: FTS + semantic + Nth backend stream(s).

        Each input is a ranked list. Additional backends each contribute
        with weight = semantic_weight (same as the primary semantic stream).
        """
        k = self.config.rrf_k
        doc_scores: Dict[int, Dict] = {}

        def _add_stream(ranked_list, weight, stream_label):
            for rank, r in enumerate(ranked_list, start=1):
                did = r["doc_id"]
                if did not in doc_scores:
                    doc_scores[did] = {
                        "doc_id": did,
                        "path": r.get("path", ""),
                        "agent": r.get("agent", ""),
                        "sigil": r.get("sigil", ""),
                        "rrf_score": 0.0,
                        "decay_score": r.get("decay_score", 1.0),
                        "fts_rank": None,
                        "sem_rank": None,
                        "chunk_text": r.get("chunk_text", ""),
                        "heading_context": r.get("heading_context", ""),
                        "page_status": r.get("page_status", "candidate"),
                        "privacy_level": r.get("privacy_level", "safe"),
                        "page_type": r.get("page_type"),
                        "evidence_refs": r.get("evidence_refs"),
                        "indexed_at": r.get("indexed_at"),
                        "layer": r.get("layer", "knowledge"),
                        "backend_name": r.get("backend_name", "faiss-disk"),
                    }
                doc_scores[did]["rrf_score"] += weight / (k + rank)
                if stream_label == "fts":
                    doc_scores[did]["fts_rank"] = rank
                elif stream_label == "sem" and doc_scores[did]["sem_rank"] is None:
                    doc_scores[did]["sem_rank"] = rank
                if r.get("chunk_text") and not doc_scores[did].get("chunk_text"):
                    doc_scores[did]["chunk_text"] = r["chunk_text"]
                    doc_scores[did]["heading_context"] = r.get("heading_context", "")
                if not doc_scores[did].get("page_type") and r.get("page_type"):
                    doc_scores[did]["page_type"] = r["page_type"]
                if not doc_scores[did].get("evidence_refs") and r.get("evidence_refs"):
                    doc_scores[did]["evidence_refs"] = r["evidence_refs"]
                if not doc_scores[did].get("indexed_at") and r.get("indexed_at"):
                    doc_scores[did]["indexed_at"] = r["indexed_at"]
                if not doc_scores[did].get("layer") and r.get("layer"):
                    doc_scores[did]["layer"] = r["layer"]

        _add_stream(fts_results, self.config.fts_weight, "fts")
        _add_stream(semantic_results, self.config.semantic_weight, "sem")
        for extra in extra_backend_results:
            _add_stream(extra, self.config.semantic_weight, "extra")

        for d in doc_scores.values():
            d["final_score"] = d["rrf_score"] * d["decay_score"]

        ranked = sorted(doc_scores.values(), key=lambda x: x["final_score"], reverse=True)
        return ranked[:limit]

    def _normalize_layers(self, layers: Optional[Sequence[str]]) -> Optional[set]:
        if layers is None:
            return None
        valid = {"identity", "episodic", "knowledge", "artifact"}
        return {str(layer).lower() for layer in layers if str(layer).lower() in valid}

    def _parse_iso_date(self, value: Optional[str], end_of_day: bool = False) -> Optional[float]:
        if not value:
            return None
        from datetime import datetime, time as dt_time
        text = str(value)
        try:
            if len(text) == 10:
                day = datetime.fromisoformat(text)
                if end_of_day:
                    day = datetime.combine(day.date(), dt_time.max)
                return day.timestamp()
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            logger.warning("Ignoring invalid ISO date filter: %r", value)
            return None

    def _filter_candidates(
        self,
        candidates: List[Dict],
        layers: Optional[Sequence[str]],
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> List[Dict]:
        layer_set = self._normalize_layers(layers)
        start_ts = self._parse_iso_date(start_date)
        end_ts = self._parse_iso_date(end_date, end_of_day=True)
        if not layer_set and start_ts is None and end_ts is None:
            return candidates
        filtered = []
        for r in candidates:
            if layer_set and (r.get("layer") or "knowledge") not in layer_set:
                continue
            created_at = r.get("created_at") or r.get("indexed_at") or 0
            if start_ts is not None and float(created_at or 0) < start_ts:
                continue
            if end_ts is not None and float(created_at or 0) > end_ts:
                continue
            filtered.append(r)
        return filtered

    def _chronological_search(
        self,
        query: str,
        limit: int,
        layers: Optional[Sequence[str]],
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> List[Dict]:
        safe_query = self._sanitize_fts_query(query)
        if not safe_query:
            return []
        layer_set = self._normalize_layers(layers)
        start_ts = self._parse_iso_date(start_date)
        end_ts = self._parse_iso_date(end_date, end_of_day=True)
        params = [safe_query]
        clauses = ["vault_fts MATCH ?"]
        if layer_set:
            placeholders = ",".join("?" * len(layer_set))
            clauses.append(f"COALESCE(ce.layer, d.layer, 'knowledge') IN ({placeholders})")
            params.extend(sorted(layer_set))
        if start_ts is not None:
            clauses.append("COALESCE(d.indexed_at, d.last_modified, ce.computed_at, 0) >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("COALESCE(d.indexed_at, d.last_modified, ce.computed_at, 0) <= ?")
            params.append(end_ts)
        params.append(limit)

        with self.db.cursor() as c:
            c.execute(f"""
                SELECT ce.chunk_id, ce.doc_id, ce.chunk_text, ce.heading_context,
                       d.path, d.agent, d.sigil, d.decay_score,
                       d.page_status, d.privacy_level, d.page_type,
                       d.evidence_refs, d.indexed_at,
                       COALESCE(ce.layer, d.layer, 'knowledge') AS layer,
                       COALESCE(d.indexed_at, d.last_modified, ce.computed_at, 0) AS created_at
                FROM vault_fts f
                JOIN documents d ON d.doc_id = f.doc_id
                JOIN chunk_embeddings ce ON ce.doc_id = d.doc_id
                WHERE {" AND ".join(clauses)}
                ORDER BY created_at ASC, ce.chunk_id ASC
                LIMIT ?
            """, params)
            rows = c.fetchall()

        return [
            {
                "doc_id": row["doc_id"],
                "chunk_id": row["chunk_id"],
                "path": row["path"],
                "agent": row["agent"],
                "sigil": row["sigil"],
                "final_score": 0.0,
                "rrf_score": None,
                "fts_rank": None,
                "sem_rank": None,
                "chunk_text": row["chunk_text"],
                "heading_context": row["heading_context"] or "",
                "decay_score": row["decay_score"] or 1.0,
                "page_status": row["page_status"] or "candidate",
                "privacy_level": row["privacy_level"] or "safe",
                "page_type": row["page_type"],
                "evidence_refs": row["evidence_refs"],
                "indexed_at": row["indexed_at"],
                "created_at": row["created_at"],
                "layer": row["layer"] or "knowledge",
            }
            for row in rows
        ]

    def retrieve(
        self,
        query: str,
        limit: int = 5,
        agent_id: Optional[str] = None,
        update_access: bool = True,
        budget_tokens: bool = True,
        depth: str = "snippet",
        include_superseded: bool = False,
        include_rejected: bool = False,
        include_drafts: bool = False,
        backend=None,
        layers: Optional[Sequence[str]] = None,
        sort: Literal["semantic", "chronological"] = "semantic",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
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
            backend: PR-3 backend override.
                   None (default) — use internal FAISSIndex (bit-identical to pre-PR-3)
                   VectorBackend  — use this backend for semantic search
                   list           — fan-out via MultiBackend, merge with RRF

        Returns list of ranked results filtered to the requested depth tier.
        Existing callers that pass no depth receive identical results (snippet).
        With backend=None (default), results are bit-identical to pre-PR-3.
        """
        if sort not in ("semantic", "chronological"):
            logger.warning("Unknown sort=%r, falling back to 'semantic'", sort)
            sort = "semantic"

        rerank_k = self.config.reranker_top_k if self.config.reranker_enabled else limit

        if sort == "chronological":
            merged = self._chronological_search(query, rerank_k, layers, start_date, end_date)
            merged = merged[:limit]
        else:
            # Step 1-2: Dual retrieval
            fts_results = self._fts_search(query, rerank_k)

            # PR-3: When backend is provided, use it instead of (or in addition to)
            # the internal FAISSIndex.  With backend=None (default), the existing
            # _semantic_search path is taken — bit-identical to pre-PR-3.
            extra_backend_results: List[List[Dict]] = []
            if backend is None:
                # Default path — bit-identical to pre-PR-3
                semantic_results = self._semantic_search(query, rerank_k)
            elif isinstance(backend, list):
                # Fan-out: build a MultiBackend from the list of backend names/objects
                from backends.multi import MultiBackend
                resolved = self._resolve_backends(backend)
                if len(resolved) == 1:
                    semantic_results = self._backend_search(
                        self.model.encode(query).astype(np.float32) if self.model else np.array([]),
                        rerank_k,
                        resolved[0],
                    )
                else:
                    multi = MultiBackend(resolved)
                    query_emb = self.model.encode(query).astype(np.float32) if self.model else np.array([])
                    hits = multi.search(query_emb, k=rerank_k)
                    # Convert VectorHit list to the dict format expected by _rrf_merge
                    semantic_results = self._hits_to_dicts(hits, query_emb)
            else:
                # Single explicit backend object
                query_emb = self.model.encode(query).astype(np.float32) if self.model else np.array([])
                semantic_results = self._backend_search(query_emb, rerank_k, backend)

            # Step 3: RRF merge — with extra streams if multi-backend
            if extra_backend_results:
                merged = self._rrf_merge_multi(
                    fts_results, semantic_results, extra_backend_results, rerank_k
                )
            else:
                merged = self._rrf_merge(fts_results, semantic_results, rerank_k)

            merged = self._filter_candidates(merged, layers, start_date, end_date)

            # Step 4: Cross-encoder re-rank
            if self.config.reranker_enabled and self.reranker:
                merged = self._rerank(query, merged)
                merged = merged[:self.config.reranker_final_k]
            else:
                merged = merged[:limit]

        # Optional agent filter
        if agent_id:
            merged = [r for r in merged if r["agent"] == agent_id or r["agent"] == "unknown"]

        # PR-2: Status lifecycle filtering
        # default: skip superseded, rejected, draft, expired
        # callers can opt back in with include_* kwargs
        _ALWAYS_EXCLUDED = {"blocked"}  # privacy_level=blocked is always excluded
        _SKIP_STATUSES = set()
        if not include_superseded:
            _SKIP_STATUSES.add("superseded")
        if not include_rejected:
            _SKIP_STATUSES.add("rejected")
        if not include_drafts:
            _SKIP_STATUSES.add("draft")
            _SKIP_STATUSES.add("expired")

        if _SKIP_STATUSES or _ALWAYS_EXCLUDED:
            filtered = []
            for r in merged:
                status = r.get("page_status") or "candidate"
                privacy = r.get("privacy_level") or "safe"
                if status in _SKIP_STATUSES:
                    continue
                if privacy in _ALWAYS_EXCLUDED:
                    continue
                filtered.append(r)
            merged = filtered

        # Step 5: Context budgeting
        if budget_tokens:
            merged = self._budget_results(merged, query)

        # Validate depth tier; default to snippet (zero change for existing callers)
        if depth not in _VALID_DEPTHS:
            logger.warning("Unknown depth=%r, falling back to 'snippet'", depth)
            depth = "snippet"

        # Format output and update access counts
        import os
        from safety import is_instruction_like
        from scoring import compute_confidence
        from rationale import explain

        results = []
        for r in merged:
            # Build a rich intermediate dict with all raw fields available.
            # _apply_depth will project it down to the requested tier.
            score = round(r.get("rerank_score", r.get("final_score", 0)), 4)

            # Compute age_days from indexed_at
            indexed_at = r.get("indexed_at")
            age_days: Optional[float] = None
            if indexed_at:
                age_days = round((time.time() - float(indexed_at)) / 86400.0, 1)

            # Compute confidence
            try:
                confidence = compute_confidence(
                    rrf_score=r.get("rrf_score"),
                    cross_encoder_score=r.get("rerank_score"),
                    decay_factor=r.get("decay_score"),
                    db=self.db,
                )
            except Exception:
                confidence = None

            # Detect injection in chunk text
            chunk_text = r.get("chunk_text", "")
            try:
                instr_like = is_instruction_like(chunk_text)
            except Exception:
                instr_like = None

            # Infer page type → source_authority mapping
            page_type = r.get("page_type")
            source_authority = _page_type_to_authority(page_type, r.get("agent", ""))

            # Wikilink from path
            path = r.get("path", "")
            rel_path = path  # full path; callers can relativize if needed
            wikilink = _path_to_wikilink(path)

            # Evidence refs (stored as JSON list or comma string)
            evidence_refs = _parse_evidence_refs(r.get("evidence_refs"))

            # Page status for envelope
            page_status = r.get("page_status") or "candidate"
            privacy_level = r.get("privacy_level") or "safe"

            # Build provenance dict
            provenance = {
                "fts_rank": r.get("fts_rank"),
                "semantic_rank": r.get("sem_rank"),
                "rrf_score": r.get("rrf_score"),
                "cross_encoder_score": r.get("rerank_score"),
                "decay_factor": r.get("decay_score"),
                "agent_origin": r.get("agent", ""),
                "age_days": age_days,
                "doc_id": r["doc_id"],
                "chunk_id": r.get("chunk_id"),
                "backend": "faiss-disk",
            }

            raw = {
                "doc_id": r["doc_id"],
                "chunk_id": r.get("chunk_id"),
                "path": path,
                "source": r.get("path", ""),
                "filename": os.path.basename(path),
                "agent": r["agent"],
                "sigil": r["sigil"],
                "score": score,
                "fts_rank": r.get("fts_rank"),
                "sem_rank": r.get("sem_rank"),
                "rrf_score": r.get("rrf_score"),
                "rerank_score": r.get("rerank_score"),
                "decay_score": r.get("decay_score"),
                "layer": r.get("layer", "knowledge"),
                "chunk_text": chunk_text,
                "heading_context": r.get("heading_context", ""),
                "token_count": r.get("token_count", 0),
                # PR-2 envelope fields
                "confidence": confidence,
                "age_days": age_days,
                "provenance": provenance,
                "privacy_level": privacy_level,
                "source_authority": source_authority,
                "review_state": page_status,
                "instruction_like": instr_like,
                "wikilink": wikilink,
                "evidence_refs": evidence_refs,
                "recommended_action": _recommended_action(page_status, instr_like, confidence),
                "recommended_wiki_updates": [],
            }

            # Rationale is computed after provenance is assembled
            try:
                raw["rationale"] = explain(raw)
            except Exception:
                raw["rationale"] = None

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

    def search(self, *args, **kwargs) -> List[Dict]:
        """Backward-compatible alias for callers that use search() terminology."""
        return self.retrieve(*args, **kwargs)

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
