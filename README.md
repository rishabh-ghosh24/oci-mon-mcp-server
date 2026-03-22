# oci-mon-mcp-server
MCP server that connects AI assistants to OCI Monitoring service. Query, analyze, and explore your OCI resource metrics through natural language.

## Repository Structure
```text
.
├── src/
│   └── oci_mon_mcp/
│       ├── __init__.py
│       ├── assistant.py
│       ├── artifacts.py
│       ├── audit.py
│       ├── errors.py
│       ├── execution.py
│       ├── identity.py
│       ├── metric_registry.py
│       ├── models.py
│       ├── oci_sdk_adapter.py
│       ├── oci_support.py
│       ├── repository.py
│       └── server.py
├── docs/
│   ├── CLIENT_SETUP.md
│   ├── QUICKSTART.md
│   ├── TROUBLESHOOTING.md
│   ├── TECHNICAL_REQUIREMENTS.md
│   ├── LEARNING_TEMPLATE_STRATEGY.md
│   ├── PROTOTYPE_PRD.md
│   ├── PRIORITIZED_BACKLOG.md
│   └── PRODUCT_TECH_NOTES.md
├── data/
│   ├── metric_registry.yaml
│   ├── seed_query_templates.json
│   ├── seed_user_memory.json
│   └── runtime/ (auto-created local state; gitignored)
├── scripts/
│   ├── manage_users.py
│   ├── aggregate_learnings.py
│   ├── promote_seeds.py
│   ├── migrate_to_multi_user.py
│   └── sanitize_utils.py
├── tests/
│   ├── test_assistant.py
│   ├── test_audit.py
│   ├── test_metric_registry.py
│   ├── test_multi_user.py
│   └── test_oci_sdk_adapter.py
├── .gitignore
├── pyproject.toml
├── LICENSE
└── README.md
```

## Current Status
- Prototype MCP server implementation is in place.
- Working prototype requirements are defined in `docs/PROTOTYPE_PRD.md`.
- Build-ready technical requirements are defined in `docs/TECHNICAL_REQUIREMENTS.md`.
- Earlier discovery notes are retained in `docs/PRODUCT_TECH_NOTES.md` and `docs/PRIORITIZED_BACKLOG.md`.
- VM setup, runtime configuration, and test flow are captured in `docs/QUICKSTART.md`.

## Prototype Capabilities
- Registry-driven multi-namespace support (9 OCI namespaces: compute, VCN, block storage, load balancer, database, autonomous DB, object storage, OKE, functions)
- Natural-language monitoring flow with clarification-first handling for ambiguous requests
- Named-instance trend flow with exact or partial instance resolution
- Runtime metric discovery for unknown namespaces via OCI ListMetrics API
- Instance caching with stale-while-revalidate for fast repeat queries
- Structured audit logging with per-request timing (JSONL format)
- Multi-user identity isolation with token-based auth for shared-VM pilot deployments
- Per-client profile isolation for Codex and Claude
- Default region and compartment persistence
- Instance Principals first, OCI config fallback support
- Structured response with summary, tables, charts, recommendations, and CSV export when needed
- Tokenized PNG and CSV artifact URLs
- Shared preference promotion through aggregation instead of live cross-user writes

## Run
See `docs/QUICKSTART.md` for the VM runbook and first test sequence.
See `docs/CLIENT_SETUP.md` for Codex, Claude, and ChatGPT client setup.

## Pilot Multi-User Mode
For the shared VM pilot, enable token enforcement and create one token per person/client pair.

Examples:

```bash
python3 scripts/manage_users.py add "rishabh" --client codex
python3 scripts/manage_users.py add "rishabh" --client claude
```

Each command prints a tokenized MCP URL:

```text
http://<vm-public-ip>:8000/mcp?u=<token>
```

Important:
- Tokens are credentials. Share them only with the intended tester.
- `rishabh + codex` and `rishabh + claude` intentionally use different profile directories.
- A fresh profile starts without a saved default region or compartment and will ask for setup first.
- Shared learnings are promoted later by `scripts/aggregate_learnings.py`; they are not written live across users.

## Seed Promotion Workflow
To promote useful generic learnings from local runtime state into repo seed files:

```bash
python3 scripts/promote_seeds.py --dry-run
python3 scripts/promote_seeds.py
```

Notes:
- Runtime files are read from `data/runtime/` by default.
- In pilot mode, per-profile state lives under `data/runtime/users/<profile_id>/`.
- Shared promoted learnings live under `data/runtime/shared/`.
- Promotion intentionally strips tenancy/user-specific values (OCIDs, IPs, emails, secret-like text).
- Review diffs before committing.
