"""Focused tests for OCI SDK result normalization behavior."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
import unittest

from oci_mon_mcp.models import ParsedQuery, QueryExecutionRequest
from oci_mon_mcp.oci_sdk_adapter import OciSdkExecutionAdapter
from oci_mon_mcp.oci_support import OciSession


class FakeSummarizeMetricsDataDetails:
    def __init__(
        self,
        *,
        namespace: str,
        query: str,
        start_time: datetime,
        end_time: datetime,
        resolution: str,
    ) -> None:
        self.namespace = namespace
        self.query = query
        self.start_time = start_time
        self.end_time = end_time
        self.resolution = resolution


class FakePagination:
    @staticmethod
    def list_call_get_all_results(function, **kwargs):
        return function(**kwargs)


class FakeMonitoringModule:
    class models:
        SummarizeMetricsDataDetails = FakeSummarizeMetricsDataDetails


class FakeOciModule:
    pagination = FakePagination()
    monitoring = FakeMonitoringModule()


class FakeMonitoringClient:
    def __init__(self, datasets: dict[str, list[object]]) -> None:
        self.datasets = datasets

    def summarize_metrics_data(self, **kwargs):
        details = kwargs["summarize_metrics_data_details"]
        metric_name = details.query.split("[", 1)[0]
        return SimpleNamespace(data=self.datasets.get(metric_name, []))


class FakeComputeClient:
    def __init__(self, instances: list[object]) -> None:
        self.instances = instances

    def list_instances(self, **kwargs):
        return SimpleNamespace(data=self.instances)


class FakeClientFactory:
    def __init__(self, *, datasets: dict[str, list[object]], instances: list[object]) -> None:
        self.datasets = datasets
        self.instances = instances

    def build_session(
        self,
        *,
        region: str,
        auth_mode: str,
        config_fallback: dict[str, str] | None = None,
        include_monitoring: bool = False,
        include_compute: bool = False,
        include_identity: bool = False,
    ) -> OciSession:
        return OciSession(
            oci=FakeOciModule(),
            region=region,
            auth_mode=auth_mode,
            tenancy_id="ocid1.tenancy.oc1..test",
            monitoring_client=FakeMonitoringClient(self.datasets) if include_monitoring else None,
            compute_client=FakeComputeClient(self.instances) if include_compute else None,
        )


def metric_stream(
    *,
    instance_id: str,
    instance_name: str,
    compartment_id: str,
    timestamp: str,
    value: float,
) -> object:
    point = SimpleNamespace(timestamp=datetime.fromisoformat(timestamp.replace("Z", "+00:00")), value=value)
    return SimpleNamespace(
        dimensions={
            "resourceId": instance_id,
            "resourceDisplayName": instance_name,
            "compartmentId": compartment_id,
        },
        aggregated_datapoints=[point],
    )


class OciSdkExecutionAdapterTests(unittest.TestCase):
    def _request(self, *, threshold: float) -> QueryExecutionRequest:
        parsed = ParsedQuery(
            intent="threshold",
            metric_key="cpu",
            metric_label="CPU utilization",
            namespace="oci_computeagent",
            metric_names=["CpuUtilization"],
            time_range="1h",
            interval="1m",
            aggregation="max",
            threshold=threshold,
            source_query="show me all compute instances with CPU utilization above 80% in the last 1 hour",
        )
        return QueryExecutionRequest(
            parsed_query=parsed,
            profile_id="default",
            region="us-ashburn-1",
            compartment_name="prod-observability",
            compartment_id="ocid1.compartment.oc1..prod",
            include_subcompartments=True,
            compartment_lookup={"ocid1.compartment.oc1..prod": "prod-observability"},
        )

    def test_threshold_no_match_reports_actual_highest_and_limits_to_top_five(self) -> None:
        instances = [
            SimpleNamespace(
                id=f"ocid1.instance.oc1..{index}",
                display_name=f"app-0{index}",
                lifecycle_state="RUNNING",
                compartment_id="ocid1.compartment.oc1..prod",
            )
            for index in range(1, 7)
        ]
        cpu_streams = [
            metric_stream(
                instance_id="ocid1.instance.oc1..1",
                instance_name="app-01",
                compartment_id="ocid1.compartment.oc1..prod",
                timestamp="2026-03-17T10:59:00Z",
                value=70.0,
            ),
            metric_stream(
                instance_id="ocid1.instance.oc1..2",
                instance_name="app-02",
                compartment_id="ocid1.compartment.oc1..prod",
                timestamp="2026-03-17T10:40:00Z",
                value=72.0,
            ),
            metric_stream(
                instance_id="ocid1.instance.oc1..3",
                instance_name="app-03",
                compartment_id="ocid1.compartment.oc1..prod",
                timestamp="2026-03-17T10:35:00Z",
                value=75.0,
            ),
            metric_stream(
                instance_id="ocid1.instance.oc1..4",
                instance_name="app-04",
                compartment_id="ocid1.compartment.oc1..prod",
                timestamp="2026-03-17T10:10:00Z",
                value=79.0,
            ),
            metric_stream(
                instance_id="ocid1.instance.oc1..5",
                instance_name="app-05",
                compartment_id="ocid1.compartment.oc1..prod",
                timestamp="2026-03-17T10:20:00Z",
                value=68.0,
            ),
            metric_stream(
                instance_id="ocid1.instance.oc1..6",
                instance_name="app-06",
                compartment_id="ocid1.compartment.oc1..prod",
                timestamp="2026-03-17T10:25:00Z",
                value=65.0,
            ),
        ]

        adapter = OciSdkExecutionAdapter(
            client_factory=FakeClientFactory(datasets={"CpuUtilization": cpu_streams}, instances=instances)
        )

        result = adapter.execute(self._request(threshold=80.0))

        self.assertIn("79.0%", result.summary)
        self.assertIn("app-04", result.summary)
        self.assertEqual(len(result.rows), 5)
        self.assertEqual(len(result.chart_series), 5)
        self.assertNotIn("app-06", [series.name for series in result.chart_series])

    def test_threshold_match_filters_chart_series_to_matching_instances_only(self) -> None:
        instances = [
            SimpleNamespace(
                id=f"ocid1.instance.oc1..{index}",
                display_name=f"app-0{index}",
                lifecycle_state="RUNNING",
                compartment_id="ocid1.compartment.oc1..prod",
            )
            for index in range(1, 4)
        ]
        cpu_streams = [
            metric_stream(
                instance_id="ocid1.instance.oc1..1",
                instance_name="app-01",
                compartment_id="ocid1.compartment.oc1..prod",
                timestamp="2026-03-17T10:55:00Z",
                value=92.0,
            ),
            metric_stream(
                instance_id="ocid1.instance.oc1..2",
                instance_name="app-02",
                compartment_id="ocid1.compartment.oc1..prod",
                timestamp="2026-03-17T10:50:00Z",
                value=88.0,
            ),
            metric_stream(
                instance_id="ocid1.instance.oc1..3",
                instance_name="app-03",
                compartment_id="ocid1.compartment.oc1..prod",
                timestamp="2026-03-17T10:58:00Z",
                value=71.0,
            ),
        ]

        adapter = OciSdkExecutionAdapter(
            client_factory=FakeClientFactory(datasets={"CpuUtilization": cpu_streams}, instances=instances)
        )

        result = adapter.execute(self._request(threshold=80.0))

        self.assertEqual([row["instance_name"] for row in result.rows], ["app-01", "app-02"])
        self.assertEqual([series.name for series in result.chart_series], ["app-01", "app-02"])


if __name__ == "__main__":
    unittest.main()
