"""JSON-backed persistence for user context and query templates."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import shutil
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - platform dependent
    fcntl = None  # type: ignore[assignment]


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
        self.runtime_dir = runtime_dir

        # Runtime, user-specific state paths.
        self.memory_path = runtime_dir / "user_memory.json"
        self.templates_path = runtime_dir / "query_templates.json"
        self.memory_lock_path = runtime_dir / ".user_memory.lock"
        self.templates_lock_path = runtime_dir / ".query_templates.lock"
        self._memory_lock = threading.RLock()
        self._templates_lock = threading.RLock()

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

    @staticmethod
    def _default_memory_payload() -> dict[str, Any]:
        return {"profiles": {}, "shared_preferences": []}

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

    @contextmanager
    def _locked(self, *, lock_path: Path, thread_lock: threading.RLock) -> Iterator[None]:
        with thread_lock:
            lock_path.touch(exist_ok=True)
            with lock_path.open("a+", encoding="utf-8") as handle:
                if fcntl is not None:  # pragma: no branch - simple platform guard
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if fcntl is not None:  # pragma: no branch - simple platform guard
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_json(self, path: Path, default_value: Any) -> Any:
        if not path.exists():
            return deepcopy(default_value)
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
            prefix=f".{path.stem}.",
            suffix=".tmp",
        ) as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            temp_path = Path(handle.name)
        temp_path.replace(path)

    def _normalize_memory_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload.setdefault("profiles", {})
        payload.setdefault("shared_preferences", [])
        return payload

    def _profile_defaults(self, profile_id: str) -> dict[str, Any]:
        return {
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
        }

    def _ensure_profile(self, payload: dict[str, Any], profile_id: str) -> tuple[dict[str, Any], bool]:
        profiles = payload.setdefault("profiles", {})
        existing = profiles.get(profile_id)
        if existing is None:
            profile = self._profile_defaults(profile_id)
            profiles[profile_id] = profile
            return profile, True

        changed = False
        profile = existing
        for key, value in self._profile_defaults(profile_id).items():
            if key not in profile:
                profile[key] = deepcopy(value)
                changed = True
        return profile, changed

    def _upsert_preference(
        self,
        preferences: list[dict[str, Any]],
        *,
        intent_key: str,
        resolved_metric: str,
        confidence: float,
        now: str,
    ) -> dict[str, Any]:
        for item in preferences:
            if item.get("intent_key") == intent_key:
                item["resolved_metric"] = resolved_metric
                item["confidence"] = confidence
                item["last_used_at"] = now
                item["usage_count"] = int(item.get("usage_count", 0)) + 1
                return item

        item = {
            "intent_key": intent_key,
            "resolved_metric": resolved_metric,
            "confidence": confidence,
            "last_used_at": now,
            "usage_count": 1,
        }
        preferences.append(item)
        return item

    def load_memory(self) -> dict[str, Any]:
        """Load the memory store."""
        with self._locked(lock_path=self.memory_lock_path, thread_lock=self._memory_lock):
            payload = self._read_json(self.memory_path, self._default_memory_payload())
            return self._normalize_memory_payload(payload)

    def save_memory(self, payload: dict[str, Any]) -> None:
        """Persist the memory store."""
        with self._locked(lock_path=self.memory_lock_path, thread_lock=self._memory_lock):
            self._write_json(self.memory_path, self._normalize_memory_payload(payload))

    def load_templates(self) -> list[dict[str, Any]]:
        """Load stored successful templates."""
        with self._locked(lock_path=self.templates_lock_path, thread_lock=self._templates_lock):
            return self._read_json(self.templates_path, [])

    def save_templates(self, payload: list[dict[str, Any]]) -> None:
        """Persist successful templates."""
        with self._locked(lock_path=self.templates_lock_path, thread_lock=self._templates_lock):
            self._write_json(self.templates_path, payload)

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        """Return a profile, creating a default one if missing."""
        with self._locked(lock_path=self.memory_lock_path, thread_lock=self._memory_lock):
            memory = self._normalize_memory_payload(
                self._read_json(self.memory_path, self._default_memory_payload())
            )
            profile, changed = self._ensure_profile(memory, profile_id)
            if changed:
                self._write_json(self.memory_path, memory)
            return deepcopy(profile)

    def update_profile(self, profile_id: str, profile: dict[str, Any]) -> None:
        """Write back the full profile object."""
        with self._locked(lock_path=self.memory_lock_path, thread_lock=self._memory_lock):
            memory = self._normalize_memory_payload(
                self._read_json(self.memory_path, self._default_memory_payload())
            )
            memory.setdefault("profiles", {})[profile_id] = deepcopy(profile)
            self._write_json(self.memory_path, memory)

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
        with self._locked(lock_path=self.memory_lock_path, thread_lock=self._memory_lock):
            memory = self._normalize_memory_payload(
                self._read_json(self.memory_path, self._default_memory_payload())
            )
            profile, _ = self._ensure_profile(memory, profile_id)
            profile["region"] = region
            profile["default_compartment_name"] = compartment_name
            profile["default_compartment_id"] = compartment_id
            profile["auth_mode"] = auth_mode
            if tenancy_id is not None:
                profile["tenancy_id"] = tenancy_id
            if available_compartments is not None:
                profile["available_compartments"] = available_compartments
            profile["pending_clarification"] = None
            self._write_json(self.memory_path, memory)
            return deepcopy(profile)

    def set_auth_fallback(
        self,
        profile_id: str,
        *,
        config_path: str,
        profile_name: str,
    ) -> dict[str, Any]:
        """Persist OCI config fallback auth settings."""
        with self._locked(lock_path=self.memory_lock_path, thread_lock=self._memory_lock):
            memory = self._normalize_memory_payload(
                self._read_json(self.memory_path, self._default_memory_payload())
            )
            profile, _ = self._ensure_profile(memory, profile_id)
            profile["auth_mode"] = "config_file"
            profile["config_fallback"] = {
                "config_path": config_path,
                "profile": profile_name,
            }
            self._write_json(self.memory_path, memory)
            return deepcopy(profile)

    def set_pending_clarification(self, profile_id: str, pending: dict[str, Any] | None) -> None:
        """Store or clear pending clarification state."""
        with self._locked(lock_path=self.memory_lock_path, thread_lock=self._memory_lock):
            memory = self._normalize_memory_payload(
                self._read_json(self.memory_path, self._default_memory_payload())
            )
            profile, _ = self._ensure_profile(memory, profile_id)
            profile["pending_clarification"] = deepcopy(pending)
            self._write_json(self.memory_path, memory)

    def set_last_resolved_context(self, profile_id: str, context: dict[str, Any]) -> None:
        """Persist the last successfully resolved query context."""
        with self._locked(lock_path=self.memory_lock_path, thread_lock=self._memory_lock):
            memory = self._normalize_memory_payload(
                self._read_json(self.memory_path, self._default_memory_payload())
            )
            profile, _ = self._ensure_profile(memory, profile_id)
            profile["last_resolved_context"] = deepcopy(context)
            self._write_json(self.memory_path, memory)

    def get_preference(self, profile_id: str, intent_key: str) -> dict[str, Any] | None:
        """Get a learned preference by intent key."""
        with self._locked(lock_path=self.memory_lock_path, thread_lock=self._memory_lock):
            memory = self._normalize_memory_payload(
                self._read_json(self.memory_path, self._default_memory_payload())
            )
            profile, changed = self._ensure_profile(memory, profile_id)
            if changed:
                self._write_json(self.memory_path, memory)

            for item in profile.get("learned_preferences", []):
                if item.get("intent_key") == intent_key:
                    result = deepcopy(item)
                    result["scope"] = "profile"
                    return result

            for item in memory.get("shared_preferences", []):
                if item.get("intent_key") == intent_key:
                    result = deepcopy(item)
                    result["scope"] = "shared"
                    return result
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
        with self._locked(lock_path=self.memory_lock_path, thread_lock=self._memory_lock):
            memory = self._normalize_memory_payload(
                self._read_json(self.memory_path, self._default_memory_payload())
            )
            profile, _ = self._ensure_profile(memory, profile_id)
            now = utc_now_iso()
            self._upsert_preference(
                profile.setdefault("learned_preferences", []),
                intent_key=intent_key,
                resolved_metric=resolved_metric,
                confidence=confidence,
                now=now,
            )
            self._upsert_preference(
                memory.setdefault("shared_preferences", []),
                intent_key=intent_key,
                resolved_metric=resolved_metric,
                confidence=confidence,
                now=now,
            )
            self._write_json(self.memory_path, memory)

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
        with self._locked(lock_path=self.templates_lock_path, thread_lock=self._templates_lock):
            templates = self._read_json(self.templates_path, [])
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
                    self._write_json(self.templates_path, templates)
                    return deepcopy(template)

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
            self._write_json(self.templates_path, templates)
            return deepcopy(template)
