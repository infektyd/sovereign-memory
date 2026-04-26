import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import { buildStatusReport, formatRecall, parseSovrdJson } from "../dist/gemini_cli.js";

test("parseSovrdJson accepts healthy JSON responses", () => {
  assert.deepEqual(parseSovrdJson('{"status":"ok","agent":"shared-daemon"}'), {
    ok: true,
    data: { status: "ok", agent: "shared-daemon" },
  });
});

test("formatRecall returns concise markdown with query and provenance", () => {
  const formatted = formatRecall("socket health", {
    results: "### daemon.md (score=1.000)\nUse /tmp/gemini_cli.sock for local health.",
    agent_id: "gemini_cli",
    layer: "knowledge",
  }, [
    {
      notePath: "/tmp/vault/wiki/sessions/socket-health.md",
      relativePath: "wiki/sessions/socket-health.md",
      wikilink: "[[wiki/sessions/socket-health]]",
      title: "Socket health",
      snippet: "Gemini CLI should check /tmp/gemini_cli.sock before using recall.",
      score: 61,
    },
  ]);

  assert.match(formatted, /Query: socket health/);
  assert.match(formatted, /agent=gemini_cli/);
  assert.match(formatted, /AI Context Pack/);
  assert.match(formatted, /\[\[wiki\/sessions\/socket-health\]\]/);
  assert.match(formatted, /Use \/tmp\/gemini_cli.sock/);
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
