#!/usr/bin/env python3
"""Manage pilot user tokens for the multi-user MCP server."""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from oci_mon_mcp.repository import RepositoryFactory, utc_now_iso


def _normalize_user_id(raw: str) -> str:
    user_id = raw.strip().lower()
    if not user_id:
        raise ValueError("user_id cannot be empty")
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
    sanitized = "".join(ch if ch in allowed else "-" for ch in user_id)
    sanitized = sanitized.strip(".-_")
    if not sanitized:
        raise ValueError("user_id must contain at least one valid character")
    return sanitized


def _normalize_client(raw: str) -> str:
    client = raw.strip().lower()
    if client not in {"codex", "claude"}:
        raise ValueError("client must be one of: codex, claude")
    return client


def _profile_id_for(user_id: str, client_type: str) -> str:
    return f"pilot_{user_id}_{client_type}"


def _find_records(registry: dict[str, dict[str, Any]], *, user_id: str, client_type: str) -> list[tuple[str, dict[str, Any]]]:
    profile_id = _profile_id_for(user_id, client_type)
    matches: list[tuple[str, dict[str, Any]]] = []
    for token, record in registry.items():
        if (
            isinstance(record, dict)
            and record.get("user_id") == user_id
            and record.get("profile_id") == profile_id
            and record.get("client_type") == client_type
        ):
            matches.append((token, record))
    return matches


def _public_url(token: str) -> str:
    import os

    scheme = os.getenv("OCI_MON_MCP_PUBLIC_SCHEME", "http")
    server_host = os.getenv("OCI_MON_MCP_PUBLIC_HOST", os.getenv("OCI_MON_MCP_HOST", "127.0.0.1"))
    port = os.getenv("OCI_MON_MCP_PUBLIC_PORT", os.getenv("OCI_MON_MCP_PORT", "8000"))
    path = os.getenv("OCI_MON_MCP_STREAMABLE_HTTP_PATH", "/mcp")
    return f"{scheme}://{server_host}:{port}{path}?u={token}"


def cmd_add(factory: RepositoryFactory, args: argparse.Namespace) -> int:
    user_id = _normalize_user_id(args.user_id)
    client_type = _normalize_client(args.client)
    profile_id = _profile_id_for(user_id, client_type)
    registry = factory.load_registry()
    active = [record for _, record in _find_records(registry, user_id=user_id, client_type=client_type) if record.get("status") == "active"]
    if active:
        raise SystemExit(
            f"Active token already exists for user {user_id} and client {client_type}. Use rotate instead."
        )
    token = secrets.token_urlsafe(16)
    registry[token] = {
        "user_id": user_id,
        "profile_id": profile_id,
        "client_type": client_type,
        "status": "active",
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
    }
    factory.save_registry(registry)
    factory.get_user_repo(profile_id, user_id=user_id).get_profile()
    print(f"user_id={user_id}")
    print(f"profile_id={profile_id}")
    print(f"client_type={client_type}")
    print(f"token={token}")
    print(f"mcp_url={_public_url(token)}")
    return 0


def cmd_remove(factory: RepositoryFactory, args: argparse.Namespace) -> int:
    user_id = _normalize_user_id(args.user_id)
    client_type = _normalize_client(args.client)
    registry = factory.load_registry()
    matches = _find_records(registry, user_id=user_id, client_type=client_type)
    if not matches:
        raise SystemExit(f"No registry entries found for user {user_id} and client {client_type}.")
    changed = False
    now = utc_now_iso()
    for _, record in matches:
        if record.get("status") != "inactive":
            record["status"] = "inactive"
            record["updated_at"] = now
            changed = True
    if changed:
        factory.save_registry(registry)
    print(f"Revoked {len(matches)} token(s) for user {user_id} and client {client_type}.")
    return 0


def cmd_rotate(factory: RepositoryFactory, args: argparse.Namespace) -> int:
    user_id = _normalize_user_id(args.user_id)
    client_type = _normalize_client(args.client)
    profile_id = _profile_id_for(user_id, client_type)
    registry = factory.load_registry()
    matches = _find_records(registry, user_id=user_id, client_type=client_type)
    if not matches:
        raise SystemExit(f"No registry entries found for user {user_id} and client {client_type}.")
    now = utc_now_iso()
    for _, record in matches:
        if record.get("status") == "active":
            record["status"] = "inactive"
            record["updated_at"] = now
    token = secrets.token_urlsafe(16)
    registry[token] = {
        "user_id": user_id,
        "profile_id": profile_id,
        "client_type": client_type,
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    factory.save_registry(registry)
    factory.get_user_repo(profile_id, user_id=user_id).ensure_user_id(user_id)
    print(f"user_id={user_id}")
    print(f"profile_id={profile_id}")
    print(f"client_type={client_type}")
    print(f"token={token}")
    print(f"mcp_url={_public_url(token)}")
    return 0


def cmd_list(factory: RepositoryFactory, _args: argparse.Namespace) -> int:
    registry = factory.load_registry()
    if not registry:
        print("<empty>")
        return 0
    print("token\tstatus\tuser_id\tclient_type\tprofile_id")
    for token, record in sorted(registry.items(), key=lambda item: (item[1].get("user_id", ""), item[1].get("client_type", ""), item[0])):
        print(
            "\t".join(
                [
                    token,
                    str(record.get("status", "")),
                    str(record.get("user_id", "")),
                    str(record.get("client_type", "")),
                    str(record.get("profile_id", "")),
                ]
            )
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None, help="Optional data directory override.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Add a new active token for a user/client pair.")
    add_parser.add_argument("user_id")
    add_parser.add_argument("--client", required=True)
    add_parser.set_defaults(func=cmd_add)

    remove_parser = subparsers.add_parser("remove", help="Deactivate all tokens for a user/client pair.")
    remove_parser.add_argument("user_id")
    remove_parser.add_argument("--client", required=True)
    remove_parser.set_defaults(func=cmd_remove)

    rotate_parser = subparsers.add_parser("rotate", help="Rotate the token for a user/client pair.")
    rotate_parser.add_argument("user_id")
    rotate_parser.add_argument("--client", required=True)
    rotate_parser.set_defaults(func=cmd_rotate)

    list_parser = subparsers.add_parser("list", help="List registry records.")
    list_parser.set_defaults(func=cmd_list)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    data_dir = Path(args.data_dir).resolve() if args.data_dir else None
    factory = RepositoryFactory(data_dir=data_dir)
    return args.func(factory, args)


if __name__ == "__main__":
    raise SystemExit(main())
