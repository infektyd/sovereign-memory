"""Sovereign Memory V3.1 — Agent identity templates."""

import os
from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent / "templates"


def list_templates() -> list[str]:
    """List available agent identity templates."""
    if not TEMPLATES_DIR.exists():
        return []
    return [d.name for d in TEMPLATES_DIR.iterdir() if d.is_dir() and not d.name.startswith("_")]


def get_template(agent_name: str) -> dict[str, str] | None:
    """Load identity template files for an agent name.

    Returns dict with 'identity' and 'soul' keys, or None if not found.
    """
    template_dir = TEMPLATES_DIR / agent_name
    if not template_dir.exists():
        return None

    result = {}
    identity_file = template_dir / "IDENTITY.md"
    soul_file = template_dir / "SOUL.md"

    if identity_file.exists():
        result["identity"] = identity_file.read_text()
    if soul_file.exists():
        result["soul"] = soul_file.read_text()

    return result if result else None
