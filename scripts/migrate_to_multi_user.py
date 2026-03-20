#!/usr/bin/env python3
"""Migrate legacy shared runtime state into the multi-user layout."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from oci_mon_mcp.repository import RepositoryFactory, read_json, utc_now_iso, write_json


def _legacy_profile_payload(profile_id: str, profile: dict[str, Any]) -> dict[str, Any]:
    config_fallback = profile.get("config_fallback") if isinstance(profile.get("config_fallback"), dict) else {}
    return {
        "profile_id": profile_id,
        "user_id": None,
        "tenancy_id": profile.get("tenancy_id", "unknown"),
        "region": profile.get("region"),
        "default_compartment_id": profile.get("default_compartment_id"),
        "default_compartment_name": profile.get("default_compartment_name"),
        "available_compartments": profile.get("available_compartments", []),
        "auth_mode": profile.get("auth_mode", "instance_principal"),
        "config_fallback": {
            "config_path": config_fallback.get("config_path", "~/.oci/config"),
            "profile": config_fallback.get("profile", "DEFAULT"),
        },
        "learned_preferences": profile.get("learned_preferences", []),
        "pending_clarification": profile.get("pending_clarification"),
        "last_resolved_context": profile.get("last_resolved_context", {}),
    }


def _backup_file(path: Path, backup_dir: Path) -> None:
    if not path.exists():
        return
    backup_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup_dir / path.name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None, help="Optional data directory override.")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve() if args.data_dir else None
    factory = RepositoryFactory(data_dir=data_dir)
    runtime_dir = factory.runtime_dir
    backup_dir = runtime_dir / ".backup" / utc_now_iso().replace(":", "-")

    legacy_memory_path = runtime_dir / "user_memory.json"
    legacy_templates_path = runtime_dir / "query_templates.json"
    _backup_file(legacy_memory_path, backup_dir)
    _backup_file(legacy_templates_path, backup_dir)

    memory = read_json(legacy_memory_path, {"profiles": {}, "shared_preferences": []})
    templates = read_json(legacy_templates_path, [])

    users_dir = runtime_dir / "users"
    users_dir.mkdir(parents=True, exist_ok=True)
    for profile_id, profile in memory.get("profiles", {}).items():
        profile_dir = users_dir / profile_id
        profile_dir.mkdir(parents=True, exist_ok=True)
        write_json(profile_dir / "user_memory.json", _legacy_profile_payload(profile_id, profile))
        write_json(profile_dir / "query_templates.json", [])

    shared_dir = runtime_dir / "shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    write_json(shared_dir / "shared_preferences.json", memory.get("shared_preferences", []))
    write_json(shared_dir / "master_templates.json", templates)

    registry_path = runtime_dir / "user_registry.json"
    if not registry_path.exists():
        write_json(registry_path, {})

    print(f"Backup created at {backup_dir}")
    print(f"Migrated {len(memory.get('profiles', {}))} profile(s) into {users_dir}")
    print(f"Moved shared preferences to {shared_dir / 'shared_preferences.json'}")
    print(f"Moved shared templates to {shared_dir / 'master_templates.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
