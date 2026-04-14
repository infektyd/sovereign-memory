"""Integration tests for SovereignAgent and core operations.

Uses a temporary SQLite DB and mocks the embedding model to avoid
requiring heavy ML dependencies in CI.
"""

import os
import sys
import time
import tempfile
import shutil
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sovereign_memory.core.config import SovereignConfig
from sovereign_memory.core.db import SovereignDB
from sovereign_memory.agents.agent_api import SovereignAgent


@pytest.fixture
def tmp_dir():
    """Create a temporary directory for test data."""
    d = tempfile.mkdtemp(prefix="sovereign_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def config(tmp_dir):
    """Config pointing at a temporary directory."""
    return SovereignConfig(
        db_path=os.path.join(tmp_dir, "test.db"),
        vault_path=os.path.join(tmp_dir, "vault"),
        graph_export_dir=os.path.join(tmp_dir, "graphs"),
        faiss_index_path=os.path.join(tmp_dir, "test.faiss"),
        writeback_path=os.path.join(tmp_dir, "learnings"),
        reranker_enabled=False,
    )


@pytest.fixture
def db(config):
    """A SovereignDB instance backed by a temp file."""
    database = SovereignDB(config)
    yield database
    database.close()


@pytest.fixture
def agent(config, db):
    """A SovereignAgent for testing."""
    return SovereignAgent("test_agent", config=config, db=db)


def _mock_embedding(dim=384):
    """Return a random normalized embedding."""
    vec = np.random.randn(dim).astype(np.float32)
    vec /= np.linalg.norm(vec)
    return vec


def _index_document(db, config, path, content, agent="unknown", whole_document=0):
    """Helper: insert a document + chunk into the DB."""
    now = time.time()
    embedding = _mock_embedding(config.embedding_dim)

    with db.cursor() as c:
        c.execute("""
            INSERT INTO documents (path, agent, sigil, whole_document, last_modified, indexed_at, decay_score)
            VALUES (?, ?, '📄', ?, ?, ?, 1.0)
        """, (path, agent, whole_document, now, now))
        doc_id = c.lastrowid

        c.execute("""
            INSERT INTO chunk_embeddings (doc_id, chunk_index, chunk_text, embedding, heading_context, computed_at)
            VALUES (?, 0, ?, ?, 'Test heading', ?)
        """, (doc_id, content, embedding.tobytes(), now))
        chunk_id = c.lastrowid

        c.execute("""
            INSERT INTO vault_fts (doc_id, path, content, agent, sigil)
            VALUES (?, ?, ?, ?, '📄')
        """, (doc_id, path, content, agent))

    return doc_id, chunk_id


class TestSovereignAgentRecall:
    """Test SovereignAgent.recall() end-to-end."""

    def test_recall_returns_list(self, agent, db, config):
        """recall() should return a list of dicts (even if empty)."""
        _index_document(db, config, "/test/doc1.md", "websocket architecture patterns")

        mock_model = MagicMock()
        mock_model.encode.return_value = _mock_embedding(config.embedding_dim)
        agent.retrieval._model = mock_model
        results = agent.recall("websocket")

        assert isinstance(results, list)

    def test_recall_empty_db(self, agent):
        """recall() on empty DB should return empty list."""
        with patch.object(agent.retrieval, '_model', False):
            results = agent.recall("anything")
        assert results == []


class TestSovereignAgentLog:
    """Test SovereignAgent.log() (episodic event logging)."""

    def test_log_returns_event_id(self, agent):
        """log() should return an integer event_id."""
        event_id = agent.log("query", "searched for websocket patterns")
        assert isinstance(event_id, int)
        assert event_id > 0

    def test_log_with_metadata(self, agent):
        """log() with metadata should store successfully."""
        event_id = agent.log(
            "finding",
            "found relevant doc",
            metadata={"source": "vault", "score": 0.95},
        )
        assert isinstance(event_id, int)

    def test_log_persists_to_db(self, agent, db):
        """Logged events should be queryable from the DB."""
        agent.log("test_event", "test content")
        with db.cursor() as c:
            c.execute("SELECT * FROM episodic_events WHERE agent_id = ?", ("test_agent",))
            rows = c.fetchall()
        assert len(rows) == 1
        assert rows[0]["event_type"] == "test_event"
        assert rows[0]["content"] == "test content"


class TestSovereignAgentTasks:
    """Test SovereignAgent.start_task() and end_task()."""

    def test_start_task_returns_task_id(self, agent):
        """start_task() should return a string task_id."""
        task_id = agent.start_task("indexing vault")
        assert isinstance(task_id, str)
        assert len(task_id) > 0

    def test_start_task_with_explicit_id(self, agent):
        """start_task() with explicit task_id should use that id."""
        task_id = agent.start_task("indexing vault", task_id="my-task-001")
        assert task_id == "my-task-001"

    def test_start_task_generates_uuid(self, agent):
        """start_task() without task_id should generate a UUID."""
        task_id = agent.start_task("some task")
        # UUID format: 8-4-4-4-12 hex chars
        assert len(task_id) == 36
        assert task_id.count("-") == 4

    def test_complete_task(self, agent, db):
        """end_task() should update the task status in DB."""
        task_id = agent.start_task("test task")
        agent.end_task(task_id, status="completed", result="done")

        with db.cursor() as c:
            c.execute("SELECT * FROM task_logs WHERE task_id = ?", (task_id,))
            row = c.fetchone()
        assert row is not None
        assert row["status"] == "completed"
        assert row["result"] == "done"

    def test_task_lifecycle(self, agent, db):
        """Full start → end lifecycle should work."""
        task_id = agent.start_task("lifecycle test")

        with db.cursor() as c:
            c.execute("SELECT status FROM task_logs WHERE task_id = ?", (task_id,))
            assert c.fetchone()["status"] == "running"

        agent.end_task(task_id, "completed", "success")

        with db.cursor() as c:
            c.execute("SELECT status FROM task_logs WHERE task_id = ?", (task_id,))
            assert c.fetchone()["status"] == "completed"


class TestIdentityContext:
    """Test identity_context() and startup_context()."""

    def test_identity_context_empty(self, agent):
        """identity_context() with no identity docs should return empty string."""
        result = agent.identity_context()
        assert result == ""

    def test_identity_context_with_docs(self, agent, db, config):
        """identity_context() should return identity docs marked whole_document=1."""
        _index_document(
            db, config,
            "/identities/test_agent/IDENTITY.md",
            "I am a test agent. I help with testing.",
            agent=f"identity:test_agent",
            whole_document=1,
        )
        result = agent.identity_context()
        assert "Agent Identity" in result
        assert "test agent" in result.lower() or "test_agent" in result

    def test_startup_context_no_data(self, agent):
        """startup_context() with empty DB should return a fallback message."""
        result = agent.startup_context()
        assert "No prior context" in result

    def test_startup_context_with_docs(self, agent, db, config):
        """startup_context() should list agent-tagged documents."""
        _index_document(
            db, config,
            "/vault/architecture.md",
            "Websocket architecture notes",
            agent="test_agent",
            whole_document=0,
        )
        result = agent.startup_context()
        assert "Prior Context" in result
        assert "architecture.md" in result


class TestThreadOperations:
    """Test thread create/get/link operations."""

    def test_create_thread_returns_id(self, agent):
        """create_thread() should return a thread_id string."""
        tid = agent.create_thread("Test conversation")
        assert isinstance(tid, str)
        assert len(tid) > 0

    def test_create_thread_with_explicit_id(self, agent):
        """create_thread() with explicit thread_id should use it."""
        tid = agent.create_thread("Test", thread_id="my-thread-001")
        assert tid == "my-thread-001"

    def test_get_thread(self, agent):
        """get_thread() should return thread context dict."""
        tid = agent.create_thread("Test conversation")
        result = agent.get_thread(tid)
        assert isinstance(result, dict)
        assert result["thread_id"] == tid
        assert result["title"] == "Test conversation"

    def test_get_thread_not_found(self, agent):
        """get_thread() for nonexistent thread should return error dict."""
        result = agent.get_thread("nonexistent-id")
        assert "error" in result

    def test_link_thread_doc(self, agent, db, config):
        """link_thread_doc() should create a thread-document link."""
        tid = agent.create_thread("Test thread")
        doc_id, _ = _index_document(db, config, "/test/doc.md", "test content")
        agent.link_thread_doc(tid, doc_id, 0.85)

        with db.cursor() as c:
            c.execute(
                "SELECT * FROM thread_doc_links WHERE thread_id = ? AND doc_id = ?",
                (tid, doc_id),
            )
            row = c.fetchone()
        assert row is not None
        assert abs(row["similarity"] - 0.85) < 0.001


class TestSchemaWholeDocument:
    """Test that the whole_document column exists and works."""

    def test_whole_document_column_exists(self, db):
        """The documents table should have a whole_document column."""
        with db.cursor() as c:
            c.execute("PRAGMA table_info(documents)")
            columns = {row["name"] for row in c.fetchall()}
        assert "whole_document" in columns

    def test_whole_document_default(self, db):
        """whole_document should default to 0."""
        with db.cursor() as c:
            c.execute("""
                INSERT INTO documents (path, agent) VALUES ('/test/default.md', 'test')
            """)
            c.execute("SELECT whole_document FROM documents WHERE path = '/test/default.md'")
            assert c.fetchone()["whole_document"] == 0

    def test_whole_document_set_to_one(self, db):
        """whole_document can be set to 1 for identity docs."""
        with db.cursor() as c:
            c.execute("""
                INSERT INTO documents (path, agent, whole_document)
                VALUES ('/identity/IDENTITY.md', 'identity:test', 1)
            """)
            c.execute("SELECT whole_document FROM documents WHERE path = '/identity/IDENTITY.md'")
            assert c.fetchone()["whole_document"] == 1
