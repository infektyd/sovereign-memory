import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { existsSync } from "node:fs";
import { URL } from "node:url";
import { AFM_HEALTH_URL, DEFAULT_AGENT_ID, DEFAULT_VAULT_PATH, DEFAULT_WORKSPACE_ID, SOCKET_PATH } from "./config.js";
import { auditTail, ensureVault, recordAudit, vaultExists } from "./vault.js";
import type { VaultSearchResult } from "./vault.js";

export interface JsonResult<T = unknown> {
  ok: boolean;
  data?: T;
  error?: string;
}

export interface RecallResponse {
  results?: string | unknown[];
  agent_id?: string;
  layer?: string;
  workspace_id?: string;
}

export interface StatusReport {
  vault: {
    path: string;
    exists: boolean;
  };
  socket: JsonResult;
  afm: JsonResult;
  audit: {
    entries: number;
    latest?: string;
  };
}

export function parseSovrdJson<T = unknown>(raw: string): JsonResult<T> {
  try {
    return { ok: true, data: JSON.parse(raw) as T };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

function socketRequest(method: "GET" | "POST", endpoint: string, body?: object): Promise<JsonResult> {
  return new Promise((resolve) => {
    if (!existsSync(SOCKET_PATH)) {
      resolve({ ok: false, error: `Socket not found: ${SOCKET_PATH}` });
      return;
    }

    const payload = body ? JSON.stringify(body) : undefined;
    const req = httpRequest(
      {
        socketPath: SOCKET_PATH,
        path: endpoint,
        method,
        headers: {
          "Content-Type": "application/json",
          ...(payload ? { "Content-Length": Buffer.byteLength(payload).toString() } : {}),
        },
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => {
          data += chunk;
        });
        res.on("end", () => {
          const parsed = parseSovrdJson(data);
          if (!parsed.ok) {
            resolve(parsed);
            return;
          }
          if (res.statusCode && res.statusCode >= 400) {
            const error =
              typeof parsed.data === "object" && parsed.data && "error" in parsed.data
                ? String((parsed.data as { error: unknown }).error)
                : `HTTP ${res.statusCode}`;
            resolve({ ok: false, data: parsed.data, error });
            return;
          }
          resolve(parsed);
        });
      },
    );
    req.on("error", (error) => resolve({ ok: false, error: error.message }));
    if (payload) req.write(payload);
    req.end();
  });
}

export async function afmHealth(url = AFM_HEALTH_URL): Promise<JsonResult> {
  return new Promise((resolve) => {
    const parsedUrl = new URL(url);
    const client = parsedUrl.protocol === "https:" ? httpsRequest : httpRequest;
    const req = client(parsedUrl, { method: "GET", timeout: 2000 }, (res) => {
      let data = "";
      res.on("data", (chunk) => {
        data += chunk;
      });
      res.on("end", () => {
        const parsed = parseSovrdJson(data);
        if (res.statusCode && res.statusCode >= 400) {
          resolve({ ok: false, data: parsed.data, error: `HTTP ${res.statusCode}` });
          return;
        }
        resolve(parsed);
      });
    });
    req.on("timeout", () => {
      req.destroy(new Error("AFM health request timed out"));
    });
    req.on("error", (error) => resolve({ ok: false, error: error.message }));
    req.end();
  });
}

export async function socketHealth(): Promise<JsonResult> {
  return socketRequest("GET", "/health");
}

export async function recallMemory(input: {
  query: string;
  agentId?: string;
  layer?: string;
  workspaceId?: string;
  limit?: number;
}): Promise<JsonResult<RecallResponse>> {
  const params = new URLSearchParams();
  params.set("q", input.query);
  params.set("agent_id", input.agentId ?? DEFAULT_AGENT_ID);
  if (input.layer) params.set("layer", input.layer);
  if (input.workspaceId) params.set("workspace_id", input.workspaceId);
  if (input.limit !== undefined) params.set("limit", String(input.limit));
  return socketRequest("GET", `/recall?${params.toString()}`) as Promise<JsonResult<RecallResponse>>;
}

export async function learnMemory(input: {
  content: string;
  category?: string;
  agentId?: string;
  workspaceId?: string;
}): Promise<JsonResult> {
  return socketRequest("POST", "/learn", {
    content: input.content,
    category: input.category ?? "general",
    agent_id: input.agentId ?? DEFAULT_AGENT_ID,
    workspace_id: input.workspaceId ?? DEFAULT_WORKSPACE_ID,
  });
}

function firstLine(text: string): string {
  return text.split("\n").find((line) => line.trim().length > 0)?.trim() ?? "";
}

function compactDaemonResults(results: string | unknown[] | undefined): string {
  if (Array.isArray(results)) return JSON.stringify(results, null, 2);
  if (!results) return "No daemon recall results.";
  return results;
}

function formatVaultContext(results: VaultSearchResult[]): string {
  if (results.length === 0) return "No Gemini Anti Gravity vault wiki matches.";
  return results
    .map((result, index) => {
      const snippet = result.snippet.replace(/\s+/g, " ");
      return `${index + 1}. ${result.wikilink} (vault score=${result.score})\n   ${snippet}`;
    })
    .join("\n");
}

export function formatRecall(query: string, response: RecallResponse, vaultResults: VaultSearchResult[] = []): string {
  const results = Array.isArray(response.results)
    ? JSON.stringify(response.results, null, 2)
    : response.results ?? "No recall results.";
  const provenance = [
    response.agent_id ? `agent=${response.agent_id}` : undefined,
    response.layer ? `layer=${response.layer}` : undefined,
    response.workspace_id ? `workspace=${response.workspace_id}` : undefined,
  ]
    .filter(Boolean)
    .join(", ");
  const daemonLead = firstLine(compactDaemonResults(response.results));
  const sections = [
    "# Gemini Anti Gravity Recall",
    `Query: ${query}`,
    provenance ? `Provenance: ${provenance}` : undefined,
    "## AI Context Pack",
    formatVaultContext(vaultResults),
    daemonLead ? `Daemon lead: ${daemonLead}` : undefined,
    "## Daemon Results",
    results,
  ].filter(Boolean);
  return sections.join("\n\n");
}

export async function buildStatusReport(input?: {
  vaultPath?: string;
  socket?: JsonResult;
  afm?: JsonResult;
}): Promise<StatusReport> {
  const vaultPath = input?.vaultPath ?? DEFAULT_VAULT_PATH;
  await ensureVault(vaultPath);
  const tail = await auditTail(vaultPath, 1);
  return {
    vault: {
      path: vaultPath,
      exists: await vaultExists(vaultPath),
    },
    socket: input?.socket ?? (await socketHealth()),
    afm: input?.afm ?? (await afmHealth()),
    audit: {
      entries: tail.entries.length,
      latest: tail.entries.at(-1),
    },
  };
}

export async function statusAndAudit(vaultPath = DEFAULT_VAULT_PATH): Promise<StatusReport> {
  const report = await buildStatusReport({ vaultPath });
  await recordAudit(vaultPath, {
    tool: "gemini_antigravity_status",
    summary: `socket=${report.socket.ok ? "ok" : "error"} afm=${report.afm.ok ? "ok" : "error"}`,
    details: report as unknown as Record<string, unknown>,
  });
  return report;
}
