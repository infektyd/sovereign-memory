import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { DEFAULT_AGENT_ID, DEFAULT_VAULT_PATH, DEFAULT_WORKSPACE_ID } from "./config.js";
import { assessLearningQuality, routeMemoryIntent } from "./policy.js";
import { formatRecall, learnMemory, recallMemory, statusAndAudit } from "./gemini_antigravity.js";
import { prepareOutcome, prepareTask } from "./task.js";
import { auditReport, auditTail, recordAudit, searchVaultNotes, vaultFirstLearn, writeVaultPage } from "./vault.js";

function textResult(text: string) {
  return {
    content: [{ type: "text" as const, text }],
  };
}

const server = new McpServer({
  name: "gemini-antigravity",
  version: "0.1.0",
});

server.registerTool(
  "gemini_antigravity_prepare_task",
  {
    title: "Gemini Anti Gravity Prepare Task",
    description:
      "Build a compact Gemini Anti Gravity task packet from vault notes, daemon recall, constraints, and optional AFM distillation.",
    inputSchema: {
      task: z.string().min(1),
      budgetTokens: z.number().int().min(1000).max(32000).optional(),
      profile: z.enum(["compact", "standard", "deep"]).optional(),
      useAfm: z.boolean().optional(),
      layer: z.enum(["identity", "episodic", "knowledge", "artifact"]).optional(),
      limit: z.number().int().min(1).max(12).optional(),
      workspaceId: z.string().optional(),
      agentId: z.string().optional(),
      vaultPath: z.string().optional(),
      includeVault: z.boolean().optional(),
      afmPrepareUrl: z.string().optional(),
      afmModel: z.string().optional(),
    },
  },
  async ({
    task,
    budgetTokens,
    profile,
    useAfm,
    layer,
    limit,
    workspaceId,
    agentId,
    vaultPath,
    includeVault,
    afmPrepareUrl,
    afmModel,
  }) => {
    const packet = await prepareTask({
      task,
      budgetTokens,
      profile,
      useAfm,
      layer,
      limit,
      workspaceId,
      agentId,
      vaultPath,
      includeVault,
      afmPrepareUrl,
      afmModel,
    });
    return textResult(JSON.stringify(packet, null, 2));
  },
);

server.registerTool(
  "gemini_antigravity_prepare_outcome",
  {
    title: "Gemini Anti Gravity Prepare Outcome",
    description:
      "Build a dry-run post-task outcome packet with learn/log/expire/do-not-store recommendations without writing memory.",
    inputSchema: {
      task: z.string().min(1),
      summary: z.string().min(1),
      changedFiles: z.array(z.string()).optional(),
      verification: z.array(z.string()).optional(),
      profile: z.enum(["compact", "standard", "deep"]).optional(),
      useAfm: z.boolean().optional(),
      vaultPath: z.string().optional(),
      afmPrepareUrl: z.string().optional(),
      afmModel: z.string().optional(),
    },
  },
  async ({ task, summary, changedFiles, verification, profile, useAfm, vaultPath, afmPrepareUrl, afmModel }) => {
    const packet = await prepareOutcome({
      task,
      summary,
      changedFiles,
      verification,
      profile,
      useAfm,
      vaultPath,
      afmPrepareUrl,
      afmModel,
    });
    return textResult(JSON.stringify(packet, null, 2));
  },
);

server.registerTool(
  "gemini_antigravity_status",
  {
    title: "Gemini Anti Gravity Memory Status",
    description: "Check Gemini Anti Gravity daemon, AFM health, Gemini Anti Gravity vault, and audit state.",
    inputSchema: {
      vaultPath: z.string().optional(),
    },
  },
  async ({ vaultPath }) => {
    const report = await statusAndAudit(vaultPath ?? DEFAULT_VAULT_PATH);
    return textResult(JSON.stringify(report, null, 2));
  },
);

server.registerTool(
  "gemini_antigravity_route",
  {
    title: "Gemini Anti Gravity Memory Intent Router",
    description: "Classify whether a task should recall, learn, write a vault note, show audit, or do nothing.",
    inputSchema: {
      task: z.string().min(1),
      vaultPath: z.string().optional(),
    },
  },
  async ({ task, vaultPath }) => {
    const intent = routeMemoryIntent(task);
    await recordAudit(vaultPath ?? DEFAULT_VAULT_PATH, {
      tool: "gemini_antigravity_route",
      summary: `${intent.action}: ${task.slice(0, 120)}`,
      details: intent as unknown as Record<string, unknown>,
    });
    return textResult(JSON.stringify(intent, null, 2));
  },
);

server.registerTool(
  "gemini_antigravity_recall",
  {
    title: "Gemini Anti Gravity Memory Recall",
    description: "Recall Gemini Anti Gravity Memory context and log the lookup in the Gemini Anti Gravity vault.",
    inputSchema: {
      query: z.string().min(1),
      layer: z.enum(["identity", "episodic", "knowledge", "artifact"]).optional(),
      limit: z.number().int().min(1).max(20).optional(),
      workspaceId: z.string().optional(),
      agentId: z.string().optional(),
      vaultPath: z.string().optional(),
      includeVault: z.boolean().optional(),
    },
  },
  async ({ query, layer, limit, workspaceId, agentId, vaultPath, includeVault }) => {
    const effectiveVaultPath = vaultPath ?? DEFAULT_VAULT_PATH;
    const vaultResults = includeVault === false ? [] : await searchVaultNotes(effectiveVaultPath, query, Math.min(limit ?? 5, 8));
    const result = await recallMemory({
      query,
      layer,
      limit,
      workspaceId: workspaceId ?? DEFAULT_WORKSPACE_ID,
      agentId: agentId ?? DEFAULT_AGENT_ID,
    });
    const responseText = result.ok && result.data ? formatRecall(query, result.data, vaultResults) : `Recall failed: ${result.error}`;
    await recordAudit(effectiveVaultPath, {
      tool: "gemini_antigravity_recall",
      summary: query,
      details: {
        ok: result.ok,
        layer,
        limit,
        workspaceId,
        agentId,
        includeVault: includeVault !== false,
        vaultMatches: vaultResults.map((match) => match.relativePath),
        error: result.error,
      },
    });
    return textResult(responseText);
  },
);

server.registerTool(
  "gemini_antigravity_learn",
  {
    title: "Gemini Anti Gravity Memory Learn",
    description: "Write a Gemini Anti Gravity vault note first, then store the learning through Gemini Anti Gravity Memory.",
    inputSchema: {
      title: z.string().min(1),
      content: z.string().min(1),
      category: z.string().optional(),
      source: z.string().optional(),
      agentId: z.string().optional(),
      workspaceId: z.string().optional(),
      vaultPath: z.string().optional(),
      requireQuality: z.boolean().optional(),
    },
  },
  async ({ title, content, category, source, agentId, workspaceId, vaultPath, requireQuality }) => {
    const quality = assessLearningQuality({ title, content, category, source });
    if (requireQuality === true && !quality.ok) {
      await recordAudit(vaultPath ?? DEFAULT_VAULT_PATH, {
        tool: "gemini_antigravity_learn",
        summary: `quality-blocked: ${title}`,
        details: { quality },
      });
      return textResult(
        JSON.stringify(
          {
            status: "quality-blocked",
            quality,
          },
          null,
          2,
        ),
      );
    }
    const store = await learnMemory({
      content,
      category,
      agentId: agentId ?? DEFAULT_AGENT_ID,
      workspaceId: workspaceId ?? DEFAULT_WORKSPACE_ID,
    });
    const note = await vaultFirstLearn({
      vaultPath: vaultPath ?? DEFAULT_VAULT_PATH,
      title,
      content,
      category,
      source,
      agentId: agentId ?? DEFAULT_AGENT_ID,
      storeResult: { ok: store.ok, data: store.data, error: store.error },
    });
    return textResult(
      JSON.stringify(
        {
          status: store.ok ? "learned" : "vault-written-memory-store-failed",
          quality,
          note,
          store,
        },
        null,
        2,
      ),
    );
  },
);

server.registerTool(
  "gemini_antigravity_learning_quality",
  {
    title: "Gemini Anti Gravity Learning Quality",
    description: "Review a potential memory before writing it to the Gemini Anti Gravity vault or Gemini Anti Gravity daemon.",
    inputSchema: {
      title: z.string().min(1),
      content: z.string().min(1),
      category: z.string().optional(),
      source: z.string().optional(),
      vaultPath: z.string().optional(),
    },
  },
  async ({ title, content, category, source, vaultPath }) => {
    const quality = assessLearningQuality({ title, content, category, source });
    await recordAudit(vaultPath ?? DEFAULT_VAULT_PATH, {
      tool: "gemini_antigravity_learning_quality",
      summary: title,
      details: { quality },
    });
    return textResult(JSON.stringify(quality, null, 2));
  },
);

server.registerTool(
  "gemini_antigravity_vault_write",
  {
    title: "Gemini Anti Gravity Vault Write",
    description: "Write a structured Gemini Anti Gravity Obsidian vault page without storing it as a durable learning.",
    inputSchema: {
      title: z.string().min(1),
      content: z.string().min(1),
      section: z.enum(["raw", "entities", "concepts", "decisions", "syntheses", "sessions"]),
      source: z.string().optional(),
      vaultPath: z.string().optional(),
    },
  },
  async ({ title, content, section, source, vaultPath }) => {
    const note = await writeVaultPage({
      vaultPath: vaultPath ?? DEFAULT_VAULT_PATH,
      title,
      content,
      section,
      source,
    });
    return textResult(JSON.stringify({ status: "written", note }, null, 2));
  },
);

server.registerTool(
  "gemini_antigravity_audit_report",
  {
    title: "Gemini Anti Gravity Audit Report",
    description: "Summarize recent Gemini Anti Gravity Memory tool activity for transparent self-auditing.",
    inputSchema: {
      limit: z.number().int().min(1).max(200).optional(),
      vaultPath: z.string().optional(),
    },
  },
  async ({ limit, vaultPath }) => {
    const report = await auditReport(vaultPath ?? DEFAULT_VAULT_PATH, limit ?? 100);
    return textResult(JSON.stringify(report, null, 2));
  },
);

server.registerTool(
  "gemini_antigravity_audit_tail",
  {
    title: "Gemini Anti Gravity Audit Tail",
    description: "Show recent Gemini Anti Gravity Memory audit entries from the Gemini Anti Gravity vault.",
    inputSchema: {
      limit: z.number().int().min(1).max(100).optional(),
      vaultPath: z.string().optional(),
    },
  },
  async ({ limit, vaultPath }) => {
    const tail = await auditTail(vaultPath ?? DEFAULT_VAULT_PATH, limit ?? 20);
    return textResult(tail.text || "No audit entries yet.");
  },
);

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
