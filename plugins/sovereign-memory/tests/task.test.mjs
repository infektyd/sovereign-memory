import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";

import { callAfmPrepareTask, prepareTask } from "../dist/task.js";

const vaultMatch = {
  notePath: "/tmp/vault/wiki/sessions/backend-handoff.md",
  relativePath: "wiki/sessions/backend-handoff.md",
  wikilink: "[[wiki/sessions/backend-handoff]]",
  title: "Backend handoff",
  snippet: "Plugin backend is stable; frontend should wait until retrieval ranking is deeper.",
  score: 77,
};

test("prepareTask builds a compact deterministic Codex task packet", async () => {
  const audits = [];
  const packet = await prepareTask(
    {
      task: "implement AFM context-light prepare task without frontend",
      budgetTokens: 30000,
      vaultPath: "/tmp/vault",
      useAfm: false,
    },
    {
      searchVault: async () => [vaultMatch],
      recall: async () => ({
        ok: true,
        data: {
          results: "### daemon.md\nUse vault-first context packs before daemon recall.",
          agent_id: "codex",
        },
      }),
      audit: async (_vaultPath, entry) => {
        audits.push(entry);
        return "/tmp/vault/logs/today.md";
      },
    },
  );

  assert.equal(packet.mode, "deterministic");
  assert.equal(packet.budgetTokens, 30000);
  assert.equal(packet.intent, "implement");
  assert.equal(packet.relevantSources[0].wikilink, "[[wiki/sessions/backend-handoff]]");
  assert.match(packet.constraints.join("\n"), /Do not run AFM extraction/);
  assert.match(packet.contextMarkdown, /Sovereign Task Packet/);
  assert.equal(packet.afm.used, false);
  assert.equal(audits[0].tool, "sovereign_prepare_task");
});

test("prepareTask uses AFM distillation when requested and available", async () => {
  const packet = await prepareTask(
    {
      task: "plan context-light memory ranking",
      vaultPath: "/tmp/vault",
      useAfm: true,
    },
    {
      searchVault: async () => [vaultMatch],
      recall: async () => ({ ok: true, data: { results: "daemon lead" } }),
      afmPrepare: async () => ({
        ok: true,
        data: {
          brief: "AFM distilled brief.",
          recommendedNextActions: ["Ship prepare_task evals."],
          risks: ["Ranking regressions need tests."],
        },
      }),
      audit: async () => "/tmp/vault/logs/today.md",
    },
  );

  assert.equal(packet.mode, "afm");
  assert.equal(packet.afm.used, true);
  assert.equal(packet.brief, "AFM distilled brief.");
  assert.deepEqual(packet.recommendedNextActions, ["Ship prepare_task evals."]);
  assert.equal(packet.relevantSources[0].relativePath, "wiki/sessions/backend-handoff.md");
});

test("prepareTask falls back when AFM distillation fails", async () => {
  const packet = await prepareTask(
    {
      task: "debug prepare task",
      vaultPath: "/tmp/vault",
      useAfm: true,
    },
    {
      searchVault: async () => [],
      recall: async () => ({ ok: false, error: "socket offline" }),
      afmPrepare: async () => ({ ok: false, error: "AFM offline" }),
      audit: async () => "/tmp/vault/logs/today.md",
    },
  );

  assert.equal(packet.mode, "deterministic");
  assert.equal(packet.afm.requested, true);
  assert.equal(packet.afm.used, false);
  assert.equal(packet.afm.error, "AFM offline");
  assert.equal(packet.recall.daemonOk, false);
  assert.match(packet.currentState.join("\n"), /socket offline/);
});

test("callAfmPrepareTask parses v0 chat-completions JSON content", async () => {
  const server = await new Promise((resolve) => {
    const srv = createServer((req, res) => {
      assert.equal(req.method, "POST");
      assert.equal(req.url, "/v1/chat/completions");
      let body = "";
      req.on("data", (chunk) => {
        body += chunk;
      });
      req.on("end", () => {
        const payload = JSON.parse(body);
        assert.equal(payload.model, "apple-foundation-models");
        assert.ok(Array.isArray(payload.messages));
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            choices: [
              {
                message: {
                  role: "assistant",
                  content: "{\"brief\":\"chat distilled\",\"recommendedNextActions\":[\"Run live test\"]}",
                },
              },
            ],
          }),
        );
      });
    });
    srv.listen(0, "127.0.0.1", () => resolve(srv));
  });

  try {
    const address = server.address();
    const result = await callAfmPrepareTask(`http://127.0.0.1:${address.port}/v1/chat/completions`, {
      task: "test v0 adapter",
      model: "apple-foundation-models",
    });

    assert.equal(result.ok, true);
    assert.equal(result.data.brief, "chat distilled");
    assert.deepEqual(result.data.recommendedNextActions, ["Run live test"]);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});
