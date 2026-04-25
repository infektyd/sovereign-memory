---
name: sovereign-memory
description: Use when the user asks Codex to recall, learn, write, audit, or operate through Sovereign Memory, or when a task likely benefits from prior local memory. Default automatic behavior is recall-only; do not learn unless explicitly requested.
---

# Sovereign Memory for Codex

Use this skill to operate Codex's local Sovereign Memory bridge and Codex-owned Obsidian vault.

## Default Behavior

- On tasks that likely benefit from prior local context, call `sovereign_status` first if available, then use `sovereign_recall` with a narrow query.
- Automatic behavior is recall-only.
- Do not call `sovereign_learn` unless the user explicitly asks to remember, learn, save to memory, or write a durable note.
- Do not run AFM extraction, session mining, adapter training, or staging review in v1.
- Keep private session content, adapter files, launchd plists, datasets, DB files, and raw vault contents out of public git.

## Manual Tools

- `sovereign_status`: Check daemon, AFM bridge, vault path, and recent audit state.
- `sovereign_recall`: Search existing Sovereign Memory and log the lookup.
- `sovereign_learn`: Write a Codex vault note first, then store the learning through Sovereign Memory.
- `sovereign_vault_write`: Write a structured Obsidian note without durable learning.
- `sovereign_audit_tail`: Show recent memory audit entries.

## Vault Rules

Codex's vault defaults to `~/.sovereign-memory/codex-vault`, or `SOVEREIGN_CODEX_VAULT_PATH` when set.

- `raw/` is immutable raw sources.
- `wiki/` is Codex-maintained synthesis.
- `schema/AGENTS.md` is the operating schema.
- `index.md` catalogs pages.
- `log.md` and `logs/YYYY-MM-DD.md` are append-only transparency logs.

For Karpathy-style LLM wiki behavior, prefer short sourced pages with Obsidian wikilinks over opaque hidden memory. The vault is the visible surface; SQLite/FTS/FAISS is the recall machinery.
