#!/usr/bin/env python3
"""Promote sanitized runtime learnings into generic seed files.

This script is intentionally conservative:
- It only keeps compute templates with known metric keys.
- It removes tenancy/region/user-specific context.
- It drops patterns that look sensitive (OCIDs, IPs, emails, secrets).
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALLOWED_METRICS = {"cpu", "memory", "disk_io_throughput", "disk_io_iops"}
ALLOWED_INTENTS = {"threshold", "top_n", "worst_performing", "named_trend"}
ALLOWED_AGGREGATIONS = {"max", "mean", "avg", "sum"}

OCID_RE = re.compile(r"ocid1\.[A-Za-z0-9_.-]+")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
SECRETISH_RE = re.compile(
    r"(?:api[_-]?key|secret|password|token|bearer|authorization)",
    re.IGNORECASE,
)
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
LONG_HEX_RE = re.compile(r"\b[a-f0-9]{24,}\b", re.IGNORECASE)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)
        handle.write("\n")


def looks_sensitive(text: str) -> bool:
    return any(
        pattern.search(text)
        for pattern in (OCID_RE, IPV4_RE, EMAIL_RE, SECRETISH_RE, UUID_RE, LONG_HEX_RE)
    )


def sanitize_pattern(text: str) -> str | None:
    candidate = text.strip()
    if not candidate:
        return None
    if looks_sensitive(candidate):
        return None
    # Strip over-specific quoted literals.
    candidate = re.sub(r'"[^"]{32,}"', '"<value>"', candidate)
    candidate = re.sub(r"'[^']{32,}'", "'<value>'", candidate)
    return candidate


def sanitize_query_text(query_text: str) -> str | None:
    if not query_text:
        return None
    cleaned = OCID_RE.sub("<resource_ocid>", query_text)
    cleaned = IPV4_RE.sub("<ip_address>", cleaned)
    cleaned = EMAIL_RE.sub("<email>", cleaned)
    cleaned = UUID_RE.sub("<uuid>", cleaned)
    cleaned = LONG_HEX_RE.sub("<id>", cleaned)
    if SECRETISH_RE.search(cleaned):
        return None
    return cleaned


def normalize_template(template: dict[str, Any], index: int) -> dict[str, Any] | None:
    intent = str(template.get("intent_type", "")).strip()
    metric = str(template.get("metric_key", "")).strip()
    agg = str(template.get("aggregation", "")).strip().lower()
    if intent not in ALLOWED_INTENTS or metric not in ALLOWED_METRICS:
        return None
    if agg and agg not in ALLOWED_AGGREGATIONS:
        return None
    if template.get("resource_type") != "compute_instance":
        return None

    patterns = [
        clean
        for raw in template.get("nl_patterns", [])
        if isinstance(raw, str)
        for clean in [sanitize_pattern(raw)]
        if clean
    ]
    if not patterns:
        return None

    query_text = sanitize_query_text(str(template.get("query_text", "")).strip())
    if not query_text:
        return None

    created = now_iso()
    return {
        "template_id": f"seed_{intent}_{metric}_{index}",
        "tenancy_id": None,
        "region": None,
        "created_at": created,
        "updated_at": created,
        "intent_type": intent,
        "nl_patterns": patterns[:5],
        "resource_type": "compute_instance",
        "metric_key": metric,
        "time_window": template.get("time_window"),
        "aggregation": agg or "max",
        "threshold": template.get("threshold"),
        "query_text": query_text,
        "usage_count": int(template.get("usage_count", 1)),
        "success_rate": float(template.get("success_rate", 1.0)),
        "last_used_at": created,
        "confidence": min(0.99, max(0.5, float(template.get("confidence", 0.9)))),
    }


def merge_templates(
    existing_seed: list[dict[str, Any]],
    runtime_templates: list[dict[str, Any]],
    *,
    min_usage_count: int,
    min_success_rate: float,
    max_templates: int,
) -> list[dict[str, Any]]:
    merged_candidates: list[dict[str, Any]] = []
    merged_candidates.extend(existing_seed)
    for index, item in enumerate(runtime_templates, start=1):
        if int(item.get("usage_count", 0)) < min_usage_count:
            continue
        if float(item.get("success_rate", 0.0)) < min_success_rate:
            continue
        normalized = normalize_template(item, index + len(existing_seed))
        if normalized is not None:
            merged_candidates.append(normalized)

    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for template in merged_candidates:
        key = (
            template.get("intent_type"),
            template.get("metric_key"),
            template.get("time_window"),
            template.get("threshold"),
            template.get("aggregation"),
        )
        current = deduped.get(key)
        if current is None or float(template.get("success_rate", 0)) > float(
            current.get("success_rate", 0)
        ):
            deduped[key] = template

    ranked = sorted(
        deduped.values(),
        key=lambda item: (
            float(item.get("success_rate", 0)),
            int(item.get("usage_count", 0)),
        ),
        reverse=True,
    )[:max_templates]

    for idx, template in enumerate(ranked, start=1):
        template["template_id"] = f"seed_{template['intent_type']}_{template['metric_key']}_{idx}"
    return ranked


def build_seed_memory(runtime_memory: dict[str, Any]) -> dict[str, Any]:
    preference_counter: Counter[tuple[str, str]] = Counter()
    profiles = runtime_memory.get("profiles", {})
    if isinstance(profiles, dict):
        for profile in profiles.values():
            for pref in profile.get("learned_preferences", []):
                intent = str(pref.get("intent_key", "")).strip()
                metric = str(pref.get("resolved_metric", "")).strip()
                if intent and metric in ALLOWED_METRICS:
                    preference_counter[(intent, metric)] += 1

    learned_preferences = []
    for (intent, metric), count in preference_counter.most_common(5):
        learned_preferences.append(
            {
                "intent_key": intent,
                "resolved_metric": metric,
                "confidence": min(0.99, 0.6 + count * 0.05),
                "last_used_at": now_iso(),
            }
        )

    return {
        "profiles": {
            "default": {
                "profile_id": "default",
                "tenancy_id": "unknown",
                "region": None,
                "default_compartment_id": None,
                "default_compartment_name": None,
                "available_compartments": [],
                "auth_mode": "instance_principal",
                "config_fallback": {
                    "config_path": "~/.oci/config",
                    "profile": "DEFAULT",
                },
                "learned_preferences": learned_preferences,
                "pending_clarification": None,
                "last_resolved_context": {},
            }
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data", help="Repository data directory.")
    parser.add_argument(
        "--runtime-dir",
        default=None,
        help="Runtime state directory (defaults to <data-dir>/runtime).",
    )
    parser.add_argument("--min-usage-count", type=int, default=2)
    parser.add_argument("--min-success-rate", type=float, default=0.8)
    parser.add_argument("--max-templates", type=int, default=25)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    runtime_dir = Path(args.runtime_dir).resolve() if args.runtime_dir else data_dir / "runtime"

    runtime_templates_path = runtime_dir / "query_templates.json"
    runtime_memory_path = runtime_dir / "user_memory.json"
    seed_templates_path = data_dir / "seed_query_templates.json"
    seed_memory_path = data_dir / "seed_user_memory.json"

    runtime_templates = load_json(runtime_templates_path, [])
    runtime_memory = load_json(runtime_memory_path, {"profiles": {}})
    existing_seed_templates = load_json(seed_templates_path, [])

    promoted_templates = merge_templates(
        existing_seed=existing_seed_templates,
        runtime_templates=runtime_templates,
        min_usage_count=args.min_usage_count,
        min_success_rate=args.min_success_rate,
        max_templates=args.max_templates,
    )
    promoted_memory = build_seed_memory(runtime_memory)

    if args.dry_run:
        print(f"Runtime templates read: {len(runtime_templates)}")
        print(f"Seed templates after promotion: {len(promoted_templates)}")
        print(
            "Top promoted template IDs:",
            ", ".join(item["template_id"] for item in promoted_templates[:5]) or "<none>",
        )
        print(
            "Learned preference count:",
            len(promoted_memory["profiles"]["default"]["learned_preferences"]),
        )
        return 0

    write_json(seed_templates_path, promoted_templates)
    write_json(seed_memory_path, promoted_memory)
    print(f"Updated {seed_templates_path}")
    print(f"Updated {seed_memory_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
