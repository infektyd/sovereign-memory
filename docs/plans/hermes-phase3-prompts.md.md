# Hermes Phase 3 — Remaining Cleanup Prompts

All prompts addressed to the Hermes agent. The Tailwind migration (3b) should be delegated to Claude Code due to the scale (393 inline styles across 18 files).

---

## Prompt 3a: Silent Error Logging (annotate catch blocks)

```
There are 11 empty catch blocks in hermes-command-center. I audited all of them and they're all intentionally optional — none are bug-hiders. Just add a brief comment to each one explaining why the error is suppressed.

Here's the full list — add a // comment inside each catch body:

1. src/components/ThemeInit.tsx:17 — catch(e) {}
   Add: // Theme init is best-effort; failure falls back to default theme

2. src/components/ThemeSelector.tsx:86 — catch {}
   Add: // localStorage may be unavailable (SSR, private browsing)

3. src/components/ThemeSelector.tsx:95 — catch {}
   Add: // Falls back to default theme if localStorage is unavailable

4. src/components/chat/ChatInput.tsx:54 — catch { // skip failed uploads }
   Already has a comment — leave as-is

5. src/components/GatewaySettings.tsx:16 — catch { setStatus("offline"); }
   Not empty — sets offline status. Leave as-is.

6. src/app/api/chat/send/route.ts:68 — catch {}
   Add: // Session index update is non-critical; message was already sent

7. src/app/api/dashboard/route.ts:61 — catch { /* no config */ }
   Already has a comment — leave as-is

8. src/app/api/dashboard/route.ts:116 — catch { /* skip */ }
   Replace with: // Malformed JSONL line; skip and continue

9. src/app/api/dashboard/route.ts:131 — catch { /* file missing */ }
   Already has a comment — leave as-is

10. src/app/api/dashboard/route.ts:165 — catch { /* skip */ }
    Replace with: // Malformed JSONL line; skip during fallback scan

11. src/app/api/dashboard/route.ts:172 — catch { /* no sessions dir */ }
    Already has a comment — leave as-is

Only modify the 5 blocks that need comments added or improved (items 1, 2, 3, 6, 8, 10). Leave the rest alone. Run npm run build when done.
```

---

## Prompt 3b: Tailwind Migration (delegate to Claude Code)

This is the big one — 393 inline CSS variable styles and 7 hover handler locations across 18 files. Delegate to Claude Code and break it into batches.

```
Delegate this to your Claude Code agent. It's a large migration across 18 files.

hermes-command-center uses Tailwind CSS v4 but has 393 inline style={{ }} props using CSS variables like var(--text-primary) instead of Tailwind utility classes. There are also 7 locations using onMouseEnter/onMouseLeave JS handlers instead of Tailwind hover: classes.

MIGRATION RULES:
- style={{ color: "var(--text-primary)" }} → className="text-[var(--text-primary)]"
- style={{ background: "var(--card-bg)" }} → className="bg-[var(--card-bg)]"
- style={{ borderColor: "var(--border-color)" }} → className="border-[var(--border-color)]"
- style={{ backgroundColor: "var(--bg-secondary)" }} → className="bg-[var(--bg-secondary)]"
- Compound styles: style={{ color: "var(--text-muted)", fontSize: "0.875rem" }} → className="text-[var(--text-muted)] text-sm"
- onMouseEnter/onMouseLeave hover handlers → Tailwind hover: classes, remove the associated useState

Here are the 21 CSS variables and their suggested Tailwind arbitrary value mappings:
- var(--text-muted) → text-[var(--text-muted)]
- var(--border-color) → border-[var(--border-color)]
- var(--text-primary) → text-[var(--text-primary)]
- var(--accent-primary) → text-[var(--accent-primary)] or bg-[var(--accent-primary)]
- var(--text-secondary) → text-[var(--text-secondary)]
- var(--bg-primary) → bg-[var(--bg-primary)]
- var(--card-bg) → bg-[var(--card-bg)]
- var(--bg-secondary) → bg-[var(--bg-secondary)]
- var(--status-error) → text-[var(--status-error)] or bg-[var(--status-error)]
- var(--status-success) → text-[var(--status-success)]
- var(--bg-tertiary) → bg-[var(--bg-tertiary)]
- var(--status-warning) → text-[var(--status-warning)]
- var(--accent-tertiary) → text-[var(--accent-tertiary)]
- var(--accent-secondary) → text-[var(--accent-secondary)]
- var(--status-info) → text-[var(--status-info)]
- var(--input-border) → border-[var(--input-border)]
- var(--input-bg) → bg-[var(--input-bg)]
- var(--link-color) → text-[var(--link-color)]
- var(--sidebar-bg) → bg-[var(--sidebar-bg)]
- var(--border-color-hover) → hover:border-[var(--border-color-hover)]
- var(--nord13, #ebcb8b) → text-[var(--nord13,#ebcb8b)]

HOVER HANDLERS TO REPLACE (7 locations):
1. src/components/ThemeSelector.tsx:125-130 — border color toggle → hover:border-[var(--accent-primary)]
2. src/components/Sidebar.tsx:160-171 — bg + text color on hover → hover:bg-[var(--bg-secondary)] hover:text-[var(--text-primary)]
3. src/components/GatewaySettings.tsx:100-105 — border accent on hover → hover:border-[var(--accent-primary)]
4. src/app/sessions/page.tsx:318-323 — row bg highlight → hover:bg-[var(--bg-secondary)]
5. src/app/sessions/page.tsx:377-384 — delete button error color → hover:text-[var(--status-error)] hover:bg-[var(--status-error)]/10
6. src/app/cron/page.tsx:1085-1090 — card border accent → hover:border-[var(--accent-primary)]
7. src/app/memory/page.tsx:382-387 — table row bg highlight → hover:bg-[var(--bg-secondary)]

For each hover handler, also remove the associated useState (e.g., const [hovered, setHovered] = useState(false)) if it becomes unused.

WORK ORDER — Do these files in this order, running npm run build after each batch:

Batch 1 — Small components (55 inline styles total):
- src/components/chat/MessageBubble.tsx (2)
- src/components/chat/FileAttachment.tsx (1)
- src/components/chat/ChatInput.tsx (9)
- src/components/chat/SessionList.tsx (14)
- src/components/ThemeSelector.tsx (5)
- src/components/Sidebar.tsx (9)
- src/components/GatewaySettings.tsx (7)
- src/components/ErrorBoundary.tsx (8)
→ npm run build

Batch 2 — Larger components:
- src/components/AgentConfig.tsx (13)
→ npm run build

Batch 3 — Pages with fewer styles:
- src/app/chat/page.tsx (10)
- src/app/settings/page.tsx (10)
→ npm run build

Batch 4 — Medium pages:
- src/app/agents/page.tsx (26)
- src/app/sessions/page.tsx (29)
→ npm run build

Batch 5 — Heavy pages:
- src/app/dashboard/page.tsx (37)
- src/app/groupchat/page.tsx (36)
→ npm run build

Batch 6 — Heaviest pages:
- src/app/system/page.tsx (48)
- src/app/memory/page.tsx (55)
- src/app/cron/page.tsx (87)
→ npm run build

CONSTRAINTS:
- Visual appearance must remain identical — same colors, hover effects, transitions
- If a style prop has non-variable values alongside variables (e.g., style={{ color: "var(--x)", padding: "12px 16px" }}), convert the variable part to className and keep the fixed part as style if there's no clean Tailwind equivalent. Most spacing values do have Tailwind equivalents though (12px = p-3, 16px = p-4).
- Keep any transition or animation inline styles that don't have clean Tailwind equivalents
- After the full migration, grep for 'style={{' across src/ — the count should be minimal (only non-variable inline styles should remain)
```

---

## Prompt 3c: Remove Unused Dependencies + Fix require()

```
Three quick cleanups in hermes-command-center:

1. Remove "ws" from both dependencies and devDependencies in package.json. It's confirmed unused — not imported anywhere in src/.

2. Remove "uuid" and "@types/uuid" from package.json. Also confirmed unused — crypto.randomUUID() is used instead at src/lib/data.ts:476.

3. In src/lib/data.ts, there are three dynamic require("child_process") calls inside function bodies:
   - Line 324: const { execSync } = require("child_process") in getOpenPorts()
   - Line 354: const { execSync } = require("child_process") in getRunningProcesses()
   - Line 551: const { spawn } = require("child_process") in sendMessage()

   Replace all three with a single top-level import at the top of the file:
   import { execSync, spawn } from "child_process";
   Then remove the three require() lines.

Run npm install to update the lockfile, then npm run build and npm run lint.
```

---

## Prompt 3d: Gateway URL Configuration

```
"http://localhost:18789" is hardcoded in 4 places across hermes-command-center. Centralize it with an environment variable.

SERVER-SIDE (use GATEWAY_URL):
1. src/app/api/gateway/status/route.ts:3 — change to:
   const GATEWAY_URL = process.env.GATEWAY_URL || "http://localhost:18789";

2. src/app/api/dashboard/route.ts:6 — same change:
   const GATEWAY_URL = process.env.GATEWAY_URL || "http://localhost:18789";

CLIENT-SIDE (use NEXT_PUBLIC_GATEWAY_URL):
3. src/components/GatewaySettings.tsx:6 — change the default state to:
   const [gatewayUrl, setGatewayUrl] = useState(process.env.NEXT_PUBLIC_GATEWAY_URL || "http://localhost:18789");

4. src/components/GatewaySettings.tsx:62 — keep the placeholder as "http://localhost:18789" (that's just hint text, fine to hardcode)

Create .env.example at the project root:
```
# OpenClaw Gateway URL (server-side API routes)
GATEWAY_URL=http://localhost:18789

# OpenClaw Gateway URL (client-side components)
NEXT_PUBLIC_GATEWAY_URL=http://localhost:18789
```

The line 335 in data.ts is just a comment mentioning port 18789 — leave it alone.

Run npm run build.
```

---

## Execution Order

Give these to Hermes in this sequence:

1. **Prompt 3c** (unused deps + require fix) — quickest, independent
2. **Prompt 3d** (gateway URL) — quick, independent
3. **Prompt 3a** (catch block comments) — quick, independent
4. **Prompt 3b** (Tailwind migration) — biggest, delegate to Claude Code, run last since it touches every file
