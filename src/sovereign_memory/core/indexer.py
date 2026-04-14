"""
Sovereign Memory V3.1 — Vault Indexer.

V3.1 changes over V3:
1. Markdown-aware chunking (via chunker.py) instead of blind word-count splitting
2. No compression — embeddings stored as raw float32[384]
3. FAISS index built/updated on each index run
4. Heading context stored per chunk for retrieval enrichment
5. Chunk text stored in full (V3 truncated to 500 chars)
"""

import os
import re
import time
import logging
from typing import Dict, List, Optional, Tuple

import numpy as np

from sovereign_memory.core.config import SovereignConfig, DEFAULT_CONFIG
from sovereign_memory.core.db import SovereignDB
from sovereign_memory.core.chunker import MarkdownChunker
from sovereign_memory.core.faiss_index import FAISSIndex

logger = logging.getLogger("sovereign.indexer")


class VaultIndexer:
    """Index Obsidian vault with markdown-aware chunking and FAISS indexing."""

    def __init__(
        self,
        db: SovereignDB,
        config: SovereignConfig = DEFAULT_CONFIG,
    ):
        self.db = db
        self.config = config
        self.chunker = MarkdownChunker(config)
        self.faiss_index = FAISSIndex(config)
        self._model = None

    @property
    def model(self):
        """Lazy-load sentence transformer."""
        if self._model is None:
            os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self.config.embedding_model)
                logger.info("Model loaded: %s", self.config.embedding_model)
            except ImportError:
                logger.warning("sentence-transformers not installed — semantic search disabled")
                self._model = False  # Sentinel: tried and failed
        return self._model if self._model is not False else None

    # ── Metadata extraction ────────────────────────────────────

    @staticmethod
    def _extract_frontmatter(content: str) -> Dict[str, str]:
        """Extract agent/sigil from YAML-style frontmatter."""
        agent_m = re.search(r"agent:\s*(\w+)", content)
        sigil_m = re.search(r"sigil:\s*(.)", content)
        return {
            "agent": agent_m.group(1) if agent_m else "unknown",
            "sigil": sigil_m.group(1) if sigil_m else "❓",
        }

    # ── Core indexing ──────────────────────────────────────────

    def index_vault(self, verbose: bool = False) -> Dict:
        """
        Full incremental index of the vault.
        Returns stats dict.
        """
        vault = self.config.vault_path
        if not os.path.isdir(vault):
            return {"status": "error", "message": f"Vault not found: {vault}"}

        # Collect current files on disk
        disk_files: Dict[str, float] = {}
        for root, _, files in os.walk(vault):
            for fname in files:
                if fname.endswith(".md"):
                    full = os.path.join(root, fname)
                    disk_files[full] = os.path.getmtime(full)

        stats = {"indexed": 0, "skipped": 0, "deleted": 0, "chunks": 0, "errors": 0}

        with self.db.transaction() as c:
            # Phase 1: Index new/changed files
            for path, mtime in disk_files.items():
                try:
                    c.execute(
                        "SELECT doc_id, last_modified FROM documents WHERE path = ?",
                        (path,),
                    )
                    row = c.fetchone()

                    if row and row["last_modified"] >= mtime:
                        stats["skipped"] += 1
                        continue

                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()

                    meta = self._extract_frontmatter(content)
                    now = time.time()

                    if row:
                        doc_id = row["doc_id"]
                        c.execute(
                            """UPDATE documents
                               SET agent=?, sigil=?, last_modified=?, indexed_at=?
                               WHERE doc_id=?""",
                            (meta["agent"], meta["sigil"], mtime, now, doc_id),
                        )
                        c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (doc_id,))
                        c.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (doc_id,))
                    else:
                        c.execute(
                            """INSERT INTO documents (path, agent, sigil, last_modified, indexed_at)
                               VALUES (?, ?, ?, ?, ?)""",
                            (path, meta["agent"], meta["sigil"], mtime, now),
                        )
                        doc_id = c.lastrowid

                    # FTS5 insert (full content for keyword search)
                    c.execute(
                        """INSERT INTO vault_fts (doc_id, path, content, agent, sigil)
                           VALUES (?, ?, ?, ?, ?)""",
                        (doc_id, path, content, meta["agent"], meta["sigil"]),
                    )

                    # Markdown-aware chunk embeddings
                    if self.model:
                        chunks = self.chunker.chunk_document(content)
                        for chunk in chunks:
                            emb = self.model.encode(chunk.text)
                            emb_bytes = emb.astype(np.float32).tobytes()

                            c.execute(
                                """INSERT INTO chunk_embeddings
                                   (doc_id, chunk_index, chunk_text, embedding,
                                    heading_context, model_name, computed_at)
                                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    doc_id, chunk.chunk_index, chunk.text,
                                    emb_bytes, chunk.heading_path,
                                    self.config.embedding_model, now,
                                ),
                            )
                            stats["chunks"] += 1

                    stats["indexed"] += 1
                    if verbose:
                        n_chunks = len(self.chunker.chunk_document(content)) if self.model else 0
                        logger.info("  ✓ %s [%s/%s] (%d chunks)",
                                    os.path.basename(path),
                                    meta["agent"], meta["sigil"], n_chunks)

                except Exception as e:
                    stats["errors"] += 1
                    if verbose:
                        logger.error("  ✗ %s: %s", path, e)

            # Phase 2: Remove docs no longer on disk
            c.execute("SELECT doc_id, path FROM documents")
            for row in c.fetchall():
                if row["path"] not in disk_files:
                    c.execute("DELETE FROM documents WHERE doc_id = ?", (row["doc_id"],))
                    c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (row["doc_id"],))
                    c.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (row["doc_id"],))
                    stats["deleted"] += 1
                    if verbose:
                        logger.info("  🗑 Removed: %s", row["path"])

        # Phase 3: Rebuild FAISS index from all embeddings
        self._rebuild_faiss_index()

        return {"status": "success", **stats}

    def _rebuild_faiss_index(self) -> None:
        """Rebuild the FAISS index from all chunk embeddings in the DB."""
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
            logger.info("FAISS index rebuilt: %d vectors (%s)",
                        len(chunk_ids), self.faiss_index._current_type)

    def get_faiss_index(self) -> FAISSIndex:
        """Get the current FAISS index (for use by retrieval engine)."""
        if self.faiss_index.count == 0:
            self._rebuild_faiss_index()
        return self.faiss_index

    # ── File watcher ───────────────────────────────────────────

    def start_watcher(self):
        """Start filesystem watcher with debounced re-indexing."""
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        DEBOUNCE_SEC = 5

        class _Handler(FileSystemEventHandler):
            def __init__(self, indexer):
                self._indexer = indexer
                self._last = 0

            def on_any_event(self, event):
                if event.is_directory or not event.src_path.endswith(".md"):
                    return
                now = time.time()
                if now - self._last > DEBOUNCE_SEC:
                    self._last = now
                    logger.info("Change detected: %s", event.src_path)
                    self._indexer.index_vault()

        observer = Observer()
        observer.schedule(_Handler(self), self.config.vault_path, recursive=True)
        observer.start()
        logger.info("Watching %s for changes...", self.config.vault_path)
        return observer
