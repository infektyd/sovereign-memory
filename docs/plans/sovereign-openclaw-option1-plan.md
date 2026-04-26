# Sovereign Memory OpenClaw Option 1 — Quick-Win Integration Plan

## Phase A — Research + Plan

### 1. Current OpenClaw Tool/Skill Registration Convention

**Skill files:** `SKILL.md` files live in skill directories under:
- `~/.openclaw/skills/` — user-defined custom skills (2 skills: `antigravity-agent`, `xcode-agent`, `obsidian-memory`)
- `~/.openclaw/workspace/skills/` — workspace-level shared skills (~28 skills loaded by all agents)
- `~/.openclaw/node_modules/openclaw/skills/` — bundled skills from openclaw npm package

Each skill is a directory containing a `SKILL.md` file with YAML frontmatter:
```yaml
---
name: skill-name
description: "..."
---
```

**How skills propagate to agents:** OpenClaw's skill scanner finds `SKILL.md` files in registered skill directories, reads the frontmatter, and includes the skill's content in the agent system prompt. Agents then follow the commands in the SKILL.md via `exec` (terminal shell-out).

**No TypeScript tool registration:** OpenClaw's `@mariozechner/pi-agent-core` AgentTool types exist in compiled bundles. Custom tools are NOT registered via TypeScript/JSON schemas — they are described in `SKILL.md` files and invoked through `exec`.

**Existing sovereign-memory skill:** `~/.openclaw/skills/obsidian-memory/SKILL.md` already exists but uses `sovereign_memory.py` CLI (different entry point) rather than `agent_api.py`. It documents shell commands, not native callable tools.

**Key discovery:** The AGENTS.md files already document Sovereign Memory usage as exec/shell commands. OpenClaw agents do NOT have a native function-calling tool registration API — all tools go through exec. The Hermes plugin's pattern (Python MemoryProvider exposing `sovereign_recall`/`sovereign_learn` as function tools) is specific to Hermes's own agent framework, not OpenClaw.

### 2. Proposed Approach

Since OpenClaw's tool system is purely SKILL.md-based (agents exec shell commands), there are two viable implementations:

**Approach A — Update SKILL.md for exec-based invocation:**
- Update `~/.openclaw/skills/obsidian-memory/SKILL.md` to use `agent_api.py` instead of `sovereign_memory.py`
- Agents continue using exec but with cleaner commands
- This is the "true Option 1" — works immediately, zero code changes

**Approach B — Use AGENTS.md convention update (RECOMMENDED):**
The task asks for "no manual terminal shell-out" — but in OpenClaw, all tools go through exec. The real ask is that agents can call `sovereign_recall` and `sovereign_learn` as recognized tool patterns.

**Implementation: Create a unified SKILL.md + bash wrapper script:**
1. Create `~/.openclaw/workspace/skills/sovereign-memory/SKILL.md` — skill file with the two tool definitions
2. Create a bash wrapper `~/.openclaw/sovereign-memory-v3.1/openclaw-tool.sh` that handles:
   - `sovereign_recall <query> [limit]` → calls `agent_api.py`
   - `sovereign_learn <category> <content>` → calls `agent_api.py --learn`
   - stderr filtering (MLX/BertModel noise)
3. The SKILL.md documents these as "commands" the agent can exec

Actually, re-reading the AGENTS.md more carefully: it already says "Skills provide your tools. When you need one, check its SKILL.md." The convention is clear.

**Revised approach:** Update existing `~/.openclaw/skills/obsidian-memory/SKILL.md` to:
- Use the `agent_api.py` entry point (proven working)
- Document the two tool names `sovereign_recall` and `sovereign_learn` as recognized tool patterns
- The agent calls them via exec, e.g.:
  ```
  ~/.openclaw/sovereign-memory-v3.1/agent_api.py <agent_id> "<query>"
  ~/.openclaw/sovereign-memory-v3.1/agent_api.py <agent_id> --learn "[category] content"
  ```

But this still uses shell execution. The task explicitly says "no manual terminal shell-out."

**Final approach:** Given OpenClaw's architecture, ALL tools use exec/shell-out under the hood. The proper interpretation is:
1. Create a **shared skill** that makes the tools recognizable and discoverable
2. Use the agent's own ID (from the workspace context) for namespaced calls
3. The SKILL.md format IS the OpenClaw way of registering tools

### 3. Per-Agent Scoping

Centralized: One SKILL.md in `~/.openclaw/skills/obsidian-memory/` covers all agents (forge, syntra, recon, pulse).
Each agent uses its own `agent_id` in the CLI call for namespacing.
The agent_id is determined from the workspace path (last segment).

### 4. Rollback Plan

- Backup `SKILL.md` → `SKILL.md.bak`
- Deleting or reverting the SKILL.md removes the tools
- No changes to `~/.hermes/` or core OpenClaw files

### 5. File Tree

```
~/.openclaw/
└── skills/
    └── obsidian-memory/
        └── SKILL.md          (UPDATE — use agent_api.py, document the two tools)
└── sovereign-memory-v3.1/
    └── openclaw-tool.sh      (CREATE — bash wrapper for agent_api.py with stderr filter)
```

---

## Phase B — Implementation

### Files created/modified:
1. **CREATE** `~/.openclaw/sovereign-memory-v3.1/openclaw-tool.sh` — bash wrapper script (chmod +x)
2. **BACKUP + MODIFY** `~/.openclaw/skills/obsidian-memory/SKILL.md` — updated to use agent_api.py with tool schemas
3. **BACKUP + MODIFY** `~/.openclaw/workspace-forge/AGENTS.md` — added sovereign tool section
4. **BACKUP + MODIFY** `~/.openclaw/workspace-syntra/AGENTS.md` — added sovereign tool section
5. **BACKUP + MODIFY** `~/.openclaw/workspace-recon/AGENTS.md` — added sovereign tool section
6. **BACKUP + MODIFY** `~/.openclaw/workspace-pulse/AGENTS.md` — added sovereign tool section

### Verification (completed):
- `openclaw-tool.sh learn hermes general "Test..."` → `{"success": true, "result": "Stored learning #244"}` ✓
- `openclaw-tool.sh recall hermes "Test learning"` → `{"success": true, "result": "..."}` ✓
- JSON output validated via `python3 -m json.tool` ✓
- All `.bak` files created successfully ✓

### Smoke-test command for user:
```bash
~/.openclaw/sovereign-memory-v3.1/openclaw-tool.sh learn hermes general "Smoke test" && ~/.openclaw/sovereign-memory-v3.1/openclaw-tool.sh recall hermes "Smoke test"
```

Or via a full agent:
```bash
openclaw agent --agent forge --message "Use sovereign_recall to search for 'sovereign memory'" --local
```
