"""OCI SDK-backed Monitoring execution adapter."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from .oci_support import OciClientFactory
from .models import ChartPoint, ChartSeries, ExecutionResult, QueryExecutionRequest


TIME_RANGE_TO_DELTA: dict[str, timedelta] = {
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}


class OciSdkExecutionAdapter:
    """Execute Monitoring queries through the OCI Python SDK."""

    def __init__(self, client_factory: OciClientFactory | None = None) -> None:
        self.client_factory = client_factory or OciClientFactory()

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
        instance_index = self._list_instances(
            oci,
            session.compute_client,
            request.compartment_id,
            include_subcompartments=request.include_subcompartments,
            compartment_lookup=request.compartment_lookup,
        )

        end_time = datetime.now(UTC)
        start_time = end_time - TIME_RANGE_TO_DELTA[request.parsed_query.time_range]
        streams: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "instance_name": None,
                "instance_ocid": None,
                "compartment": request.compartment_name,
                "lifecycle_state": "UNKNOWN",
                "metric_values": {},
                "points": defaultdict(list),
                "time_of_max": None,
                "max_value": None,
                "latest_value": None,
            }
        )

        for query_text in request.query_text.splitlines():
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
            if request.include_subcompartments:
                summarize_kwargs["compartment_id_in_subtree"] = True
            response = session.monitoring_client.summarize_metrics_data(**summarize_kwargs)
            for metric_data in response.data:
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
            matched = [row for row in rows if row["max_value"] > float(request.parsed_query.threshold or 0)]
            if not matched:
                highest = rows[0] if rows else None
                if highest is None:
                    summary = (
                        f"No recent {metric_label} datapoints were found in {request.compartment_name}."
                    )
                else:
                    summary = (
                        f"No compute instances crossed {request.parsed_query.threshold:.0f}% {metric_label} "
                        f"in the last {request.parsed_query.time_range} in {request.compartment_name}. "
                        f"The highest observed value was {highest['max_value']:.1f}% on "
                        f"{highest['instance_name']} at {highest['time_of_max']}."
                    )
                return ExecutionResult(
                    summary=summary,
                    rows=rows,
                    chart_series=chart_series,
                    no_match_highest=highest,
                )
            rows = matched

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
            metadata = {
                "id": instance.id,
                "display_name": instance.display_name,
                "lifecycle_state": instance.lifecycle_state,
                "compartment_id": getattr(instance, "compartment_id", None),
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
        for stream in streams.values():
            merged_points = self._merge_metric_points(
                stream=stream,
                metric_names=request.parsed_query.metric_names,
            )
            if not merged_points:
                continue
            max_point = max(merged_points, key=lambda item: item[1])
            latest_point = merged_points[-1]
            row = {
                "instance_name": stream["instance_name"],
                "instance_ocid": stream["instance_ocid"],
                "compartment": stream["compartment"],
                "lifecycle_state": stream["lifecycle_state"],
                "metric": request.parsed_query.metric_label,
                "threshold": request.parsed_query.threshold,
                "max_value": max_point[1],
                "time_of_max": max_point[0],
                "latest_value": latest_point[1],
                "recommendation": "",
            }
            rows.append(row)
            chart_candidates.append(
                (
                    max_point[1],
                    ChartSeries(
                        name=str(stream["instance_name"]),
                        points=[ChartPoint(time=time_str, value=value) for time_str, value in merged_points],
                    ),
                )
            )

        rows.sort(key=lambda row: (row["time_of_max"], row["max_value"]), reverse=True)
        if request.parsed_query.intent in {"top_n", "worst_performing"}:
            rows.sort(key=lambda row: row["max_value"], reverse=True)

        chart_candidates.sort(key=lambda item: item[0], reverse=True)
        chart_series = [series for _, series in chart_candidates]
        return rows, chart_series

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
