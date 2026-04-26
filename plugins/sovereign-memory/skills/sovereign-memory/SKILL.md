---
name: sovereign-memory
description: Use when the user asks the agent to recall, learn, write, audit, or operate through Sovereign Memory, or when a task likely benefits from prior local memory. Works across Claude Code, Codex, Hermes, and OpenClaw. Default automatic behavior is recall-only; do not learn unless explicitly requested.
---

# Sovereign Memory

Use this skill to operate the local Sovereign Memory bridge and the agent's Obsidian vault. Sovereign Memory is shared across multiple agents (Claude Code, Codex, Hermes, OpenClaw) — each has its own vault, but they all talk to the same daemon and can recall each other's notes, tagged with `agent_origin`.

## Spine Integration (Claude Code)

When loaded as a Claude Code plugin, Sovereign Memory wires four hooks into the session:

- **SessionStart** — boots identity context, recent audit, and any pending learnings from the inbox.
- **UserPromptSubmit** — auto-recalls before each turn and injects ranked vault + daemon results.
- **PreCompact** — captures scar tissue (failed paths, dead ends) so post-compaction Claude doesn't re-walk them.
- **Stop** — drafts candidate learnings into the vault inbox (never auto-writes); next session reviews them.

All hook output is wrapped in `<sovereign:context version="1" event="..." agent="claude-code" tokens="...">` envelopes containing JSON. Parse the JSON; don't reformat the envelope. Disable with `SOVEREIGN_CLAUDECODE_HOOKS=off`.

The Claude Code vault lives at `~/.sovereign-memory/claudecode-vault` (override: `SOVEREIGN_CLAUDECODE_VAULT_PATH`). The Codex vault at `~/.sovereign-memory/codex-vault` is a peer, not a parent — they share a daemon, not a directory.

## Default Behavior

- On tasks that likely benefit from prior local context, call `sovereign_status` first if available, then use `sovereign_recall` with a narrow query.
- Automatic behavior is recall-only.
- Do not call `sovereign_learn` unless the user explicitly asks to remember, learn, save to memory, or write a durable note.
- Do not run AFM extraction, session mining, adapter training, or staging review in v1.
- Keep private session content, adapter files, launchd plists, datasets, DB files, and raw vault contents out of public git.

## Manual Tools

- `sovereign_route`: Classify whether a task should recall, learn, write a note, show audit, check status, or do nothing.
- `sovereign_status`: Check daemon, AFM bridge, vault path, and recent audit state.
- `sovereign_prepare_task`: Build a compact Codex task packet with ranked context, source reasons, privacy metadata, and optional AFM distillation.
- `sovereign_prepare_outcome`: Build a dry-run post-task outcome packet without writing durable memory.
- `sovereign_recall`: Search existing Sovereign Memory, prepend a Codex vault context pack, and log the lookup.
- `sovereign_learning_quality`: Review a potential memory before writing it.
- `sovereign_learn`: Write a Codex vault note first, quality-report it, then store the learning through Sovereign Memory.
- `sovereign_vault_write`: Write a structured Obsidian note without durable learning.
- `sovereign_audit_report`: Summarize recent memory tool activity.
- `sovereign_audit_tail`: Show recent memory audit entries.
- `sovereign_negotiate_handoff`: Build an agent-to-agent handoff envelope (identity, top recalls with provenance, scar tissue, open questions, inbox pointer) optimized for another LLM to consume — use before delegating to a subagent or another session.

## Slash Commands (Claude Code)

- `/sovereign-memory:recall <query>` — quick recall against the Claude Code vault + daemon.
- `/sovereign-memory:learn` — commit a durable learning (vault-first, quality-checked).
- `/sovereign-memory:status` — daemon + AFM + vault health.
- `/sovereign-memory:audit` — recent tool activity from the audit log.
- `/sovereign-memory:prepare-task <task>` — ranked task packet before complex work.
- `/sovereign-memory:prepare-outcome` — dry-run outcome packet, no writes.

## Vault Rules

Each agent has its own vault. Defaults:
- Claude Code: `~/.sovereign-memory/claudecode-vault` (override: `SOVEREIGN_CLAUDECODE_VAULT_PATH`).
- Codex: `~/.sovereign-memory/codex-vault` (override: `SOVEREIGN_CODEX_VAULT_PATH`).

- `raw/` is immutable raw sources.
- `wiki/` is agent-maintained synthesis.
- `schema/AGENTS.md` is the operating schema.
- `index.md` catalogs pages.
- `log.md` and `logs/YYYY-MM-DD.md` are append-only transparency logs.
- `inbox/` (Claude Code) holds candidate learnings drafted by the Stop hook awaiting next-session review.

For Karpathy-style LLM wiki behavior, prefer short sourced pages with Obsidian wikilinks over opaque hidden memory. The vault is the visible surface; SQLite/FTS/FAISS is the recall machinery.

## Cross-Agent Awareness

When a recalled snippet has `agent_origin` other than your own (e.g., Claude Code recalls a note Codex wrote), treat it as authoritative for what *that agent* concluded — but verify before acting on it in your own context. If it's load-bearing, recall scoped to that agent or read the source note directly.
