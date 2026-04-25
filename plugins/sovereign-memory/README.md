# Sovereign Memory Codex Plugin

Local-first Codex plugin for Sovereign Memory. It exposes recall, manual vault-first learning, structured Obsidian note writes, and audit-tail tools through MCP.

## Runtime Defaults

- Sovereign daemon socket: `/tmp/sovereign.sock`
- AFM health URL: `http://127.0.0.1:11437/health`
- Codex vault: `~/.sovereign-memory/codex-vault`

Override with:

```bash
export SOVEREIGN_CODEX_VAULT_PATH=/path/to/codex-vault
export SOVEREIGN_SOCKET_PATH=/tmp/sovereign.sock
export SOVEREIGN_AFM_HEALTH_URL=http://127.0.0.1:11437/health
```

## Tools

- `sovereign_status`
- `sovereign_recall`
- `sovereign_learn`
- `sovereign_vault_write`
- `sovereign_audit_tail`

Automatic behavior should remain recall-only. Learning is manual and vault-first.

## Development

```bash
npm install
npm test
```
