"""Sovereign Memory V3.1 — Agent modules."""

__all__ = ["SovereignAgent"]


def __getattr__(name):
    if name == "SovereignAgent":
        from .agent_api import SovereignAgent
        return SovereignAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
