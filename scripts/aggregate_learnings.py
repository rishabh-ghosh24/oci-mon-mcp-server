#!/usr/bin/env python3
"""Aggregate per-user learnings into shared runtime stores."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from oci_mon_mcp.repository import RepositoryFactory, read_json, utc_now_iso
from sanitize_utils import sanitize_pattern, sanitize_query_text


def _parse_time(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _iter_active_profiles(factory: RepositoryFactory) -> list[tuple[str, str, dict[str, Any]]]:
    profile_map = factory.profile_identity_map()
    results: list[tuple[str, str, dict[str, Any]]] = []
    users_dir = factory.runtime_dir / "users"
    if not users_dir.exists():
        return results
    for profile_dir in sorted(users_dir.iterdir()):
        if not profile_dir.is_dir():
            continue
        profile_id = profile_dir.name
        if profile_id == "default":
            continue
        summary = profile_map.get(profile_id)
        if summary is None or not summary.get("active"):
            continue
        memory_path = profile_dir / "user_memory.json"
        payload = read_json(memory_path, {})
        user_id = summary.get("user_id") or payload.get("user_id")
        if not user_id:
            continue
        results.append((profile_id, str(user_id), payload))
    return results


def aggregate_preferences(factory: RepositoryFactory) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for _profile_id, user_id, payload in _iter_active_profiles(factory):
        for pref in payload.get("learned_preferences", []):
            intent_key = str(pref.get("intent_key", "")).strip()
            resolved_metric = str(pref.get("resolved_metric", "")).strip()
            if not intent_key or not resolved_metric:
                continue
            key = (intent_key, resolved_metric)
            bucket = grouped.setdefault(
                key,
                {
                    "intent_key": intent_key,
                    "resolved_metric": resolved_metric,
                    "distinct_user_ids": set(),
                    "total_usage_count": 0,
                    "confidence": 0.0,
                    "last_used_at": None,
                },
            )
            bucket["distinct_user_ids"].add(user_id)
            bucket["total_usage_count"] += int(pref.get("usage_count", 0) or 0)
            bucket["confidence"] = max(bucket["confidence"], float(pref.get("confidence", 0.0) or 0.0))
            last_used_at = str(pref.get("last_used_at", "") or "")
            if _parse_time(last_used_at) > _parse_time(bucket["last_used_at"]):
                bucket["last_used_at"] = last_used_at

    winners: dict[str, dict[str, Any]] = {}
    conflicts: set[str] = set()
    for candidate in grouped.values():
        distinct_users = len(candidate["distinct_user_ids"])
        if distinct_users < 2 or int(candidate["total_usage_count"]) < 3:
            continue
        normalized = {
            "intent_key": candidate["intent_key"],
            "resolved_metric": candidate["resolved_metric"],
            "confidence": min(0.99, max(0.5, float(candidate["confidence"]))),
            "usage_count": int(candidate["total_usage_count"]),
            "last_used_at": candidate["last_used_at"] or utc_now_iso(),
        }
        existing = winners.get(candidate["intent_key"])
        if existing is None:
            winners[candidate["intent_key"]] = normalized
            continue
        current_rank = (
            int(existing["usage_count"]),
            float(existing["confidence"]),
            _parse_time(existing["last_used_at"]),
        )
        candidate_rank = (
            int(normalized["usage_count"]),
            float(normalized["confidence"]),
            _parse_time(normalized["last_used_at"]),
        )
        if candidate_rank > current_rank:
            winners[candidate["intent_key"]] = normalized
            conflicts.discard(candidate["intent_key"])
        elif candidate_rank == current_rank:
            conflicts.add(candidate["intent_key"])

    for intent_key in conflicts:
        winners.pop(intent_key, None)

    merged = {item.get("intent_key"): item for item in factory.shared.read_shared_preferences() if item.get("intent_key")}
    for intent_key, item in winners.items():
        merged[intent_key] = item
    return [merged[key] for key in sorted(merged)]


def _sanitize_patterns(patterns: list[Any]) -> list[str]:
    cleaned: list[str] = []
    for raw in patterns:
        if not isinstance(raw, str):
            continue
        sanitized = sanitize_pattern(raw)
        if sanitized and sanitized not in cleaned:
            cleaned.append(sanitized)
    return cleaned[:5]


def aggregate_templates(factory: RepositoryFactory) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for profile_id, user_id, _payload in _iter_active_profiles(factory):
        templates_path = factory.runtime_dir / "users" / profile_id / "query_templates.json"
        for template in read_json(templates_path, []):
            key = (
                template.get("intent_type"),
                template.get("metric_key"),
                template.get("time_window"),
                template.get("threshold"),
                template.get("aggregation"),
            )
            bucket = grouped.setdefault(
                key,
                {
                    "template": template,
                    "distinct_user_ids": set(),
                    "total_usage_count": 0,
                    "success_rates": [],
                    "confidence": 0.0,
                    "last_used_at": None,
                    "patterns": [],
                },
            )
            bucket["distinct_user_ids"].add(user_id)
            bucket["total_usage_count"] += int(template.get("usage_count", 0) or 0)
            bucket["success_rates"].append(float(template.get("success_rate", 0.0) or 0.0))
            bucket["confidence"] = max(bucket["confidence"], float(template.get("confidence", 0.0) or 0.0))
            bucket["patterns"].extend(_sanitize_patterns(template.get("nl_patterns", [])))
            last_used_at = str(template.get("last_used_at", "") or "")
            if _parse_time(last_used_at) > _parse_time(bucket["last_used_at"]):
                bucket["last_used_at"] = last_used_at

    promoted: list[dict[str, Any]] = []
    created_at = utc_now_iso()
    for bucket in grouped.values():
        distinct_users = len(bucket["distinct_user_ids"])
        total_usage_count = int(bucket["total_usage_count"])
        avg_success_rate = (
            sum(bucket["success_rates"]) / len(bucket["success_rates"]) if bucket["success_rates"] else 0.0
        )
        if distinct_users < 2 or total_usage_count < 3 or avg_success_rate < 0.8:
            continue
        template = bucket["template"]
        patterns = []
        for pattern in bucket["patterns"]:
            if pattern not in patterns:
                patterns.append(pattern)
        query_text = sanitize_query_text(str(template.get("query_text", "") or ""))
        if not patterns or not query_text:
            continue
        promoted.append(
            {
                "template_id": "",
                "tenancy_id": None,
                "region": None,
                "created_at": created_at,
                "updated_at": created_at,
                "intent_type": template.get("intent_type"),
                "nl_patterns": patterns[:5],
                "resource_type": template.get("resource_type", "compute_instance"),
                "metric_key": template.get("metric_key"),
                "time_window": template.get("time_window"),
                "aggregation": template.get("aggregation"),
                "threshold": template.get("threshold"),
                "query_text": query_text,
                "usage_count": total_usage_count,
                "success_rate": round(avg_success_rate, 3),
                "last_used_at": bucket["last_used_at"] or created_at,
                "confidence": min(0.99, max(0.5, float(bucket["confidence"]))),
            }
        )

    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for template in factory.shared.read_master_templates():
        key = (
            template.get("intent_type"),
            template.get("metric_key"),
            template.get("time_window"),
            template.get("threshold"),
            template.get("aggregation"),
        )
        merged[key] = template
    for template in promoted:
        key = (
            template.get("intent_type"),
            template.get("metric_key"),
            template.get("time_window"),
            template.get("threshold"),
            template.get("aggregation"),
        )
        merged[key] = template

    ranked = sorted(
        merged.values(),
        key=lambda item: (
            float(item.get("success_rate", 0.0) or 0.0),
            int(item.get("usage_count", 0) or 0),
        ),
        reverse=True,
    )
    for index, template in enumerate(ranked, start=1):
        template["template_id"] = template.get("template_id") or f"shared_{template['intent_type']}_{template['metric_key']}_{index}"
    return ranked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None, help="Optional data directory override.")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve() if args.data_dir else None
    factory = RepositoryFactory(data_dir=data_dir)

    shared_preferences = aggregate_preferences(factory)
    shared_templates = aggregate_templates(factory)
    factory.shared.write_shared_preferences(shared_preferences)
    factory.shared.write_master_templates(shared_templates)

    print(f"Updated shared_preferences.json with {len(shared_preferences)} entries")
    print(f"Updated master_templates.json with {len(shared_templates)} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
