# Quickstart

## 1. Goal
This quickstart gets the prototype running on an OCI Compute VM so you can test the end-to-end
flow from natural-language query to live metric results.

## 2. Prerequisites
- OCI tenancy with Monitoring data in the target compartment
- OCI Compute VM on Oracle Linux 9
- Python 3.11+
- network egress from the VM to OCI APIs
- public access to the VM if you want remote clients to open artifact URLs directly

Important:
- run the install commands from the repository root, the directory that contains `pyproject.toml`
- use Python 3.11 explicitly; many OEL images still default `python3` to 3.9

## 3. Auth Model
- Preferred: Instance Principals
- Fallback: OCI config profile

The prototype will try Instance Principals first. If that fails during live execution, it can ask
to switch to OCI config fallback.

## 4. IAM Baseline
Use a dedicated dynamic group for the VM.

### Minimum read path for this prototype
```text
Allow dynamic-group <mcp_vm_dynamic_group> to inspect compartments in tenancy
Allow dynamic-group <mcp_vm_dynamic_group> to inspect instances in compartment <observability_compartment>
Allow dynamic-group <mcp_vm_dynamic_group> to read metrics in compartment <observability_compartment>
```

### Tenancy-wide prototype variant
```text
Allow dynamic-group <mcp_vm_dynamic_group> to inspect compartments in tenancy
Allow dynamic-group <mcp_vm_dynamic_group> to inspect instances in tenancy
Allow dynamic-group <mcp_vm_dynamic_group> to read metrics in tenancy
```

Validate policy syntax in OCI before rollout.

## 5. Install
If Python 3.11 is not installed yet:

```bash
sudo dnf install -y python3.11 python3.11-devel
```

Clone the repo and move into it:

```bash
git clone <your-repo-url> /home/opc/oci-mon-mcp-server
cd /home/opc/oci-mon-mcp-server
ls pyproject.toml
python3.11 --version
```

Create the virtualenv with Python 3.11:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

For local development and tests:
```bash
pip install -e ".[dev]"
```

If `ls pyproject.toml` fails, you are not in the repo root yet.

## 6. Runtime Environment
Set these before starting the server on the VM:

```bash
export OCI_MON_MCP_HOST=0.0.0.0
export OCI_MON_MCP_PORT=8000
export OCI_MON_MCP_TRANSPORT=streamable-http

# Public base URL used in PNG/CSV artifact links.
# Replace with the VM public IP or DNS name.
export OCI_MON_MCP_ARTIFACT_BASE_URL=http://<vm-public-ip>:8765
export OCI_MON_MCP_ARTIFACT_HOST=0.0.0.0
export OCI_MON_MCP_ARTIFACT_PORT=8765
export OCI_MON_MCP_PUBLIC_HOST=<vm-public-ip>
export OCI_MON_MCP_PUBLIC_PORT=8000

# Avoid matplotlib cache warnings on the VM.
export MPLCONFIGDIR=/tmp/matplotlib
# Optional: suppress expected noisy /mcp probe access logs (404/400).
export OCI_MON_MCP_SUPPRESS_EXPECTED_MCP_PROBE_LOGS=1
# Optional: location for shared VM runtime state (templates/shared learnings/profile memory).
# Defaults to <repo>/data/runtime
export OCI_MON_MCP_STATE_DIR=/home/opc/oci-mon-mcp-server/data/runtime
# Enable token enforcement for the shared-VM pilot.
export OCI_MON_MCP_REQUIRE_TOKEN=1
```

If you want the main MCP endpoint on a different path:
```bash
export OCI_MON_MCP_STREAMABLE_HTTP_PATH=/mcp
```

### Optional: persist runtime env vars once (recommended)
If you usually run the server manually, save exports in `~/.bashrc` once:

```bash
cat >> ~/.bashrc <<'EOF'
export OCI_MON_MCP_HOST=0.0.0.0
export OCI_MON_MCP_PORT=8000
export OCI_MON_MCP_TRANSPORT=streamable-http
export OCI_MON_MCP_ARTIFACT_BASE_URL=http://<vm-public-ip>:8765
export OCI_MON_MCP_ARTIFACT_HOST=0.0.0.0
export OCI_MON_MCP_ARTIFACT_PORT=8765
export OCI_MON_MCP_PUBLIC_HOST=<vm-public-ip>
export OCI_MON_MCP_PUBLIC_PORT=8000
export MPLCONFIGDIR=/tmp/matplotlib
export OCI_MON_MCP_STATE_DIR=/home/opc/oci-mon-mcp-server/data/runtime
export OCI_MON_MCP_REQUIRE_TOKEN=1
EOF

source ~/.bashrc
```

If you run with systemd, prefer an environment file:

```bash
sudo tee /etc/oci-mon-mcp-server.env >/dev/null <<'EOF'
OCI_MON_MCP_HOST=0.0.0.0
OCI_MON_MCP_PORT=8000
OCI_MON_MCP_TRANSPORT=streamable-http
OCI_MON_MCP_ARTIFACT_BASE_URL=http://<vm-public-ip>:8765
OCI_MON_MCP_ARTIFACT_HOST=0.0.0.0
OCI_MON_MCP_ARTIFACT_PORT=8765
OCI_MON_MCP_PUBLIC_HOST=<vm-public-ip>
OCI_MON_MCP_PUBLIC_PORT=8000
OCI_MON_MCP_REQUIRE_TOKEN=1
MPLCONFIGDIR=/tmp/matplotlib
OCI_MON_MCP_STATE_DIR=/home/opc/oci-mon-mcp-server/data/runtime
EOF
```

## 6.1 Pilot Multi-User Setup
For the 3-4 tester pilot, the server is intended to run with token enforcement enabled.

Each tester/client pair gets a unique tokenized MCP URL:

```text
http://<vm-public-ip>:8000/mcp?u=<token>
```

Create tokens with:

```bash
python3 scripts/manage_users.py add "alice" --client codex
python3 scripts/manage_users.py add "alice" --client claude
python3 scripts/manage_users.py list
```

Important rules:
- Use the same `user_id` for the same human across clients.
- Use separate `--client` values for `codex` and `claude`.
- Tokens are credentials. Treat them like secrets.
- `codex` and `claude` get separate profile directories on purpose so conversation state does not collide.

Expected profile storage:

```text
data/runtime/users/pilot_alice_codex/
data/runtime/users/pilot_alice_claude/
```

The first live interaction for a fresh profile should ask for:
- default OCI region
- default compartment

The server no longer relies on the model guessing a region for a brand-new profile.

## 6.2 Migration from Legacy Shared Runtime State
If you already ran an older shared-state version of the server, migrate runtime state once:

```bash
python3 scripts/migrate_to_multi_user.py
```

What it does:
- backs up the legacy runtime files under `data/runtime/.backup/`
- creates per-profile directories under `data/runtime/users/`
- moves shared templates/preferences into `data/runtime/shared/`
- creates `data/runtime/user_registry.json` for token management

## 7. Start the Server
```bash
oci-mon-mcp-server
```

Equivalent:
```bash
python -m oci_mon_mcp.server
```

### Optional: quick redeploy and restart (manual/no systemd)
Use this after pulling changes:

```bash
cd /home/opc/oci-mon-mcp-server
source .venv/bin/activate
git pull
pip install -e .
pkill -f "oci_mon_mcp.server|oci-mon-mcp-server" || true
nohup oci-mon-mcp-server >/tmp/oci-mon-mcp.log 2>&1 &
tail -n 40 /tmp/oci-mon-mcp.log
```

### Optional: run with systemd (recommended for persistence)
If you do not already have a service, create one:

```bash
sudo tee /etc/systemd/system/oci-mon-mcp-server.service >/dev/null <<'EOF'
[Unit]
Description=OCI Mon MCP Server
After=network.target

[Service]
Type=simple
User=opc
WorkingDirectory=/home/opc/oci-mon-mcp-server
EnvironmentFile=/etc/oci-mon-mcp-server.env
# Use system Python for ExecStart and point PYTHONPATH to the venv + source tree.
# This avoids 203/EXEC Permission denied on some Oracle Linux + SELinux setups.
Environment="PYTHONPATH=/home/opc/oci-mon-mcp-server/src:/home/opc/oci-mon-mcp-server/.venv/lib64/python3.11/site-packages"
ExecStart=/usr/bin/python3.11 -m oci_mon_mcp.server
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
```

Enable/start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now oci-mon-mcp-server
sudo systemctl status oci-mon-mcp-server --no-pager -l
```

For subsequent code updates with systemd:

```bash
cd /home/opc/oci-mon-mcp-server
source .venv/bin/activate
git pull
pip install -e .
sudo systemctl restart oci-mon-mcp-server
sudo systemctl status oci-mon-mcp-server --no-pager -l
```

Health check endpoint (recommended for probes/uptime checks):

```bash
curl -i http://127.0.0.1:8000/healthz
```

Pilot token check:

```bash
curl -i http://127.0.0.1:8000/mcp
```

Expected result in pilot mode:
- `/healthz` returns `200`
- bare `/mcp` returns `401 Missing or invalid MCP user token`

If startup fails, inspect logs:

```bash
sudo journalctl -u oci-mon-mcp-server -n 80 --no-pager
```

Client connection setup is documented in `docs/CLIENT_SETUP.md`.

## 8. MCP Tools Exposed
The prototype exposes these tools:
- `monitoring_assistant`
- `setup_default_context`
- `change_default_context`
- `discover_accessible_compartments`
- `configure_auth_fallback`
- `use_instance_principals`
- `list_saved_templates`

## 8.1 Result Shape Defaults
- Queries return a short summary, then a table, then chart/artifacts.
- By default, the table shows the top 20 rows.
- By default, the chart shows the top 10 series.
- If more than 20 rows are returned, a CSV artifact is generated for the full export.
- If no compute instances cross a threshold query, the response still shows the top 5 highest
  instances in that window and names the highest observed value, instance, and time.
- For tenancy-wide queries, rows include `compartment` and `lifecycle_state` from Compute metadata
  when available.

## 8.2 Seed Data and Learning
- The repo ships generic starter templates in `data/seed_query_templates.json`.
- The repo ships generic starter memory in `data/seed_user_memory.json`.
- At first startup, runtime state is created under `data/runtime/` (or `OCI_MON_MCP_STATE_DIR`).
- Runtime files should not be committed.
- Per-profile state lives under `data/runtime/users/<profile_id>/`.
- Shared promoted learnings live under `data/runtime/shared/`.
- Successful templates are saved per profile first.
- Learned metric preferences are saved per profile first.
- Shared cross-user preferences are promoted later by aggregation.

### Aggregate shared learnings for the pilot
To promote multi-user learnings into shared runtime stores:

```bash
python3 scripts/aggregate_learnings.py
```

This is intended for scheduled execution on the VM after multiple users have built up history.

### Promote generic learnings safely
To promote runtime learnings back into generic seed files:

```bash
python3 scripts/promote_seeds.py --dry-run
python3 scripts/promote_seeds.py
```

The promotion script is conservative and skips entries that contain likely sensitive data
(for example OCIDs, IP addresses, emails, or secret-like tokens).

### Response table format (standard)
Use this exact table schema and order in user-facing responses for CPU utilization queries:

| Instance | Compartment | Lifecycle | Max CPU % | Time of Max (UTC) | Latest CPU % |
|---|---|---|---:|---|---:|
| `<instance_name>` | `<compartment>` | `<lifecycle_state>` | `<max_value_2dp>` | `<time_of_max_iso>` | `<latest_value_2dp>` |

Formatting rules:
- Keep exactly these 6 columns, in this order.
- Use 2 decimal places for `Max CPU %` and `Latest CPU %`.
- Keep `Time of Max (UTC)` in ISO-8601 UTC format.
- Show top 20 rows in the table; include CSV artifact for full row set.

## 9. First Test Sequence

### Option A: manual context setup
1. Call `setup_default_context` with:
   - `region`
   - `compartment_name`
   - `compartment_id` if you already know it
2. Then ask:
   - `show me all compute instances with CPU utilization above 80% in the last 1 hour`

### Option B: discover first
1. Call `discover_accessible_compartments` with your region.
2. Pick a compartment from the returned list.
3. Call `setup_default_context`.
4. Then ask:
   - `show me all compute instances with CPU utilization above 80% in the last 1 hour`

## 10. Recommended Prototype Queries

### Compute (baseline)
- `show me all compute instances with CPU utilization above 80% in the last 1 hour`
- `show me the worst performing compute instances`
- `now do the same for memory`
- `show CPU trend for app-01`
- `show me compute io`
- `show CPU utilization across tenancy for last 1 hour`
- `show CPU utilization in compartment ProdMgmt for last 24 hours`

### Networking / VCN
- `show network bytes in for all instances in the last 1 hour`
- `show ingress traffic across tenancy`

### Database
- `show database CPU utilization for the last 24 hours`
- `show autonomous DB sessions`

### Load Balancer
- `show HTTP requests on load balancers`
- `show active connections for the last 1 hour`

### OKE / Kubernetes
- `show OKE node CPU utilization`
- `show kubernetes memory usage`

### Other
- `show function invocations for the last 24 hours`
- `show bucket size for object storage`

## 11. Expected Behavior
- If region/compartment are not saved yet, the server asks for them.
- If a request is ambiguous, the server asks clarifying questions before querying OCI.
- If rows exceed the on-screen limit, the response includes a CSV artifact URL.
- If chart data is available, the response includes a PNG artifact URL.
- If Instance Principals fail, the server can prompt for OCI config fallback.
- `GET /healthz` returns a simple `200` JSON payload when the service is up.
- MCP endpoint checks against `/mcp` with plain curl may show protocol errors such as
  `400 Missing session ID`; this is expected for non-MCP requests.
- Query scope defaults to including subcompartments unless the request explicitly says not to.

## 12. Data Files
The prototype persists local state under `data/`:
- `data/runtime/user_memory.json` for defaults, learned preferences, pending clarification state,
  and shared VM-wide preferences
- `data/runtime/query_templates.json` for successful NL-to-query templates shared across users on
  the VM
- `data/artifacts/` for generated PNG and CSV files

## 13. Operational Notes
- Compute CPU and memory metrics require the Compute Instance Monitoring plugin to be enabled.
- The server supports 9 OCI metric namespaces: `oci_computeagent`, `oci_vcn`, `oci_blockstore`,
  `oci_lbaas`, `oci_database`, `oci_autonomous_database`, `oci_objectstorage`, `oci_oke`, `oci_faas`.
- Metrics are driven by `data/metric_registry.yaml`. To add a new namespace or metric, edit the
  YAML and restart the server. Override the registry path with `OCI_MON_MCP_METRIC_REGISTRY_PATH`.
- Unknown namespaces not in the registry are auto-discovered at runtime via the OCI ListMetrics API.
- `storage usage %` inside the instance is intentionally reported as unavailable from standard OCI
  Monitoring metrics alone. Database and Object Storage metrics are available separately.
- Disk I/O is supported after clarification.
- **Audit logging**: every query is logged to `data/logs/audit.log` (JSONL format) with user identity,
  query text, OCI API calls, and timing breakdown. Logs rotate at 50 MB (5 backups, gzip compressed).
  Archives older than 90 days are eligible for cleanup via `AuditLogger.cleanup_archives()`.
- If you see `does not appear to be a Python project`, you are running `pip install -e ...` outside
  the repository root.
- If you see Python 3.9 in the virtualenv, recreate it with `python3.11 -m venv .venv`.
