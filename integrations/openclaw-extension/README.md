# Sovereign Memory OpenClaw Extension

OpenClaw plugin bridge for Sovereign Memory. The extension exposes the Python
Sovereign Memory package to OpenClaw through a small Unix-socket HTTP daemon and
a TypeScript memory manager.

## Architecture

- `sovrd.py` starts a local HTTP server over a Unix socket.
- `src/bridge.ts` calls the daemon with reconnect handling.
- `src/sovereign-manager.ts` implements the OpenClaw memory manager surface.
- `src/types.ts` defines layer and workspace metadata for memory routing.

The extension is local-first. It does not require cloud API keys.

## Configuration

The daemon and bridge use environment variables instead of machine-specific
paths:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SOVEREIGN_SOCKET_PATH` | `/tmp/sovereign.sock` | Unix socket for the daemon |
| `SOVEREIGN_DB_PATH` | package default | SQLite database path |
| `SOVEREIGN_VAULT_PATH` | package default | Vault/wiki root for file reads |
| `SOVEREIGN_DEFAULT_AGENT_ID` | `default` | Agent fallback when no agent ID is supplied |
| `SOVEREIGN_PYTHON` | `python3` | Python executable used by the TypeScript process supervisor |
| `OPENCLAW_HOME` | `.openclaw` under the user home | Optional flat-file mirror root |

## Development

```bash
cd integrations/openclaw-extension
npm install
npm run build
python3 sovrd.py
```

In another shell:

```bash
curl --unix-socket /tmp/sovereign.sock http://localhost/health
npm run smoke:day5
```

## Endpoints

| Method | Endpoint | Description |
| --- | --- | --- |
| `GET` | `/health` | Health check |
| `GET` | `/recall?q=...` | Recall memories by query |
| `GET` | `/read?key=...` | Read memory by key |
| `GET` | `/identity` | Get identity context |
| `GET` | `/full` | Get identity plus memory context |
| `POST` | `/learn` | Store a learning |

## Notes

Generated build output, `node_modules`, local logs, model adapters, and machine
paths are intentionally excluded from the public repository.
