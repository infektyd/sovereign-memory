"""Sovereign Memory V3.1 — Source/index modules."""

__all__ = ["WikiIndexer", "WikiPageParser", "WikiPage", "WikiFrontmatter", "index_all"]


def __getattr__(name):
    if name in ("WikiIndexer", "WikiPageParser", "WikiPage", "WikiFrontmatter"):
        from .wiki_indexer import WikiIndexer, WikiPageParser, WikiPage, WikiFrontmatter
        return locals().get(name)
    if name == "index_all":
        from .index_all import index_all
        return index_all
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
