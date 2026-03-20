# Client Setup

This document shows how to connect the VM-hosted OCI Monitoring MCP server to Codex, Claude, and
ChatGPT after the server is running.

In pilot mode, each tester/client pair must use a tokenized MCP URL:

```text
http://<vm-public-ip>:8000/mcp?u=<token>
```

If you place Nginx or another reverse proxy in front of the VM, replace that with your HTTPS URL.

## 1. Before You Add It to a Client

Confirm the server is already running on the VM from `docs/QUICKSTART.md`.

If you keep the default environment variables, these are the important URLs:

```text
MCP server URL: http://<vm-public-ip>:8000/mcp?u=<token>
Artifact base URL: http://<vm-public-ip>:8765
```

The main MCP endpoint is what you add to Codex, Claude, or ChatGPT. The artifact base URL is only
used for chart and CSV links returned by the server.

Create tokens on the VM with:

```bash
python3 scripts/manage_users.py add "alice" --client codex
python3 scripts/manage_users.py add "alice" --client claude
```

Important:
- Use the same `user_id` for the same person across clients.
- Codex and Claude must use different tokens.
- Each token maps to a separate profile directory and separate saved context.

## 2. Codex CLI and Codex Desktop

Codex CLI and the Codex IDE/desktop surfaces share MCP configuration, so adding the server once is
enough for both.

### Recommended command
```bash
codex mcp add ociMonitoring --url "http://<vm-public-ip>:8000/mcp?u=<token>"
codex mcp list
```

### Config file alternative
Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.ociMonitoring]
url = "http://<vm-public-ip>:8000/mcp?u=<token>"
```

After that, open Codex and ask for a monitoring query such as:

```text
show me all compute instances with CPU utilization above 80% in the last 1 hour
```

## 3. Claude Code

Add the remote HTTP server:

```bash
claude mcp add --transport http ociMonitoring "http://<vm-public-ip>:8000/mcp?u=<token>"
claude mcp list
```

Inside Claude Code, use:

```text
/mcp
```

Use `/mcp` to check status and complete authentication if you later place OAuth in front of the
server.

## 4. Claude Desktop

Claude Desktop app should be treated differently from Codex and Claude Code.

For the desktop app, use a local stdio bridge such as `mcp-remote` so Claude Desktop can talk to
the remote HTTP MCP endpoint.

Prerequisites:
- Node.js 18+
- Claude Desktop installed

Add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "oci-mon": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://<vm-public-ip>:8000/mcp?u=<token>"
      ]
    }
  }
}
```

Typical config file locations:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\\Claude\\claude_desktop_config.json`

After editing the file, restart Claude Desktop.

Why this is needed:
- Claude Desktop app expects a local stdio MCP process
- this server exposes remote streamable HTTP
- `mcp-remote` bridges stdio to HTTP

Do not validate Claude Desktop by opening `/mcp?...` in a browser. Validate the server with
`/healthz`, then let Claude Desktop talk to the MCP endpoint through the bridge.

If you are using Claude.ai web connectors instead of Claude Desktop app, the setup may be different
from the local desktop app flow above.

## 5. ChatGPT Web Developer Mode

Use ChatGPT developer mode if you want to test the same remote server from ChatGPT on the web.

### Steps
1. Go to `Settings > Apps > Advanced settings > Developer mode` and enable it.
2. Open `Settings > Apps`.
3. Click `Create app`.
4. Enter the remote MCP URL:

```text
http://<vm-public-ip>:8000/mcp?u=<token>
```

5. Save the app, then enable it in a developer mode conversation.

This prototype uses streamable HTTP at `/mcp`, which matches the supported remote MCP transport.

## 6. Fastest Validation Path

For the quickest prototype test:
- add it to Codex first
- let the server ask for initial setup on the first query, or run `setup_default_context`
- ask one CPU query
- then add the Claude-specific token to Claude Code
- use Claude Desktop or ChatGPT only after the public endpoint is stable

## 7. Common Mistakes

- Using the VM root URL instead of the MCP path. Use `/mcp`.
- Adding the artifact URL instead of the MCP URL. Add port `8000`, not `8765`.
- Reusing the same token across Codex and Claude. Use one token per person/client pair.
- Using the bare MCP URL in pilot mode. In pilot mode you need `?u=<token>`.
- Testing `/mcp?...` in a browser and assuming `404` means the server is broken. Use `/healthz` for health checks.
- Treating Claude Desktop app like Codex. Claude Desktop app typically needs a stdio-to-HTTP bridge such as `mcp-remote`.
- Exposing HTTP publicly for longer-term use. For anything beyond short prototype testing, put
  HTTPS in front of both the MCP endpoint and the artifact endpoint.
