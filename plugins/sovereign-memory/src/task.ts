import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { URL } from "node:url";
import {
  AFM_PREPARE_TASK_MODEL,
  AFM_PREPARE_TASK_URL,
  DEFAULT_AGENT_ID,
  DEFAULT_VAULT_PATH,
  DEFAULT_WORKSPACE_ID,
} from "./config.js";
import { recallMemory } from "./sovereign.js";
import type { JsonResult, RecallResponse } from "./sovereign.js";
import { recordAudit, searchVaultNotes } from "./vault.js";
import type { VaultSearchResult } from "./vault.js";

export interface TaskSource {
  title: string;
  wikilink: string;
  relativePath: string;
  snippet: string;
  score: number;
}

export interface PreparedTaskPacket {
  task: string;
  budgetTokens: number;
  mode: "deterministic" | "afm";
  intent: string;
  brief: string;
  constraints: string[];
  currentState: string[];
  relevantSources: TaskSource[];
  recommendedNextActions: string[];
  risks: string[];
  recall: {
    daemonOk: boolean;
    daemonLead?: string;
    error?: string;
  };
  afm: {
    requested: boolean;
    used: boolean;
    url?: string;
    error?: string;
  };
  contextMarkdown: string;
}

export interface PrepareTaskInput {
  task: string;
  budgetTokens?: number;
  useAfm?: boolean;
  layer?: "identity" | "episodic" | "knowledge" | "artifact";
  limit?: number;
  workspaceId?: string;
  agentId?: string;
  vaultPath?: string;
  includeVault?: boolean;
  afmPrepareUrl?: string;
  afmModel?: string;
}

export interface PrepareTaskDeps {
  searchVault?: typeof searchVaultNotes;
  recall?: typeof recallMemory;
  afmPrepare?: (url: string, payload: Record<string, unknown>) => Promise<JsonResult<Partial<PreparedTaskPacket>>>;
  audit?: typeof recordAudit;
}

function clampBudgetTokens(value: number | undefined): number {
  if (!Number.isFinite(value ?? NaN)) return 4000;
  return Math.max(1000, Math.min(32000, Math.trunc(value ?? 4000)));
}

function classifyIntent(task: string): string {
  const text = task.toLowerCase();
  if (/review|audit|risk/.test(text)) return "review";
  if (/debug|fix|broken|failing|error/.test(text)) return "debug";
  if (/test|verify|smoke/.test(text)) return "verify";
  if (/plan|design|think|upgrade|architecture/.test(text)) return "plan";
  if (/build|implement|add|create/.test(text)) return "implement";
  return "work";
}

function firstLine(text: string | unknown[] | undefined): string | undefined {
  const raw = Array.isArray(text) ? JSON.stringify(text) : text;
  return raw?.split("\n").find((line) => line.trim().length > 0)?.trim();
}

function constraintsForTask(task: string): string[] {
  const constraints = [
    "Default automatic behavior is recall-only; durable learning and vault writes must stay explicit.",
    "Keep adapter files, launchd plists, datasets, DB files, raw sessions, vault raw/log material, and local runtime files out of public git unless sanitized.",
  ];
  if (/afm|foundation|adapter|extract|training|session mining/i.test(task)) {
    constraints.push("Do not run AFM extraction, adapter training, session mining, staging review, or production extraction unless explicitly requested.");
  }
  if (/frontend|dashboard|ui/i.test(task)) {
    constraints.push("Frontend/dashboard work should wait until the plugin backend behavior is stable and verified.");
  }
  return constraints;
}

function sourceFromVault(result: VaultSearchResult): TaskSource {
  return {
    title: result.title,
    wikilink: result.wikilink,
    relativePath: result.relativePath,
    snippet: result.snippet.replace(/\s+/g, " "),
    score: result.score,
  };
}

function deterministicPacket(input: {
  task: string;
  budgetTokens: number;
  vaultResults: VaultSearchResult[];
  recallResult: JsonResult<RecallResponse>;
  afmRequested: boolean;
  afmUrl: string;
  afmError?: string;
}): PreparedTaskPacket {
  const intent = classifyIntent(input.task);
  const relevantSources = input.vaultResults.map(sourceFromVault);
  const daemonLead = input.recallResult.ok ? firstLine(input.recallResult.data?.results) : undefined;
  const constraints = constraintsForTask(input.task);
  const currentState = [
    relevantSources.length > 0
      ? `Vault context available from ${relevantSources.length} ranked note${relevantSources.length === 1 ? "" : "s"}.`
      : "No matching Codex vault notes were found for this task.",
    input.recallResult.ok ? "Daemon recall responded." : `Daemon recall unavailable: ${input.recallResult.error ?? "unknown error"}.`,
  ];
  if (daemonLead) currentState.push(`Daemon lead: ${daemonLead}`);
  const recommendedNextActions = [
    "Read the highest-ranked source notes before editing.",
    "Make the narrowest code change that satisfies the task packet.",
    "Run focused tests first, then the plugin build/test suite before handoff.",
  ];
  const risks = [
    "Older broad semantic recall can outrank fresher Codex vault notes unless source ranking is explicit.",
    "Private local memory material can accidentally leak if public-safety scans are skipped.",
  ];
  const brief = [
    `Intent: ${intent}.`,
    relevantSources[0] ? `Top source: ${relevantSources[0].wikilink} - ${relevantSources[0].snippet}` : "No top vault source.",
    constraints[0],
  ].join(" ");
  const contextMarkdown = [
    "# Sovereign Task Packet",
    `Task: ${input.task}`,
    `Intent: ${intent}`,
    `Budget: ${input.budgetTokens} tokens`,
    "## Brief",
    brief,
    "## Constraints",
    constraints.map((item) => `- ${item}`).join("\n"),
    "## Current State",
    currentState.map((item) => `- ${item}`).join("\n"),
    "## Relevant Sources",
    relevantSources.length === 0
      ? "- None"
      : relevantSources.map((source) => `- ${source.wikilink} (score=${source.score}) ${source.snippet}`).join("\n"),
    "## Recommended Next Actions",
    recommendedNextActions.map((item) => `- ${item}`).join("\n"),
    "## Risks",
    risks.map((item) => `- ${item}`).join("\n"),
  ].join("\n\n");

  return {
    task: input.task,
    budgetTokens: input.budgetTokens,
    mode: "deterministic",
    intent,
    brief,
    constraints,
    currentState,
    relevantSources,
    recommendedNextActions,
    risks,
    recall: {
      daemonOk: input.recallResult.ok,
      daemonLead,
      error: input.recallResult.error,
    },
    afm: {
      requested: input.afmRequested,
      used: false,
      url: input.afmUrl,
      error: input.afmError,
    },
    contextMarkdown,
  };
}

function mergeAfmPacket(base: PreparedTaskPacket, afmData: Partial<PreparedTaskPacket>): PreparedTaskPacket {
  const merged = {
    ...base,
    ...afmData,
    task: base.task,
    budgetTokens: base.budgetTokens,
    mode: "afm" as const,
    intent: afmData.intent ?? base.intent,
    constraints: afmData.constraints ?? base.constraints,
    currentState: afmData.currentState ?? base.currentState,
    relevantSources: base.relevantSources,
    recommendedNextActions: afmData.recommendedNextActions ?? base.recommendedNextActions,
    risks: afmData.risks ?? base.risks,
    recall: base.recall,
    afm: {
      ...base.afm,
      requested: true,
      used: true,
      error: undefined,
    },
  };
  return {
    ...merged,
    contextMarkdown: afmData.contextMarkdown ?? base.contextMarkdown,
    brief: afmData.brief ?? base.brief,
  };
}

function extractJsonObject(raw: string): unknown | undefined {
  const trimmed = raw.trim();
  const fenced = trimmed.match(/```(?:json)?\s*([\s\S]*?)```/i)?.[1]?.trim();
  const candidates = [trimmed, fenced].filter((candidate): candidate is string => Boolean(candidate));
  const objectMatch = trimmed.match(/\{[\s\S]*\}/)?.[0];
  if (objectMatch) candidates.push(objectMatch);
  for (const candidate of candidates) {
    try {
      return JSON.parse(candidate);
    } catch {
      // Keep trying more permissive candidates.
    }
  }
  return undefined;
}

function contentFromChatCompletion(data: unknown): string | undefined {
  if (!data || typeof data !== "object") return undefined;
  const choices = (data as { choices?: unknown }).choices;
  if (!Array.isArray(choices)) return undefined;
  const first = choices[0];
  if (!first || typeof first !== "object") return undefined;
  const message = (first as { message?: unknown }).message;
  if (!message || typeof message !== "object") return undefined;
  const content = (message as { content?: unknown }).content;
  return typeof content === "string" ? content : undefined;
}

function normalizeAfmResponse(data: unknown): Partial<PreparedTaskPacket> {
  const chatContent = contentFromChatCompletion(data);
  const parsed = chatContent ? extractJsonObject(chatContent) : data;
  if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
    const partial = parsed as Partial<PreparedTaskPacket> & { ok?: unknown };
    const normalized: Partial<PreparedTaskPacket> = {};
    if (typeof partial.brief === "string") normalized.brief = partial.brief;
    if (typeof partial.intent === "string") normalized.intent = partial.intent;
    if (typeof partial.contextMarkdown === "string") normalized.contextMarkdown = partial.contextMarkdown;
    if (Array.isArray(partial.constraints)) normalized.constraints = partial.constraints.filter((item) => typeof item === "string");
    if (Array.isArray(partial.currentState)) normalized.currentState = partial.currentState.filter((item) => typeof item === "string");
    if (Array.isArray(partial.recommendedNextActions)) {
      normalized.recommendedNextActions = partial.recommendedNextActions.filter((item) => typeof item === "string");
    }
    if (Array.isArray(partial.risks)) normalized.risks = partial.risks.filter((item) => typeof item === "string");
    if (Object.keys(normalized).length > 0) return normalized;
  }
  if (chatContent?.trim()) return { brief: chatContent.trim() };
  return {};
}

function buildAfmChatPayload(payload: Record<string, unknown>): Record<string, unknown> {
  const sourceLines = Array.isArray(payload.relevantSources)
    ? payload.relevantSources
        .slice(0, 4)
        .map((source) => {
          if (!source || typeof source !== "object") return "";
          const item = source as { wikilink?: unknown; snippet?: unknown; score?: unknown };
          return `${String(item.wikilink ?? "source")} score=${String(item.score ?? "?")}: ${String(item.snippet ?? "").slice(0, 220)}`;
        })
        .filter(Boolean)
    : [];
  return {
    model: typeof payload.model === "string" ? payload.model : AFM_PREPARE_TASK_MODEL,
    temperature: 0,
    max_tokens: 220,
    messages: [
      {
        role: "user",
        content:
          [
            "Return compact JSON only for Codex task prep.",
            "Keys: brief, recommendedNextActions, risks.",
            "No secrets, no raw private logs.",
            `Task: ${String(payload.task ?? "").slice(0, 500)}`,
            `Intent: ${String(payload.intent ?? "work")}`,
            `Budget: ${String(payload.budgetTokens ?? 4000)} tokens`,
            `Constraints: ${Array.isArray(payload.constraints) ? payload.constraints.join(" | ").slice(0, 700) : ""}`,
            `State: ${Array.isArray(payload.currentState) ? payload.currentState.join(" | ").slice(0, 700) : ""}`,
            `Sources: ${sourceLines.join(" || ").slice(0, 1200)}`,
            `Daemon: ${String(payload.daemonLead ?? "").slice(0, 400)}`,
          ].join("\n"),
      },
    ],
  };
}

export async function callAfmPrepareTask(
  url: string,
  payload: Record<string, unknown>,
): Promise<JsonResult<Partial<PreparedTaskPacket>>> {
  return new Promise((resolve) => {
    const parsedUrl = new URL(url);
    const client = parsedUrl.protocol === "https:" ? httpsRequest : httpRequest;
    const isChatCompletions = parsedUrl.pathname.endsWith("/chat/completions");
    const body = JSON.stringify(isChatCompletions ? buildAfmChatPayload(payload) : payload);
    const req = client(
      parsedUrl,
      {
        method: "POST",
        timeout: 45000,
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body).toString(),
        },
      },
      (res) => {
        let data = "";
        res.on("data", (chunk) => {
          data += chunk;
        });
        res.on("end", () => {
          try {
            const parsed = JSON.parse(data) as unknown;
            if (res.statusCode && res.statusCode >= 400) {
              resolve({ ok: false, data: normalizeAfmResponse(parsed), error: `HTTP ${res.statusCode}` });
              return;
            }
            resolve({ ok: true, data: normalizeAfmResponse(parsed) });
          } catch (error) {
            resolve({ ok: false, error: error instanceof Error ? error.message : String(error) });
          }
        });
      },
    );
    req.on("timeout", () => {
      req.destroy(new Error("AFM prepare_task request timed out"));
    });
    req.on("error", (error) => resolve({ ok: false, error: error.message }));
    req.write(body);
    req.end();
  });
}

export async function prepareTask(input: PrepareTaskInput, deps: PrepareTaskDeps = {}): Promise<PreparedTaskPacket> {
  const vaultPath = input.vaultPath ?? DEFAULT_VAULT_PATH;
  const agentId = input.agentId ?? DEFAULT_AGENT_ID;
  const workspaceId = input.workspaceId ?? DEFAULT_WORKSPACE_ID;
  const limit = Math.max(1, Math.min(input.limit ?? 6, 12));
  const budgetTokens = clampBudgetTokens(input.budgetTokens);
  const afmUrl = input.afmPrepareUrl ?? AFM_PREPARE_TASK_URL;
  const search = deps.searchVault ?? searchVaultNotes;
  const recall = deps.recall ?? recallMemory;
  const afmPrepare = deps.afmPrepare ?? callAfmPrepareTask;
  const audit = deps.audit ?? recordAudit;

  const [vaultResults, recallResult] = await Promise.all([
    input.includeVault === false ? Promise.resolve([]) : search(vaultPath, input.task, limit),
    recall({
      query: input.task,
      layer: input.layer,
      limit,
      workspaceId,
      agentId,
    }),
  ]);

  let packet = deterministicPacket({
    task: input.task,
    budgetTokens,
    vaultResults,
    recallResult,
    afmRequested: input.useAfm === true,
    afmUrl,
  });

  if (input.useAfm === true) {
    const afmResult = await afmPrepare(afmUrl, {
      task: input.task,
      budgetTokens,
      intent: packet.intent,
      constraints: packet.constraints,
      currentState: packet.currentState,
      relevantSources: packet.relevantSources,
      daemonLead: packet.recall.daemonLead,
      model: input.afmModel ?? AFM_PREPARE_TASK_MODEL,
    });
    if (afmResult.ok && afmResult.data) {
      packet = mergeAfmPacket(packet, afmResult.data);
    } else {
      packet.afm.error = afmResult.error ?? "AFM prepare_task returned no data.";
    }
  }

  await audit(vaultPath, {
    tool: "sovereign_prepare_task",
    summary: input.task.slice(0, 120),
    details: {
      mode: packet.mode,
      intent: packet.intent,
      budgetTokens,
      vaultMatches: packet.relevantSources.map((source) => source.relativePath),
      daemonOk: packet.recall.daemonOk,
      afm: packet.afm,
    },
  });

  return packet;
}
