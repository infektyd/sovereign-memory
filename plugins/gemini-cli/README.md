# Gemini CLI Memory Gemini CLI Plugin

Local-first Gemini CLI plugin for Gemini CLI Memory. It exposes recall, AI-facing vault context packs, manual vault-first learning, learning quality checks, structured Obsidian note writes, and audit tools through MCP.

The optional frontend lives in `frontend/`. Run `npm run console` to start the local-only bridge at `http://127.0.0.1:8765/` and generate real `sovereign_prepare_task` and `sovereign_prepare_outcome` packets from the same backend functions used by MCP/CLI. Opening `frontend/index.html` directly still works as a static packet inspector fallback.

## Runtime Defaults

- Sovereign daemon socket: `/tmp/sovereign.sock`
- AFM health URL: `http://127.0.0.1:11437/health`
- Gemini CLI vault: `~/.gemini-cli/gemini_cli-vault`

Override with:

```bash
export SOVEREIGN_GEMINI_CLI_VAULT_PATH=/path/to/gemini_cli-vault
export SOVEREIGN_SOCKET_PATH=/tmp/sovereign.sock
export SOVEREIGN_AFM_HEALTH_URL=http://127.0.0.1:11437/health
export SOVEREIGN_AFM_PREPARE_TASK_URL=http://127.0.0.1:11437/v1/chat/completions
```

## Tools

- `sovereign_status`
- `sovereign_prepare_task`
- `sovereign_prepare_outcome`
- `sovereign_route`
- `sovereign_recall`
- `sovereign_learning_quality`
- `sovereign_learn`
- `sovereign_vault_write`
- `sovereign_audit_report`
- `sovereign_audit_tail`

Automatic behavior should remain recall-only. `sovereign_route` can recommend recall/status/audit automatically, but learning and vault writes stay manual and vault-first. `sovereign_learn` returns a quality report and can block weak memories with `requireQuality`.

## Local Console

```bash
npm run console
```

The console exposes only local HTTP endpoints:

- `GET /api/health`
- `GET /api/status`
- `GET /api/audit-tail?limit=20`
- `POST /api/prepare-task`
- `POST /api/prepare-outcome`

The server binds to `127.0.0.1`, refuses non-local bind hosts, rejects non-local host/origin/fetch-metadata requests, requires JSON POST bodies, caps JSON request bodies, redacts machine-local paths in browser-facing status/audit responses, and does not expose learn or vault-write endpoints. Browser requests cannot override the server-owned vault path or AFM target. `prepare-task` keeps its existing audit behavior; `prepare-outcome` remains dry-run only.

## Development

```bash
npm install
npm test
npm run console
npm run design:lint
npm run test:live:prepare
```
