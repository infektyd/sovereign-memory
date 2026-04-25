"""Data models for structured memory extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExtractedMemory:
    """A single candidate memory claim extracted from text."""

    claim: str
    category: str = "general"
    confidence: float = 0.7
    durability: str = "durable"
    source: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "claim": self.claim,
            "category": self.category,
            "confidence": self.confidence,
            "durability": self.durability,
            "metadata": self.metadata,
        }
        if self.source:
            data["source"] = self.source
        return data


@dataclass
class ExtractionResult:
    """Structured extraction output from a local model bridge."""

    memories: list[ExtractedMemory]
    summary: str = ""
    raw: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "memories": [memory.as_dict() for memory in self.memories],
        }
