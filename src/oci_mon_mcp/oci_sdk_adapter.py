"""OCI SDK-backed Monitoring execution adapter."""

from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from .oci_support import OciClientFactory
from .models import ChartPoint, ChartSeries, ExecutionResult, QueryExecutionRequest

logger = logging.getLogger(__name__)

TIME_RANGE_TO_DELTA: dict[str, timedelta] = {
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}

THRESHOLD_NO_MATCH_LIMIT = 5

_DEFAULT_INSTANCE_CACHE_TTL = 900  # 15 minutes


class _InstanceCache:
    """Thread-safe instance listing cache with stale-while-revalidate."""

    def __init__(self, ttl_seconds: int = _DEFAULT_INSTANCE_CACHE_TTL) -> None:
        self._ttl = ttl_seconds
        self._store: dict[tuple, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        self._refreshing: set[tuple] = set()

    def get(self, key: tuple) -> tuple[dict | None, bool]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None, False
            ts, data = entry
            stale = (time.monotonic() - ts) > self._ttl
            return data, stale

    def put(self, key: tuple, data: dict) -> None:
        with self._lock:
            self._store[key] = (time.monotonic(), data)
            self._refreshing.discard(key)

    def mark_refreshing(self, key: tuple) -> bool:
        """Return True if this caller should trigger the refresh (not already in progress)."""
        with self._lock:
            if key in self._refreshing:
                return False
            self._refreshing.add(key)
            return True


class OciSdkExecutionAdapter:
    """Execute Monitoring queries through the OCI Python SDK."""

    def __init__(self, client_factory: OciClientFactory | None = None) -> None:
        self.client_factory = client_factory or OciClientFactory()
        self._instance_cache = _InstanceCache(
            ttl_seconds=int(os.getenv("OCI_MON_MCP_INSTANCE_CACHE_TTL", str(_DEFAULT_INSTANCE_CACHE_TTL)))
        )

    def execute(self, request: QueryExecutionRequest) -> ExecutionResult:
        if not request.compartment_id:
            raise RuntimeError(
                "A compartment OCID is required for OCI execution. Update the default context with "
                "a compartment OCID before running live queries."
            )

        session = self.client_factory.build_session(
            region=request.region,
            auth_mode=request.auth_mode,
            config_fallback=request.config_fallback,
            include_monitoring=True,
            include_compute=True,
        )
        assert session.monitoring_client is not None
        assert session.compute_client is not None
        oci = session.oci

        # --- Instance listing with stale-while-revalidate cache ---
        cache_key = (request.region, request.compartment_id, request.include_subcompartments)
        cached_index, is_stale = self._instance_cache.get(cache_key)
        if cached_index is not None:
            instance_index = cached_index
            if is_stale and self._instance_cache.mark_refreshing(cache_key):
                threading.Thread(
                    target=self._refresh_instance_cache,
                    args=(oci, session.compute_client, request, cache_key),
                    daemon=True,
                ).start()
        else:
            instance_index = self._list_instances(
                oci,
                session.compute_client,
                request.compartment_id,
                include_subcompartments=request.include_subcompartments,
                compartment_lookup=request.compartment_lookup,
            )
            self._instance_cache.put(cache_key, instance_index)

        end_time = datetime.now(UTC)
        start_time = end_time - TIME_RANGE_TO_DELTA[request.parsed_query.time_range]
        streams: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "instance_name": None,
                "instance_ocid": None,
                "compartment": request.compartment_name,
                "lifecycle_state": "UNKNOWN",
                "time_created": None,
                "metric_values": {},
                "points": defaultdict(list),
                "time_of_max": None,
                "max_value": None,
                "latest_value": None,
            }
        )

        # --- Parallel metric queries ---
        queries = request.query_text.splitlines()
        include_subtree = request.include_subcompartments
        if len(queries) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(queries)) as pool:
                futures = [
                    pool.submit(
                        self._fetch_metric, session, oci, q, request,
                        start_time, end_time, include_subtree,
                    )
                    for q in queries
                ]
                metric_results = [f.result() for f in futures]
        else:
            metric_results = [
                self._fetch_metric(
                    session, oci, queries[0], request,
                    start_time, end_time, include_subtree,
                )
            ]

        # Merge metric results into streams (sequential — no lock needed)
        for metric_name, metric_data_list in metric_results:
            for metric_data in metric_data_list:
                dimensions = metric_data.dimensions or {}
                resource_id = dimensions.get("resourceId") or dimensions.get("resourceDisplayName")
                if resource_id is None:
                    continue
                instance = streams[resource_id]
                metadata = instance_index.get(resource_id, {})
                compartment_id = dimensions.get("compartmentId") or metadata.get("compartment_id")
                compartment_name = (
                    request.compartment_lookup.get(compartment_id, compartment_id)
                    if compartment_id
                    else request.compartment_name
                )
                instance["instance_name"] = (
                    dimensions.get("resourceDisplayName")
                    or metadata.get("display_name")
                    or resource_id
                )
                instance["instance_ocid"] = dimensions.get("resourceId") or metadata.get("id")
                instance["compartment"] = compartment_name
                instance["lifecycle_state"] = metadata.get("lifecycle_state", "UNKNOWN")
                instance["time_created"] = metadata.get("time_created")

                points = sorted(
                    metric_data.aggregated_datapoints or [],
                    key=lambda item: item.timestamp,
                )
                if not points:
                    continue
                value_pairs = [
                    (
                        point.timestamp.isoformat(),
                        float(point.value),
                    )
                    for point in points
                    if getattr(point, "value", None) is not None
                ]
                if not value_pairs:
                    continue
                instance["points"][metric_name] = value_pairs
                max_point = max(value_pairs, key=lambda item: item[1])
                instance["metric_values"][metric_name] = {
                    "max_value": max_point[1],
                    "time_of_max": max_point[0],
                    "latest_value": value_pairs[-1][1],
                }

        rows, chart_series = self._normalize_results(request, streams)
        metric_label = request.parsed_query.metric_label.lower()
        if request.parsed_query.intent == "threshold":
            threshold_value = float(request.parsed_query.threshold or 0)
            matched = [row for row in rows if row.get("aggregated_value", 0.0) > threshold_value]
            if not matched:
                highest = max(rows, key=lambda row: row.get("aggregated_value", 0.0), default=None)
                top_rows = sorted(
                    rows,
                    key=lambda row: row.get("aggregated_value", 0.0),
                    reverse=True,
                )[:THRESHOLD_NO_MATCH_LIMIT]
                if highest is None:
                    summary = (
                        f"No recent {metric_label} datapoints were found in {request.compartment_name}."
                    )
                else:
                    highest_time = highest.get("time_of_aggregate") or highest.get("time_of_max") or "N/A"
                    summary = (
                        f"No compute instances crossed {request.parsed_query.threshold:.0f}% {metric_label} "
                        f"in the last {request.parsed_query.time_range} in {request.compartment_name}. "
                        f"The highest observed value was {highest.get('aggregated_value', 0.0):.1f}% on "
                        f"{highest['instance_name']} at {highest_time}."
                    )
                return ExecutionResult(
                    summary=summary,
                    rows=top_rows,
                    chart_series=self._filter_chart_series(
                        chart_series,
                        instance_names={row.get("instance_name") for row in top_rows},
                    )[:THRESHOLD_NO_MATCH_LIMIT],
                    no_match_highest=highest,
                )
            rows = matched
            chart_series = self._filter_chart_series(
                chart_series,
                instance_names={row.get("instance_name") for row in matched},
            )

        if request.parsed_query.top_n:
            rows = rows[: request.parsed_query.top_n]
            chart_series = chart_series[: request.parsed_query.top_n]

        if request.parsed_query.intent == "named_trend" and request.parsed_query.instance_name:
            summary = (
                f"Retrieved {metric_label} trend data for {request.parsed_query.instance_name} "
                f"in {request.compartment_name}."
            )
        else:
            summary = (
                f"Found {len(rows)} compute instances for {metric_label} in "
                f"{request.compartment_name} over the last {request.parsed_query.time_range}."
            )
        return ExecutionResult(summary=summary, rows=rows, chart_series=chart_series)

    def _fetch_metric(
        self,
        session: Any,
        oci: Any,
        query_text: str,
        request: QueryExecutionRequest,
        start_time: datetime,
        end_time: datetime,
        include_subtree: bool,
    ) -> tuple[str, list[Any]]:
        """Fetch a single metric from OCI Monitoring. Thread-safe."""
        metric_name = query_text.split("[", 1)[0]
        details = oci.monitoring.models.SummarizeMetricsDataDetails(
            namespace=request.parsed_query.namespace,
            query=query_text,
            start_time=start_time,
            end_time=end_time,
            resolution=request.parsed_query.interval,
        )
        summarize_kwargs: dict[str, Any] = {
            "compartment_id": request.compartment_id,
            "summarize_metrics_data_details": details,
        }
        if include_subtree:
            summarize_kwargs["compartment_id_in_subtree"] = True
        try:
            response = session.monitoring_client.summarize_metrics_data(**summarize_kwargs)
        except Exception as exc:
            if include_subtree and self._is_non_tenancy_subtree_error(exc):
                summarize_kwargs.pop("compartment_id_in_subtree", None)
                response = session.monitoring_client.summarize_metrics_data(**summarize_kwargs)
            else:
                raise
        return metric_name, response.data

    def _refresh_instance_cache(
        self,
        oci: Any,
        compute_client: Any,
        request: QueryExecutionRequest,
        cache_key: tuple,
    ) -> None:
        """Background refresh for stale instance cache entries."""
        try:
            index = self._list_instances(
                oci,
                compute_client,
                request.compartment_id,
                include_subcompartments=request.include_subcompartments,
                compartment_lookup=request.compartment_lookup,
            )
            self._instance_cache.put(cache_key, index)
            logger.debug("Instance cache refreshed for %s", cache_key)
        except Exception:
            logger.warning("Background instance cache refresh failed for %s", cache_key, exc_info=True)
            # Clear refreshing flag so next query retries
            with self._instance_cache._lock:
                self._instance_cache._refreshing.discard(cache_key)

    def _is_non_tenancy_subtree_error(self, exc: Exception) -> bool:
        message = str(exc)
        if "compartmentIdInSubtree" not in message:
            return False
        if "non-tenancy compartment" in message:
            return True
        code = getattr(exc, "code", None)
        return code == "InvalidParameter"

    def _list_instances(
        self,
        oci: Any,
        compute_client: Any,
        compartment_id: str,
        *,
        include_subcompartments: bool,
        compartment_lookup: dict[str, str] | None = None,
    ) -> dict[str, dict[str, str]]:
        list_kwargs: dict[str, Any] = {"compartment_id": compartment_id}
        if include_subcompartments:
            list_kwargs["compartment_id_in_subtree"] = True
            list_kwargs["access_level"] = "ACCESSIBLE"
        try:
            response = oci.pagination.list_call_get_all_results(compute_client.list_instances, **list_kwargs)
            instances = response.data
        except Exception as exc:
            # Some SDK/client variants do not accept subtree kwargs on list_instances.
            message = str(exc)
            if include_subcompartments and (
                "unknown kwargs" in message or "unexpected keyword argument" in message
            ):
                instances = self._list_instances_with_compartment_fallback(
                    oci=oci,
                    compute_client=compute_client,
                    root_compartment_id=compartment_id,
                    compartment_lookup=compartment_lookup or {},
                )
            else:
                raise
        instance_index: dict[str, dict[str, str]] = {}
        for instance in instances:
            time_created = getattr(instance, "time_created", None)
            metadata = {
                "id": instance.id,
                "display_name": instance.display_name,
                "lifecycle_state": instance.lifecycle_state,
                "compartment_id": getattr(instance, "compartment_id", None),
                "time_created": time_created.isoformat() if time_created else None,
            }
            instance_index[instance.id] = metadata
            display_name = getattr(instance, "display_name", None)
            if display_name:
                # Monitoring dimensions can fall back to display name for resource keys.
                instance_index.setdefault(display_name, metadata)
        return instance_index

    def _list_instances_with_compartment_fallback(
        self,
        *,
        oci: Any,
        compute_client: Any,
        root_compartment_id: str,
        compartment_lookup: dict[str, str],
    ) -> list[Any]:
        compartment_ids: list[str] = [root_compartment_id]
        for cid in compartment_lookup:
            if cid not in compartment_ids:
                compartment_ids.append(cid)

        collected: dict[str, Any] = {}
        for compartment_id in compartment_ids:
            try:
                response = oci.pagination.list_call_get_all_results(
                    compute_client.list_instances,
                    compartment_id=compartment_id,
                )
            except Exception:
                continue
            for instance in response.data:
                collected[instance.id] = instance
        return list(collected.values())

    def _normalize_results(
        self,
        request: QueryExecutionRequest,
        streams: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[ChartSeries]]:
        rows: list[dict[str, Any]] = []
        chart_candidates: list[tuple[float, ChartSeries]] = []
        aggregation = request.parsed_query.aggregation
        for stream in streams.values():
            if request.parsed_query.metric_key == "cpu_memory":
                cpu_points = stream["points"].get("CpuUtilization", [])
                memory_points = stream["points"].get("MemoryUtilization", [])
                if not cpu_points or not memory_points:
                    continue
                cpu_stats = self._compute_metric_stats(cpu_points)
                memory_stats = self._compute_metric_stats(memory_points)
                assert cpu_stats is not None
                assert memory_stats is not None
                cpu_agg = self._value_for_aggregation(cpu_stats, aggregation)
                memory_agg = self._value_for_aggregation(memory_stats, aggregation)
                row = {
                    "instance_name": stream["instance_name"],
                    "instance_ocid": stream["instance_ocid"],
                    "compartment": stream["compartment"],
                    "lifecycle_state": stream["lifecycle_state"],
                    "time_created": stream["time_created"],
                    "metric": request.parsed_query.metric_label,
                    "threshold": request.parsed_query.threshold,
                    "aggregation": aggregation,
                    "cpu_mean_value": cpu_stats["mean_value"],
                    "memory_mean_value": memory_stats["mean_value"],
                    "cpu_max_value": cpu_stats["max_value"],
                    "memory_max_value": memory_stats["max_value"],
                    "cpu_latest_value": cpu_stats["latest_value"],
                    "memory_latest_value": memory_stats["latest_value"],
                    "aggregated_value": cpu_agg + memory_agg,
                    "time_of_aggregate": None,
                    "recommendation": "",
                }
                rows.append(row)
                continue

            merged_points = self._merge_metric_points(
                stream=stream,
                metric_names=request.parsed_query.metric_names,
            )
            if not merged_points:
                continue
            stats = self._compute_metric_stats(merged_points)
            if stats is None:
                continue
            aggregated_value = self._value_for_aggregation(stats, aggregation)
            time_of_aggregate = stats["time_of_max"] if aggregation == "max" else None
            row = {
                "instance_name": stream["instance_name"],
                "instance_ocid": stream["instance_ocid"],
                "compartment": stream["compartment"],
                "lifecycle_state": stream["lifecycle_state"],
                "time_created": stream["time_created"],
                "metric": request.parsed_query.metric_label,
                "threshold": request.parsed_query.threshold,
                "aggregation": aggregation,
                "mean_value": stats["mean_value"],
                "max_value": stats["max_value"],
                "time_of_max": stats["time_of_max"],
                "latest_value": stats["latest_value"],
                "aggregated_value": aggregated_value,
                "time_of_aggregate": time_of_aggregate,
                "recommendation": "",
            }
            rows.append(row)
            chart_candidates.append(
                (
                    aggregated_value,
                    ChartSeries(
                        name=str(stream["instance_name"]),
                        points=[ChartPoint(time=time_str, value=value) for time_str, value in merged_points],
                    ),
                )
            )

        rows.sort(
            key=lambda row: (
                row.get("time_of_aggregate") or row.get("time_of_max") or "",
                row.get("aggregated_value", 0.0),
            ),
            reverse=True,
        )
        if request.parsed_query.intent in {"top_n", "worst_performing"}:
            rows.sort(key=lambda row: row.get("aggregated_value", 0.0), reverse=True)

        chart_candidates.sort(key=lambda item: item[0], reverse=True)
        chart_series = [series for _, series in chart_candidates]
        return rows, chart_series

    def _filter_chart_series(
        self,
        chart_series: list[ChartSeries],
        *,
        instance_names: set[Any],
    ) -> list[ChartSeries]:
        normalized_names = {str(name) for name in instance_names if name}
        if not normalized_names:
            return []
        return [series for series in chart_series if series.name in normalized_names]

    def _compute_metric_stats(
        self,
        points: list[tuple[str, float]],
    ) -> dict[str, float | str] | None:
        if not points:
            return None
        max_point = max(points, key=lambda item: item[1])
        latest_point = points[-1]
        mean_value = sum(value for _, value in points) / len(points)
        return {
            "max_value": max_point[1],
            "time_of_max": max_point[0],
            "latest_value": latest_point[1],
            "mean_value": mean_value,
        }

    def _value_for_aggregation(
        self,
        stats: dict[str, float | str],
        aggregation: str,
    ) -> float:
        if aggregation == "mean":
            return float(stats["mean_value"])
        return float(stats["max_value"])

    def _merge_metric_points(
        self,
        *,
        stream: dict[str, Any],
        metric_names: list[str],
    ) -> list[tuple[str, float]]:
        series_groups = [stream["points"].get(metric_name, []) for metric_name in metric_names]
        if not series_groups or not series_groups[0]:
            return []
        if len(series_groups) == 1:
            return [(time_str, float(value)) for time_str, value in series_groups[0]]
        totals: dict[str, float] = defaultdict(float)
        for series in series_groups:
            for time_str, value in series:
                totals[time_str] += float(value)
        return sorted(totals.items(), key=lambda item: item[0])
