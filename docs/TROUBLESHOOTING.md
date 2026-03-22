# Troubleshooting

Common issues and solutions for the OCI Monitoring MCP Server.

## My metric or namespace isn't recognized

The server uses a metric registry (`data/metric_registry.yaml`) to map natural-language metric names to OCI Monitoring namespaces and metric names.

**Solutions:**
- Check if the namespace is listed in `data/metric_registry.yaml`
- If the namespace is not in the static registry, the server will attempt runtime discovery via the OCI ListMetrics API
- Ensure the namespace name matches OCI's namespace exactly (e.g., `oci_computeagent`, `oci_vcn`, not `computeagent` or `vcn`)
- After adding a namespace to the registry YAML, restart the server: `sudo systemctl restart oci-mon-mcp-server`
- You can override the registry path with the `OCI_MON_MCP_METRIC_REGISTRY_PATH` environment variable

## My compute instance doesn't appear in results

Instance cache has a 15-minute TTL with stale-while-revalidate. New instances will appear within 15 minutes of creation. The cache refreshes in the background — the first query after TTL expires serves stale data while refreshing.

**Solutions:**
- Wait up to 15 minutes for the cache to refresh automatically
- Restart the server to clear the cache: `sudo systemctl restart oci-mon-mcp-server`
- Adjust the TTL via `OCI_MON_MCP_INSTANCE_CACHE_TTL` (in seconds, default: `900`)

## I don't see my new compute instance in results

The server caches compute instance listings for **15 minutes** (stale-while-revalidate). A newly created or deleted instance may not appear immediately.

**Solutions:**
- Wait up to 15 minutes for the cache to refresh automatically
- Restart the server to clear the cache: `sudo systemctl restart oci-mon-mcp-server`
- Adjust the TTL via `OCI_MON_MCP_INSTANCE_CACHE_TTL` (in seconds, default: `900`)

The cache uses stale-while-revalidate: when the TTL expires, the next query still returns cached data instantly while a background thread refreshes the cache for subsequent queries.

## Queries are slow (10+ seconds)

The primary bottleneck is OCI Monitoring API latency, not server CPU or memory. Typical breakdown:

| Step | Time |
|---|---|
| Instance listing (cached after first query) | ~3-5s first, ~0s cached |
| OCI Monitoring API call | ~5-7s |
| Chart generation | <1s |

**What helps:**
- Instance caching saves ~3-5s on repeat queries (automatic)
- Multi-metric queries (CPU+Memory) run in parallel
- Coarser intervals reduce data transfer (automatic for longer time ranges)

**What does NOT help:**
- Upgrading VM CPU/RAM (server uses <60MB memory)
- Caching metric data (users expect current values)

## Charts not rendering inline (Codex / non-Claude clients)

Inline chart rendering depends on client support. Codex does not currently render MCP image content or markdown image URLs inline.

**Workaround:** Use the clickable artifact URL link provided below the table to view the chart in a browser. The artifact URL is always included in the response.

## LLM client auto-switches auth to config file

Some LLM clients (Claude Desktop, Codex) proactively call `configure_auth_fallback`, breaking instance principal auth.

The server has a code-level guard: `configure_auth_fallback` requires `user_confirmed=true` and will reject calls without it.

**If auth was already switched:**
- Call `use_instance_principals` to revert
- The server instructions tell clients not to call `configure_auth_fallback` proactively

## Output validation error: None is not of type 'string'

This occurs when a tool has a TypedDict return annotation with optional fields. The JSON Schema validation rejects `null` values for string-typed fields.

**Affected tools:** `discover_accessible_compartments` (fixed by removing TypedDict annotation)

If you see this on other tools, the fix is to either remove the TypedDict return annotation or make fields nullable.

## Server not reachable after VM reboot

The server uses a systemd service that auto-starts on boot.

**Check status:**
```bash
sudo systemctl status oci-mon-mcp-server --no-pager -l
```

**If not running:**
```bash
sudo systemctl start oci-mon-mcp-server
```

**Verify auto-start is enabled:**
```bash
systemctl is-enabled oci-mon-mcp-server
```

## Query returns "needs_clarification" for default context

First-time users must set up their default region and compartment before querying.

**Solution:** Call `setup_default_context` with explicit `region` and `compartment_name` parameters.

In pilot mode (token-based auth), context is stored per-profile, not globally.

## Combined CPU+memory query not supported

The query parser currently handles single-metric queries. "Show CPU and memory" is not parsed as a valid query.

**Workaround:** Run two separate queries:
1. "Show CPU utilization for all instances"
2. "Show memory utilization for all instances"

## Inline chart not rendering in Claude Code CLI

Terminal clients cannot render images — this is expected behavior. The chart image is in the MCP response but terminals cannot display it.

Charts render inline on:
- Claude Desktop
- Codex app

The artifact URL link is always available as a fallback in any client.

## How to update the server after code changes

```bash
ssh opc@<vm-ip>
cd /path/to/oci-mon-mcp-server
git pull --ff-only origin <branch>
sudo systemctl restart oci-mon-mcp-server
sudo systemctl status oci-mon-mcp-server --no-pager -l
```

Always use `--ff-only` for safe pulls that fail if there are conflicts rather than creating merge commits.

## Chart data looks coarse / too few data points

The server uses optimized intervals to balance chart detail with query performance:

| Time Range | Interval | Data Points |
|---|---|---|
| 15 minutes | 1 min | ~15 |
| 30 minutes | 1 min | ~30 |
| 1 hour | 5 min | ~12 |
| 6 hours | 5 min | ~72 |
| 24 hours | 1 hour | ~24 |
| 7 days | 1 day | ~7 |

Result correctness (max, avg, latest values) is not affected — OCI computes aggregations at whatever interval is specified. Only the chart granularity changes.

## Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `OCI_MON_MCP_METRIC_REGISTRY_PATH` | `data/metric_registry.yaml` | Path to metric registry YAML |
| `OCI_MON_MCP_INSTANCE_CACHE_TTL` | `900` | Instance listing cache TTL in seconds |
| `OCI_MON_MCP_ARTIFACT_PORT` | `8765` | Artifact HTTP server port |
| `OCI_MON_MCP_ARTIFACT_HOST` | `0.0.0.0` | Artifact HTTP server bind address |
| `OCI_MON_MCP_ARTIFACT_BASE_URL` | (auto) | Base URL for artifact links |
| `OCI_MON_MCP_HOST` | `0.0.0.0` | MCP server bind address |
| `OCI_MON_MCP_PORT` | `8000` | MCP server port |
| `OCI_MON_MCP_REQUIRE_TOKEN` | `0` | Require user token for multi-user pilot |
| `OCI_MON_MCP_JSON_RESPONSE` | `1` | Use JSON responses instead of SSE |
| `OCI_MON_MCP_STATELESS_HTTP` | `1` | Enable stateless HTTP mode |
| `OCI_MON_MCP_TRANSPORT` | `streamable-http` | MCP transport type |
