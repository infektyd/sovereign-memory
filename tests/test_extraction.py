"""Tests for local extraction normalization."""

from sovereign_memory.extraction import extract_json_from_text, normalize_extractions
from sovereign_memory.extraction.local_model_client import LocalModelClient


def test_extract_json_from_fenced_response():
    parsed = extract_json_from_text(
        """Here is the JSON:
```json
{"summary": "ok", "memories": [{"claim": "Agents use local memory.", "confidence": 0.9}]}
```
"""
    )
    assert parsed["summary"] == "ok"
    assert parsed["memories"][0]["claim"] == "Agents use local memory."


def test_normalize_extractions_clamps_and_defaults():
    result = normalize_extractions(
        {
            "summary": "example",
            "memories": [
                {
                    "claim": "Identity files load whole.",
                    "category": "decision",
                    "confidence": 2,
                    "durability": "durable",
                    "extra": "kept",
                },
                {"content": "Invalid category falls back.", "category": "weird"},
                {"category": "fact"},
            ],
        },
        source="session.md",
    )

    assert result.summary == "example"
    assert len(result.memories) == 2
    assert result.memories[0].confidence == 1.0
    assert result.memories[0].metadata == {"extra": "kept"}
    assert result.memories[0].source == "session.md"
    assert result.memories[1].category == "general"


def test_local_model_client_extracts_message_content():
    data = {"choices": [{"message": {"content": "{\"memories\": []}"}}]}
    assert LocalModelClient._message_content(data) == "{\"memories\": []}"
