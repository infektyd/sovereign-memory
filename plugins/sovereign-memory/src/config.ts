import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export const PLUGIN_ROOT = path.resolve(__dirname, "..");

export const DEFAULT_VAULT_PATH =
  process.env.SOVEREIGN_CODEX_VAULT_PATH ??
  path.join(os.homedir(), ".sovereign-memory", "codex-vault");

export const SOCKET_PATH =
  process.env.SOVEREIGN_SOCKET_PATH ?? "/tmp/sovereign.sock";

export const AFM_HEALTH_URL =
  process.env.SOVEREIGN_AFM_HEALTH_URL ?? "http://127.0.0.1:11437/health";

export const DEFAULT_AGENT_ID = process.env.SOVEREIGN_CODEX_AGENT_ID ?? "codex";

export const DEFAULT_WORKSPACE_ID =
  process.env.SOVEREIGN_CODEX_WORKSPACE_ID ?? "workspace-codex";
