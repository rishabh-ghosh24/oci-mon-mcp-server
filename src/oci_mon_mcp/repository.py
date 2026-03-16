"""JSON-backed persistence for user context and query templates."""

from __future__ import annotations

import json
import os
import shutil
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    """Return a stable UTC timestamp."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_data_dir() -> Path:
    """Resolve the default data directory for local persistence."""
    return Path(__file__).resolve().parents[2] / "data"


class JsonRepository:
    """Persist learned state and successful query templates."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        runtime_dir = Path(
            os.getenv("OCI_MON_MCP_STATE_DIR", str(self.data_dir / "runtime"))
        )
        runtime_dir.mkdir(parents=True, exist_ok=True)

        # Runtime, user-specific state paths.
        self.memory_path = runtime_dir / "user_memory.json"
        self.templates_path = runtime_dir / "query_templates.json"

        # Repo-tracked generic bootstrap data.
        self.seed_memory_path = self.data_dir / "seed_user_memory.json"
        self.seed_templates_path = self.data_dir / "seed_query_templates.json"

        # Legacy in-repo mutable state paths used by older versions.
        self.legacy_memory_path = self.data_dir / "user_memory.json"
        self.legacy_templates_path = self.data_dir / "query_templates.json"

        self._ensure_runtime_file(
            target_path=self.memory_path,
            default_value={"profiles": {}},
            seed_path=self.seed_memory_path,
            legacy_path=self.legacy_memory_path,
        )
        self._ensure_runtime_file(
            target_path=self.templates_path,
            default_value=[],
            seed_path=self.seed_templates_path,
            legacy_path=self.legacy_templates_path,
        )

    def _ensure_file(self, path: Path, default_value: Any) -> None:
        if path.exists():
            return
        path.write_text(json.dumps(default_value, indent=2) + "\n", encoding="utf-8")

    def _ensure_runtime_file(
        self,
        *,
        target_path: Path,
        default_value: Any,
        seed_path: Path,
        legacy_path: Path,
    ) -> None:
        if target_path.exists():
            return
        if legacy_path.exists():
            shutil.copy2(legacy_path, target_path)
            return
        if seed_path.exists():
            shutil.copy2(seed_path, target_path)
            return
        self._ensure_file(target_path, default_value)

    def _read_json(self, path: Path, default_value: Any) -> Any:
        if not path.exists():
            return deepcopy(default_value)
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write_json(self, path: Path, payload: Any) -> None:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")

    def load_memory(self) -> dict[str, Any]:
        """Load the memory store."""
        payload = self._read_json(self.memory_path, {"profiles": {}})
        payload.setdefault("profiles", {})
        return payload

    def save_memory(self, payload: dict[str, Any]) -> None:
        """Persist the memory store."""
        payload.setdefault("profiles", {})
        self._write_json(self.memory_path, payload)

    def load_templates(self) -> list[dict[str, Any]]:
        """Load stored successful templates."""
        return self._read_json(self.templates_path, [])

    def save_templates(self, payload: list[dict[str, Any]]) -> None:
        """Persist successful templates."""
        self._write_json(self.templates_path, payload)

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        """Return a profile, creating a default one if missing."""
        memory = self.load_memory()
        profiles = memory.setdefault("profiles", {})
        profile = profiles.setdefault(
            profile_id,
            {
                "profile_id": profile_id,
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
                "learned_preferences": [],
                "pending_clarification": None,
                "last_resolved_context": {},
            },
        )
        self.save_memory(memory)
        return profile

    def update_profile(self, profile_id: str, profile: dict[str, Any]) -> None:
        """Write back the full profile object."""
        memory = self.load_memory()
        memory.setdefault("profiles", {})[profile_id] = profile
        self.save_memory(memory)

    def set_default_context(
        self,
        profile_id: str,
        *,
        region: str,
        compartment_name: str,
        compartment_id: str | None,
        auth_mode: str = "instance_principal",
        tenancy_id: str | None = None,
        available_compartments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Persist default region and compartment."""
        profile = self.get_profile(profile_id)
        profile["region"] = region
        profile["default_compartment_name"] = compartment_name
        profile["default_compartment_id"] = compartment_id
        profile["auth_mode"] = auth_mode
        if tenancy_id is not None:
            profile["tenancy_id"] = tenancy_id
        if available_compartments is not None:
            profile["available_compartments"] = available_compartments
        profile["pending_clarification"] = None
        self.update_profile(profile_id, profile)
        return profile

    def set_auth_fallback(
        self,
        profile_id: str,
        *,
        config_path: str,
        profile_name: str,
    ) -> dict[str, Any]:
        """Persist OCI config fallback auth settings."""
        profile = self.get_profile(profile_id)
        profile["auth_mode"] = "config_file"
        profile["config_fallback"] = {
            "config_path": config_path,
            "profile": profile_name,
        }
        self.update_profile(profile_id, profile)
        return profile

    def set_pending_clarification(self, profile_id: str, pending: dict[str, Any] | None) -> None:
        """Store or clear pending clarification state."""
        profile = self.get_profile(profile_id)
        profile["pending_clarification"] = pending
        self.update_profile(profile_id, profile)

    def set_last_resolved_context(self, profile_id: str, context: dict[str, Any]) -> None:
        """Persist the last successfully resolved query context."""
        profile = self.get_profile(profile_id)
        profile["last_resolved_context"] = context
        self.update_profile(profile_id, profile)

    def get_preference(self, profile_id: str, intent_key: str) -> dict[str, Any] | None:
        """Get a learned preference by intent key."""
        profile = self.get_profile(profile_id)
        for item in profile.get("learned_preferences", []):
            if item.get("intent_key") == intent_key:
                return item
        return None

    def remember_preference(
        self,
        profile_id: str,
        *,
        intent_key: str,
        resolved_metric: str,
        confidence: float = 0.9,
    ) -> None:
        """Upsert a learned preference."""
        profile = self.get_profile(profile_id)
        preferences = profile.setdefault("learned_preferences", [])
        now = utc_now_iso()
        for item in preferences:
            if item.get("intent_key") == intent_key:
                item["resolved_metric"] = resolved_metric
                item["confidence"] = confidence
                item["last_used_at"] = now
                self.update_profile(profile_id, profile)
                return
        preferences.append(
            {
                "intent_key": intent_key,
                "resolved_metric": resolved_metric,
                "confidence": confidence,
                "last_used_at": now,
            }
        )
        self.update_profile(profile_id, profile)

    def list_templates(self, *, profile_id: str | None = None) -> list[dict[str, Any]]:
        """List saved templates, optionally narrowed by profile scope."""
        templates = self.load_templates()
        if profile_id is None:
            return templates
        profile = self.get_profile(profile_id)
        region = profile.get("region")
        tenancy_id = profile.get("tenancy_id")
        return [
            template
            for template in templates
            if (
                (template.get("region") in {None, region})
                and (template.get("tenancy_id") in {None, tenancy_id})
            )
        ]

    def save_template(
        self,
        *,
        profile_id: str,
        parsed_query: dict[str, Any],
        query_text: str,
    ) -> dict[str, Any]:
        """Save or update a successful query template."""
        profile = self.get_profile(profile_id)
        templates = self.load_templates()
        now = utc_now_iso()
        template_key = (
            parsed_query["intent"],
            parsed_query["metric_key"],
            parsed_query.get("time_range"),
            parsed_query.get("threshold"),
            parsed_query.get("aggregation"),
        )
        for template in templates:
            existing_key = (
                template.get("intent_type"),
                template.get("metric_key"),
                template.get("time_window"),
                template.get("threshold"),
                template.get("aggregation"),
            )
            if (
                existing_key == template_key
                and template.get("region") == profile.get("region")
                and template.get("tenancy_id") == profile.get("tenancy_id")
            ):
                patterns = template.setdefault("nl_patterns", [])
                if parsed_query["source_query"] not in patterns:
                    patterns.append(parsed_query["source_query"])
                template["updated_at"] = now
                template["last_used_at"] = now
                template["usage_count"] = int(template.get("usage_count", 0)) + 1
                self.save_templates(templates)
                return template

        template = {
            "template_id": f"tmpl_{parsed_query['intent']}_{parsed_query['metric_key']}_{len(templates) + 1}",
            "tenancy_id": profile.get("tenancy_id"),
            "region": profile.get("region"),
            "created_at": now,
            "updated_at": now,
            "intent_type": parsed_query["intent"],
            "nl_patterns": [parsed_query["source_query"]],
            "resource_type": "compute_instance",
            "metric_key": parsed_query["metric_key"],
            "time_window": parsed_query["time_range"],
            "aggregation": parsed_query["aggregation"],
            "threshold": parsed_query.get("threshold"),
            "query_text": query_text,
            "usage_count": 1,
            "success_rate": 1.0,
            "last_used_at": now,
            "confidence": 0.95,
        }
        templates.append(template)
        self.save_templates(templates)
        return template
