"""
Sovereign Memory V3.1 — Write-Back Memory.

Agents don't just read from memory — they write back.

When a Claw discovers something useful during a task (a pattern, a fix,
a decision rationale, a learned preference), it can store that as a
"learning" that persists across sessions.

Learnings are:
- Indexed in FTS5 for keyword search
- Embedded for semantic search
- Categorized (pattern, fix, decision, preference, fact)
- Versioned: new learnings can supersede old ones
- Optionally written to disk as markdown files (for Obsidian integration)
"""

import os
import json
import time
import logging
from typing import Dict, List, Optional
from datetime import datetime

import numpy as np

from config import SovereignConfig, DEFAULT_CONFIG
from db import SovereignDB

logger = logging.getLogger("sovereign.writeback")


# Categories for learnings
CATEGORIES = {
    "pattern":    "Recurring patterns discovered in code, behavior, or data",
    "fix":        "Bug fixes, workarounds, solutions to known problems",
    "decision":   "Decisions made and their rationale",
    "preference": "User or agent preferences learned over time",
    "fact":       "Facts discovered about the codebase, infrastructure, or domain",
    "procedure":  "Step-by-step procedures that worked",
    "general":    "Uncategorized learning",
}


class WriteBackMemory:
    """
    Write-back memory: agents store new learnings for future recall.

    Usage:
        wb = WriteBackMemory(db, config)
        wb.store_learning(
            agent_id="forge",
            content="WebSocket reconnection needs a 500ms backoff before retry",
            category="fix",
            source_query="websocket connection drops",
            source_doc_ids=[42, 67],
        )

        # Later, another agent can find it:
        learnings = wb.recall_learnings("websocket connection issues", limit=5)
    """

    def __init__(
        self,
        db: SovereignDB,
        config: SovereignConfig = DEFAULT_CONFIG,
    ):
        self.db = db
        self.config = config
        self._model = None

    @property
    def model(self):
        """Return the process-wide embedding model singleton."""
        from models import get_embedder
        return get_embedder()

    def store_learning(
        self,
        agent_id: str,
        content: str,
        category: str = "general",
        source_query: Optional[str] = None,
        source_doc_ids: Optional[List[int]] = None,
        confidence: float = 1.0,
        supersedes: Optional[int] = None,
    ) -> int:
        """
        Store a new learning.

        Args:
            agent_id: Which agent discovered this
            content: The learning text
            category: One of CATEGORIES keys
            source_query: The query that led to this learning
            source_doc_ids: Document IDs that informed this learning
            confidence: How confident (0-1) the agent is
            supersedes: learning_id this replaces (versioning)

        Returns:
            learning_id
        """
        if category not in CATEGORIES:
            category = "general"

        now = time.time()
        doc_ids_json = json.dumps(source_doc_ids) if source_doc_ids else None

        # Embed the learning for semantic search
        emb_bytes = None
        if self.model:
            emb = self.model.encode(content).astype(np.float32)
            emb_bytes = emb.tobytes()

        with self.db.cursor() as c:
            c.execute("""
                INSERT INTO learnings
                (agent_id, category, content, source_doc_ids, source_query,
                 confidence, embedding, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                agent_id, category, content, doc_ids_json, source_query,
                confidence, emb_bytes, now,
            ))
            learning_id = c.lastrowid

            # Mark superseded learning
            if supersedes:
                c.execute(
                    "UPDATE learnings SET superseded_by = ? WHERE learning_id = ?",
                    (learning_id, supersedes),
                )

        logger.info(
            "Stored learning #%d [%s/%s]: %.60s...",
            learning_id, agent_id, category, content,
        )

        # Write to disk as markdown (for Obsidian integration)
        if self.config.writeback_enabled:
            self._write_to_disk(learning_id, agent_id, category, content, now)

        return learning_id

    def recall_learnings(
        self,
        query: str,
        agent_id: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict]:
        """
        Recall relevant learnings via hybrid FTS + semantic search.
        Only returns non-superseded learnings.
        """
        results = []

        # FTS5 search
        import re
        safe_q = re.sub(r'[^\w\s\-]', ' ', query)
        words = safe_q.split()
        if not words:
            return results

        fts_query = " ".join(words)

        with self.db.cursor() as c:
            sql = """
                SELECT lf.learning_id, lf.agent_id, lf.content, lf.category,
                       l.confidence, l.created_at, l.access_count,
                       l.source_query, l.source_doc_ids
                FROM learnings_fts lf
                JOIN learnings l ON l.learning_id = lf.learning_id
                WHERE learnings_fts MATCH ?
                      AND l.superseded_by IS NULL
            """
            params = [fts_query]

            if agent_id:
                sql += " AND lf.agent_id = ?"
                params.append(agent_id)
            if category:
                sql += " AND lf.category = ?"
                params.append(category)

            sql += " ORDER BY rank LIMIT ?"
            params.append(limit * 2)

            try:
                c.execute(sql, params)
                for row in c.fetchall():
                    results.append({
                        "learning_id": row["learning_id"],
                        "agent_id": row["agent_id"],
                        "content": row["content"],
                        "category": row["category"],
                        "confidence": row["confidence"],
                        "created_at": datetime.fromtimestamp(row["created_at"]).isoformat(),
                        "access_count": row["access_count"],
                        "source_query": row["source_query"],
                    })
            except Exception as e:
                logger.warning("Learning FTS search failed: %s", e)

        # Semantic search (if we have embeddings)
        if self.model and len(results) < limit:
            semantic_results = self._semantic_search_learnings(
                query, agent_id, category, limit - len(results)
            )
            # Merge, avoiding duplicates
            seen_ids = {r["learning_id"] for r in results}
            for sr in semantic_results:
                if sr["learning_id"] not in seen_ids:
                    results.append(sr)
                    seen_ids.add(sr["learning_id"])

        # Update access counts
        for r in results[:limit]:
            with self.db.cursor() as c:
                c.execute(
                    """UPDATE learnings
                       SET access_count = access_count + 1, last_accessed = ?
                       WHERE learning_id = ?""",
                    (time.time(), r["learning_id"]),
                )

        return results[:limit]

    def _semantic_search_learnings(
        self,
        query: str,
        agent_id: Optional[str],
        category: Optional[str],
        limit: int,
    ) -> List[Dict]:
        """Semantic search over learning embeddings."""
        if not self.model:
            return []

        query_emb = self.model.encode(query).astype(np.float32)
        query_norm = np.linalg.norm(query_emb)
        if query_norm < 1e-8:
            return []
        query_emb = query_emb / query_norm

        results = []
        with self.db.cursor() as c:
            sql = """
                SELECT learning_id, agent_id, category, content, confidence,
                       created_at, access_count, source_query, embedding
                FROM learnings
                WHERE superseded_by IS NULL AND embedding IS NOT NULL
            """
            params = []
            if agent_id:
                sql += " AND agent_id = ?"
                params.append(agent_id)
            if category:
                sql += " AND category = ?"
                params.append(category)

            c.execute(sql, params)
            scored = []
            for row in c.fetchall():
                emb = np.frombuffer(row["embedding"], dtype=np.float32)
                norm = np.linalg.norm(emb)
                if norm < 1e-8:
                    continue
                sim = float(np.dot(query_emb, emb / norm))
                scored.append((sim, row))

            scored.sort(key=lambda x: x[0], reverse=True)
            for sim, row in scored[:limit]:
                results.append({
                    "learning_id": row["learning_id"],
                    "agent_id": row["agent_id"],
                    "content": row["content"],
                    "category": row["category"],
                    "confidence": row["confidence"],
                    "created_at": datetime.fromtimestamp(row["created_at"]).isoformat(),
                    "access_count": row["access_count"],
                    "source_query": row["source_query"],
                    "similarity": round(sim, 4),
                })

        return results

    def _write_to_disk(
        self,
        learning_id: int,
        agent_id: str,
        category: str,
        content: str,
        timestamp: float,
    ) -> None:
        """Write learning as a markdown file to the writeback directory."""
        try:
            dt = datetime.fromtimestamp(timestamp)
            filename = f"{dt.strftime('%Y%m%d_%H%M%S')}_{agent_id}_{category}.md"
            filepath = os.path.join(self.config.writeback_path, filename)

            md_content = f"""---
learning_id: {learning_id}
agent: {agent_id}
category: {category}
created: {dt.isoformat()}
---

# {category.title()}: {content[:80]}

{content}
"""
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(md_content)

            logger.debug("Wrote learning to disk: %s", filepath)
        except Exception as e:
            logger.warning("Failed to write learning to disk: %s", e)

    def get_stats(self) -> Dict:
        """Get write-back memory statistics."""
        with self.db.cursor() as c:
            c.execute("""
                SELECT
                    COUNT(*) as total,
                    COUNT(CASE WHEN superseded_by IS NULL THEN 1 END) as active,
                    COUNT(CASE WHEN superseded_by IS NOT NULL THEN 1 END) as superseded
                FROM learnings
            """)
            row = c.fetchone()
            total = row["total"]
            active = row["active"]

            # Per-category breakdown
            c.execute("""
                SELECT category, COUNT(*) as count
                FROM learnings
                WHERE superseded_by IS NULL
                GROUP BY category
                ORDER BY count DESC
            """)
            categories = {row["category"]: row["count"] for row in c.fetchall()}

            # Per-agent breakdown
            c.execute("""
                SELECT agent_id, COUNT(*) as count
                FROM learnings
                WHERE superseded_by IS NULL
                GROUP BY agent_id
                ORDER BY count DESC
            """)
            agents = {row["agent_id"]: row["count"] for row in c.fetchall()}

        return {
            "total_learnings": total,
            "active": active,
            "superseded": row["superseded"],
            "by_category": categories,
            "by_agent": agents,
        }
