---
name: gemini-cli
description: Use when the user asks Gemini CLI to recall, learn, write, audit, or operate through Gemini CLI Memory, or when a task likely benefits from prior local memory. Default automatic behavior is recall-only; do not learn unless explicitly requested.
---

# Gemini CLI Memory for Gemini CLI

Use this skill to operate Gemini CLI's local Gemini CLI Memory bridge and Gemini CLI-owned Obsidian vault.

## Default Behavior

- On tasks that likely benefit from prior local context, call `gemini_cli_status` first if available, then use `gemini_cli_recall` with a narrow query.
- Automatic behavior is recall-only.
- Do not call `gemini_cli_learn` unless the user explicitly asks to remember, learn, save to memory, or write a durable note.
- Do not run AFM extraction, session mining, adapter training, or staging review in v1.
- Keep private session content, adapter files, launchd plists, datasets, DB files, and raw vault contents out of public git.

## Manual Tools

- `gemini_cli_route`: Classify whether a task should recall, learn, write a note, show audit, check status, or do nothing.
- `gemini_cli_status`: Check daemon, AFM bridge, vault path, and recent audit state.
- `gemini_cli_prepare_task`: Build a compact Gemini CLI task packet with ranked context, source reasons, privacy metadata, and optional AFM distillation.
- `gemini_cli_prepare_outcome`: Build a dry-run post-task outcome packet without writing durable memory.
- `gemini_cli_recall`: Search existing Gemini CLI Memory, prepend a Gemini CLI vault context pack, and log the lookup.
- `gemini_cli_learning_quality`: Review a potential memory before writing it.
- `gemini_cli_learn`: Write a Gemini CLI vault note first, quality-report it, then store the learning through Gemini CLI Memory.
- `gemini_cli_vault_write`: Write a structured Obsidian note without durable learning.
- `gemini_cli_audit_report`: Summarize recent memory tool activity.
- `gemini_cli_audit_tail`: Show recent memory audit entries.

## Vault Rules

Gemini CLI's vault defaults to `~/.gemini-cli/gemini_cli-vault`, or to `GEMINI_CLI_GEMINI_CLI_VAULT_PATH` when set.

- `raw/` is immutable raw sources.
- `wiki/` is Gemini CLI-maintained synthesis.
- `schema/AGENTS.md` is the operating schema.
- `index.md` catalogs pages.
- `log.md` and `logs/YYYY-MM-DD.md` are append-only transparency logs.

For Karpathy-style LLM wiki behavior, prefer short sourced pages with Obsidian wikilinks over opaque hidden memory. The vault is the visible surface; SQLite/FTS/FAISS is the recall machinery.
