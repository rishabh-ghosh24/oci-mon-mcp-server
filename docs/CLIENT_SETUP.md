# Client Setup

This document shows how to connect the VM-hosted OCI Monitoring MCP server to Codex, Claude, and
ChatGPT after the server is running.

Assume your remote MCP endpoint is:

```text
http://<vm-public-ip>:8000/mcp
```

If you place Nginx or another reverse proxy in front of the VM, replace that with your HTTPS URL.

## 1. Before You Add It to a Client

Confirm the server is already running on the VM from `docs/QUICKSTART.md`.

If you keep the default environment variables, these are the important URLs:

```text
MCP server URL: http://<vm-public-ip>:8000/mcp
Artifact base URL: http://<vm-public-ip>:8765
```

The main MCP endpoint is what you add to Codex, Claude, or ChatGPT. The artifact base URL is only
used for chart and CSV links returned by the server.

## 2. Codex CLI and Codex Desktop

Codex CLI and the Codex IDE/desktop surfaces share MCP configuration, so adding the server once is
enough for both.

### Recommended command
```bash
codex mcp add ociMonitoring --url http://<vm-public-ip>:8000/mcp
codex mcp list
```

### Config file alternative
Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.ociMonitoring]
url = "http://<vm-public-ip>:8000/mcp"
```

After that, open Codex and ask for a monitoring query such as:

```text
show me all compute instances with CPU utilization above 80% in the last 1 hour
```

## 3. Claude Code

Add the remote HTTP server:

```bash
claude mcp add --transport http ociMonitoring http://<vm-public-ip>:8000/mcp
claude mcp list
```

Inside Claude Code, use:

```text
/mcp
```

Use `/mcp` to check status and complete authentication if you later place OAuth in front of the
server.

## 4. Claude Desktop

For this VM deployment, treat the server as a remote MCP server, not a local stdio server.

### Pro and Max plans
1. Open `Settings > Connectors`.
2. Click `Add custom connector`.
3. Enter the remote MCP URL:

```text
http://<vm-public-ip>:8000/mcp
```

4. Save it.
5. In a conversation, enable the connector from the `+` menu under `Connectors`.

### Team and Enterprise
1. An owner first adds the connector in `Organization settings > Connectors`.
2. Individual users then go to `Settings > Connectors`.
3. They click `Connect` for that custom connector.
4. They enable it in the conversation from the `+` menu under `Connectors`.

Do not use `claude_desktop_config.json` for this VM-hosted remote server. Claude Desktop does not
use that file for remote MCP connectors.

## 5. ChatGPT Web Developer Mode

Use ChatGPT developer mode if you want to test the same remote server from ChatGPT on the web.

### Steps
1. Go to `Settings > Apps > Advanced settings > Developer mode` and enable it.
2. Open `Settings > Apps`.
3. Click `Create app`.
4. Enter the remote MCP URL:

```text
http://<vm-public-ip>:8000/mcp
```

5. Save the app, then enable it in a developer mode conversation.

This prototype uses streamable HTTP at `/mcp`, which matches the supported remote MCP transport.

## 6. Fastest Validation Path

For the quickest prototype test:
- add it to Codex first
- run `setup_default_context`
- ask one CPU query
- then add the same endpoint to Claude Code
- use Claude Desktop or ChatGPT only after the public endpoint is stable

## 7. Common Mistakes

- Using the VM root URL instead of the MCP path. Use `/mcp`.
- Adding the artifact URL instead of the MCP URL. Add port `8000`, not `8765`.
- Using `claude_desktop_config.json` for a remote server. That is for local stdio-style setups.
- Exposing HTTP publicly for longer-term use. For anything beyond short prototype testing, put
  HTTPS in front of both the MCP endpoint and the artifact endpoint.
