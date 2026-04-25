# Sovereign Memory Codex Plugin

Local-first Codex plugin for Sovereign Memory. It exposes recall, AI-facing vault context packs, manual vault-first learning, learning quality checks, structured Obsidian note writes, and audit tools through MCP.

The optional static frontend lives in `frontend/`. Open `frontend/index.html` in a browser to inspect `sovereign_prepare_task` and `sovereign_prepare_outcome` packets without running a server.

## Runtime Defaults

- Sovereign daemon socket: `/tmp/sovereign.sock`
- AFM health URL: `http://127.0.0.1:11437/health`
- Codex vault: `~/.sovereign-memory/codex-vault`

Override with:

```bash
export SOVEREIGN_CODEX_VAULT_PATH=/path/to/codex-vault
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

## Development

```bash
npm install
npm test
npm run design:lint
npm run test:live:prepare
```
