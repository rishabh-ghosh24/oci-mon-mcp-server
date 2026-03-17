# oci-mon-mcp-server
MCP server that connects AI assistants to OCI Monitoring service. Query, analyze, and explore your OCI resource metrics through natural language.

## Repository Structure
```text
.
├── docs/
│   ├── CLIENT_SETUP.md
│   ├── TECHNICAL_REQUIREMENTS.md
│   ├── LEARNING_TEMPLATE_STRATEGY.md
│   ├── PROTOTYPE_PRD.md
│   ├── PRIORITIZED_BACKLOG.md
│   ├── PRODUCT_TECH_NOTES.md
│   └── QUICKSTART.md
├── data/
│   ├── seed_query_templates.json
│   ├── seed_user_memory.json
│   └── runtime/ (auto-created local state; gitignored)
├── src/
│   └── oci_mon_mcp/
│       ├── __init__.py
│       └── server.py
├── tests/
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
- Natural-language monitoring flow for compute CPU and memory queries
- Clarification-first handling for ambiguous requests
- Named-instance trend flow with exact or partial instance resolution
- Disk I/O flow after clarification
- Default region and compartment persistence
- Instance Principals first, OCI config fallback support
- Structured response with summary, tables, charts, recommendations, and CSV export when needed
- Tokenized PNG and CSV artifact URLs
- Seeded generic templates and preferences for better first-run behavior across users

## Run
See `docs/QUICKSTART.md` for the VM runbook and first test sequence.
See `docs/CLIENT_SETUP.md` for Codex, Claude, and ChatGPT client setup.

## Seed Promotion Workflow
To promote useful generic learnings from local runtime state into repo seed files:

```bash
python3 scripts/promote_seeds.py --dry-run
python3 scripts/promote_seeds.py
```

Notes:
- Runtime files are read from `data/runtime/` by default and are shared by the VM-hosted server
  across users/profiles.
- Promotion intentionally strips tenancy/user-specific values (OCIDs, IPs, emails, secret-like text).
- Review diffs before committing.
