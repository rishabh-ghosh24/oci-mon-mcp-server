# Prioritized Backlog

## Must Have (V1)
- Namespace and metric discovery across tenancy.
- Natural language to MQL generation with pre-execution validation.
- Query execution with output in table format and chart-ready series.
- Modes: Fast (default), Balanced, Deep.
- Default lookback (1h) when not specified; 6h/24h/7d quick options.
- Forecasting horizons: 24h/7d/30d/90d with explicit prompt when missing.
- Anomaly and outlier detection over selected time windows.
- Alarm read plus create/update workflows with explicit confirmation.
- No alarm delete capability exposed via MCP tools.
- Automatic template learning store from successful validated queries.
- CSV/PDF export support.
- OCI auth via Instance Principals, with OCI config/API key fallback.
- OCI IAM-first access model with audit logging for actions.

## Should Have (Early Post-V1)
- Golden test corpus for NL to MQL conversion quality.
- Template lifecycle controls (confidence decay and stale pruning jobs).
- MCP service observability (latency, errors, cache hit ratio, model confidence).
- Robust retry and circuit-breaker behavior for OCI throttling/error classes.
- Prompt-injection and tool-abuse defenses for mutation workflows.
- External API hardening: rate limits, key rotation, response contract versioning.

## Later
- Internal fine-grained authorization layer (RBAC in server) on top of OCI IAM.
- Multi-tenancy support with explicit tenant/profile context switching.
- Lightweight web UI for persistent dashboards and richer visualizations.
- Advanced governance for exports (policy-driven redaction/watermarking).

## Acceptance Criteria (V1)
- For a plain-language metric question, system returns valid MQL plus executed results.
- For unsupported or ambiguous questions, system asks a targeted clarification.
- For forecast request without horizon, system asks for horizon before execution.
- Alarm create/update is never executed without explicit confirmation in the same flow.
- Alarm delete is not available as a tool operation.
- Successful query patterns are autosaved and reused when confidence is sufficient.
- Outputs include both tabular data and chart-ready data points.
- Core paths work with both Instance Principals and OCI config/API key auth.
