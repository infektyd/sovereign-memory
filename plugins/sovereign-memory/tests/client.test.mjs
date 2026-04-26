import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { buildStatusReport, formatRecall, parseSovrdJson } from "../dist/sovereign.js";

test("parseSovrdJson accepts healthy JSON responses", () => {
  assert.deepEqual(parseSovrdJson('{"status":"ok","agent":"shared-daemon"}'), {
    ok: true,
    data: { status: "ok", agent: "shared-daemon" },
  });
});

test("formatRecall returns concise markdown with query and provenance", () => {
  const formatted = formatRecall("socket health", {
    results: "### daemon.md (score=1.000)\nUse /tmp/sovereign.sock for local health.",
    agent_id: "codex",
    layer: "knowledge",
  }, [
    {
      notePath: "/tmp/vault/wiki/sessions/socket-health.md",
      relativePath: "wiki/sessions/socket-health.md",
      wikilink: "[[wiki/sessions/socket-health]]",
      title: "Socket health",
      snippet: "Codex should check /tmp/sovereign.sock before using recall.",
      score: 61,
    },
  ]);

  assert.match(formatted, /Query: socket health/);
  assert.match(formatted, /agent=codex/);
  assert.match(formatted, /AI Context Pack/);
  assert.match(formatted, /\[\[wiki\/sessions\/socket-health\]\]/);
  assert.match(formatted, /Use \/tmp\/sovereign.sock/);
});

test("formatRecall includes backend badge when recall reports backend provenance", () => {
  const formatted = formatRecall("backend provenance", {
    results: "### result.md (score=1.000)",
    agent_id: "codex",
    backend: "faiss-disk+qdrant",
  });

  assert.match(formatted, /\[faiss-disk\+qdrant\]/);
});

test("buildStatusReport includes vault, socket, AFM, and audit state", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-status-"));
  try {
    const report = await buildStatusReport({
      vaultPath: root,
      socket: { ok: true, data: { status: "ok" } },
      afm: { ok: false, error: "offline" },
    });

    assert.equal(report.vault.exists, true);
    assert.equal(report.socket.ok, true);
    assert.equal(report.afm.ok, false);
    assert.equal(report.audit.entries, 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
