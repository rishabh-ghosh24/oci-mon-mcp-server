# OCI Monitoring MCP Prototype Technical Requirements

## 1. Purpose
This document defines the engineering requirements for the first working prototype of the OCI
Monitoring MCP server. It is the build-ready technical companion to
`docs/PROTOTYPE_PRD.md`.

## 2. Implementation Goal
Build a Python-based MCP server running on an OCI Compute VM that can:
- receive natural-language monitoring requests
- clarify ambiguity before execution
- query OCI Monitoring for supported compute flows
- return structured results plus PNG and CSV artifacts
- persist learned meanings and successful query templates

## 3. Architecture Overview

### 3.1 Runtime
- Host: OCI Compute VM on Oracle Linux 9
- Language: Python 3.11+
- Runtime style: single MCP server process with local JSON-backed persistence
- Primary target clients: Codex CLI and Codex Desktop
- MCP framework choice: official `mcp` Python SDK
- Prototype server helper: `mcp.server.fastmcp.FastMCP` from the official SDK is allowed
- Explicitly out of scope: the standalone third-party `fastmcp` package and its separate release line

### 3.2 Major Components
1. MCP tool layer
2. conversation and clarification state manager
3. query interpretation layer
4. deterministic query builder
5. OCI Monitoring adapter
6. artifact generation service
7. local persistence layer

### 3.3 Core Design Rule
Natural language should only determine the structured intent. Final query construction must use
deterministic builders/templates for the supported flows rather than unrestricted raw query
generation.

## 4. Tool Surface

### 4.1 Primary User-Facing Tool
Expose one main tool:
- `monitoring_assistant`

Suggested input shape:
```json
{
  "query": "show me all compute instances with CPU utilization above 80% in the last 1 hour",
  "profile_id": "optional-stable-user-id"
}
```

### 4.2 Support Tools
Expose only a small support surface:
- `setup_default_context`
- `change_default_context`
- `list_saved_templates`

If preferred, `setup_default_context` and `change_default_context` may later be folded into the
main tool, but the prototype should keep the overall tool count small.

### 4.3 MCP SDK Decision
Use the official `mcp` Python SDK as the only MCP dependency for this repository.

Allowed:
- `mcp`
- `mcp.server.fastmcp.FastMCP` when it helps reduce boilerplate for the prototype

Not allowed:
- the standalone third-party `fastmcp` package
- coupling server behavior to third-party FastMCP-specific features outside the official SDK path

Reason:
- this keeps the prototype fast to build while avoiding dependency on a separate framework release
  line that is outside the official SDK boundary

## 5. Main Response Contract
The primary tool must always return a response object with this top-level shape:

```json
{
  "status": "success | needs_clarification | error",
  "interpretation": "short explanation of how the query was understood",
  "clarifications": [],
  "summary": "human-readable answer",
  "tables": [],
  "charts": [],
  "recommendations": [],
  "artifacts": [],
  "details": {}
}
```

### 5.1 `status`
- `success`: query executed or a valid no-match result was produced
- `needs_clarification`: system requires more information before execution
- `error`: execution could not complete

### 5.2 `clarifications`
When clarification is needed, populate with ordered questions and do not execute the query yet.

Suggested shape:
```json
[
  {
    "id": "metric_choice",
    "question": "Do you mean CPU, memory, storage usage, or another metric?"
  }
]
```

### 5.3 `tables`
Each table block should describe one result set.

Suggested shape:
```json
[
  {
    "id": "cpu_threshold_results",
    "title": "Compute instances above 80% CPU utilization",
    "columns": [
      "instance_name",
      "instance_ocid",
      "compartment",
      "lifecycle_state",
      "metric",
      "threshold",
      "max_value",
      "time_of_max",
      "latest_value",
      "recommendation"
    ],
    "rows": []
  }
]
```

### 5.4 `charts`
Each chart block should contain structured time-series data whether or not a PNG artifact is also
generated.

Suggested shape:
```json
[
  {
    "id": "cpu_top5_chart",
    "title": "Top 5 compute instances by max CPU utilization",
    "type": "line",
    "x_axis": "time",
    "y_axis": "cpu_utilization_percent",
    "legend_position": "right",
    "series": [],
    "threshold_line": {
      "value": 80,
      "color": "#7fb6ff",
      "line_width": 2
    }
  }
]
```

### 5.5 `artifacts`
Artifacts should include chart PNG and CSV links when generated.

Suggested shape:
```json
[
  {
    "id": "cpu_chart_png",
    "type": "image/png",
    "title": "CPU threshold chart",
    "url": "https://host/artifacts/abc123?token=...",
    "expires_at": "2026-03-16T12:00:00Z"
  }
]
```

### 5.6 `details`
Include implementation-facing details that should not dominate the main answer:
- generated query string
- scope details
- interval
- namespace
- metric
- template id if reused
- whether output was truncated
- timing metadata

## 6. Supported Interpretation Model

### 6.1 Supported Intents
The parser/interpreter must support:
- threshold queries
- top-N queries
- worst-performing queries
- threshold-crossing queries
- named-instance trend queries

### 6.2 Supported Metrics
- CPU
- memory
- disk I/O, after clarification

### 6.3 Special Interpretation Rules
- `show me all compute instances with CPU utilization above 80% in the last 1 hour` should map to
  `did any instance cross 80 percent max CPU utilization within the last hour`
- `worst performing compute instances` must not execute until the metric is clarified
- `storage usage` must return an explanation that standard OCI Monitoring does not provide
  filesystem-space-used percentage inside the instance
- `io` must trigger clarification

### 6.4 Follow-Up Resolution
When a prior successful query exists in the current conversation, carry forward:
- region
- compartment
- resource type
- time range
- threshold
- output mode

If the next message is ambiguous and a clarification is pending:
- first try to interpret it as an answer to the pending question
- if it clearly starts a new request, discard the pending state
- if uncertain, ask whether to continue the previous query or start a new one

## 7. Clarification Engine Requirements

### 7.1 General Behavior
- Ask all necessary clarifications in one message.
- Do not partially execute a mixed clear/unclear request.
- Do not guess thresholds.
- Do not silently reuse learned meanings when ambiguity remains; confirm them first.

### 7.2 Clarification Triggers
Clarification is required when:
- the metric is unclear
- the threshold is missing for `high` or similar language
- `io` is requested without type
- the time range is vague
- multiple instance matches remain after partial name matching

### 7.3 Clarification State
Maintain a short-lived pending clarification object containing:
- original query
- unresolved fields
- ordered clarification questions
- timestamp
- partial interpretation state

Pending state must expire after a reasonable timeout or when a clearly unrelated new request starts.

## 8. OCI Access Requirements

### 8.1 Auth Order
1. Instance Principals
2. OCI config file fallback

### 8.2 Fallback Behavior
If Instance Principals fail:
- detect and classify the auth error
- ask whether to use OCI config fallback
- default fallback config path to `~/.oci/config`
- default fallback profile to `DEFAULT`
- allow user override
- persist fallback settings for future use

### 8.3 Execution Adapter
Use the Python OCI SDK as the primary OCI integration path.

Reasons:
- structured authentication handling
- cleaner error management
- easier pagination and retries
- cleaner query-string handling than shelling out
- easier testability

OCI CLI documentation may still be used as:
- reference material
- operator troubleshooting guidance
- validation during development

### 8.4 Compute Monitoring Prerequisites
For compute metrics in `oci_computeagent` to exist:
- the Compute Instance Monitoring plugin must be enabled
- plugins must be running
- the instance must be able to reach the Monitoring service

Missing compute metrics should trigger troubleshooting guidance rather than a silent empty response.

## 9. Namespace and Metric Discovery

### 9.1 Discovery Use
Runtime discovery should be used for:
- validating whether namespaces and metrics are present
- detecting missing-data situations
- supporting clarifications and explanation

### 9.2 Discovery Constraints
- Discovery should not be treated as unrestricted query generation.
- Supported product behavior remains constrained to the compute-centric flows in this prototype.

### 9.3 Discovery Caching
Cache namespace and metric discovery results locally with TTL.

Suggested cache key:
- tenancy id
- region
- compartment id

Suggested TTL:
- 15 minutes for prototype

## 10. Metric Mapping Requirements

### 10.1 Baseline Metric Meanings
Prototype metric interpretation should map to well-known OCI Monitoring metrics:
- namespace: `oci_computeagent`
- CPU -> `CpuUtilization`
- memory -> `MemoryUtilization`
- disk I/O throughput -> `DiskBytesRead` and `DiskBytesWritten`
- disk I/O IOPS -> `DiskIopsRead` and `DiskIopsWritten`

Use `resourceId` and `resourceDisplayName` as the primary resource-identifying dimensions for
compute flows.

### 10.2 Storage Usage Rule
If the user asks for storage usage percentage inside the instance:
- do not fabricate a metric mapping
- return a clear explanation that filesystem-used percentage is not available from standard OCI
  Monitoring metrics alone

### 10.3 Disk I/O Clarification Rule
When user clarifies `disk I/O`, ask:
- throughput or IOPS
- read, write, or both

If the user remains vague:
- default to both read and write
- graph throughput
- include throughput and IOPS in the table if available

If the user clarifies `io` as network I/O or another non-disk type:
- do not execute it in this prototype
- explain that only disk I/O is supported in the current prototype

## 11. Query Builder Requirements

### 11.1 Generation Strategy
Generate final Monitoring queries via deterministic builders/templates, not free-form raw text
generation.

The interpreter must extract:
- resource type
- metric
- threshold
- aggregation
- ranking rule
- time window
- region
- compartment

The builder must then assemble the final query string and dimensions.

### 11.1.1 Baseline Query Patterns
Use OCI Monitoring MQL-compatible query patterns as the basis for generated queries.

Reference patterns for the prototype:
- named instance trend:
  - `CpuUtilization[1m]{resourceId = "<instance_ocid>"}.max()`
- per-instance metric filtering:
  - `CpuUtilization[1m]{resourceId = "<instance_ocid>"}.max()`
- threshold-count pattern:
  - `(CpuUtilization[1m].max() > 80).grouping().count()`
- grouped queries:
  - use `groupBy(...)` when the implementation needs grouped metric streams

Final generated query text may vary by flow, but it must always specify:
- metric name
- interval
- statistic
- required dimensions when filtering

### 11.2 Aggregation Defaults
- threshold crossing and worst-performing queries default to `max`
- if the user explicitly asks for average, use `mean`

### 11.3 Query Interval Defaults
- 15m, 30m, 1h -> 1m interval
- 6h, 24h -> 5m interval
- 7d -> 1h interval
- adapt automatically if backend constraints require changes

### 11.4 Sorting Rules
- if the result shape has a timestamp, sort newest first
- otherwise sort by severity
- threshold/top/worst-performing results use `time_of_max`
- named-instance trend outputs use `latest_datapoint_time`

### 11.5 Instance Lookup and Metric Join Requirements
The implementation must not rely only on Monitoring dimensions for user-facing metadata.

Required behavior:
- list compute instances in the selected compartment from Compute service APIs
- map metric streams back to instances primarily by `resourceId`
- enrich results with:
  - instance display name
  - instance OCID
  - compartment name or OCID
  - lifecycle state

This is required so the server can:
- include instances that currently exist even when metric data is missing
- distinguish no-data from no-resource situations
- produce stable display names and lifecycle-state output

## 12. Output and Artifact Requirements

### 12.1 Table Output Rules
- default on-screen row limit: 20
- if user requests more rows, allow up to 100 on screen
- export full result set to CSV when rows exceed the visible limit or when the user requests export

### 12.2 Chart Output Rules
For threshold/top compute flows:
- generate a line chart by default
- chart only the top 5 offenders
- place legend on the right
- include threshold line when applicable using light or mid blue and slightly thicker stroke

### 12.3 Artifact Types
- PNG chart artifact
- CSV export artifact

### 12.4 Artifact Delivery
Expose artifacts via a small read-only HTTP endpoint on the VM.

Artifact URLs must:
- use unguessable artifact identifiers
- include short-lived tokens
- include expiry

Suggested token TTL:
- 10 to 15 minutes for prototype

## 13. Recommendation Engine Requirements

### 13.1 General Rules
Recommendations must be:
- deterministic
- short
- operational
- metric-specific
- non-destructive

### 13.2 Workload Specificity
Use workload-specific recommendations only when there is a strong signal from:
- instance display name
- tags
- prior learned mappings
- metric namespace or related context

If confidence is weak, use generic recommendations.

### 13.3 Example Rule Areas
- high CPU -> inspect load, recent changes, and top processes; consider scaling if sustained
- high memory -> inspect memory growth, leaks, and workload health; gather evidence before restart
- likely WebLogic memory case -> suggest heap and thread diagnostics before restart when confidence
  is strong

## 14. Persistence Requirements

### 14.1 Context and Learning Store
Persist a JSON file for context and learned clarifications.

Suggested path:
- `data/user_memory.json`

Suggested shape:
```json
{
  "profiles": {
    "rishabh": {
      "tenancy_id": "ocid1.tenancy...",
      "region": "us-ashburn-1",
      "default_compartment_id": "ocid1.compartment...",
      "default_compartment_name": "prod-observability",
      "auth_mode": "instance_principal",
      "config_fallback": {
        "config_path": "~/.oci/config",
        "profile": "DEFAULT"
      },
      "learned_preferences": [
        {
          "intent_key": "worst_performing_compute_instances",
          "resolved_metric": "cpu",
          "confidence": 0.9,
          "last_used_at": "2026-03-16T10:00:00Z"
        }
      ]
    }
  }
}
```

Scope this store by:
- tenancy
- region
- user profile

### 14.2 Query Template Store
Persist a separate JSON file for successful templates.

Suggested existing path:
- `data/query_templates.json`

Suggested shape:
```json
[
  {
    "template_id": "tmpl_compute_cpu_threshold",
    "tenancy_id": "ocid1.tenancy...",
    "region": "us-ashburn-1",
    "created_at": "2026-03-16T10:00:00Z",
    "updated_at": "2026-03-16T10:00:00Z",
    "intent_type": "threshold_query",
    "nl_patterns": [
      "show me all compute instances with CPU utilization above 80% in the last 1 hour"
    ],
    "resource_type": "compute_instance",
    "metric_key": "cpu",
    "time_window": "1h",
    "aggregation": "max",
    "threshold": 80,
    "query_text": "generated query here",
    "usage_count": 1,
    "success_rate": 1.0,
    "last_used_at": "2026-03-16T10:00:00Z",
    "confidence": 0.95
  }
]
```

Save a template only when:
- ambiguity is resolved
- query execution succeeds
- results are valid, including valid zero-match results
- final interpretation is confirmed or unambiguous

Scope this store by:
- tenancy
- region

## 15. Query Safety and Performance Rules

### 15.1 Broad Query Handling
- If a request is obviously too broad, do not run it silently.
- Ask the user to narrow it by metric, scope, time range, or top-N.
- If a request is only moderately broad, run it with prototype limits and clearly state that output
  was truncated.

### 15.2 Prototype Limits
- chart top 5 series only
- default visible rows 20
- maximum visible rows 100 on request
- broader result sets should be export-only beyond the visible limit
- default scope is one compartment only unless explicitly widened

### 15.3 Missing Data Rules
Differentiate between:
- no recent datapoints in the requested scope and time range
- missing metric collection, such as compute monitoring plugin not enabled
- IAM or auth errors

If some instances have data and others do not:
- return available results
- list missing-data instances separately when possible

## 16. Error-Handling Requirements
Errors must be classified and surfaced clearly. Minimum categories:
- clarification required
- authentication failure
- authorization failure
- namespace/metric unavailable
- no recent datapoints
- backend query failure
- artifact generation failure

Errors should not be disguised as empty results.

## 17. Prototype Setup Flow

### 17.1 Initial Setup
On first run:
1. Determine stable user id from client if available.
2. If unavailable, ask for a local profile label.
3. Ask for region.
4. Discover accessible compartments.
5. Ask the user to choose a default compartment.
6. Save region, compartment, profile, and auth mode.

### 17.2 Auth Initialization
- attempt Instance Principals first
- if it fails, offer OCI config fallback

## 18. OCI Reference Sources to Preserve
Keep these Oracle sources as the implementation reference set:
- Monitoring Query Language reference
- Monitoring query-building tasks
- Compute instance metrics reference
- VNIC metrics reference
- Block volume metrics reference
- Monitoring metric namespace and metric listing references

These references should inform:
- exact metric names
- namespace behavior
- MQL syntax
- CLI and API troubleshooting

## 19. Minimum Acceptance Test Corpus
The implementation must include automated or manual verification for at least these prompts:

1. `show me all compute instances with CPU utilization above 80% in the last 1 hour`
2. `show me the worst performing compute instances`
3. clarification reply: `CPU`
4. follow-up: `now do the same for memory`
5. `show CPU trend for <instance-name>`
6. `show me computes with high memory`
7. clarification reply: `85%`
8. `show me top 5 compute instances by CPU in the last 24 hours`
9. `show me compute storage utilization`
10. `show me compute io`
11. clarification reply: `disk io`
12. follow-up clarification reply: `throughput, both`

## 20. Suggested Implementation Sequence
1. setup/default-context persistence
2. auth manager
3. main MCP tool skeleton and response contract
4. clarification state handling
5. compute CPU threshold flow
6. top-N and worst-performing CPU flow
7. follow-up context carry-forward
8. memory flow
9. disk I/O clarification and query flow
10. chart and CSV artifact generation
11. template persistence and learning reuse

## 21. Deferred Technical Items
These are intentionally deferred:
- anomaly detection engine
- forecasting engine
- alert mutation workflows
- multi-tenancy abstractions in exposed UX
- advanced cache invalidation
- HTML interactive chart artifacts
- sophisticated template scoring and pruning jobs
