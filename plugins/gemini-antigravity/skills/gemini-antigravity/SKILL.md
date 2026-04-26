---
name: gemini-antigravity
description: Use when the user asks Gemini Anti Gravity to recall, learn, write, audit, or operate through Gemini Anti Gravity Memory, or when a task likely benefits from prior local memory. Default automatic behavior is recall-only; do not learn unless explicitly requested.
---

# Gemini Anti Gravity Memory for Gemini Anti Gravity

Use this skill to operate Gemini Anti Gravity's local Gemini Anti Gravity Memory bridge and Gemini Anti Gravity-owned Obsidian vault.

## Default Behavior

- On tasks that likely benefit from prior local context, call `gemini_antigravity_status` first if available, then use `gemini_antigravity_recall` with a narrow query.
- Automatic behavior is recall-only.
- Do not call `gemini_antigravity_learn` unless the user explicitly asks to remember, learn, save to memory, or write a durable note.
- Do not run AFM extraction, session mining, adapter training, or staging review in v1.
- Keep private session content, adapter files, launchd plists, datasets, DB files, and raw vault contents out of public git.

## Manual Tools

- `gemini_antigravity_route`: Classify whether a task should recall, learn, write a note, show audit, check status, or do nothing.
- `gemini_antigravity_status`: Check daemon, AFM bridge, vault path, and recent audit state.
- `gemini_antigravity_prepare_task`: Build a compact Gemini Anti Gravity task packet with ranked context, source reasons, privacy metadata, and optional AFM distillation.
- `gemini_antigravity_prepare_outcome`: Build a dry-run post-task outcome packet without writing durable memory.
- `gemini_antigravity_recall`: Search existing Gemini Anti Gravity Memory, prepend a Gemini Anti Gravity vault context pack, and log the lookup.
- `gemini_antigravity_learning_quality`: Review a potential memory before writing it.
- `gemini_antigravity_learn`: Write a Gemini Anti Gravity vault note first, quality-report it, then store the learning through Gemini Anti Gravity Memory.
- `gemini_antigravity_vault_write`: Write a structured Obsidian note without durable learning.
- `gemini_antigravity_audit_report`: Summarize recent memory tool activity.
- `gemini_antigravity_audit_tail`: Show recent memory audit entries.

## Vault Rules

Gemini Anti Gravity's vault defaults to `~/.gemini-antigravity/gemini_antigravity-vault`, or to `GEMINI_ANTIGRAVITY_GEMINI_ANTIGRAVITY_VAULT_PATH` when set.

- `raw/` is immutable raw sources.
- `wiki/` is Gemini Anti Gravity-maintained synthesis.
- `schema/AGENTS.md` is the operating schema.
- `index.md` catalogs pages.
- `log.md` and `logs/YYYY-MM-DD.md` are append-only transparency logs.

For Karpathy-style LLM wiki behavior, prefer short sourced pages with Obsidian wikilinks over opaque hidden memory. The vault is the visible surface; SQLite/FTS/FAISS is the recall machinery.
