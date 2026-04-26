import json
import os
from pathlib import Path

import pytest

import sovrd


def _packet(**overrides):
    base = {
        "from_agent": "codex",
        "to_agent": "claude-code",
        "kind": "handoff",
        "task": "Review auth migration",
        "envelope": '<sovereign:context event="Handoff">api_key=secret-token</sovereign:context>',
        "wikilink_refs": ["wiki/decisions/auth-migration"],
        "trace_id": "trace-pr10",
    }
    base.update(overrides)
    return base


def test_daemon_handoff_validates_redacts_and_writes_inbox_outbox(monkeypatch, tmp_path):
    sender = tmp_path / "codex-vault"
    recipient = tmp_path / "claudecode-vault"
    monkeypatch.setenv(
        "SOVEREIGN_AGENT_VAULTS",
        json.dumps({"codex": str(sender), "claude-code": str(recipient)}),
    )

    response = sovrd._dispatch(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "daemon.handoff",
            "params": {
                "from_agent": "codex",
                "to_agent": "claude-code",
                "packet": _packet(),
            },
        }
    )

    assert "error" not in response
    result = response["result"]
    assert result["status"] == "ok"
    assert result["redacted"] is True

    inbox_files = list((recipient / "inbox").glob("*.json"))
    outbox_files = list((sender / "outbox").glob("*.json"))
    handoff_pages = list((sender / "wiki" / "handoffs").glob("*.md"))
    assert len(inbox_files) == 1
    assert len(outbox_files) == 1
    assert len(handoff_pages) == 1

    inbox_packet = json.loads(inbox_files[0].read_text())
    outbox_packet = json.loads(outbox_files[0].read_text())
    assert inbox_packet["envelope"].count("[REDACTED]") >= 1
    assert "secret-token" not in json.dumps(inbox_packet)
    assert inbox_packet == outbox_packet

    page = handoff_pages[0].read_text()
    assert "type: handoff" in page
    assert "status: accepted" in page
    assert "Review auth migration" in page
    assert "[REDACTED]" in page

    assert "handoff_sent" in (sender / "log.md").read_text()
    assert "handoff_received" in (recipient / "log.md").read_text()


def test_daemon_handoff_rejects_invalid_packet(monkeypatch, tmp_path):
    monkeypatch.setenv("SOVEREIGN_AGENT_VAULTS", json.dumps({"codex": str(tmp_path / "codex")}))

    response = sovrd._dispatch(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "daemon.handoff",
            "params": {
                "from_agent": "codex",
                "to_agent": "claude-code",
                "packet": _packet(kind="learn_now"),
            },
        }
    )

    assert response["error"]["code"] == -32602
    assert "kind" in response["error"]["message"]


def test_daemon_handoff_gracefully_reports_missing_destination(monkeypatch, tmp_path):
    monkeypatch.setenv("SOVEREIGN_AGENT_VAULTS", json.dumps({"codex": str(tmp_path / "codex")}))
    monkeypatch.setenv("SOVEREIGN_HANDOFF_CREATE_MISSING_VAULTS", "0")

    response = sovrd._dispatch(
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "daemon.handoff",
            "params": {
                "from_agent": "codex",
                "to_agent": "ghost-agent",
                "packet": _packet(to_agent="ghost-agent"),
            },
        }
    )

    assert "error" not in response
    assert response["result"]["status"] == "degraded"
    assert response["result"]["delivered"] is False
    assert "destination vault" in response["result"]["reason"]
