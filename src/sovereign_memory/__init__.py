"""
Sovereign Memory V3.1 — Intelligent persistent memory for AI agent orchestration.

Provides hybrid retrieval (FAISS + FTS5), write-back learnings, episodic
event tracking, memory decay, and knowledge graph export.

Quick start:
    from sovereign_memory import SovereignAgent, SovereignConfig

    agent = SovereignAgent("hermes")
    context = agent.startup_context(limit=10)
    results = agent.recall("What is the websocket architecture?")
    agent.close()
"""

from sovereign_memory.core.config import SovereignConfig, DEFAULT_CONFIG

__version__ = "3.1.0"

__all__ = ["SovereignAgent", "SovereignConfig", "DEFAULT_CONFIG"]


def __getattr__(name):
    """Lazy-load SovereignAgent to avoid heavy imports at package-discovery time."""
    if name == "SovereignAgent":
        from sovereign_memory.agents.agent_api import SovereignAgent
        return SovereignAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
