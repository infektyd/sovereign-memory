"""Memory extraction pipeline backed by a local chat model bridge."""

from __future__ import annotations

import json
import re
from typing import Any

from sovereign_memory.extraction.local_model_client import LocalModelClient
from sovereign_memory.extraction.models import ExtractedMemory, ExtractionResult


SYSTEM_PROMPT = (
    "You extract durable memories from conversation or project text for a local "
    "agent memory system. Return only JSON. Prefer precise, source-grounded "
    "claims over broad summaries. Do not include secrets, raw credentials, "
    "private paths, or transient command noise."
)

USER_PROMPT_TEMPLATE = """Extract durable memory candidates from this text.

Return a JSON object with:
- summary: one short sentence
- memories: array of objects with:
  - claim: standalone factual claim
  - category: one of preference, fact, decision, goal, constraint, pattern, fix, procedure, general
  - confidence: number from 0.0 to 1.0
  - durability: durable or ephemeral

Source: {source}

Text:
{text}
"""


def extract_json_from_text(raw: str) -> Any:
    """Parse JSON from direct, fenced, or lightly wrapped model output."""
    text = raw.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fenced:
        try:
            return json.loads(fenced.group(1).strip())
        except json.JSONDecodeError:
            pass

    for pattern in (r"\{[\s\S]*\}", r"\[[\s\S]*\]"):
        match = re.search(pattern, text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
    return None


def normalize_extractions(parsed: Any, *, source: str | None = None, raw: str = "") -> ExtractionResult:
    """Normalize common model response shapes into ExtractionResult."""
    summary = ""
    memories_raw: Any

    if isinstance(parsed, list):
        memories_raw = parsed
    elif isinstance(parsed, dict):
        summary = str(parsed.get("summary", ""))
        memories_raw = (
            parsed.get("memories")
            or parsed.get("facts")
            or parsed.get("claims")
            or parsed.get("items")
            or []
        )
    else:
        memories_raw = []

    memories: list[ExtractedMemory] = []
    if isinstance(memories_raw, list):
        for item in memories_raw:
            if not isinstance(item, dict):
                continue
            claim = str(item.get("claim") or item.get("content") or "").strip()
            if not claim:
                continue
            memories.append(
                ExtractedMemory(
                    claim=claim,
                    category=_clean_category(item.get("category")),
                    confidence=_clean_confidence(item.get("confidence")),
                    durability=_clean_durability(item.get("durability")),
                    source=source,
                    metadata=_metadata_without_core_fields(item),
                )
            )

    return ExtractionResult(memories=memories, summary=summary, raw=raw)


class MemoryExtractor:
    """Extract structured memory candidates using a local model bridge."""

    def __init__(self, client: LocalModelClient | None = None) -> None:
        self.client = client or LocalModelClient()

    def extract_text(
        self,
        text: str,
        *,
        source: str | None = None,
        max_chars: int = 20000,
    ) -> ExtractionResult:
        prompt = USER_PROMPT_TEMPLATE.format(
            source=source or "inline",
            text=text[:max_chars],
        )
        raw = self.client.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
        )
        parsed = extract_json_from_text(raw)
        return normalize_extractions(parsed, source=source, raw=raw)

    def extract_file(self, path: str) -> ExtractionResult:
        with open(path, "r", encoding="utf-8") as handle:
            return self.extract_text(handle.read(), source=path)


def _clean_category(value: Any) -> str:
    category = str(value or "general").strip().lower()
    allowed = {"preference", "fact", "decision", "goal", "constraint", "pattern", "fix", "procedure", "general"}
    return category if category in allowed else "general"


def _clean_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.7
    return max(0.0, min(1.0, confidence))


def _clean_durability(value: Any) -> str:
    durability = str(value or "durable").strip().lower()
    return durability if durability in {"durable", "ephemeral"} else "durable"


def _metadata_without_core_fields(item: dict[str, Any]) -> dict[str, Any]:
    omitted = {"claim", "content", "category", "confidence", "durability"}
    return {key: value for key, value in item.items() if key not in omitted}
