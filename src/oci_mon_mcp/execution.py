"""Execution adapter interfaces for Monitoring queries."""

from __future__ import annotations

import importlib
from typing import Protocol

from .models import ExecutionResult, QueryExecutionRequest


class MonitoringExecutionAdapter(Protocol):
    """Adapter contract for query execution."""

    def execute(self, request: QueryExecutionRequest) -> ExecutionResult:
        """Run a Monitoring query and normalize the result."""


class UnsupportedExecutionAdapter:
    """Placeholder adapter until OCI execution is wired."""

    def execute(self, request: QueryExecutionRequest) -> ExecutionResult:
        raise RuntimeError(
            "OCI Monitoring execution is not wired yet. "
            "The request was interpreted and query text was generated successfully."
        )


def build_default_execution_adapter() -> MonitoringExecutionAdapter:
    """Select the live SDK adapter when OCI is installed."""
    try:
        importlib.import_module("oci")
    except ImportError:
        return UnsupportedExecutionAdapter()

    from .oci_sdk_adapter import OciSdkExecutionAdapter

    return OciSdkExecutionAdapter()
