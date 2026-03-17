# Learning Template Strategy (V1)

## Goal
Improve reliability by reusing previously successful query interpretations.

## V1 Mechanism
- Store successful NL prompt -> validated MQL mapping in a local JSON file.
- Save automatically only when query execution succeeds and result quality checks pass.
- Match future requests using similarity over intent, namespace, metric, and dimensions.
- Show reused template metadata in response (template id and confidence).

## Guardrails
- Never auto-apply a low-confidence template.
- Never reuse templates that refer to missing namespaces/metrics.
- Keep user-visible explanation when template is reused or rejected.
- Do not require manual user approval for template save in V1.

## Suggested Data File
- Path: `data/runtime/query_templates.json`
- Fields:
  - `template_id`
  - `created_at`
  - `updated_at`
  - `nl_patterns`
  - `namespace`
  - `metric`
  - `dimensions`
  - `mql`
  - `usage_count`
  - `success_rate`
  - `last_used_at`
  - `confidence`

## Lifecycle
- Increment `usage_count` and update `success_rate` after each execution.
- Decay confidence over time if not used recently.
- Mark stale templates for review/prune.
