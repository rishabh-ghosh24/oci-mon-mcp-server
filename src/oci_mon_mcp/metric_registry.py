"""Static metric registry loaded from YAML.

Provides exact-key lookup and natural-language alias resolution
for OCI monitoring metrics across all supported namespaces.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


@dataclass(frozen=True)
class MetricEntry:
    """A single metric definition from the registry."""

    metric_key: str
    namespace: str
    label: str
    metric_names: tuple[str, ...]
    y_axis: str
    unit: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class NamespaceInfo:
    """Metadata about an OCI monitoring namespace."""

    namespace: str
    display_name: str
    resource_type: str
    sdk_client: str


class MetricRegistry:
    """Registry of OCI monitoring metrics, loaded from a YAML file."""

    def __init__(
        self,
        entries: dict[str, MetricEntry],
        namespace_infos: dict[str, NamespaceInfo],
        namespace_metrics: dict[str, list[str]],
    ) -> None:
        self._entries = entries
        self._namespace_infos = namespace_infos
        self._namespace_metrics = namespace_metrics
        self._lock = threading.Lock()

        # Pre-build alias index: list of (alias, metric_key) sorted by alias
        # length descending so longest-match-first search works.
        alias_pairs: list[tuple[str, str]] = []
        for key, entry in entries.items():
            for alias in entry.aliases:
                alias_pairs.append((alias.lower(), key))
        alias_pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
        self._alias_index = alias_pairs

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_yaml(cls, path: str | Path) -> MetricRegistry:
        """Load the registry from a YAML file."""
        with open(path, "r") as fh:
            data = yaml.safe_load(fh)

        entries: dict[str, MetricEntry] = {}
        namespace_infos: dict[str, NamespaceInfo] = {}
        namespace_metrics: dict[str, list[str]] = {}

        for ns_key, ns_data in data["namespaces"].items():
            namespace_infos[ns_key] = NamespaceInfo(
                namespace=ns_key,
                display_name=ns_data["display_name"],
                resource_type=ns_data["resource_type"],
                sdk_client=ns_data["sdk_client"],
            )
            ns_metric_keys: list[str] = []

            for metric_key, metric_data in ns_data["metrics"].items():
                entries[metric_key] = MetricEntry(
                    metric_key=metric_key,
                    namespace=ns_key,
                    label=metric_data["label"],
                    metric_names=tuple(metric_data["metric_names"]),
                    y_axis=metric_data["y_axis"],
                    unit=metric_data["unit"],
                    aliases=tuple(metric_data.get("aliases", [])),
                )
                ns_metric_keys.append(metric_key)

            namespace_metrics[ns_key] = ns_metric_keys

        return cls(entries, namespace_infos, namespace_metrics)

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def resolve(self, metric_key: str) -> Optional[MetricEntry]:
        """Exact lookup by metric key. Returns None if not found."""
        return self._entries.get(metric_key)

    def resolve_by_alias(self, text: str) -> Optional[MetricEntry]:
        """Find the best-matching metric for a natural-language string.

        Uses longest-alias-wins strategy: iterates aliases sorted by
        descending length and returns the first match found as a
        substring of *text*.
        """
        text_lower = text.lower()
        for alias, metric_key in self._alias_index:
            if alias in text_lower:
                return self._entries[metric_key]
        return None

    def list_namespaces(self) -> list[str]:
        """Return all namespace keys."""
        return list(self._namespace_infos.keys())

    def get_namespace_info(self, namespace: str) -> Optional[NamespaceInfo]:
        """Return metadata for a namespace, or None."""
        return self._namespace_infos.get(namespace)

    def list_metrics_for_namespace(self, namespace: str) -> list[str]:
        """Return metric keys belonging to a namespace."""
        return list(self._namespace_metrics.get(namespace, []))

    # ------------------------------------------------------------------
    # Runtime discovery
    # ------------------------------------------------------------------

    def register_discovered_namespace(
        self,
        namespace: str,
        display_name: str,
        metrics: list[dict[str, str]],
    ) -> NamespaceInfo:
        """Register a namespace discovered at runtime (e.g. via ListMetrics).

        This allows namespaces not present in the static YAML to be used
        for metric resolution after calling the OCI ListMetrics API.
        """
        with self._lock:
            ns_metric_keys: list[str] = []
            new_alias_pairs: list[tuple[str, str]] = []

            for m in metrics:
                metric_name = m["metric_name"]
                key = f"{namespace}__{metric_name}"
                entry = MetricEntry(
                    metric_key=key,
                    label=f"{display_name} {metric_name}",
                    namespace=namespace,
                    metric_names=(metric_name,),
                    y_axis=metric_name.lower(),
                    aliases=(metric_name.lower(),),
                    unit=m.get("unit", ""),
                )
                self._entries[key] = entry
                ns_metric_keys.append(key)
                for alias in entry.aliases:
                    new_alias_pairs.append((alias.lower(), key))

            # Merge new aliases into the existing index, keeping longest-first order.
            self._alias_index = sorted(
                self._alias_index + new_alias_pairs,
                key=lambda pair: len(pair[0]),
                reverse=True,
            )

            ns_info = NamespaceInfo(
                namespace=namespace,
                display_name=display_name,
                resource_type="",
                sdk_client="",
            )
            self._namespace_infos[namespace] = ns_info
            self._namespace_metrics[namespace] = ns_metric_keys
            return ns_info

    @property
    def all_metric_keys(self) -> list[str]:
        """Return all metric keys in the registry."""
        return list(self._entries.keys())
