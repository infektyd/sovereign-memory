import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";

import { buildAfmChatPayload, prepareOutcome, callAfmPrepareTask, prepareTask } from "../dist/task.js";

const vaultMatch = {
  notePath: "/tmp/vault/wiki/sessions/backend-handoff.md",
  relativePath: "wiki/sessions/backend-handoff.md",
  wikilink: "[[wiki/sessions/backend-handoff]]",
  title: "Backend handoff",
  snippet: "Plugin backend is stable; frontend should wait until retrieval ranking is deeper.",
  score: 77,
};

function vaultSource(overrides = {}) {
  return {
    notePath: "/tmp/vault/wiki/sessions/20260425-backend-handoff.md",
    relativePath: "wiki/sessions/20260425-backend-handoff.md",
    wikilink: "[[wiki/sessions/20260425-backend-handoff]]",
    title: "Backend handoff",
    snippet: "Plugin backend is stable; frontend should wait until retrieval ranking is deeper.",
    score: 20,
    ...overrides,
  };
}

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
  assert.equal(packet.profile, "standard");
  assert.equal(packet.budgetTokens, 30000);
  assert.equal(packet.budget.tokens, 30000);
  assert.equal(packet.intent, "implement");
  assert.equal(packet.relevantSources[0].wikilink, "[[wiki/sessions/backend-handoff]]");
  assert.ok(packet.relevantSources[0].reasons.includes("lexical match"));
  assert.match(packet.constraints.join("\n"), /Do not run AFM extraction/);
  assert.match(packet.contextMarkdown, /Sovereign Task Packet/);
  assert.equal(packet.afm.used, false);
  assert.equal(audits[0].tool, "sovereign_prepare_task");
});

test("prepareTask ranks fresh handoff notes above older sessions and explains why", async () => {
  const packet = await prepareTask(
    {
      task: "what is the latest backend handoff before frontend dashboard work",
      vaultPath: "/tmp/vault",
    },
    {
      searchVault: async () => [
        vaultSource({
          relativePath: "wiki/sessions/20240101-old-frontend-note.md",
          wikilink: "[[wiki/sessions/20240101-old-frontend-note]]",
          title: "Old frontend note",
          snippet: "Frontend dashboard can be explored later.",
          score: 40,
        }),
        vaultSource({
          relativePath: "wiki/sessions/20260425-codex-sovereign-memory-plugin-backend-handoff-clean.md",
          wikilink: "[[wiki/sessions/20260425-codex-sovereign-memory-plugin-backend-handoff-clean]]",
          title: "Codex Sovereign Memory plugin backend handoff clean",
          snippet: "Frontend/dashboard work should wait until the plugin backend stabilizes further.",
          score: 18,
        }),
      ],
      recall: async () => ({ ok: true, data: { results: "daemon lead" } }),
      audit: async () => "/tmp/vault/logs/today.md",
    },
  );

  assert.match(packet.relevantSources[0].relativePath, /20260425-codex-sovereign-memory-plugin-backend-handoff-clean/);
  assert.equal(packet.relevantSources[0].freshness, "fresh");
  assert.equal(packet.relevantSources[0].authority, "handoff");
  assert.ok(packet.relevantSources[0].reasons.includes("fresh handoff"));
  assert.match(packet.constraints.join("\n"), /Frontend\/dashboard work should wait/);
});

test("prepareTask profile budgets shape source counts and snippets", async () => {
  const manySources = Array.from({ length: 8 }, (_, index) =>
    vaultSource({
      relativePath: `wiki/sessions/2026042${index}-source-${index}.md`,
      wikilink: `[[wiki/sessions/2026042${index}-source-${index}]]`,
      title: `Source ${index}`,
      snippet: `Source ${index} `.repeat(80),
      score: 15 + index,
    }),
  );
  const deps = {
    searchVault: async () => manySources,
    recall: async () => ({ ok: true, data: { results: "daemon lead" } }),
    audit: async () => "/tmp/vault/logs/today.md",
  };

  const compact = await prepareTask({ task: "rank context", profile: "compact", vaultPath: "/tmp/vault" }, deps);
  const deep = await prepareTask({ task: "rank context", profile: "deep", vaultPath: "/tmp/vault" }, deps);

  assert.equal(compact.budgetTokens, 1500);
  assert.equal(deep.budgetTokens, 12000);
  assert.ok(compact.relevantSources.length < deep.relevantSources.length);
  assert.ok(compact.relevantSources[0].snippet.length < deep.relevantSources[0].snippet.length);
});

test("AFM payload omits blocked and private sources while keeping safe source reasons", () => {
  const payload = buildAfmChatPayload({
    task: "prepare public-safe context",
    budgetTokens: 1500,
    model: "apple-foundation-models",
    profile: "compact",
    relevantSources: [
      {
        wikilink: "[[safe]]",
        snippet: "Safe public context.",
        score: 10,
        privacyLevel: "safe",
        reasons: ["lexical match"],
      },
      {
        wikilink: "[[private]]",
        snippet: "Private session token should not cross the AFM boundary.",
        score: 99,
        privacyLevel: "private",
        reasons: ["private signal"],
      },
      {
        wikilink: "[[blocked]]",
        snippet: "api key secret raw log",
        score: 100,
        privacyLevel: "blocked",
        reasons: ["blocked sensitive content"],
      },
    ],
  });
  const body = JSON.stringify(payload);

  assert.match(body, /Safe public context/);
  assert.match(body, /lexical match/);
  assert.doesNotMatch(body, /Private session token/);
  assert.doesNotMatch(body, /api key secret/);
});

test("prepareOutcome returns a dry-run outcome packet without audit or learning writes", async () => {
  const packet = await prepareOutcome(
    {
      task: "ship AFM prepare task hardening",
      summary: "Added deterministic tests, privacy metadata, and live AFM opt-in checks.",
      changedFiles: ["plugins/sovereign-memory/src/task.ts", "plugins/sovereign-memory/tests/task.test.mjs"],
      verification: ["npm test passed"],
      profile: "compact",
      useAfm: false,
      vaultPath: "/tmp/vault",
    },
    {
      afmPrepare: async () => {
        throw new Error("AFM should not be called when useAfm is false");
      },
    },
  );

  assert.equal(packet.profile, "compact");
  assert.equal(packet.afm.used, false);
  assert.equal(packet.outcomeDraft.learnCandidates.length, 1);
  assert.match(packet.outcomeDraft.logOnly.join("\n"), /npm test passed/);
  assert.match(packet.outcomeDraft.doNotStore.join("\n"), /raw logs/);
});

test("prepareOutcome applies AFM outcome draft suggestions when requested", async () => {
  const packet = await prepareOutcome(
    {
      task: "summarize backend hardening",
      summary: "Prepared source ranking and budget profile changes.",
      profile: "compact",
      useAfm: true,
      vaultPath: "/tmp/vault",
    },
    {
      afmPrepare: async (_url, payload) => {
        assert.equal(payload.purpose, "outcome");
        assert.equal(payload.profile, "compact");
        assert.match(String(payload.summary), /budget profile/);
        return {
          ok: true,
          data: {
            outcomeDraft: {
              learnCandidates: ["Remember that prepare task now has profile-aware ranking."],
              logOnly: ["npm test passed"],
              expires: ["Refresh after next retrieval pass."],
              doNotStore: ["Do not store raw AFM responses."],
            },
          },
        };
      },
    },
  );

  assert.equal(packet.mode, "afm");
  assert.equal(packet.afm.used, true);
  assert.deepEqual(packet.outcomeDraft.learnCandidates, ["Remember that prepare task now has profile-aware ranking."]);
  assert.deepEqual(packet.outcomeDraft.doNotStore, ["Do not store raw AFM responses."]);
});

test("AFM outcome payload uses compact outcome instructions and redacts local paths", () => {
  const payload = buildAfmChatPayload({
    purpose: "outcome",
    task: "summarize backend work",
    summary: "Changed /Users/example/private/repo/file.ts and verified behavior.",
    changedFiles: ["/Users/example/private/repo/file.ts", "plugins/sovereign-memory/src/task.ts"],
    verification: ["npm test passed", "raw log at /Volumes/private/log.txt"],
    profile: "compact",
    budgetTokens: 1500,
    model: "apple-foundation-models",
  });
  const body = JSON.stringify(payload);

  assert.match(body, /Return compact JSON only for Codex outcome prep/);
  assert.match(body, /plugins\/sovereign-memory\/src\/task.ts/);
  assert.doesNotMatch(body, /\/Users\/example/);
  assert.doesNotMatch(body, /\/Volumes\/private/);
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
