# OCI Monitoring MCP: Product and Technical Notes

## Confirmed Scope (V1)
- Runtime: VM-hosted MCP server on OCI Compute (OEL9 target), Python implementation.
- Users: OCI Cloud Ops/SRE and NOC responders, with broader self-service monitoring usage.
- Monitoring coverage: discover and support all available OCI Monitoring namespaces in tenancy.
- Query model: natural language to OCI MQL generation and execution.
- Output: table-first results plus chart-ready data for visualizations.
- Analytics: anomaly detection, outlier detection, and forecasting.
- Alarm capability: read alarm context and support alarm creation/update from metric insights.
- Alarm delete operations are out of scope for V1 (manual delete only in OCI Console).
- Safety policy: all alarm mutations require explicit user confirmation.
- Learning capability: persist validated successful queries/MQL patterns as reusable templates to
  reduce retries, hallucinations, and mapping errors over time.
  Learning is automatic (autosave) for successful validated queries.

## Must-Have Architecture Controls (V1)
- Query safety controls:
  - Cap lookback, returned streams, and raw points per request.
  - Reject high-cardinality dimension fan-out unless user opts into deep mode.
  - Enforce timeout and retry budgets by mode (Fast/Balanced/Deep).
- Reliability controls:
  - Namespace/metric discovery cache with TTL and invalidation.
  - Structured error handling for OCI throttling, auth failures, and transient network errors.
  - Deterministic fallback path when NLP parsing confidence is low.

## Framework Direction
- Python implementation using the MCP Python ecosystem.
- Use the official `mcp` Python SDK as the repository-standard MCP dependency.
- The built-in `mcp.server.fastmcp.FastMCP` helper is acceptable for prototype scaffolding because
  it is part of the official SDK surface.
- Do not depend on the standalone third-party `fastmcp` package or its separate release line.

## Confirmed Defaults
- Auth priority:
  1. Instance Principals (preferred)
  2. OCI config profile/API key (fallback)
- Tenancy mode: single tenancy for V1.
- Internal extensibility: keep auth/context abstractions ready for future multi-tenancy, but do not
  expose tenancy selection or multi-tenant options to end users in V1 UX.
- Query performance modes:
  1. Fast (default)
  2. Balanced
  3. Deep
- Default lookback for unspecified time range: last 1 hour.
- Quick time options: 6h, 24h, 7d.
- Forecast horizons supported: 24h, 7d, 30d, 90d.
- If forecast horizon is not provided, prompt the user to choose horizon.

## Initial Test Namespace Focus
- oci_computeagent
- oci_vcn
- oci_blockstore
- oci_lbaas / load balancer metrics
- oci_objectstorage

## Deferred / Later Considerations (Do Not Forget)
- Multi-tenancy support with tenancy/profile selection and isolation boundaries.
- Internal authorization layer (tool-level RBAC and role mapping) beyond OCI IAM.
- Optional lightweight web UI for persistent dashboards and richer visual UX.
- Centralized model/cache strategy for deeper anomaly/forecast workloads.
- Role-based guardrails for alarm operations and privileged metric access.
- Export pipeline hardening for PDF/CSV with large dataset pagination.
- Cost controls for deep analytics workloads (compute/time budgets).
- Template governance for learned queries: versioning, confidence scoring, and stale-template
  pruning.

## Non-Goals For Day 0
- Full multi-tenant control plane.
- Complex web frontend before MCP/API workflows are stable.
- Auto-creating alarms without explicit user confirmation.
- Alarm delete operations from MCP tools.
