# Cortex MCP Server

MCP server that exposes the xiReactor Cortex knowledge base API as tools for Claude. Supports two transports:

- **Stdio** — for Claude Desktop / Claude Code (local, single-user)
- **Streamable HTTP** — for Claude Co-work (remote, multi-user, OAuth 2.1)

## Setup

```bash
cd mcp/

# Create venv (requires Python 3.10+)
uv venv --python 3.12 .venv
source .venv/bin/activate

# Install dependencies
uv pip install -r requirements.txt
```

## Configuration

| Env Var | Default | Description |
|---|---|---|
| `CORTEX_BASE_URL` | `http://localhost:8010` | Cortex API base URL |
| `CORTEX_API_KEY` | *(required)* | Bearer token for API auth |
| `MCP_BASE_URL` | `http://localhost:8011` | External URL for OAuth issuer (remote only) |
| `MCP_PORT` | `8001` | Port for remote HTTP server |
| `TOKEN_EXPIRY_SECONDS` | `3600` | OAuth access token lifetime |

## Claude Desktop Integration (Stdio)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cortex": {
      "command": "/Users/admina/Projects/xireactor-cortex/mcp/.venv/bin/python",
      "args": ["/Users/admina/Projects/xireactor-cortex/mcp/server.py"],
      "env": {
        "PYTHONUNBUFFERED": "1",
        "CORTEX_API_KEY": "bkai_adm1_testkey_admin"
      }
    }
  }
}
```

Restart Claude Desktop after editing the config. The 11 Cortex tools will appear automatically.

## Claude Co-work Integration (Remote / Streamable HTTP)

### How it works

1. The MCP server runs as a container alongside the Cortex API
2. Claude Co-work connects via OAuth 2.1 (Dynamic Client Registration + PKCE)
3. After auth, Co-work users get access to all 11 Cortex tools in their conversations

### Admin setup: Add custom connector

1. Go to your Claude organization settings (admin access required)
2. Navigate to **Integrations** → **Custom connectors**
3. Add a new connector:
   - **URL:** `http://localhost:8011/mcp` (or your deployed MCP URL)
   - **Name:** Cortex Knowledge Base
4. Claude will auto-discover OAuth endpoints via `/.well-known/oauth-authorization-server`
5. The first connection triggers Dynamic Client Registration — no manual client ID setup needed

### Server-side setup

1. Provision a Cortex API key for the org:
   - The key determines which org's data is accessible
   - One connector instance = one org's permissions (RLS-enforced)

2. Set the API key as an environment variable on the MCP container:
   ```bash
   CORTEX_API_KEY=bkai_XXXX_your_org_key
   ```

3. Deploy via docker-compose (see below)

### Deploy with Docker Compose

```bash
# Set required env vars
export CORTEX_API_KEY=bkai_XXXX_your_org_key
export MCP_BASE_URL=http://localhost:8011  # or your deployed URL

# Start all services
docker-compose up -d
```

Services:
| Service | Internal Port | External Port | Description |
|---|---|---|---|
| `db` | 5432 | 5442 | PostgreSQL + pgvector |
| `api` | 8000 | 8010 | Cortex REST API |
| `mcp` | 8001 | 8011 | MCP remote server |

### Reverse proxy configuration

For production, the MCP server needs to be accessible at your public domain (e.g., `https://your-domain.example.com/mcp`). Configure your reverse proxy (nginx/Caddy) to route:

**Nginx:**
```nginx
# Cortex API (existing)
location / {
    proxy_pass http://localhost:8010;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}

# MCP server — OAuth endpoints + Streamable HTTP
location /mcp {
    proxy_pass http://localhost:8011/mcp;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;        # Required for SSE streaming
    proxy_cache off;
}

# MCP OAuth endpoints (must be at root for discovery)
location /.well-known/oauth-authorization-server {
    proxy_pass http://localhost:8011/.well-known/oauth-authorization-server;
}
location /.well-known/oauth-protected-resource {
    proxy_pass http://localhost:8011/.well-known/oauth-protected-resource;
}
location /authorize {
    proxy_pass http://localhost:8011/authorize;
}
location /token {
    proxy_pass http://localhost:8011/token;
}
location /register {
    proxy_pass http://localhost:8011/register;
}
location /revoke {
    proxy_pass http://localhost:8011/revoke;
}
```

**Caddy:**
```caddyfile
your-domain.example.com {
    handle /mcp* {
        reverse_proxy localhost:8011
    }
    handle /.well-known/oauth-* {
        reverse_proxy localhost:8011
    }
    handle /authorize {
        reverse_proxy localhost:8011
    }
    handle /token {
        reverse_proxy localhost:8011
    }
    handle /register {
        reverse_proxy localhost:8011
    }
    handle /revoke {
        reverse_proxy localhost:8011
    }
    handle {
        reverse_proxy localhost:8010
    }
}
```

## Tool Inventory

| Tool | API Endpoint | Description |
|---|---|---|
| `search_entries` | `GET /entries` | Full-text search + filters (content_type, path, department, tag) |
| `get_entry` | `GET /entries/{id}` | Retrieve single entry by ID |
| `get_index` | `GET /index` | Tiered index map (L1-L5 depth levels) |
| `get_neighbors` | `GET /entries/{id}/links` | Graph traversal — linked entries at depth 1-3 |
| `create_entry` | `POST /entries` | Create new KB entry |
| `update_entry` | `PUT /entries/{id}` | Partial update with auto-versioning |
| `delete_entry` | `DELETE /entries/{id}` | Soft-delete (archive) |
| `create_link` | `POST /entries/{id}/links` | Create typed link between entries |
| `submit_staging` | `POST /staging` | Submit change to governance pipeline |
| `list_staging` | `GET /staging` | List staging items by status |
| `review_staging` | `POST /staging/{id}/approve\|reject` | Approve or reject staging item (admin) |

## Smoke Tests

```bash
# Stdio transport (against live API)
CORTEX_API_KEY=bkai_adm1_testkey_admin python test_tools.py

# Remote transport (start server first, or use docker-compose)
MCP_TEST_URL=http://localhost:8011 python test_remote.py
```
