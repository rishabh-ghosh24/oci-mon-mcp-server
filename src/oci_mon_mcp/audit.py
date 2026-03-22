"""Structured JSONL audit logger with rotation and archival."""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Sanitisation patterns – defined inline, no phantom imports
# ---------------------------------------------------------------------------

_OCID_RE = re.compile(r"ocid1\.[a-z0-9]+\.[a-z0-9]*\.[a-z0-9-]*\.[a-z0-9]+")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_URL_TOKEN_RE = re.compile(r"(token|key|secret|password|auth)=[^\s&]+", re.IGNORECASE)


def _sanitize(text: str) -> str:
    """Mask OCIDs, IP addresses, and URL tokens in *text*."""
    text = _OCID_RE.sub("<OCID>", text)
    text = _IP_RE.sub("<IP>", text)
    text = _URL_TOKEN_RE.sub(r"\1=<REDACTED>", text)
    return text


# ---------------------------------------------------------------------------
# Audit entry dataclass
# ---------------------------------------------------------------------------


@dataclass
class AuditEntry:
    profile_id: str
    user_id: str
    query_text: str
    resolved_intent: str
    namespace: str
    metric_key: str | None = None
    compartment: str | None = None
    scope: str | None = None
    mql_queries: list[str] = field(default_factory=list)
    result_row_count: int | None = None
    artifact_generated: bool = False
    error: str | None = None
    timing: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Gzip rotation helpers
# ---------------------------------------------------------------------------


class _GzipNamer:
    """Append ``.gz`` to the rotated file name."""

    def __call__(self, default_name: str) -> str:
        return default_name + ".gz"


class _GzipRotator:
    """Compress the rotated log file with gzip and move to archive dir."""

    def __init__(self, archive_dir: Path) -> None:
        self.archive_dir = archive_dir

    def __call__(self, source: str, dest: str) -> None:
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        dest_path = self.archive_dir / Path(dest).name
        with open(source, "rb") as f_in:
            with gzip.open(str(dest_path), "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        os.remove(source)


# ---------------------------------------------------------------------------
# Audit logger
# ---------------------------------------------------------------------------

_DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50 MB


class AuditLogger:
    """Writes sanitised JSONL audit records with automatic rotation."""

    def __init__(
        self,
        log_path: Path | str,
        archive_dir: Path | str | None = None,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        backup_count: int = 5,
        retention_days: int = 90,
    ) -> None:
        self.log_path = Path(log_path)
        self.archive_dir = Path(archive_dir) if archive_dir else self.log_path.parent / "archive"
        self.retention_days = retention_days

        # Ensure parent directories exist
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        # Set up rotating file handler
        handler = RotatingFileHandler(
            str(self.log_path),
            maxBytes=max_bytes,
            backupCount=backup_count,
        )
        handler.namer = _GzipNamer()
        handler.rotator = _GzipRotator(self.archive_dir)

        self._logger = logging.getLogger(f"audit.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        # Avoid duplicate handlers on repeated instantiation
        self._logger.handlers.clear()
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log(self, entry: AuditEntry) -> None:
        """Serialise *entry* as a sanitised JSONL line."""
        record = asdict(entry)

        # Sanitise sensitive fields
        for key in ("query_text", "error"):
            if record.get(key):
                record[key] = _sanitize(record[key])

        if record.get("mql_queries"):
            record["mql_queries"] = [_sanitize(q) for q in record["mql_queries"]]

        if record.get("compartment"):
            record["compartment"] = _sanitize(record["compartment"])

        record["timestamp"] = datetime.now(timezone.utc).isoformat()

        self._logger.info(json.dumps(record, separators=(",", ":")))

    def cleanup_archives(self) -> int:
        """Remove gzipped archive files older than *retention_days*.

        Returns the number of files removed.
        """
        if not self.archive_dir.exists():
            return 0

        cutoff = time.time() - (self.retention_days * 86400)
        removed = 0

        for path in self.archive_dir.iterdir():
            if path.suffix == ".gz" and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1

        return removed
