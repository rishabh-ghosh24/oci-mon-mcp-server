# OCI Monitoring MCP Prototype PRD

## 1. Document Purpose
This document defines the product requirements for the first working prototype of the OCI
Monitoring MCP server. The prototype is a thin vertical slice intended to validate the complete
end-to-end flow:

1. User asks a monitoring question in natural language.
2. System asks clarifying questions when the request is ambiguous.
3. System runs the corresponding OCI Monitoring query.
4. System returns an interpreted answer, structured results, a chart artifact, and practical
   recommendations.

This prototype is intentionally narrow and optimized for reliability over breadth.

## 2. Prototype Goal
Prove that a user can query real OCI metrics in natural language and receive useful results from
their tenancy without having to know MQL.

The prototype is successful if it demonstrates:
- natural-language understanding for a constrained set of compute-focused intents
- correct clarification behavior before execution when meaning is ambiguous
- real metric retrieval from OCI Monitoring
- structured response output with summary, table, chart, and recommendations
- persistence of learned clarifications and successful NL-to-query templates

## 3. Problem Statement
OCI users often know the operational question they want answered, but not the exact namespace,
metric, dimensions, or MQL syntax needed to answer it. The prototype should bridge that gap by
turning operational language into valid monitoring queries and clear results.

## 4. Target Users
- OCI Cloud Operations engineers
- SRE and NOC responders
- operators who need quick answers during triage

## 5. Product Principles
- Reliability over breadth: support a smaller set of flows and execute them well.
- Clarify before running: if the meaning is unclear, ask first.
- Safe defaults: use saved region and compartment, but do not silently guess thresholds or intent.
- Useful output by default: return summary, structured data, visualization, and recommendations
  when applicable.
- Learn from success: persist clarified meanings and successful query templates for reuse.

## 6. Prototype Scope

### 6.1 In Scope
- VM-hosted MCP server running on OCI Compute
- single-tenancy usage
- region and default compartment setup on first run
- authentication via Instance Principals first, OCI config profile fallback
- compute-focused monitoring flows
- CPU and memory metric analysis
- disk I/O analysis after clarification
- structured results with summary, tables, chart data, PNG chart artifact, and CSV export
- short operational recommendations
- learning store for clarified meanings and default preferences
- query template store for successful NL-to-query mappings

### 6.2 Out of Scope for This Prototype
- anomaly detection
- forecasting
- alert creation, update, or delete
- dashboard widget creation
- multi-region queries in one request
- multi-tenancy
- full web UI
- advanced role-based authorization beyond OCI IAM

## 7. Supported User Intents
The prototype must explicitly support the following compute-focused intents:

- show/list/find compute instances above a threshold
- top N compute instances by metric
- worst performing compute instances, with clarification when metric is not specified
- determine whether any compute instances crossed a threshold in a time window
- show metric trend for a named compute instance

### 7.1 Supported Metrics
- CPU
- memory
- disk I/O, after clarification

### 7.2 Metric Semantics
- `CPU` means compute CPU utilization
- `memory` means compute memory utilization
- `storage` means filesystem space used inside the instance from the user's perspective, but the
  prototype must explain that filesystem usage percentage is not available from standard OCI
  Monitoring metrics alone
- `io` is ambiguous and must trigger clarification, such as disk I/O versus something else

### 7.3 Unsupported or Conditional Cases
- If the user asks for `storage usage` or filesystem utilization, explain that the percentage used
  inside the instance is not available from standard OCI Monitoring metrics alone.
- If the user clarifies `io` as something other than disk I/O, treat that as out of scope for this
  prototype.
- If the user asks for something outside prototype scope, say so explicitly and, when possible,
  offer the closest supported alternative.

## 8. Default Behaviors

### 8.1 First-Run Setup
On first use, the system must:
1. Ask for the default OCI region.
2. Discover accessible compartments where possible.
3. Ask the user to choose a default compartment.
4. Persist region and default compartment for later use.
5. If user identity is not available from the MCP client, ask for a simple local profile label.

### 8.2 Scope Defaults
- Default scope is the saved default compartment only.
- Do not include subcompartments unless the user explicitly asks.
- If the user explicitly asks for another compartment, all compartments, or an entire tenancy, use
  that requested scope.

### 8.3 Time Defaults
- Default time range when omitted: last 1 hour
- Supported relative time ranges:
  - last 15 minutes
  - last 30 minutes
  - last 1 hour
  - last 6 hours
  - last 24 hours
  - last 7 days
- If the user says something vague such as `recently`, ask a clarification question.

### 8.4 Threshold Defaults
- Do not silently assume a threshold.
- If a user says `high CPU` or `high memory` without a number, ask for the threshold.
- `top` and `worst performing` queries do not require a threshold.

### 8.5 Ranking Defaults
- For threshold and top/worst-performing queries, default ranking uses the maximum observed value in
  the selected window.
- Default top-N count is 5 if the user does not specify a number.

## 9. Clarification Rules
- If there is ambiguity, ask clarifying questions before running the query.
- Ask all necessary clarifications in a single short message rather than stretching them across
  multiple turns.
- If a request mixes clear and unclear parts, ask first and only show results after clarification.
- If a prior learned meaning exists, suggest it with confirmation rather than applying it silently.

Example:
- User: `show me the worst performing compute instances`
- System: `Do you mean worst performing by CPU, memory, storage usage, or another metric?`
- If prior learning exists: `Last time this meant CPU. Use CPU again?`

## 10. Follow-Up Behavior
Follow-up requests should carry forward previously resolved context when reasonable, including:
- region
- compartment
- resource type
- threshold, unless the user changes it
- time window, unless the user changes it
- output and visualization behavior

If the follow-up is ambiguous, the system must ask before executing.

Example:
- User: `show me all compute instances with CPU utilization above 80% in the last 1 hour`
- User: `now do the same for memory`

## 11. Response Expectations
Every successful response should, by default, include:
- a short interpretation line explaining how the request was understood
- a concise summary
- a structured table when useful
- a chart when useful
- practical recommendations when applicable

### 11.1 Visualization Expectations
- Default chart for threshold/top compute metric flows is a line chart.
- Show one line per instance for the top 20 offenders only.
- Put the legend on the right.
- If a threshold is part of the query, draw a slightly thicker threshold line in mid/light blue.

### 11.2 Table Expectations
Default on-screen table behavior:
- show up to 20 rows by default
- keep on-screen rows capped at 10 for consistency
- if more rows exist, provide a CSV export for the full result set

### 11.3 Sorting Expectations
- When a timestamp exists, sort newest first.
- If no timestamp exists in that output shape, sort by severity.
- For threshold/top/worst-performing results, use `time of max value`.
- For named-instance trend outputs, use `latest datapoint time`.

## 12. No-Match Behavior
When no resources match:
- say that no compute instances crossed the requested threshold in the selected window and
  compartment
- still show a chart when useful for the top 5 highest instances in that window when no threshold
  matches are found
- mention the highest observed value, which instance reached it, and when it happened

## 13. Recommendations
Recommendations in the prototype must be:
- deterministic and rule-based
- metric-specific
- short and operational
- safe, without risky remediation commands

Workload-specific recommendations are allowed only when there is a strong signal from:
- instance display name
- defined or freeform tags
- prior learned mappings
- metric namespace or similar resource context

If confidence is low, stay generic.

## 14. Learning and Reuse

### 14.1 Learned Clarifications and Preferences
The system should store:
- default region
- default compartment
- user-confirmed interpretations such as `worst performing compute instances -> CPU`
- last-used timestamps
- confidence scores

Scope learned preferences by:
- tenancy
- region
- user profile

### 14.2 Successful Query Templates
Store successful natural-language question to query mappings separately from user preferences.

A query template should be saved only when:
- ambiguity was resolved
- the query executed successfully
- the results were valid, including valid zero-match results
- the interpretation was either explicitly confirmed or unambiguous

Scope successful query templates by:
- tenancy
- region

## 15. Prototype Acceptance Criteria
The prototype is accepted when it can reliably demonstrate all of the following on a real tenancy:

1. First-run setup asks for region and default compartment, then saves them.
2. `show me all compute instances with CPU utilization above 80% in the last 1 hour` works end to
   end.
3. `show me the worst performing compute instances` triggers clarification and then works.
4. `now do the same for memory` works as a follow-up using carried context.
5. If no instances match, the system returns a no-match summary plus top-offender context.
6. If rows exceed the on-screen limit, the system provides CSV export.
7. The system stores successful query templates and learned clarifications after success.

## 16. Representative User Flows

### 16.1 Explicit Threshold Query
User:
`show me all compute instances with CPU utilization above 80% in the last 1 hour`

System:
- interprets the metric as CPU
- interprets the aggregation as max over the last hour
- uses the saved region and default compartment
- returns matching instances, a summary, a table, a line chart, and recommendations

### 16.2 Ambiguous Worst-Performing Query
User:
`show me the worst performing compute instances`

System:
- asks whether the user means CPU, memory, storage usage, or another metric
- if the user says storage usage, explains that filesystem usage percentage is not available from
  standard OCI Monitoring alone
- otherwise executes the clarified query

### 16.3 Multi-Metric Query
User:
`list all the computes which have high resource utilization - mem, cpu, storage, io`

System:
- asks for missing thresholds
- explains that filesystem storage usage percentage is not available from standard OCI Monitoring
- asks what `io` means if not clearly specified
- after clarification, returns separate summaries, tables, and charts per metric

## 17. Deferred Roadmap Items
These are intentionally deferred until after the working prototype:
- anomaly detection
- forecasting
- alert creation/update flows
- dashboard widget creation
- broader namespace coverage such as database or network anomaly workflows
- richer interactive HTML visualizations
- advanced governance, RBAC, and multi-tenancy
