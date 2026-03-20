"""JSON-backed persistence for user context, learnings, and query templates."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
from typing import Any, Iterator

from .identity import get_current_identity

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


def read_json(path: Path, default_value: Any) -> Any:
    """Load JSON from disk or return a deep copy of the provided default."""
    if not path.exists():
        return deepcopy(default_value)
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically so readers never see partial content."""
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


@contextmanager
def locked_file(lock_path: Path, thread_lock: threading.RLock) -> Iterator[None]:
    """Coordinate file access across threads and processes."""
    with thread_lock:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.touch(exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            if fcntl is not None:  # pragma: no branch - simple platform guard
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:  # pragma: no branch - simple platform guard
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_json_file(path: Path, default_value: Any) -> None:
    """Create a JSON file if it does not yet exist."""
    if path.exists():
        return
    write_json(path, default_value)


def _merge_missing(existing: dict[str, Any], defaults: dict[str, Any]) -> bool:
    changed = False
    for key, value in defaults.items():
        if key not in existing:
            existing[key] = deepcopy(value)
            changed = True
            continue
        if isinstance(value, dict) and isinstance(existing.get(key), dict):
            if _merge_missing(existing[key], value):
                changed = True
    return changed


def _template_key(template: dict[str, Any]) -> tuple[Any, ...]:
    return (
        template.get("intent_type"),
        template.get("metric_key"),
        template.get("time_window"),
        template.get("threshold"),
        template.get("aggregation"),
    )


def _profile_defaults(profile_id: str, user_id: str | None) -> dict[str, Any]:
    resolved_user_id = user_id if user_id is not None else profile_id
    return {
        "profile_id": profile_id,
        "user_id": resolved_user_id,
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


def _extract_seed_shared_preferences(seed_memory_path: Path) -> list[dict[str, Any]]:
    payload = read_json(seed_memory_path, {"profiles": {}, "shared_preferences": []})
    shared = payload.get("shared_preferences")
    if isinstance(shared, list):
        return deepcopy(shared)
    default_profile = payload.get("profiles", {}).get("default", {})
    return deepcopy(default_profile.get("learned_preferences", []))


class UserRepository:
    """Per-profile runtime state stored in its own directory."""

    def __init__(
        self,
        profile_id: str,
        user_id: str,
        *,
        runtime_dir: Path,
        data_dir: Path,
    ) -> None:
        self.profile_id = profile_id
        self.user_id = user_id
        self.runtime_dir = runtime_dir
        self.data_dir = data_dir
        self.profile_dir = runtime_dir / "users" / profile_id
        self.memory_path = self.profile_dir / "user_memory.json"
        self.templates_path = self.profile_dir / "query_templates.json"
        self.memory_lock_path = self.profile_dir / ".user_memory.lock"
        self.templates_lock_path = self.profile_dir / ".query_templates.lock"
        self._memory_lock = threading.RLock()
        self._templates_lock = threading.RLock()
        self.legacy_runtime_memory_path = runtime_dir / "user_memory.json"
        self._ensure_bootstrap_files()

    def _ensure_bootstrap_files(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if not self.memory_path.exists():
            write_json(self.memory_path, self._bootstrap_profile())
        ensure_json_file(self.templates_path, [])

    def _bootstrap_profile(self) -> dict[str, Any]:
        default_profile = _profile_defaults(self.profile_id, self.user_id)
        legacy_memory = read_json(self.legacy_runtime_memory_path, {"profiles": {}})
        legacy_profile = legacy_memory.get("profiles", {}).get(self.profile_id)
        if not isinstance(legacy_profile, dict):
            return default_profile
        profile = deepcopy(legacy_profile)
        profile["profile_id"] = self.profile_id
        profile["user_id"] = profile.get("user_id") or self.user_id
        _merge_missing(profile, default_profile)
        return profile

    def ensure_user_id(self, user_id: str | None) -> None:
        """Update the durable user_id when registry identity is known."""
        if not user_id or user_id == self.user_id:
            return
        self.user_id = user_id
        with locked_file(self.memory_lock_path, self._memory_lock):
            profile = read_json(self.memory_path, _profile_defaults(self.profile_id, self.user_id))
            profile["user_id"] = user_id
            _merge_missing(profile, _profile_defaults(self.profile_id, user_id))
            write_json(self.memory_path, profile)

    def get_profile(self) -> dict[str, Any]:
        """Return the stored profile payload."""
        with locked_file(self.memory_lock_path, self._memory_lock):
            profile = read_json(self.memory_path, _profile_defaults(self.profile_id, self.user_id))
            changed = _merge_missing(profile, _profile_defaults(self.profile_id, self.user_id))
            if self.user_id and profile.get("user_id") != self.user_id:
                profile["user_id"] = self.user_id
                changed = True
            if changed:
                write_json(self.memory_path, profile)
            return deepcopy(profile)

    def update_profile(self, profile: dict[str, Any]) -> None:
        """Replace the stored profile payload."""
        payload = deepcopy(profile)
        payload["profile_id"] = self.profile_id
        payload["user_id"] = payload.get("user_id") or self.user_id
        _merge_missing(payload, _profile_defaults(self.profile_id, payload["user_id"]))
        with locked_file(self.memory_lock_path, self._memory_lock):
            write_json(self.memory_path, payload)

    def set_default_context(
        self,
        *,
        region: str,
        compartment_name: str,
        compartment_id: str | None,
        auth_mode: str = "instance_principal",
        tenancy_id: str | None = None,
        available_compartments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Persist the default region and compartment."""
        with locked_file(self.memory_lock_path, self._memory_lock):
            profile = read_json(self.memory_path, _profile_defaults(self.profile_id, self.user_id))
            _merge_missing(profile, _profile_defaults(self.profile_id, self.user_id))
            profile["region"] = region
            profile["default_compartment_name"] = compartment_name
            profile["default_compartment_id"] = compartment_id
            profile["auth_mode"] = auth_mode
            profile["pending_clarification"] = None
            if tenancy_id is not None:
                profile["tenancy_id"] = tenancy_id
            if available_compartments is not None:
                profile["available_compartments"] = available_compartments
            write_json(self.memory_path, profile)
            return deepcopy(profile)

    def set_auth_fallback(self, *, config_path: str, profile_name: str) -> dict[str, Any]:
        """Persist OCI config fallback settings."""
        with locked_file(self.memory_lock_path, self._memory_lock):
            profile = read_json(self.memory_path, _profile_defaults(self.profile_id, self.user_id))
            _merge_missing(profile, _profile_defaults(self.profile_id, self.user_id))
            profile["auth_mode"] = "config_file"
            profile["config_fallback"] = {
                "config_path": config_path,
                "profile": profile_name,
            }
            write_json(self.memory_path, profile)
            return deepcopy(profile)

    def set_pending_clarification(self, pending: dict[str, Any] | None) -> None:
        """Store or clear pending clarification state."""
        with locked_file(self.memory_lock_path, self._memory_lock):
            profile = read_json(self.memory_path, _profile_defaults(self.profile_id, self.user_id))
            _merge_missing(profile, _profile_defaults(self.profile_id, self.user_id))
            profile["pending_clarification"] = deepcopy(pending)
            write_json(self.memory_path, profile)

    def set_last_resolved_context(self, context: dict[str, Any]) -> None:
        """Persist the last resolved query context."""
        with locked_file(self.memory_lock_path, self._memory_lock):
            profile = read_json(self.memory_path, _profile_defaults(self.profile_id, self.user_id))
            _merge_missing(profile, _profile_defaults(self.profile_id, self.user_id))
            profile["last_resolved_context"] = deepcopy(context)
            write_json(self.memory_path, profile)

    def get_preference(self, intent_key: str) -> dict[str, Any] | None:
        """Return the learned preference for this profile, if any."""
        profile = self.get_profile()
        for item in profile.get("learned_preferences", []):
            if item.get("intent_key") == intent_key:
                result = deepcopy(item)
                result["scope"] = "profile"
                return result
        return None

    def remember_preference(
        self,
        *,
        intent_key: str,
        resolved_metric: str,
        confidence: float = 0.9,
    ) -> None:
        """Upsert a learned preference for this profile only."""
        with locked_file(self.memory_lock_path, self._memory_lock):
            profile = read_json(self.memory_path, _profile_defaults(self.profile_id, self.user_id))
            _merge_missing(profile, _profile_defaults(self.profile_id, self.user_id))
            preferences = profile.setdefault("learned_preferences", [])
            now = utc_now_iso()
            for item in preferences:
                if item.get("intent_key") == intent_key:
                    item["resolved_metric"] = resolved_metric
                    item["confidence"] = confidence
                    item["last_used_at"] = now
                    item["usage_count"] = int(item.get("usage_count", 0)) + 1
                    write_json(self.memory_path, profile)
                    return
            preferences.append(
                {
                    "intent_key": intent_key,
                    "resolved_metric": resolved_metric,
                    "confidence": confidence,
                    "last_used_at": now,
                    "usage_count": 1,
                }
            )
            write_json(self.memory_path, profile)

    def list_templates(
        self,
        *,
        region: str | None = None,
        tenancy_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List personal templates filtered to the active scope."""
        with locked_file(self.templates_lock_path, self._templates_lock):
            templates = read_json(self.templates_path, [])
        return [
            deepcopy(template)
            for template in templates
            if (template.get("region") in {None, region})
            and (template.get("tenancy_id") in {None, tenancy_id})
        ]

    def save_template(self, *, parsed_query: dict[str, Any], query_text: str) -> dict[str, Any]:
        """Save or update a successful personal query template."""
        profile = self.get_profile()
        with locked_file(self.templates_lock_path, self._templates_lock):
            templates = read_json(self.templates_path, [])
            now = utc_now_iso()
            incoming_key = (
                parsed_query["intent"],
                parsed_query["metric_key"],
                parsed_query.get("time_range"),
                parsed_query.get("threshold"),
                parsed_query.get("aggregation"),
            )
            for template in templates:
                existing_key = _template_key(template)
                if (
                    existing_key == incoming_key
                    and template.get("region") == profile.get("region")
                    and template.get("tenancy_id") == profile.get("tenancy_id")
                ):
                    patterns = template.setdefault("nl_patterns", [])
                    if parsed_query["source_query"] not in patterns:
                        patterns.append(parsed_query["source_query"])
                    template["updated_at"] = now
                    template["last_used_at"] = now
                    template["usage_count"] = int(template.get("usage_count", 0)) + 1
                    write_json(self.templates_path, templates)
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
            write_json(self.templates_path, templates)
            return deepcopy(template)


class SharedRepository:
    """Shared cross-user learnings read by the live server."""

    def __init__(self, *, runtime_dir: Path, data_dir: Path) -> None:
        self.runtime_dir = runtime_dir
        self.data_dir = data_dir
        self.shared_dir = runtime_dir / "shared"
        self.templates_path = self.shared_dir / "master_templates.json"
        self.preferences_path = self.shared_dir / "shared_preferences.json"
        self.templates_lock_path = self.shared_dir / ".master_templates.lock"
        self.preferences_lock_path = self.shared_dir / ".shared_preferences.lock"
        self._templates_lock = threading.RLock()
        self._preferences_lock = threading.RLock()
        self.seed_memory_path = data_dir / "seed_user_memory.json"
        self.seed_templates_path = data_dir / "seed_query_templates.json"
        self.legacy_runtime_memory_path = runtime_dir / "user_memory.json"
        self.legacy_runtime_templates_path = runtime_dir / "query_templates.json"
        self.legacy_repo_memory_path = data_dir / "user_memory.json"
        self.legacy_repo_templates_path = data_dir / "query_templates.json"
        self._ensure_bootstrap_files()

    def _ensure_bootstrap_files(self) -> None:
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        if not self.preferences_path.exists():
            write_json(self.preferences_path, self._bootstrap_shared_preferences())
        if not self.templates_path.exists():
            write_json(self.templates_path, self._bootstrap_templates())

    def _bootstrap_shared_preferences(self) -> list[dict[str, Any]]:
        for source in (self.legacy_runtime_memory_path, self.legacy_repo_memory_path):
            payload = read_json(source, None)
            if isinstance(payload, dict) and isinstance(payload.get("shared_preferences"), list):
                return deepcopy(payload["shared_preferences"])
        return _extract_seed_shared_preferences(self.seed_memory_path)

    def _bootstrap_templates(self) -> list[dict[str, Any]]:
        for source in (
            self.legacy_runtime_templates_path,
            self.legacy_repo_templates_path,
            self.seed_templates_path,
        ):
            payload = read_json(source, None)
            if isinstance(payload, list):
                return deepcopy(payload)
        return []

    def get_preference(self, intent_key: str) -> dict[str, Any] | None:
        """Return a shared preference by intent key."""
        with locked_file(self.preferences_lock_path, self._preferences_lock):
            preferences = read_json(self.preferences_path, [])
        for item in preferences:
            if item.get("intent_key") == intent_key:
                result = deepcopy(item)
                result["scope"] = "shared"
                return result
        return None

    def list_templates(
        self,
        *,
        region: str | None = None,
        tenancy_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List shared templates filtered to the active scope."""
        with locked_file(self.templates_lock_path, self._templates_lock):
            templates = read_json(self.templates_path, [])
        return [
            deepcopy(template)
            for template in templates
            if (template.get("region") in {None, region})
            and (template.get("tenancy_id") in {None, tenancy_id})
        ]

    def read_shared_preferences(self) -> list[dict[str, Any]]:
        """Return the raw shared preference payload."""
        with locked_file(self.preferences_lock_path, self._preferences_lock):
            return read_json(self.preferences_path, [])

    def write_shared_preferences(self, preferences: list[dict[str, Any]]) -> None:
        """Persist shared preferences atomically."""
        with locked_file(self.preferences_lock_path, self._preferences_lock):
            write_json(self.preferences_path, preferences)

    def read_master_templates(self) -> list[dict[str, Any]]:
        """Return the raw shared template payload."""
        with locked_file(self.templates_lock_path, self._templates_lock):
            return read_json(self.templates_path, [])

    def write_master_templates(self, templates: list[dict[str, Any]]) -> None:
        """Persist shared templates atomically."""
        with locked_file(self.templates_lock_path, self._templates_lock):
            write_json(self.templates_path, templates)


class RepositoryFactory:
    """Create per-user repositories and serve registry-backed identity lookups."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir = Path(os.getenv("OCI_MON_MCP_STATE_DIR", str(self.data_dir / "runtime")))
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.runtime_dir / "user_registry.json"
        self.registry_lock_path = self.runtime_dir / ".user_registry.lock"
        self._registry_lock = threading.RLock()
        self._registry_cache: dict[str, dict[str, Any]] | None = None
        self._registry_mtime_ns: int | None = None
        self._user_repos: dict[str, UserRepository] = {}
        ensure_json_file(self.registry_path, {})
        self.shared = SharedRepository(runtime_dir=self.runtime_dir, data_dir=self.data_dir)

    def load_registry(self) -> dict[str, dict[str, Any]]:
        """Return the current token registry, reloading when it changes on disk."""
        with self._registry_lock:
            ensure_json_file(self.registry_path, {})
            mtime_ns = self.registry_path.stat().st_mtime_ns
            if self._registry_cache is not None and self._registry_mtime_ns == mtime_ns:
                return deepcopy(self._registry_cache)
            with locked_file(self.registry_lock_path, self._registry_lock):
                payload = read_json(self.registry_path, {})
            self._registry_cache = payload
            self._registry_mtime_ns = self.registry_path.stat().st_mtime_ns
            return deepcopy(payload)

    def save_registry(self, payload: dict[str, dict[str, Any]]) -> None:
        """Persist the token registry."""
        with locked_file(self.registry_lock_path, self._registry_lock):
            write_json(self.registry_path, payload)
        with self._registry_lock:
            self._registry_cache = deepcopy(payload)
            self._registry_mtime_ns = self.registry_path.stat().st_mtime_ns

    def resolve_token(self, token: str | None) -> dict[str, Any] | None:
        """Resolve an active token to its registry record."""
        if not token:
            return None
        record = self.load_registry().get(token)
        if not isinstance(record, dict):
            return None
        if record.get("status") != "active":
            return None
        if not record.get("profile_id") or not record.get("user_id"):
            return None
        return deepcopy(record)

    def profile_registry_records(self, profile_id: str) -> list[dict[str, Any]]:
        """Return all registry records associated with a profile."""
        return [
            deepcopy(record)
            for record in self.load_registry().values()
            if isinstance(record, dict) and record.get("profile_id") == profile_id
        ]

    def profile_registry_summary(self, profile_id: str) -> dict[str, Any] | None:
        """Return the best-known registry summary for a profile."""
        records = self.profile_registry_records(profile_id)
        if not records:
            return None
        active = any(record.get("status") == "active" for record in records)
        preferred = next(
            (record for record in records if record.get("status") == "active"),
            records[0],
        )
        return {
            "profile_id": profile_id,
            "user_id": preferred.get("user_id"),
            "client_type": preferred.get("client_type"),
            "active": active,
            "records": records,
        }

    def profile_identity_map(self) -> dict[str, dict[str, Any]]:
        """Return profile activity and user mapping derived from the registry."""
        summaries: dict[str, dict[str, Any]] = {}
        for record in self.load_registry().values():
            if not isinstance(record, dict):
                continue
            profile_id = record.get("profile_id")
            user_id = record.get("user_id")
            if not profile_id or not user_id:
                continue
            summary = summaries.setdefault(
                profile_id,
                {
                    "profile_id": profile_id,
                    "user_id": user_id,
                    "active": False,
                },
            )
            summary["user_id"] = summary.get("user_id") or user_id
            if record.get("status") == "active":
                summary["active"] = True
        return summaries

    def _resolve_user_id(self, profile_id: str, user_id: str | None = None) -> str:
        if user_id:
            return user_id
        identity = get_current_identity()
        if identity is not None and identity.profile_id == profile_id:
            return identity.user_id
        summary = self.profile_registry_summary(profile_id)
        if summary and summary.get("user_id"):
            return str(summary["user_id"])
        return profile_id

    def get_user_repo(self, profile_id: str, user_id: str | None = None) -> UserRepository:
        """Return the cached user repository for a profile."""
        resolved_user_id = self._resolve_user_id(profile_id, user_id)
        repo = self._user_repos.get(profile_id)
        if repo is None:
            repo = UserRepository(
                profile_id,
                resolved_user_id,
                runtime_dir=self.runtime_dir,
                data_dir=self.data_dir,
            )
            self._user_repos[profile_id] = repo
            return repo
        repo.ensure_user_id(resolved_user_id)
        return repo


class JsonRepository:
    """Compatibility facade exposing the original repository API."""

    def __init__(
        self,
        data_dir: Path | None = None,
        factory: RepositoryFactory | None = None,
    ) -> None:
        self.factory = factory or RepositoryFactory(data_dir=data_dir)

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        return self.factory.get_user_repo(profile_id).get_profile()

    def update_profile(self, profile_id: str, profile: dict[str, Any]) -> None:
        self.factory.get_user_repo(profile_id).update_profile(profile)

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
        return self.factory.get_user_repo(profile_id).set_default_context(
            region=region,
            compartment_name=compartment_name,
            compartment_id=compartment_id,
            auth_mode=auth_mode,
            tenancy_id=tenancy_id,
            available_compartments=available_compartments,
        )

    def set_auth_fallback(
        self,
        profile_id: str,
        *,
        config_path: str,
        profile_name: str,
    ) -> dict[str, Any]:
        return self.factory.get_user_repo(profile_id).set_auth_fallback(
            config_path=config_path,
            profile_name=profile_name,
        )

    def set_pending_clarification(self, profile_id: str, pending: dict[str, Any] | None) -> None:
        self.factory.get_user_repo(profile_id).set_pending_clarification(pending)

    def set_last_resolved_context(self, profile_id: str, context: dict[str, Any]) -> None:
        self.factory.get_user_repo(profile_id).set_last_resolved_context(context)

    def get_preference(self, profile_id: str, intent_key: str) -> dict[str, Any] | None:
        personal = self.factory.get_user_repo(profile_id).get_preference(intent_key)
        if personal is not None:
            return personal
        return self.factory.shared.get_preference(intent_key)

    def remember_preference(
        self,
        profile_id: str,
        *,
        intent_key: str,
        resolved_metric: str,
        confidence: float = 0.9,
    ) -> None:
        self.factory.get_user_repo(profile_id).remember_preference(
            intent_key=intent_key,
            resolved_metric=resolved_metric,
            confidence=confidence,
        )

    def list_templates(self, *, profile_id: str | None = None) -> list[dict[str, Any]]:
        if profile_id is None:
            return self.factory.shared.list_templates()
        profile = self.get_profile(profile_id)
        region = profile.get("region")
        tenancy_id = profile.get("tenancy_id")
        shared_templates = self.factory.shared.list_templates(region=region, tenancy_id=tenancy_id)
        personal_templates = self.factory.get_user_repo(profile_id).list_templates(
            region=region,
            tenancy_id=tenancy_id,
        )
        merged: dict[tuple[Any, ...], dict[str, Any]] = {
            _template_key(template): deepcopy(template) for template in shared_templates
        }
        for template in personal_templates:
            merged.setdefault(_template_key(template), deepcopy(template))
        return list(merged.values())

    def save_template(
        self,
        *,
        profile_id: str,
        parsed_query: dict[str, Any],
        query_text: str,
    ) -> dict[str, Any]:
        return self.factory.get_user_repo(profile_id).save_template(
            parsed_query=parsed_query,
            query_text=query_text,
        )
