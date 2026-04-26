import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export const PLUGIN_ROOT = path.resolve(__dirname, "..");

export const DEFAULT_VAULT_PATH =
  process.env.GEMINI_ANTIGRAVITY_VAULT_PATH ??
  path.join(os.homedir(), ".gemini-antigravity", "gemini_antigravity-vault");

export const SOCKET_PATH =
  process.env.GEMINI_ANTIGRAVITY_SOCKET_PATH ?? "/tmp/sovereign.sock";

export const AFM_HEALTH_URL =
  process.env.GEMINI_ANTIGRAVITY_AFM_HEALTH_URL ?? "http://127.0.0.1:11437/health";

export const AFM_PREPARE_TASK_URL =
  process.env.GEMINI_ANTIGRAVITY_AFM_PREPARE_TASK_URL ?? "http://127.0.0.1:11437/v1/chat/completions";

export const AFM_PREPARE_TASK_MODEL =
  process.env.GEMINI_ANTIGRAVITY_AFM_PREPARE_TASK_MODEL ?? "apple-foundation-models";

export const DEFAULT_AGENT_ID = process.env.GEMINI_ANTIGRAVITY_AGENT_ID ?? "gemini_antigravity";

export const DEFAULT_WORKSPACE_ID =
  process.env.GEMINI_ANTIGRAVITY_WORKSPACE_ID ?? "workspace-gemini_antigravity";
