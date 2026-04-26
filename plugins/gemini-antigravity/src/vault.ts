import { access, appendFile, mkdir, readFile, readdir, stat, writeFile } from "node:fs/promises";
import path from "node:path";

export type VaultSection =
  | "raw"
  | "entities"
  | "concepts"
  | "decisions"
  | "syntheses"
  | "sessions";

export interface EnsureVaultResult {
  vaultPath: string;
  created: string[];
}

export interface AuditEntry {
  tool: string;
  summary: string;
  details?: Record<string, unknown>;
  timestamp?: Date;
}

export interface WriteVaultPageInput {
  vaultPath: string;
  title: string;
  content: string;
  section: VaultSection;
  source?: string;
  frontmatter?: Record<string, string | number | boolean | undefined>;
}

export interface LearnInput {
  vaultPath: string;
  title: string;
  content: string;
  category?: string;
  source?: string;
  agentId?: string;
  storeResult?: Record<string, unknown>;
}

export interface VaultWriteResult {
  notePath: string;
  relativePath: string;
  wikilink: string;
}

export interface AuditTailResult {
  entries: string[];
  text: string;
}

export interface AuditReport {
  entries: number;
  tools: Record<string, number>;
  recentSummaries: string[];
  latest?: string;
}

export interface VaultSearchResult {
  notePath: string;
  relativePath: string;
  wikilink: string;
  title: string;
  snippet: string;
  score: number;
}

const VAULT_DIRS = [
  "raw",
  "wiki",
  "wiki/entities",
  "wiki/concepts",
  "wiki/decisions",
  "wiki/syntheses",
  "wiki/sessions",
  "schema",
  "logs",
  ".obsidian",
];

function isoDate(date = new Date()): string {
  return date.toISOString().slice(0, 10);
}

function compactDate(date = new Date()): string {
  return isoDate(date).replaceAll("-", "");
}

function slugify(title: string): string {
  const slug = title
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^\w\s-]/g, "")
    .trim()
    .replace(/[\s_-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || "untitled";
}

function yamlValue(value: string | number | boolean | undefined): string {
  if (value === undefined) return "";
  if (typeof value === "boolean" || typeof value === "number") return String(value);
  if (/^[A-Za-z0-9_.:/@ -]+$/.test(value)) return value;
  return JSON.stringify(value);
}

function frontmatter(data: Record<string, string | number | boolean | undefined>): string {
  const lines = Object.entries(data)
    .filter(([, value]) => value !== undefined)
    .map(([key, value]) => `${key}: ${yamlValue(value)}`);
  return `---\n${lines.join("\n")}\n---\n`;
}

async function exists(filePath: string): Promise<boolean> {
  try {
    await access(filePath);
    return true;
  } catch {
    return false;
  }
}

function sectionPath(section: VaultSection, title: string): string {
  const slug = slugify(title);
  if (section === "raw") return path.join("raw", `${compactDate()}-${slug}.md`);
  if (section === "sessions") return path.join("wiki", "sessions", `${compactDate()}-${slug}.md`);
  return path.join("wiki", section, `${slug}.md`);
}

function wikilinkFor(relativePath: string): string {
  const withoutExt = relativePath.replace(/\.md$/, "");
  return `[[${withoutExt}]]`;
}

function queryTerms(query: string): string[] {
  const stop = new Set([
    "a",
    "an",
    "and",
    "are",
    "for",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
  ]);
  return [...new Set(query.toLowerCase().match(/[a-z0-9_/-]{3,}/g) ?? [])].filter((term) => !stop.has(term));
}

function titleFromMarkdown(relativePath: string, markdown: string): string {
  const fmTitle = markdown.match(/^title:\s*(.+)$/m)?.[1]?.trim();
  if (fmTitle) return fmTitle.replace(/^["']|["']$/g, "");
  const heading = markdown.match(/^#\s+(.+)$/m)?.[1]?.trim();
  if (heading) return heading;
  return path.basename(relativePath, ".md");
}

function snippetFor(markdown: string, terms: string[], maxLength = 280): string {
  const plain = markdown
    .replace(/^---[\s\S]*?---/m, "")
    .replace(/^#+\s+/gm, "")
    .replace(/\s+/g, " ")
    .trim();
  const lower = plain.toLowerCase();
  const firstHit = terms.map((term) => lower.indexOf(term)).filter((index) => index >= 0).sort((a, b) => a - b)[0] ?? 0;
  const start = Math.max(0, firstHit - 80);
  const end = Math.min(plain.length, start + maxLength);
  const prefix = start > 0 ? "..." : "";
  const suffix = end < plain.length ? "..." : "";
  return `${prefix}${plain.slice(start, end).trim()}${suffix}`;
}

async function listMarkdownFiles(root: string): Promise<string[]> {
  const entries = await readdir(root, { withFileTypes: true });
  const files = await Promise.all(
    entries.map(async (entry) => {
      const full = path.join(root, entry.name);
      if (entry.isDirectory()) return listMarkdownFiles(full);
      if (entry.isFile() && entry.name.endsWith(".md")) return [full];
      return [];
    }),
  );
  return files.flat();
}

function scoreVaultNote(query: string, terms: string[], relativePath: string, title: string, markdown: string): number {
  const haystack = `${title}\n${relativePath}\n${markdown}`.toLowerCase();
  const titleLower = title.toLowerCase();
  const queryLower = query.toLowerCase().trim();
  let score = 0;
  if (queryLower && haystack.includes(queryLower)) score += 50;
  for (const term of terms) {
    const count = haystack.split(term).length - 1;
    if (count > 0) score += Math.min(count, 5);
    if (titleLower.includes(term)) score += 3;
  }
  if (relativePath.startsWith("wiki/sessions/")) score += 1;
  if (/gemini_antigravity_learning:\s*true/i.test(markdown)) score += 2;
  return score;
}

function schemaContent(): string {
  return `# Gemini Anti Gravity Gemini Anti Gravity Memory Vault

This vault is Gemini Anti Gravity's local-first LLM wiki for Gemini Anti Gravity Memory.

## Operating Rules

- Treat \`raw/\` as immutable raw sources. Do not edit raw source notes after writing them; create a new note if the source changes.
- Treat \`wiki/\` as Gemini Anti Gravity-maintained synthesis. Keep pages short, sourced, and linked with Obsidian wikilinks.
- Prefer durable facts, decisions, procedures, and user preferences over full chat transcripts.
- Default automatic behavior is recall-only. Do not write learnings unless the user explicitly asks or a tool call is explicitly manual.
- Keep private session content, adapter files, launchd plists, datasets, and generated DB state out of public git.
- Update \`index.md\` and append to \`log.md\` whenever Gemini Anti Gravity creates or learns from a note.

## Layout

- \`raw/\`: raw sources and session excerpts.
- \`wiki/entities/\`: people, projects, repos, services, machines, and named systems.
- \`wiki/concepts/\`: reusable ideas and patterns.
- \`wiki/decisions/\`: decisions with rationale.
- \`wiki/syntheses/\`: cross-source summaries and comparisons.
- \`wiki/sessions/\`: task/session learnings written as durable notes.
- \`logs/\`: daily audit entries for tool transparency.
`;
}

function indexContent(): string {
  return `# Gemini Anti Gravity Gemini Anti Gravity Memory Index

This index is maintained by the Gemini Anti Gravity Memory Gemini Anti Gravity plugin.

## Recent Pages

`;
}

function logContent(): string {
  return `# Gemini Anti Gravity Memory Gemini Anti Gravity Log

Append-only audit of Gemini Anti Gravity memory operations.

`;
}

export async function ensureVault(vaultPath: string): Promise<EnsureVaultResult> {
  const created: string[] = [];
  await mkdir(vaultPath, { recursive: true });
  for (const dir of VAULT_DIRS) {
    const full = path.join(vaultPath, dir);
    await mkdir(full, { recursive: true });
    created.push(full);
  }

  const schemaPath = path.join(vaultPath, "schema", "AGENTS.md");
  if (!(await exists(schemaPath))) {
    await writeFile(schemaPath, schemaContent(), "utf8");
  }

  const indexPath = path.join(vaultPath, "index.md");
  if (!(await exists(indexPath))) {
    await writeFile(indexPath, indexContent(), "utf8");
  }

  const logPath = path.join(vaultPath, "log.md");
  if (!(await exists(logPath))) {
    await writeFile(logPath, logContent(), "utf8");
  }

  return { vaultPath, created };
}

export async function recordAudit(vaultPath: string, entry: AuditEntry): Promise<string> {
  await ensureVault(vaultPath);
  const timestamp = entry.timestamp ?? new Date();
  const date = isoDate(timestamp);
  const line = `## [${timestamp.toISOString()}] ${entry.tool} | ${entry.summary}\n\n${
    entry.details ? `\`\`\`json\n${JSON.stringify(entry.details, null, 2)}\n\`\`\`\n\n` : ""
  }`;
  await appendFile(path.join(vaultPath, "log.md"), line, "utf8");
  const dailyPath = path.join(vaultPath, "logs", `${date}.md`);
  if (!(await exists(dailyPath))) {
    await writeFile(dailyPath, `# ${date} Gemini Anti Gravity Memory Audit\n\n`, "utf8");
  }
  await appendFile(dailyPath, line, "utf8");
  return dailyPath;
}

async function appendIndex(vaultPath: string, title: string, relativePath: string, summary: string): Promise<void> {
  await ensureVault(vaultPath);
  const indexPath = path.join(vaultPath, "index.md");
  const existing = await readFile(indexPath, "utf8");
  const link = wikilinkFor(relativePath);
  if (existing.includes(link)) return;
  const line = `- ${link} - ${summary.replace(/\s+/g, " ").slice(0, 160)}\n`;
  await appendFile(indexPath, line, "utf8");
}

export async function writeVaultPage(input: WriteVaultPageInput): Promise<VaultWriteResult> {
  await ensureVault(input.vaultPath);
  const relativePath = sectionPath(input.section, input.title);
  const notePath = path.join(input.vaultPath, relativePath);
  await mkdir(path.dirname(notePath), { recursive: true });

  const fm = frontmatter({
    title: input.title,
    source: input.source,
    created: new Date().toISOString(),
    section: input.section,
    immutable: input.section === "raw" ? true : undefined,
    ...input.frontmatter,
  });
  const body = `${fm}\n# ${input.title}\n\n${input.content.trim()}\n`;
  await writeFile(notePath, body, "utf8");

  await appendIndex(input.vaultPath, input.title, relativePath, input.content);
  await recordAudit(input.vaultPath, {
    tool: "gemini_antigravity_vault_write",
    summary: input.title,
    details: { notePath, section: input.section, source: input.source },
  });

  return { notePath, relativePath, wikilink: wikilinkFor(relativePath) };
}

export async function vaultFirstLearn(input: LearnInput): Promise<VaultWriteResult> {
  const result = await writeVaultPage({
    vaultPath: input.vaultPath,
    title: input.title,
    content: input.content,
    section: "sessions",
    source: input.source,
    frontmatter: {
      agent: input.agentId ?? "gemini_antigravity",
      category: input.category ?? "general",
      gemini_antigravity_learning: true,
    },
  });

  await recordAudit(input.vaultPath, {
    tool: "gemini_antigravity_learn",
    summary: input.title,
    details: {
      notePath: result.notePath,
      category: input.category ?? "general",
      source: input.source,
      storeResult: input.storeResult,
    },
  });

  return result;
}

export async function auditTail(vaultPath: string, limit = 20): Promise<AuditTailResult> {
  await ensureVault(vaultPath);
  const todayPath = path.join(vaultPath, "logs", `${isoDate()}.md`);
  const fallbackPath = path.join(vaultPath, "log.md");
  const target = (await exists(todayPath)) ? todayPath : fallbackPath;
  let text = "";
  try {
    text = await readFile(target, "utf8");
  } catch {
    return { entries: [], text: "" };
  }
  const entries = text
    .split(/^## /m)
    .filter((entry) => entry.trim().length > 0 && !entry.startsWith("#"))
    .map((entry) => `## ${entry.trim()}`)
    .slice(-limit);
  return { entries, text: entries.join("\n\n") };
}

export async function auditReport(vaultPath: string, limit = 100): Promise<AuditReport> {
  const tail = await auditTail(vaultPath, limit);
  const tools: Record<string, number> = {};
  const recentSummaries: string[] = [];
  for (const entry of tail.entries) {
    const header = entry.match(/^## \[[^\]]+\]\s+([^|]+)\|\s+(.+)$/m);
    if (!header) continue;
    const tool = header[1].trim();
    const summary = header[2].trim();
    tools[tool] = (tools[tool] ?? 0) + 1;
    recentSummaries.push(`${tool}: ${summary}`);
  }
  return {
    entries: tail.entries.length,
    tools,
    recentSummaries: recentSummaries.slice(-10),
    latest: tail.entries.at(-1),
  };
}

export async function searchVaultNotes(vaultPath: string, query: string, limit = 5): Promise<VaultSearchResult[]> {
  await ensureVault(vaultPath);
  const wikiRoot = path.join(vaultPath, "wiki");
  const terms = queryTerms(query);
  if (terms.length === 0) return [];

  let files: string[] = [];
  try {
    files = await listMarkdownFiles(wikiRoot);
  } catch {
    return [];
  }

  const scored = await Promise.all(
    files.map(async (notePath) => {
      const markdown = await readFile(notePath, "utf8");
      const relativePath = path.relative(vaultPath, notePath);
      const title = titleFromMarkdown(relativePath, markdown);
      const score = scoreVaultNote(query, terms, relativePath, title, markdown);
      return {
        notePath,
        relativePath,
        wikilink: wikilinkFor(relativePath),
        title,
        snippet: snippetFor(markdown, terms),
        score,
      };
    }),
  );

  return scored
    .filter((result) => result.score > 0)
    .sort((a, b) => b.score - a.score || a.relativePath.localeCompare(b.relativePath))
    .slice(0, limit);
}

export async function vaultExists(vaultPath: string): Promise<boolean> {
  try {
    const st = await stat(vaultPath);
    return st.isDirectory();
  } catch {
    return false;
  }
}
