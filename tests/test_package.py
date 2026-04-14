"""Tests for Sovereign Memory package structure and imports."""

import os
import sys
import pytest

# Ensure src is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestPackageImports:
    """Verify lazy imports work without numpy/faiss installed."""

    def test_import_config(self):
        from sovereign_memory.core.config import SovereignConfig, DEFAULT_CONFIG
        assert SovereignConfig is not None
        assert DEFAULT_CONFIG is not None

    def test_config_defaults(self):
        from sovereign_memory.core.config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG.embedding_dim == 384
        assert DEFAULT_CONFIG.chunk_strategy == "markdown"
        assert DEFAULT_CONFIG.fts_weight == 0.35

    def test_config_sovereign_home(self):
        from sovereign_memory.core.config import _sovereign_home
        home = _sovereign_home()
        assert isinstance(home, str)
        assert len(home) > 0

    def test_config_sovereign_home_env_override(self, monkeypatch):
        from sovereign_memory.core.config import _sovereign_home
        monkeypatch.setenv("SOVEREIGN_HOME", "/tmp/test_sovereign")
        assert _sovereign_home() == "/tmp/test_sovereign"

    def test_import_package(self):
        import sovereign_memory
        assert sovereign_memory.__version__ == "3.1.0"

    def test_lazy_sovereign_agent(self):
        from sovereign_memory import SovereignAgent
        assert SovereignAgent is not None

    def test_lazy_core_modules(self):
        from sovereign_memory.core import SovereignDB, MarkdownChunker
        assert SovereignDB is not None
        assert MarkdownChunker is not None


class TestChunker:
    """Test the markdown-aware chunker in isolation."""

    def test_chunker_init(self):
        from sovereign_memory.core.chunker import MarkdownChunker
        from sovereign_memory.core.config import DEFAULT_CONFIG
        chunker = MarkdownChunker(DEFAULT_CONFIG)
        assert chunker.chunk_size == 384
        assert chunker.strategy == "markdown"

    def test_chunk_simple_doc(self):
        from sovereign_memory.core.chunker import MarkdownChunker
        from sovereign_memory.core.config import DEFAULT_CONFIG
        chunker = MarkdownChunker(DEFAULT_CONFIG)

        doc = """# Title

This is a simple paragraph.

## Section One

Some content under section one.

## Section Two

More content under section two.
"""
        chunks = chunker.chunk_document(doc)
        assert len(chunks) >= 2
        assert chunks[0].heading_path != "" or chunks[0].text != ""

    def test_chunk_empty_doc(self):
        from sovereign_memory.core.chunker import MarkdownChunker
        from sovereign_memory.core.config import DEFAULT_CONFIG
        chunker = MarkdownChunker(DEFAULT_CONFIG)
        chunks = chunker.chunk_document("")
        # Should handle gracefully
        assert isinstance(chunks, list)


class TestIdentityTemplates:
    """Test the identity template system."""

    def test_list_templates(self):
        from sovereign_memory.identities import list_templates
        templates = list_templates()
        assert isinstance(templates, list)

    def test_example_template_exists(self):
        from sovereign_memory.identities import get_template
        # The _example template should NOT be listed (starts with _)
        # but should still be loadable
        template = get_template("_example")
        assert template is not None
        assert "identity" in template
        assert "soul" in template

    def test_nonexistent_template(self):
        from sovereign_memory.identities import get_template
        template = get_template("nonexistent_agent_xyz")
        assert template is None


class TestCLI:
    """Test CLI module loads without errors."""

    def test_cli_module_import(self):
        from sovereign_memory.cli import main
        assert callable(main)

    def test_cli_no_args(self):
        """CLI with no args should exit cleanly (help printed)."""
        from sovereign_memory.cli import main
        with pytest.raises(SystemExit) as exc_info:
            sys.argv = ["sovereign-memory"]
            main()
        assert exc_info.value.code == 0
