import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  auditTail,
  auditReport,
  ensureVault,
  recordAudit,
  searchVaultNotes,
  vaultFirstLearn,
  writeVaultPage,
} from "../dist/vault.js";

test("ensureVault creates the Gemini Anti Gravity LLM wiki structure and schema", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-vault-"));
  try {
    const result = await ensureVault(root);

    assert.equal(result.vaultPath, root);
    assert.ok(result.created.includes(path.join(root, "raw")));
    assert.ok(result.created.includes(path.join(root, "wiki", "entities")));

    const schema = await readFile(path.join(root, "schema", "AGENTS.md"), "utf8");
    assert.match(schema, /Gemini Anti Gravity Gemini Anti Gravity Memory Vault/);
    assert.match(schema, /raw sources/i);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("vaultFirstLearn writes a note, updates index, and appends audit logs", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-learn-"));
  try {
    const result = await vaultFirstLearn({
      vaultPath: root,
      title: "Socket daemon health check",
      content: "Gemini Anti Gravity Memory daemon health is checked through /tmp/gemini_antigravity.sock.",
      category: "fact",
      source: "unit-test",
      agentId: "gemini_antigravity",
      storeResult: { ok: true, detail: "learned" },
    });

    assert.match(result.notePath, /wiki\/sessions\/\d{8}-socket-daemon-health-check\.md$/);

    const note = await readFile(result.notePath, "utf8");
    assert.match(note, /agent: gemini_antigravity/);
    assert.match(note, /category: fact/);
    assert.match(note, /Gemini Anti Gravity Memory daemon health/);

    const index = await readFile(path.join(root, "index.md"), "utf8");
    assert.match(index, /\[\[wiki\/sessions\/\d{8}-socket-daemon-health-check\]\]/);

    const log = await readFile(path.join(root, "log.md"), "utf8");
    assert.match(log, /gemini_antigravity_learn/);
    assert.match(log, /Socket daemon health check/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("writeVaultPage supports raw and wiki pages without treating them as learnings", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-page-"));
  try {
    const raw = await writeVaultPage({
      vaultPath: root,
      title: "Session Excerpt",
      content: "Immutable session source.",
      section: "raw",
      source: "unit-test",
    });
    const concept = await writeVaultPage({
      vaultPath: root,
      title: "Recall Transparency",
      content: "Memory tools should show what they read and write.",
      section: "concepts",
      source: "unit-test",
    });

    assert.match(raw.notePath, /raw\/\d{8}-session-excerpt\.md$/);
    assert.match(concept.notePath, /wiki\/concepts\/recall-transparency\.md$/);

    const rawNote = await readFile(raw.notePath, "utf8");
    assert.match(rawNote, /immutable: true/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("auditTail returns recent audit entries from daily logs", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-audit-"));
  try {
    await ensureVault(root);
    await recordAudit(root, {
      tool: "gemini_antigravity_status",
      summary: "status checked",
      details: { socket: "ok" },
    });

    const tail = await auditTail(root, 5);

    assert.equal(tail.entries.length, 1);
    assert.match(tail.text, /gemini_antigravity_status/);
    assert.match(tail.text, /status checked/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("auditReport summarizes recent tool activity", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-audit-report-"));
  try {
    await ensureVault(root);
    await recordAudit(root, {
      tool: "gemini_antigravity_recall",
      summary: "recall checked",
      details: { ok: true },
    });
    await recordAudit(root, {
      tool: "gemini_antigravity_learning_quality",
      summary: "quality checked",
      details: { ok: true },
    });

    const report = await auditReport(root, 10);

    assert.equal(report.entries, 2);
    assert.equal(report.tools.gemini_antigravity_recall, 1);
    assert.equal(report.tools.gemini_antigravity_learning_quality, 1);
    assert.deepEqual(report.recentSummaries, [
      "gemini_antigravity_recall: recall checked",
      "gemini_antigravity_learning_quality: quality checked",
    ]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("searchVaultNotes ranks Gemini Anti Gravity wiki learnings for recall context", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "sm-search-"));
  try {
    await vaultFirstLearn({
      vaultPath: root,
      title: "Gemini Anti Gravity plugin full suite marker",
      content: "SM_SEARCH_MARKER confirms vault-first learning is visible to AI recall context packs.",
      category: "fact",
      source: "unit-test",
      agentId: "gemini_antigravity",
      storeResult: { ok: true },
    });
    await writeVaultPage({
      vaultPath: root,
      title: "Unrelated concept",
      content: "This note discusses a different subject.",
      section: "concepts",
      source: "unit-test",
    });

    const results = await searchVaultNotes(root, "SM_SEARCH_MARKER AI recall context", 3);

    assert.equal(results.length, 1);
    assert.match(results[0].relativePath, /wiki\/sessions\/\d{8}-gemini-anti-gravity-plugin-full-suite-marker\.md$/);
    assert.match(results[0].wikilink, /\[\[wiki\/sessions\/\d{8}-gemini-anti-gravity-plugin-full-suite-marker\]\]/);
    assert.match(results[0].snippet, /SM_SEARCH_MARKER/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
