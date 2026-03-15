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
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

For local development and tests:
```bash
pip install -e ".[dev]"
```

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

# Avoid matplotlib cache warnings on the VM.
export MPLCONFIGDIR=/tmp/matplotlib
```

If you want the main MCP endpoint on a different path:
```bash
export OCI_MON_MCP_STREAMABLE_HTTP_PATH=/mcp
```

## 7. Start the Server
```bash
oci-mon-mcp-server
```

Equivalent:
```bash
python -m oci_mon_mcp.server
```

## 8. MCP Tools Exposed
The prototype exposes these tools:
- `monitoring_assistant`
- `setup_default_context`
- `change_default_context`
- `discover_accessible_compartments`
- `configure_auth_fallback`
- `use_instance_principals`
- `list_saved_templates`

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
- `show me all compute instances with CPU utilization above 80% in the last 1 hour`
- `show me the worst performing compute instances`
- `now do the same for memory`
- `show CPU trend for app-01`
- `show me compute io`

## 11. Expected Behavior
- If region/compartment are not saved yet, the server asks for them.
- If a request is ambiguous, the server asks clarifying questions before querying OCI.
- If rows exceed the on-screen limit, the response includes a CSV artifact URL.
- If chart data is available, the response includes a PNG artifact URL.
- If Instance Principals fail, the server can prompt for OCI config fallback.

## 12. Data Files
The prototype persists local state under `data/`:
- `data/user_memory.json` for defaults, learned preferences, and pending clarification state
- `data/query_templates.json` for successful NL-to-query templates
- `data/artifacts/` for generated PNG and CSV files

## 13. Operational Notes
- Compute CPU and memory metrics require the Compute Instance Monitoring plugin to be enabled.
- The prototype currently supports compute-focused flows only.
- `storage usage %` inside the instance is intentionally reported as unavailable from standard OCI
  Monitoring metrics alone.
- Disk I/O is supported after clarification.
