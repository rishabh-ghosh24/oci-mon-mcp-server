# Namespace-Agnostic Metric Engine + Audit Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hardcoded `oci_computeagent` metric handling with a registry-driven engine that supports any OCI metric namespace, and add structured JSONL audit logging with per-request timing breakdowns.

**Architecture:** A static YAML metric registry maps OCI namespaces to their metrics, resource types, and display metadata. The assistant's query parser resolves metric references against this registry instead of a hardcoded dict. A runtime fallback calls `ListMetrics` for unknown namespaces. A separate audit logging module wraps each MCP tool call to capture identity, query text, OCI API call details, timing breakdowns, and results — written as JSONL to `data/logs/audit.log` with rotation and archival.

**Tech Stack:** Python 3.11, PyYAML (for registry), `logging.handlers.RotatingFileHandler` (for log rotation), `gzip` + `shutil` (for archival), existing OCI SDK, existing FastMCP server.

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `data/metric_registry.yaml` | Static registry of OCI namespaces, metrics, resource types, display metadata |
| `src/oci_mon_mcp/metric_registry.py` | Load, validate, and query the metric registry; runtime fallback via `ListMetrics` |
| `src/oci_mon_mcp/audit.py` | JSONL audit logger with rotation, archival, and sanitization |
| `tests/test_metric_registry.py` | Tests for registry loading, lookup, and fallback |
| `tests/test_audit.py` | Tests for audit log writing, rotation, archival, and sanitization |

### Modified Files

| File | What Changes |
|------|-------------|
| `src/oci_mon_mcp/assistant.py` | Replace `METRIC_CONFIGS` dict with registry lookups; update `_extract_metric()`, `_build_parsed_query()` |
| `src/oci_mon_mcp/oci_sdk_adapter.py` | Remove hardcoded `"CpuUtilization"` / `"MemoryUtilization"` in `_normalize_results()` (lines 410-411); use metric names from parsed query |
| `src/oci_mon_mcp/server.py` | Initialize audit logger; wrap tool handlers with audit middleware |
| `src/oci_mon_mcp/oci_support.py` | Add `MonitoringClient.list_metrics()` wrapper for runtime fallback |
| `.gitignore` | Add `data/logs/` |
| `tests/test_assistant.py` | Update tests to work with registry-based metric resolution |

---

## Task 1: Create the Metric Registry YAML

**Files:**
- Modify: `pyproject.toml` (add `pyyaml>=6.0` dependency)
- Create: `data/metric_registry.yaml`
- Create: `src/oci_mon_mcp/metric_registry.py`
- Create: `tests/test_metric_registry.py`

- [ ] **Step 0: Add PyYAML dependency**

Add `"pyyaml>=6.0"` to the dependencies list in `pyproject.toml`, then install:

```bash
pip install pyyaml
```

- [ ] **Step 1: Write failing test — registry loads and resolves a known metric**

```python
# tests/test_metric_registry.py
import unittest
from oci_mon_mcp.metric_registry import MetricRegistry

class MetricRegistryTests(unittest.TestCase):
    def test_load_registry_and_resolve_cpu(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        entry = registry.resolve("cpu")
        self.assertEqual(entry.namespace, "oci_computeagent")
        self.assertEqual(entry.metric_names, ("CpuUtilization",))
        self.assertEqual(entry.label, "CPU utilization")
        self.assertEqual(entry.y_axis, "cpu_utilization_percent")

    def test_resolve_unknown_key_returns_none(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        entry = registry.resolve("nonexistent_metric")
        self.assertIsNone(entry)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_metric_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'oci_mon_mcp.metric_registry'`

- [ ] **Step 3: Create the registry YAML**

```yaml
# data/metric_registry.yaml
# OCI Metric Namespace Registry
# Each namespace defines its metrics, resource type, and display metadata.
# metric_key: unique logical key used in query parsing and templates
# aliases: natural language terms that map to this metric_key

namespaces:
  oci_computeagent:
    display_name: "Compute Agent"
    resource_type: Instance
    sdk_client: ComputeClient
    metrics:
      - metric_key: cpu
        label: "CPU utilization"
        metric_names: [CpuUtilization]
        y_axis: cpu_utilization_percent
        aliases: [cpu, processor, compute utilization]
        unit: percent
      - metric_key: memory
        label: "Memory utilization"
        metric_names: [MemoryUtilization]
        y_axis: memory_utilization_percent
        aliases: [memory, mem, ram]
        unit: percent
      - metric_key: cpu_memory
        label: "CPU and memory utilization"
        metric_names: [CpuUtilization, MemoryUtilization]
        y_axis: utilization_percent
        aliases: []
        unit: percent
      - metric_key: disk_io_throughput
        label: "Disk I/O throughput"
        metric_names: [DiskBytesRead, DiskBytesWritten]
        y_axis: disk_io_bytes
        aliases: [disk throughput, io throughput, disk bytes]
        unit: bytes_per_second
      - metric_key: disk_io_iops
        label: "Disk I/O IOPS"
        metric_names: [DiskIopsRead, DiskIopsWritten]
        y_axis: disk_io_iops
        aliases: [disk iops, io iops, iops]
        unit: count_per_second

  oci_vcn:
    display_name: "Virtual Cloud Network"
    resource_type: Vnic
    sdk_client: VirtualNetworkClient
    metrics:
      - metric_key: vcn_bytes_in
        label: "Network bytes in"
        metric_names: [VnicFromNetworkBytes]
        y_axis: network_bytes
        aliases: [network in, bytes in, ingress bytes, inbound traffic]
        unit: bytes
      - metric_key: vcn_bytes_out
        label: "Network bytes out"
        metric_names: [VnicToNetworkBytes]
        y_axis: network_bytes
        aliases: [network out, bytes out, egress bytes, outbound traffic]
        unit: bytes
      - metric_key: vcn_packets_in
        label: "Network packets in"
        metric_names: [VnicFromNetworkPackets]
        y_axis: network_packets
        aliases: [packets in, ingress packets]
        unit: count
      - metric_key: vcn_packets_out
        label: "Network packets out"
        metric_names: [VnicToNetworkPackets]
        y_axis: network_packets
        aliases: [packets out, egress packets]
        unit: count

  oci_blockstore:
    display_name: "Block Storage"
    resource_type: Volume
    sdk_client: BlockstorageClient
    metrics:
      - metric_key: volume_read_throughput
        label: "Volume read throughput"
        metric_names: [VolumeReadThroughput]
        y_axis: volume_throughput_bytes
        aliases: [volume read, block read, disk read throughput]
        unit: bytes_per_second
      - metric_key: volume_write_throughput
        label: "Volume write throughput"
        metric_names: [VolumeWriteThroughput]
        y_axis: volume_throughput_bytes
        aliases: [volume write, block write, disk write throughput]
        unit: bytes_per_second
      - metric_key: volume_read_ops
        label: "Volume read operations"
        metric_names: [VolumeReadOps]
        y_axis: volume_ops
        aliases: [volume read ops, block read ops]
        unit: count_per_second
      - metric_key: volume_write_ops
        label: "Volume write operations"
        metric_names: [VolumeWriteOps]
        y_axis: volume_ops
        aliases: [volume write ops, block write ops]
        unit: count_per_second

  oci_lbaas:
    display_name: "Load Balancer"
    resource_type: LoadBalancer
    sdk_client: LoadBalancerClient
    metrics:
      - metric_key: lb_http_requests
        label: "HTTP requests"
        metric_names: [HttpRequests]
        y_axis: request_count
        aliases: [http requests, load balancer requests, lb requests]
        unit: count
      - metric_key: lb_active_connections
        label: "Active connections"
        metric_names: [ActiveConnections]
        y_axis: connection_count
        aliases: [active connections, lb connections]
        unit: count
      - metric_key: lb_bandwidth
        label: "Load balancer bandwidth"
        metric_names: [BytesReceived, BytesSent]
        y_axis: bandwidth_bytes
        aliases: [lb bandwidth, load balancer bandwidth]
        unit: bytes

  oci_database:
    display_name: "Database System"
    resource_type: DbSystem
    sdk_client: DatabaseClient
    metrics:
      - metric_key: db_cpu
        label: "Database CPU utilization"
        metric_names: [CpuUtilization]
        y_axis: cpu_utilization_percent
        aliases: [db cpu, database cpu, database processor]
        unit: percent
      - metric_key: db_storage
        label: "Database storage utilization"
        metric_names: [StorageUtilization]
        y_axis: storage_utilization_percent
        aliases: [db storage, database storage, database disk]
        unit: percent

  oci_autonomous_database:
    display_name: "Autonomous Database"
    resource_type: AutonomousDatabase
    sdk_client: DatabaseClient
    metrics:
      - metric_key: adb_cpu
        label: "Autonomous DB CPU utilization"
        metric_names: [CpuUtilization]
        y_axis: cpu_utilization_percent
        aliases: [adb cpu, autonomous cpu, atp cpu, adw cpu]
        unit: percent
      - metric_key: adb_storage
        label: "Autonomous DB storage utilization"
        metric_names: [StorageUtilization]
        y_axis: storage_utilization_percent
        aliases: [adb storage, autonomous storage, atp storage, adw storage]
        unit: percent
      - metric_key: adb_sessions
        label: "Autonomous DB sessions"
        metric_names: [Sessions]
        y_axis: session_count
        aliases: [adb sessions, autonomous sessions, database sessions]
        unit: count

  oci_objectstorage:
    display_name: "Object Storage"
    resource_type: Bucket
    sdk_client: ObjectStorageClient
    metrics:
      - metric_key: bucket_size
        label: "Bucket size"
        metric_names: [StoredBytes]
        y_axis: storage_bytes
        aliases: [bucket size, object storage size, stored bytes]
        unit: bytes
      - metric_key: bucket_requests
        label: "Bucket request count"
        metric_names: [TotalRequestCount]
        y_axis: request_count
        aliases: [bucket requests, object storage requests]
        unit: count

  oci_oke:
    display_name: "Container Engine (OKE)"
    resource_type: Cluster
    sdk_client: ContainerEngineClient
    metrics:
      - metric_key: oke_node_cpu
        label: "OKE node CPU"
        metric_names: [CpuUtilization]
        y_axis: cpu_utilization_percent
        aliases: [oke cpu, kubernetes cpu, k8s cpu, node cpu]
        unit: percent
      - metric_key: oke_node_memory
        label: "OKE node memory"
        metric_names: [MemoryUtilization]
        y_axis: memory_utilization_percent
        aliases: [oke memory, kubernetes memory, k8s memory, node memory]
        unit: percent

  oci_faas:
    display_name: "Functions"
    resource_type: Function
    sdk_client: FunctionsManagementClient
    metrics:
      - metric_key: fn_invocations
        label: "Function invocations"
        metric_names: [FunctionInvocationCount]
        y_axis: invocation_count
        aliases: [function invocations, fn invocations, function calls]
        unit: count
      - metric_key: fn_duration
        label: "Function duration"
        metric_names: [FunctionExecutionDuration]
        y_axis: duration_ms
        aliases: [function duration, fn duration, function latency]
        unit: milliseconds
      - metric_key: fn_errors
        label: "Function errors"
        metric_names: [FunctionErrorCount]
        y_axis: error_count
        aliases: [function errors, fn errors]
        unit: count
```

- [ ] **Step 4: Write the MetricRegistry module**

```python
# src/oci_mon_mcp/metric_registry.py
"""Registry-driven metric resolution for OCI namespaces."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MetricEntry:
    """A resolved metric configuration."""

    metric_key: str
    label: str
    namespace: str
    metric_names: tuple[str, ...]
    y_axis: str
    aliases: tuple[str, ...] = ()
    unit: str = ""
    resource_type: str = ""
    sdk_client: str = ""


@dataclass(frozen=True, slots=True)
class NamespaceInfo:
    """Metadata about a namespace."""

    namespace: str
    display_name: str
    resource_type: str
    sdk_client: str
    metrics: list[MetricEntry]


class MetricRegistry:
    """Load and query the OCI metric registry."""

    def __init__(self, namespaces: dict[str, NamespaceInfo]) -> None:
        self._namespaces = namespaces
        # Build lookup indexes
        self._by_key: dict[str, MetricEntry] = {}
        self._by_alias: dict[str, MetricEntry] = {}
        for ns_info in namespaces.values():
            for entry in ns_info.metrics:
                self._by_key[entry.metric_key] = entry
                for alias in entry.aliases:
                    self._by_alias[alias.lower()] = entry

    @classmethod
    def from_yaml(cls, path: str | Path) -> MetricRegistry:
        """Load registry from a YAML file."""
        path = Path(path)
        with open(path) as f:
            data = yaml.safe_load(f)

        namespaces: dict[str, NamespaceInfo] = {}
        for ns_name, ns_data in data.get("namespaces", {}).items():
            entries = []
            for m in ns_data.get("metrics", []):
                entries.append(
                    MetricEntry(
                        metric_key=m["metric_key"],
                        label=m["label"],
                        namespace=ns_name,
                        metric_names=tuple(m["metric_names"]),
                        y_axis=m["y_axis"],
                        aliases=tuple(m.get("aliases", [])),
                        unit=m.get("unit", ""),
                        resource_type=ns_data.get("resource_type", ""),
                        sdk_client=ns_data.get("sdk_client", ""),
                    )
                )
            namespaces[ns_name] = NamespaceInfo(
                namespace=ns_name,
                display_name=ns_data.get("display_name", ns_name),
                resource_type=ns_data.get("resource_type", ""),
                sdk_client=ns_data.get("sdk_client", ""),
                metrics=entries,
            )
        return cls(namespaces)

    def resolve(self, metric_key: str) -> MetricEntry | None:
        """Resolve a metric_key to its entry. Returns None if unknown."""
        return self._by_key.get(metric_key)

    def resolve_by_alias(self, text: str) -> MetricEntry | None:
        """Find the best matching metric entry for natural language text."""
        text_lower = text.lower()
        # Check direct alias match first
        for alias, entry in sorted(
            self._by_alias.items(), key=lambda kv: len(kv[0]), reverse=True
        ):
            if alias in text_lower:
                return entry
        return None

    def list_namespaces(self) -> list[str]:
        """Return all known namespace names."""
        return list(self._namespaces.keys())

    def get_namespace_info(self, namespace: str) -> NamespaceInfo | None:
        """Get metadata for a namespace."""
        return self._namespaces.get(namespace)

    def list_metrics_for_namespace(self, namespace: str) -> list[MetricEntry]:
        """List all metrics registered for a namespace."""
        ns_info = self._namespaces.get(namespace)
        return list(ns_info.metrics) if ns_info else []

    @property
    def all_metric_keys(self) -> list[str]:
        """Return all known metric keys."""
        return list(self._by_key.keys())
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_metric_registry.py -v`
Expected: PASS

- [ ] **Step 6: Add more registry tests**

```python
# Append to tests/test_metric_registry.py

    def test_resolve_by_alias_finds_memory(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        entry = registry.resolve_by_alias("show me the ram usage")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.metric_key, "memory")
        self.assertEqual(entry.namespace, "oci_computeagent")

    def test_resolve_by_alias_finds_network(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        entry = registry.resolve_by_alias("show network ingress bytes")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.metric_key, "vcn_bytes_in")
        self.assertEqual(entry.namespace, "oci_vcn")

    def test_list_namespaces_returns_all(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        namespaces = registry.list_namespaces()
        self.assertIn("oci_computeagent", namespaces)
        self.assertIn("oci_vcn", namespaces)
        self.assertIn("oci_database", namespaces)
        self.assertGreaterEqual(len(namespaces), 8)

    def test_namespace_info_includes_resource_type(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        info = registry.get_namespace_info("oci_database")
        self.assertIsNotNone(info)
        self.assertEqual(info.resource_type, "DbSystem")
        self.assertEqual(info.sdk_client, "DatabaseClient")

    def test_alias_disambiguation_oke_cpu_vs_cpu(self):
        """Verify 'oke cpu' resolves to oke_node_cpu, not oci_computeagent cpu."""
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        entry = registry.resolve_by_alias("show oke cpu utilization")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.metric_key, "oke_node_cpu")
        self.assertEqual(entry.namespace, "oci_oke")

    def test_alias_disambiguation_db_cpu_vs_cpu(self):
        """Verify 'db cpu' resolves to db_cpu, not oci_computeagent cpu."""
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        entry = registry.resolve_by_alias("database cpu utilization")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.metric_key, "db_cpu")
        self.assertEqual(entry.namespace, "oci_database")

    def test_all_entries_have_required_fields(self):
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        for key in registry.all_metric_keys:
            entry = registry.resolve(key)
            self.assertIsNotNone(entry, f"Missing entry for key: {key}")
            self.assertTrue(entry.label, f"Empty label for key: {key}")
            self.assertTrue(entry.namespace, f"Empty namespace for key: {key}")
            self.assertTrue(entry.metric_names, f"Empty metric_names for key: {key}")
            self.assertTrue(entry.y_axis, f"Empty y_axis for key: {key}")
```

- [ ] **Step 7: Run all tests to verify**

Run: `python -m pytest tests/test_metric_registry.py -v`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
git add data/metric_registry.yaml src/oci_mon_mcp/metric_registry.py tests/test_metric_registry.py
git commit -m "feat: add static metric registry with 9 OCI namespaces"
```

---

## Task 2: Wire Registry Into Assistant — Replace METRIC_CONFIGS

**Files:**
- Modify: `src/oci_mon_mcp/assistant.py` (lines 42-73: replace `METRIC_CONFIGS`; lines 1524-1542: update `_extract_metric()`)
- Modify: `src/oci_mon_mcp/server.py` (initialize registry and pass to assistant)
- Modify: `tests/test_assistant.py` (update to work with registry)

- [ ] **Step 1: Write failing test — assistant resolves VCN metric from registry**

```python
# Add to tests/test_assistant.py

    def test_vcn_network_query_resolves_from_registry(self):
        """Verify the assistant can handle a VCN namespace query via registry."""
        response = self.service.handle_query(
            query="show me network bytes in for all instances in the last 1 hour",
            profile_id=self.profile_id,
        )
        self.assertIn(response.status, ("success", "needs_clarification"))
        # If success, namespace should be oci_vcn
        if response.status == "success":
            self.assertEqual(response.details.namespace, "oci_vcn")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_assistant.py::MonitoringAssistantServiceTests::test_vcn_network_query_resolves_from_registry -v`
Expected: FAIL — VCN metric not recognized by current `_extract_metric()`

- [ ] **Step 3: Update assistant.py — replace METRIC_CONFIGS with registry**

In `assistant.py`:

1. Remove the `METRIC_CONFIGS` dict (lines 42-73)
2. Accept `MetricRegistry` in `MonitoringAssistantService.__init__()`
3. Update `_extract_metric()` to use `registry.resolve_by_alias()` with fallback to existing keyword matching for backward compatibility
4. Update `_build_parsed_query()` to use `registry.resolve(metric_key)` instead of `METRIC_CONFIGS[metric_key]`

Key changes:

```python
# In __init__:
def __init__(self, ..., metric_registry: MetricRegistry | None = None):
    ...
    default_path = Path(__file__).parent.parent.parent / "data" / "metric_registry.yaml"
    registry_path = os.getenv("OCI_MON_MCP_METRIC_REGISTRY_PATH", str(default_path))
    self._registry = metric_registry or MetricRegistry.from_yaml(registry_path)

# Replace _extract_metric():
def _extract_metric(self, text: str) -> str | None:
    normalized = text.lower()
    # Preserve existing keyword shortcuts for backward compat
    has_cpu = "cpu" in normalized
    has_memory = (
        "memory" in normalized or "mem " in f"{normalized} " or normalized.endswith("mem")
    )
    if has_cpu and has_memory:
        return "cpu_memory"
    if has_cpu:
        return "cpu"
    if has_memory:
        return "memory"
    if "storage" in normalized:
        # Check registry for specific storage metrics first (db_storage, adb_storage, etc.)
        entry = self._registry.resolve_by_alias(normalized)
        if entry and "storage" in entry.metric_key:
            return entry.metric_key
        return "storage"  # Falls through to _storage_not_available_response
    if "throughput" in normalized and "io" in normalized:
        return "disk_io_throughput"
    if "iops" in normalized and "io" in normalized:
        return "disk_io_iops"
    # Fall through to registry alias matching
    entry = self._registry.resolve_by_alias(normalized)
    if entry:
        return entry.metric_key
    return None

# Replace METRIC_CONFIGS lookup in _build_parsed_query():
def _build_parsed_query(self, ..., metric_key: str, ...):
    entry = self._registry.resolve(metric_key)
    if entry is None:
        raise ValueError(f"Unknown metric_key: {metric_key}")
    metric_names = list(entry.metric_names)
    # ... rest unchanged, using entry.label, entry.namespace, entry.y_axis ...
```

- [ ] **Step 4: Update server.py — initialize registry**

```python
# In create_mcp_server() or server initialization:
from oci_mon_mcp.metric_registry import MetricRegistry

registry = MetricRegistry.from_yaml(
    Path(__file__).parent.parent.parent / "data" / "metric_registry.yaml"
)
# Pass to assistant service
```

- [ ] **Step 5: Run all tests to verify nothing broke**

Run: `python -m pytest tests/ -v`
Expected: All existing tests PASS + new VCN test PASS

- [ ] **Step 6: Commit**

```bash
git add src/oci_mon_mcp/assistant.py src/oci_mon_mcp/server.py tests/test_assistant.py
git commit -m "feat: wire metric registry into assistant, replace METRIC_CONFIGS"
```

---

## Task 3: Remove Hardcoded Metric Names in Normalization

**Files:**
- Modify: `src/oci_mon_mcp/oci_sdk_adapter.py` (lines 409-440: generalize `cpu_memory` handling)
- Modify: `tests/test_oci_sdk_adapter.py` (add test for multi-metric normalization)

- [ ] **Step 1: Write failing test — multi-metric normalization uses metric names from query**

```python
# Add to tests/test_oci_sdk_adapter.py

    def test_multi_metric_normalization_uses_parsed_metric_names(self):
        """Verify normalization doesn't hardcode CpuUtilization/MemoryUtilization."""
        from oci_mon_mcp.models import ParsedQuery, QueryExecutionRequest

        parsed = ParsedQuery(
            intent="threshold",
            metric_key="cpu_memory",
            metric_label="CPU and memory utilization",
            namespace="oci_computeagent",
            metric_names=["CpuUtilization", "MemoryUtilization"],
            time_range="1h",
            interval="5m",
            aggregation="max",
            source_query="show cpu and memory",
        )
        request = QueryExecutionRequest(parsed_query=parsed)
        streams = {
            "ocid1.instance.test": {
                "instance_name": "test-instance",
                "instance_ocid": "ocid1.instance.test",
                "compartment": "test-compartment",
                "lifecycle_state": "RUNNING",
                "time_created": "2026-01-01",
                "points": {
                    "CpuUtilization": [("2024-04-01T12:00:00+00:00", 85.0), ("2024-04-01T13:00:00+00:00", 90.0)],
                    "MemoryUtilization": [("2024-04-01T12:00:00+00:00", 60.0), ("2024-04-01T13:00:00+00:00", 65.0)],
                },
            }
        }
        adapter = self._make_adapter()
        rows, chart_series = adapter._normalize_results(request, streams)
        self.assertEqual(len(rows), 1)
        self.assertIn("cpu_mean_value", rows[0])
        self.assertIn("memory_mean_value", rows[0])
```

- [ ] **Step 2: Run test to verify it passes with current code (baseline)**

Run: `python -m pytest tests/test_oci_sdk_adapter.py::OciSdkExecutionAdapterTests::test_multi_metric_normalization_uses_parsed_metric_names -v`
Expected: PASS (current code already handles this case, but with hardcoded names)

- [ ] **Step 3: Refactor _normalize_results to use metric_names from parsed query**

In `oci_sdk_adapter.py` lines 409-411, replace hardcoded metric names with dynamic lookup from parsed query. **Keep `cpu_memory` as a recognized special case for backward compatibility** (existing templates and result formatting depend on the `cpu_mean_value` / `memory_mean_value` row keys). Add a generic dual-metric path for new namespaces:

```python
# Before:
if request.parsed_query.metric_key == "cpu_memory":
    cpu_points = stream["points"].get("CpuUtilization", [])
    memory_points = stream["points"].get("MemoryUtilization", [])

# After:
if request.parsed_query.metric_key == "cpu_memory":
    # Backward-compatible path: use metric_names from parsed query, keep row key names
    metric_names = request.parsed_query.metric_names
    cpu_points = stream["points"].get(metric_names[0], [])
    memory_points = stream["points"].get(metric_names[1], [])
elif len(request.parsed_query.metric_names) == 2:
    # Generic dual-metric path for new namespaces (e.g., BytesReceived + BytesSent)
    metric_names = request.parsed_query.metric_names
    m0_name = metric_names[0]
    m1_name = metric_names[1]
    m0_points = stream["points"].get(m0_name, [])
    m1_points = stream["points"].get(m1_name, [])
    if not m0_points or not m1_points:
        continue
    m0_stats = self._compute_metric_stats(m0_points)
    m1_stats = self._compute_metric_stats(m1_points)
    m0_agg = self._value_for_aggregation(m0_stats, aggregation)
    m1_agg = self._value_for_aggregation(m1_stats, aggregation)
    row = {
        "instance_name": stream["instance_name"],
        "instance_ocid": stream["instance_ocid"],
        "compartment": stream["compartment"],
        "lifecycle_state": stream["lifecycle_state"],
        "time_created": stream["time_created"],
        "metric": request.parsed_query.metric_label,
        f"{m0_name.lower()}_mean_value": m0_stats["mean_value"],
        f"{m1_name.lower()}_mean_value": m1_stats["mean_value"],
        f"{m0_name.lower()}_max_value": m0_stats["max_value"],
        f"{m1_name.lower()}_max_value": m1_stats["max_value"],
        "aggregated_value": m0_agg + m1_agg,
    }
    rows.append(row)
    continue
```

- [ ] **Step 4: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_mon_mcp/oci_sdk_adapter.py tests/test_oci_sdk_adapter.py
git commit -m "refactor: remove hardcoded metric names in multi-metric normalization"
```

---

## Task 4: Runtime Fallback — ListMetrics for Unknown Namespaces

**Files:**
- Modify: `src/oci_mon_mcp/metric_registry.py` (add runtime discovery method)
- Modify: `src/oci_mon_mcp/oci_support.py` (add `list_metrics()` wrapper)
- Create: `tests/test_metric_registry.py` (add fallback tests)

- [ ] **Step 1: Write failing test — registry returns discovered metrics for unknown namespace**

```python
# Add to tests/test_metric_registry.py

    def test_runtime_fallback_discovers_unknown_namespace(self):
        """When a namespace isn't in the static registry, runtime discovery should work."""
        registry = MetricRegistry.from_yaml("data/metric_registry.yaml")
        # Simulate discovery result
        discovered = registry.register_discovered_namespace(
            namespace="oci_custom_namespace",
            display_name="Custom Service",
            metrics=[
                {"metric_name": "CustomMetric1", "unit": "count"},
                {"metric_name": "CustomMetric2", "unit": "percent"},
            ],
        )
        self.assertIsNotNone(discovered)
        self.assertEqual(discovered.namespace, "oci_custom_namespace")

        # Now resolve should work
        entry = registry.resolve("oci_custom_namespace__CustomMetric1")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.namespace, "oci_custom_namespace")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_metric_registry.py::MetricRegistryTests::test_runtime_fallback_discovers_unknown_namespace -v`
Expected: FAIL — `register_discovered_namespace` doesn't exist

- [ ] **Step 3: Implement runtime discovery registration**

Add to `MetricRegistry`:

```python
def register_discovered_namespace(
    self,
    namespace: str,
    display_name: str,
    metrics: list[dict[str, str]],
) -> NamespaceInfo:
    """Register metrics discovered at runtime via ListMetrics API."""
    entries = []
    for m in metrics:
        metric_name = m["metric_name"]
        key = f"{namespace}__{metric_name}"
        entry = MetricEntry(
            metric_key=key,
            label=f"{display_name} {metric_name}",
            namespace=namespace,
            metric_names=[metric_name],
            y_axis=metric_name.lower(),
            aliases=[metric_name.lower()],
            unit=m.get("unit", ""),
            resource_type="",
            sdk_client="",
        )
        entries.append(entry)
        self._by_key[key] = entry
        for alias in entry.aliases:
            self._by_alias[alias] = entry

    ns_info = NamespaceInfo(
        namespace=namespace,
        display_name=display_name,
        resource_type="",
        sdk_client="",
        metrics=entries,
    )
    self._namespaces[namespace] = ns_info
    return ns_info
```

- [ ] **Step 4: Add ListMetrics wrapper to oci_support.py**

```python
# Add to oci_support.py

def list_metrics_for_namespace(
    monitoring_client: Any,
    compartment_id: str,
    namespace: str,
) -> list[dict[str, str]]:
    """Call OCI Monitoring ListMetrics to discover available metrics."""
    details = monitoring_client.models.ListMetricsDetails(
        namespace=namespace,
    )
    response = monitoring_client.list_metrics(
        compartment_id=compartment_id,
        list_metrics_details=details,
    )
    seen = set()
    result = []
    for metric in response.data:
        if metric.name not in seen:
            seen.add(metric.name)
            result.append({
                "metric_name": metric.name,
                "unit": getattr(metric, "unit", ""),
            })
    return result
```

- [ ] **Step 5: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/oci_mon_mcp/metric_registry.py src/oci_mon_mcp/oci_support.py tests/test_metric_registry.py
git commit -m "feat: add runtime metric discovery fallback for unknown namespaces"
```

---

## Task 5: Structured Audit Logging Module

**Files:**
- Create: `src/oci_mon_mcp/audit.py`
- Create: `tests/test_audit.py`
- Modify: `.gitignore` (add `data/logs/`)

- [ ] **Step 1: Write failing test — audit logger writes JSONL entry**

```python
# tests/test_audit.py
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from oci_mon_mcp.audit import AuditLogger, AuditEntry


class AuditLoggerTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = Path(self.tmpdir) / "audit.log"
        self.archive_path = Path(self.tmpdir) / "archive"
        self.logger = AuditLogger(
            log_path=self.log_path,
            archive_dir=self.archive_path,
            max_bytes=1024,  # Small for testing rotation
            backup_count=2,
            retention_days=90,
        )

    def test_write_audit_entry(self):
        entry = AuditEntry(
            profile_id="pilot_alice_codex",
            user_id="alice",
            query_text="show me cpu utilization",
            resolved_intent="threshold",
            namespace="oci_computeagent",
        )
        self.logger.log(entry)
        self.assertTrue(self.log_path.exists())
        with open(self.log_path) as f:
            line = f.readline()
            record = json.loads(line)
        self.assertEqual(record["profile_id"], "pilot_alice_codex")
        self.assertEqual(record["query_text"], "show me cpu utilization")
        self.assertIn("timestamp", record)

    def test_timing_breakdown_included(self):
        entry = AuditEntry(
            profile_id="pilot_bob_claude",
            user_id="bob",
            query_text="top 5 by memory",
            resolved_intent="top_n",
            namespace="oci_computeagent",
            timing={
                "total_ms": 9200,
                "breakdown": {
                    "query_parsing_ms": 15,
                    "oci_api_calls": [
                        {"api": "SummarizeMetricsData", "duration_ms": 4800},
                    ],
                    "chart_generation_ms": 280,
                },
            },
        )
        self.logger.log(entry)
        with open(self.log_path) as f:
            record = json.loads(f.readline())
        self.assertEqual(record["timing"]["total_ms"], 9200)
        self.assertEqual(record["timing"]["breakdown"]["query_parsing_ms"], 15)

    def test_sensitive_data_masked(self):
        entry = AuditEntry(
            profile_id="pilot_alice_codex",
            user_id="alice",
            query_text="show cpu in ocid1.compartment.oc1..aaa",
            resolved_intent="threshold",
            namespace="oci_computeagent",
            mql_queries=["CpuUtilization[5m]{compartmentId = \"ocid1.compartment.oc1..aaa\"}"],
        )
        self.logger.log(entry)
        with open(self.log_path) as f:
            record = json.loads(f.readline())
        # OCIDs should be masked in the audit log
        self.assertNotIn("ocid1.compartment.oc1..aaa", record["query_text"])
        self.assertIn("<OCID>", record["query_text"])

    def test_cleanup_archives_removes_old_files(self):
        """Verify archive cleanup removes files older than retention_days."""
        self.archive_path.mkdir(parents=True, exist_ok=True)
        # Create a fake old archive (200 days old)
        old_file = self.archive_path / "audit.log.1.gz"
        old_file.write_bytes(b"old data")
        old_mtime = time.time() - (200 * 86400)
        os.utime(old_file, (old_mtime, old_mtime))
        # Create a recent archive (10 days old)
        recent_file = self.archive_path / "audit.log.2.gz"
        recent_file.write_bytes(b"recent data")
        recent_mtime = time.time() - (10 * 86400)
        os.utime(recent_file, (recent_mtime, recent_mtime))

        removed = self.logger.cleanup_archives()
        self.assertEqual(removed, 1)
        self.assertFalse(old_file.exists())
        self.assertTrue(recent_file.exists())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_audit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'oci_mon_mcp.audit'`

- [ ] **Step 3: Implement the audit logger**

```python
# src/oci_mon_mcp/audit.py
"""Structured JSONL audit logging with rotation and archival."""

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

logger = logging.getLogger(__name__)

_OCID_RE = re.compile(r"ocid1\.[a-z]+\.oc1\.\.[a-zA-Z0-9]+")
_IP_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
_TOKEN_RE = re.compile(r"\bu=[A-Za-z0-9_-]{16,}\b")


def _sanitize(text: str) -> str:
    """Mask OCIDs, IPs, and tokens in audit log entries."""
    text = _OCID_RE.sub("<OCID>", text)
    text = _IP_RE.sub("<IP>", text)
    text = _TOKEN_RE.sub("u=<TOKEN>", text)
    return text


@dataclass
class AuditEntry:
    """A single audit log entry."""

    profile_id: str = ""
    user_id: str = ""
    query_text: str = ""
    resolved_intent: str = ""
    namespace: str = ""
    metric_key: str = ""
    compartment: str = ""
    scope: str = ""  # "compartment" or "tenancy"
    mql_queries: list[str] = field(default_factory=list)
    result_row_count: int = 0
    artifact_generated: bool = False
    error: str = ""
    timing: dict[str, Any] = field(default_factory=dict)


class _GzipRotator:
    """Compress rotated log files with gzip."""

    def __call__(self, source: str, dest: str) -> None:
        with open(source, "rb") as f_in:
            with gzip.open(f"{dest}.gz", "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        os.remove(source)


class _GzipNamer:
    """Name rotated files with .gz extension."""

    def __call__(self, name: str) -> str:
        return name + ".gz"


class AuditLogger:
    """JSONL audit logger with rotation and archival."""

    def __init__(
        self,
        log_path: str | Path = "data/logs/audit.log",
        archive_dir: str | Path = "data/logs/archive",
        max_bytes: int = 50 * 1024 * 1024,  # 50MB
        backup_count: int = 5,
        retention_days: int = 90,
    ) -> None:
        self._log_path = Path(log_path)
        self._archive_dir = Path(archive_dir)
        self._retention_days = retention_days

        # Ensure directories exist
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._archive_dir.mkdir(parents=True, exist_ok=True)

        # Set up rotating file handler
        self._handler = RotatingFileHandler(
            filename=str(self._log_path),
            maxBytes=max_bytes,
            backupCount=backup_count,
        )
        self._handler.rotator = _GzipRotator()
        self._handler.namer = _GzipNamer()

        self._audit_logger = logging.getLogger("oci_mon_mcp.audit")
        self._audit_logger.addHandler(self._handler)
        self._audit_logger.setLevel(logging.INFO)
        self._audit_logger.propagate = False

    def log(self, entry: AuditEntry) -> None:
        """Write an audit entry as a single JSONL line."""
        record = asdict(entry)
        record["timestamp"] = datetime.now(timezone.utc).isoformat()

        # Sanitize sensitive data
        if record.get("query_text"):
            record["query_text"] = _sanitize(record["query_text"])
        if record.get("mql_queries"):
            record["mql_queries"] = [
                _sanitize(q) for q in record["mql_queries"]
            ]
        if record.get("error"):
            record["error"] = _sanitize(record["error"])

        self._audit_logger.info(json.dumps(record, default=str))

    def cleanup_archives(self) -> int:
        """Remove archived logs older than retention_days. Returns count removed."""
        if not self._archive_dir.exists():
            return 0
        cutoff = time.time() - (self._retention_days * 86400)
        removed = 0
        for f in self._archive_dir.glob("*.gz"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        return removed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_audit.py -v`
Expected: PASS

- [ ] **Step 5: Add .gitignore entry**

Append `data/logs/` to `.gitignore`.

- [ ] **Step 6: Commit**

```bash
git add src/oci_mon_mcp/audit.py tests/test_audit.py .gitignore
git commit -m "feat: add structured JSONL audit logger with rotation and archival"
```

---

## Task 6: Wire Audit Logging Into Server and Assistant

**Files:**
- Modify: `src/oci_mon_mcp/server.py` (initialize audit logger, wrap tool calls)
- Modify: `src/oci_mon_mcp/assistant.py` (capture timing breakdowns and emit audit entries)
- Modify: `tests/test_multi_user.py` (add audit integration test)

- [ ] **Step 1: Write failing test — audit entry created on monitoring_assistant call**

```python
# Add to tests/test_multi_user.py

    def test_handle_query_creates_audit_entry(self):
        """Verify that a handle_query call produces an audit log entry."""
        import tempfile
        from pathlib import Path
        from oci_mon_mcp.audit import AuditLogger

        tmpdir = tempfile.mkdtemp()
        log_path = Path(tmpdir) / "audit.log"
        audit_logger = AuditLogger(log_path=log_path)

        # Inject audit logger into service
        self.service._audit_logger = audit_logger

        self.service.handle_query(
            query="show me cpu utilization for all instances in the last 1 hour",
            profile_id=self.profile_id,
        )

        self.assertTrue(log_path.exists())
        with open(log_path) as f:
            lines = f.readlines()
        self.assertGreaterEqual(len(lines), 1)

        import json
        record = json.loads(lines[-1])
        self.assertIn("timestamp", record)
        self.assertIn("timing", record)
        self.assertIn("total_ms", record["timing"])

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_multi_user.py::ServerIdentityTests::test_monitoring_assistant_creates_audit_entry -v`
Expected: FAIL — `_audit_logger` attribute doesn't exist on the service

- [ ] **Step 3: Add timing capture and audit emission to assistant.py**

In `MonitoringAssistantService`:

```python
# In __init__, accept optional audit logger:
def __init__(self, ..., audit_logger: AuditLogger | None = None):
    ...
    self._audit_logger = audit_logger

# In handle_query(), wrap the main flow with timing:
def handle_query(self, query: str, ...) -> AssistantResponse:
    import time
    from oci_mon_mcp.identity import get_current_identity
    start_time = time.monotonic()
    timing = {"breakdown": {}}

    # ... existing logic ...

    # After execution completes:
    total_ms = int((time.monotonic() - start_time) * 1000)
    timing["total_ms"] = total_ms

    identity = get_current_identity()  # May be None if no token middleware
    if self._audit_logger:
        from oci_mon_mcp.audit import AuditEntry
        self._audit_logger.log(AuditEntry(
            profile_id=effective_profile,
            user_id=identity.user_id if identity else "",
            query_text=query,
            resolved_intent=response.get("interpretation", ""),
            namespace=response.get("details", {}).get("namespace", ""),
            metric_key=response.get("details", {}).get("metric", ""),
            compartment=response.get("details", {}).get("scope", {}).get("compartment", ""),
            scope="tenancy" if response.get("details", {}).get("scope", {}).get("include_subcompartments") else "compartment",
            mql_queries=[response.get("details", {}).get("query_text", "")],
            result_row_count=len(response.get("tables", [{}])[0].get("rows", [])) if response.get("tables") else 0,
            artifact_generated=bool(response.get("artifacts")),
            error=response.get("summary", "") if response.get("status") == "error" else "",
            timing=timing,
        ))

    return response
```

- [ ] **Step 4: Initialize audit logger in server.py**

```python
# In create_mcp_server() or server setup:
from oci_mon_mcp.audit import AuditLogger

audit_logger = AuditLogger(
    log_path=Path(state_dir) / ".." / "logs" / "audit.log",
    archive_dir=Path(state_dir) / ".." / "logs" / "archive",
)
# Pass to assistant service
```

- [ ] **Step 5: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/oci_mon_mcp/assistant.py src/oci_mon_mcp/server.py tests/test_multi_user.py
git commit -m "feat: wire audit logging into assistant with per-request timing"
```

---

## Task 7: Add OCI API Call Timing to Adapter

**Files:**
- Modify: `src/oci_mon_mcp/oci_sdk_adapter.py` (capture per-API-call timing in `_fetch_metric()`)
- Modify: `src/oci_mon_mcp/models.py` (add timing field to `ExecutionResult`)

- [ ] **Step 1: Write failing test — execution result includes API call timing**

```python
# Add to tests/test_oci_sdk_adapter.py

    def test_execution_result_includes_api_timing(self):
        """Verify that execution results include per-API-call timing."""
        # Use the existing fake adapter flow
        from oci_mon_mcp.models import ExecutionResult
        result = ExecutionResult(
            rows=[],
            chart_series=[],
            timing={
                "oci_api_calls": [
                    {"api": "SummarizeMetricsData", "namespace": "oci_computeagent", "duration_ms": 4800},
                ],
                "total_api_ms": 4800,
            },
        )
        self.assertIn("oci_api_calls", result.timing)
        self.assertEqual(result.timing["oci_api_calls"][0]["duration_ms"], 4800)
```

- [ ] **Step 2: Run test — should pass since it's testing the dataclass**

Run: `python -m pytest tests/test_oci_sdk_adapter.py -v`
Expected: PASS (or FAIL if `timing` field doesn't exist on `ExecutionResult`)

- [ ] **Step 3: Add timing field to ExecutionResult and capture in adapter**

In `models.py`, add `timing` to `ExecutionResult`:

```python
@dataclass(slots=True)
class ExecutionResult:
    rows: list[dict[str, Any]]
    chart_series: list[ChartSeries]
    timing: dict[str, Any] = field(default_factory=dict)
```

In `oci_sdk_adapter.py`, wrap `_fetch_metric()` with timing:

```python
def _fetch_metric(self, ...):
    import time
    start = time.monotonic()
    # ... existing fetch logic ...
    duration_ms = int((time.monotonic() - start) * 1000)
    return metric_name, metric_data_list, {
        "api": "SummarizeMetricsData",
        "namespace": request.parsed_query.namespace,
        "metric": metric_name,
        "duration_ms": duration_ms,
    }
```

**IMPORTANT:** Update the unpacking in `execute()` where `_fetch_metric` results are consumed. The return type changes from a 2-tuple to a 3-tuple:

```python
# Before:
for metric_name, metric_data_list in metric_results:

# After:
api_timings = []
for metric_name, metric_data_list, call_timing in metric_results:
    api_timings.append(call_timing)
    # ... rest of existing processing ...

# Include api_timings in ExecutionResult:
return ExecutionResult(
    rows=rows,
    chart_series=chart_series,
    timing={"oci_api_calls": api_timings, "total_api_ms": sum(t["duration_ms"] for t in api_timings)},
)
```

- [ ] **Step 4: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/oci_mon_mcp/models.py src/oci_mon_mcp/oci_sdk_adapter.py tests/test_oci_sdk_adapter.py
git commit -m "feat: capture per-API-call timing in execution results"
```

---

## Task 8: Update Seed Templates and Documentation

**Files:**
- Modify: `data/seed_query_templates.json` (add namespace field to existing templates)
- Modify: `docs/TROUBLESHOOTING.md` (add metric registry troubleshooting section)
- Modify: `docs/TECHNICAL_REQUIREMENTS.md` (update supported metrics section)

- [ ] **Step 1: Update seed templates with explicit namespace**

Add `"namespace": "oci_computeagent"` to each existing template entry.

- [ ] **Step 2: Add troubleshooting section for metric registry**

Add to `docs/TROUBLESHOOTING.md`:
- "My metric/namespace isn't recognized" — how to check the registry, how runtime fallback works
- "I added a namespace to the registry but queries still fail" — restart required, YAML validation

- [ ] **Step 3: Update technical requirements**

Update `docs/TECHNICAL_REQUIREMENTS.md` to reflect that metrics are now registry-driven, not hardcoded. List all supported namespaces.

- [ ] **Step 4: Commit**

```bash
git add data/seed_query_templates.json docs/TROUBLESHOOTING.md docs/TECHNICAL_REQUIREMENTS.md
git commit -m "docs: update templates and documentation for registry-driven metrics"
```

---

---

## Verification Checklist

Before marking this plan complete:

- [ ] All existing 29 tests still pass
- [ ] New registry tests pass (Task 1)
- [ ] New audit tests pass (Task 5)
- [ ] VCN namespace query works end-to-end (Task 2)
- [ ] Multi-metric normalization doesn't hardcode metric names (Task 3)
- [ ] Audit log file created on first query (Task 6)
- [ ] Timing breakdown includes per-API-call detail (Task 7)
- [ ] `data/logs/` is gitignored
- [ ] No hardcoded `oci_computeagent` references remain in `assistant.py`
- [ ] Metric registry YAML has all 9 namespaces
