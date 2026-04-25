import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { DEFAULT_AGENT_ID, DEFAULT_VAULT_PATH, DEFAULT_WORKSPACE_ID } from "./config.js";
import { formatRecall, learnMemory, recallMemory, statusAndAudit } from "./sovereign.js";
import { auditTail, recordAudit, searchVaultNotes, vaultFirstLearn, writeVaultPage } from "./vault.js";

function textResult(text: string) {
  return {
    content: [{ type: "text" as const, text }],
  };
}

const server = new McpServer({
  name: "sovereign-memory",
  version: "0.1.0",
});

server.registerTool(
  "sovereign_status",
  {
    title: "Sovereign Memory Status",
    description: "Check Sovereign daemon, AFM health, Codex vault, and audit state.",
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
  "sovereign_recall",
  {
    title: "Sovereign Memory Recall",
    description: "Recall Sovereign Memory context and log the lookup in the Codex vault.",
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
      tool: "sovereign_recall",
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
  "sovereign_learn",
  {
    title: "Sovereign Memory Learn",
    description: "Write a Codex vault note first, then store the learning through Sovereign Memory.",
    inputSchema: {
      title: z.string().min(1),
      content: z.string().min(1),
      category: z.string().optional(),
      source: z.string().optional(),
      agentId: z.string().optional(),
      workspaceId: z.string().optional(),
      vaultPath: z.string().optional(),
    },
  },
  async ({ title, content, category, source, agentId, workspaceId, vaultPath }) => {
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
  "sovereign_vault_write",
  {
    title: "Sovereign Vault Write",
    description: "Write a structured Codex Obsidian vault page without storing it as a durable learning.",
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
  "sovereign_audit_tail",
  {
    title: "Sovereign Audit Tail",
    description: "Show recent Sovereign Memory audit entries from the Codex vault.",
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
