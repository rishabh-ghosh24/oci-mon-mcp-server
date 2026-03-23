"""Behavior tests for the OCI Monitoring MCP prototype foundation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from oci_mon_mcp.artifacts import ArtifactManager
from oci_mon_mcp.assistant import MonitoringAssistantService, _interval_for_duration, _time_range_to_minutes
from oci_mon_mcp.execution import MonitoringExecutionAdapter
from oci_mon_mcp.errors import (
    AuthFallbackSuggestedError,
    CompartmentResolutionError,
    InstanceResolutionError,
)
from oci_mon_mcp.models import (
    ArtifactLink,
    ChartPoint,
    ChartSeries,
    ExecutionResult,
    ParsedQuery,
    QueryExecutionRequest,
)
from oci_mon_mcp.oci_support import OciContextResolver
from oci_mon_mcp.repository import JsonRepository, RepositoryFactory


class FakeExecutionAdapter(MonitoringExecutionAdapter):
    """Test adapter that returns deterministic query results."""

    def execute(self, request: QueryExecutionRequest) -> ExecutionResult:
        metric_label = request.parsed_query.metric_label
        rows = [
            {
                "instance_name": "app-01",
                "instance_ocid": "ocid1.instance.oc1..aaaa",
                "compartment": request.compartment_name,
                "lifecycle_state": "RUNNING",
                "metric": metric_label,
                "threshold": request.parsed_query.threshold,
                "max_value": 92.1,
                "time_of_max": "2026-03-16T10:11:00Z",
                "latest_value": 74.3,
                "recommendation": "Inspect the workload before restarting.",
            },
            {
                "instance_name": "app-02",
                "instance_ocid": "ocid1.instance.oc1..bbbb",
                "compartment": request.compartment_name,
                "lifecycle_state": "RUNNING",
                "metric": metric_label,
                "threshold": request.parsed_query.threshold,
                "max_value": 88.7,
                "time_of_max": "2026-03-16T09:55:00Z",
                "latest_value": 66.2,
                "recommendation": "Check recent deploys and traffic changes.",
            },
        ]
        chart_series = [
            ChartSeries(
                name="app-01",
                points=[
                    ChartPoint(time="2026-03-16T10:00:00Z", value=82.0),
                    ChartPoint(time="2026-03-16T10:11:00Z", value=92.1),
                ],
            ),
            ChartSeries(
                name="app-02",
                points=[
                    ChartPoint(time="2026-03-16T10:00:00Z", value=79.0),
                    ChartPoint(time="2026-03-16T09:55:00Z", value=88.7),
                ],
            ),
        ]
        return ExecutionResult(
            summary=(
                f"Found {len(rows)} compute instances in {request.compartment_name} with "
                f"high {metric_label.lower()}."
            ),
            rows=rows,
            chart_series=chart_series,
            recommendations=["Check load and recent changes on the affected instances."],
            artifacts=[
                ArtifactLink(
                    id="chart_png",
                    type="image/png",
                    title="Metric chart",
                    url="https://example.com/artifacts/chart.png?token=abc",
                    expires_at="2026-03-16T12:00:00Z",
                )
            ],
        )


class FailingInstancePrincipalAdapter(MonitoringExecutionAdapter):
    """Adapter that requests OCI config fallback."""

    def execute(self, request: QueryExecutionRequest) -> ExecutionResult:
        raise AuthFallbackSuggestedError("Instance Principals auth failed for this request.")


class FakeContextResolver(OciContextResolver):
    """Simple in-memory compartment resolver for tests."""

    def __init__(self) -> None:
        super().__init__(client_factory=None)

    def list_accessible_compartments(
        self,
        *,
        region: str,
        auth_mode: str,
        config_fallback: dict[str, str] | None = None,
    ) -> dict[str, object]:
        return {
            "tenancy_id": "ocid1.tenancy.oc1..test",
            "region": region,
            "count": 2,
            "compartments": [
                {
                    "name": "prod-observability",
                    "id": "ocid1.compartment.oc1..prod",
                    "lifecycle_state": "ACTIVE",
                    "is_root": "false",
                },
                {
                    "name": "shared-observability",
                    "id": "ocid1.compartment.oc1..shared",
                    "lifecycle_state": "ACTIVE",
                    "is_root": "false",
                },
            ],
        }

    def resolve_compartment(
        self,
        *,
        region: str,
        auth_mode: str,
        compartment_name: str,
        compartment_id: str | None,
        config_fallback: dict[str, str] | None = None,
    ) -> dict[str, str]:
        for item in self.list_accessible_compartments(
            region=region,
            auth_mode=auth_mode,
            config_fallback=config_fallback,
        )["compartments"]:
            if item["name"].lower() == compartment_name.lower():
                return {
                    "tenancy_id": "ocid1.tenancy.oc1..test",
                    "compartment_id": item["id"],
                    "compartment_name": item["name"],
                }
        raise CompartmentResolutionError(f"No accessible compartment matched '{compartment_name}'.")

    def resolve_instance_name(
        self,
        *,
        region: str,
        auth_mode: str,
        compartment_id: str,
        instance_name: str,
        config_fallback: dict[str, str] | None = None,
    ) -> dict[str, str]:
        candidates = [
            {"id": "ocid1.instance.oc1..app01", "name": "app-01", "lifecycle_state": "RUNNING"},
            {"id": "ocid1.instance.oc1..app02", "name": "app-02", "lifecycle_state": "RUNNING"},
            {"id": "ocid1.instance.oc1..weblogic01", "name": "weblogic-01", "lifecycle_state": "RUNNING"},
        ]
        exact = [item for item in candidates if item["name"] == instance_name]
        if len(exact) == 1:
            return exact[0]
        partial = [item for item in candidates if instance_name.lower() in item["name"].lower()]
        if len(partial) == 1:
            return partial[0]
        raise InstanceResolutionError(
            f"Multiple instances partially match '{instance_name}'.",
            options=partial,
        )


class FailingSetupResolver(FakeContextResolver):
    """Resolver that fails compartment validation during setup."""

    def resolve_compartment(
        self,
        *,
        region: str,
        auth_mode: str,
        compartment_name: str,
        compartment_id: str | None,
        config_fallback: dict[str, str] | None = None,
    ) -> dict[str, str]:
        raise CompartmentResolutionError(
            f"Multiple accessible compartments are named '{compartment_name}'.",
            options=[
                {"name": "prod-observability", "id": "ocid1.compartment.oc1..prod"},
                {"name": "prod-observability-dr", "id": "ocid1.compartment.oc1..dr"},
            ],
        )


class MonitoringAssistantServiceTests(unittest.TestCase):
    """High-value tests for setup, clarification, and follow-up behavior."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        repository = JsonRepository(data_dir=Path(self.tempdir.name))
        artifact_dir = Path(self.tempdir.name) / "artifacts"
        self.service = MonitoringAssistantService(
            repository=repository,
            execution_adapter=FakeExecutionAdapter(),
            context_resolver=FakeContextResolver(),
            artifact_manager=ArtifactManager(
                base_dir=artifact_dir,
                base_url="http://127.0.0.1:9000",
                auto_start=False,
            ),
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_first_query_requires_default_context(self) -> None:
        response = self.service.handle_query(
            "show me all compute instances with CPU utilization above 80% in the last 1 hour"
        )

        self.assertEqual(response.status, "needs_clarification")
        self.assertEqual(len(response.clarifications), 2)
        self.assertIn("default region", response.summary.lower())

    def test_setup_default_context_persists_profile(self) -> None:
        response = self.service.setup_default_context(
            region="us-ashburn-1",
            compartment_name="prod-observability",
        )

        self.assertEqual(response.status, "success")
        self.assertIn("us-ashburn-1", response.summary)
        self.assertIn("prod-observability", response.summary)
        self.assertEqual(response.details.scope["compartment_id"], "ocid1.compartment.oc1..prod")

    def test_setup_default_context_does_not_save_when_validation_fails(self) -> None:
        repository = JsonRepository(data_dir=Path(self.tempdir.name) / "invalid-setup")
        service = MonitoringAssistantService(
            repository=repository,
            execution_adapter=FakeExecutionAdapter(),
            context_resolver=FailingSetupResolver(),
            artifact_manager=ArtifactManager(
                base_dir=Path(self.tempdir.name) / "invalid-setup-artifacts",
                base_url="http://127.0.0.1:9000",
                auto_start=False,
            ),
        )

        response = service.setup_default_context(
            region="us-ashburn-1",
            compartment_name="prod-observability",
        )

        self.assertEqual(response.status, "needs_clarification")
        self.assertIn("not saved", response.summary.lower())
        profile = repository.get_profile("default")
        self.assertIsNone(profile["default_compartment_name"])
        self.assertIsNone(profile["default_compartment_id"])

    def test_worst_performing_query_requires_metric_then_succeeds(self) -> None:
        self.service.setup_default_context(
            region="us-ashburn-1",
            compartment_name="prod-observability",
        )

        first_response = self.service.handle_query("show me the worst performing compute instances")
        self.assertEqual(first_response.status, "needs_clarification")
        self.assertEqual(first_response.clarifications[0].id, "metric_choice")

        second_response = self.service.handle_query("CPU")
        self.assertEqual(second_response.status, "success")
        self.assertIn("worst-performing compute instances", second_response.interpretation)
        self.assertEqual(second_response.details.template_id, "tmpl_worst_performing_cpu_1")
        self.assertEqual(
            second_response.tables[0].columns,
            [
                "instance_name",
                "compartment",
                "lifecycle_state",
                "max_value",
                "time_of_max",
                "latest_value",
            ],
        )
        self.assertNotIn("instance_ocid", second_response.tables[0].rows[0])
        self.assertNotIn("recommendation", second_response.tables[0].rows[0])

    def test_high_memory_without_threshold_requires_clarification(self) -> None:
        self.service.setup_default_context(
            region="us-ashburn-1",
            compartment_name="prod-observability",
        )

        first_response = self.service.handle_query("show me computes with high memory")
        self.assertEqual(first_response.status, "needs_clarification")
        self.assertEqual(first_response.clarifications[0].id, "threshold")

        second_response = self.service.handle_query("85%")
        self.assertEqual(second_response.status, "success")
        self.assertIn("85%", second_response.interpretation)

    def test_follow_up_reuses_previous_context(self) -> None:
        self.service.setup_default_context(
            region="us-ashburn-1",
            compartment_name="prod-observability",
        )

        first_response = self.service.handle_query(
            "show me all compute instances with CPU utilization above 80% in the last 1 hour"
        )
        self.assertEqual(first_response.status, "success")

        second_response = self.service.handle_query("now do the same for memory")
        self.assertEqual(second_response.status, "success")
        self.assertIn("memory utilization", second_response.interpretation.lower())
        self.assertIn("80%", second_response.interpretation)

    def test_storage_request_is_explicitly_rejected(self) -> None:
        self.service.setup_default_context(
            region="us-ashburn-1",
            compartment_name="prod-observability",
        )

        response = self.service.handle_query("show me compute storage utilization")
        self.assertEqual(response.status, "error")
        self.assertIn("not available from standard OCI Monitoring", response.summary)

    def test_disk_io_clarification_resolves_from_short_answers(self) -> None:
        self.service.setup_default_context(
            region="us-ashburn-1",
            compartment_name="prod-observability",
        )

        first_response = self.service.handle_query("show me compute io")
        self.assertEqual(first_response.status, "needs_clarification")
        self.assertEqual(first_response.clarifications[0].id, "io_type")

        second_response = self.service.handle_query("disk")
        self.assertEqual(second_response.status, "needs_clarification")
        self.assertEqual(second_response.clarifications[0].id, "io_measure")

        third_response = self.service.handle_query("throughput, both")
        self.assertEqual(third_response.status, "success")
        self.assertIn("disk i/o throughput", third_response.interpretation.lower())

    def test_discover_accessible_compartments_returns_listing(self) -> None:
        self.service.setup_default_context(
            region="us-ashburn-1",
            compartment_name="prod-observability",
        )
        listing = self.service.discover_accessible_compartments(
            profile_id="default",
        )
        self.assertEqual(listing["count"], 2)
        self.assertEqual(listing["compartments"][0]["name"], "prod-observability")

    def test_discover_accessible_compartments_requires_initial_setup_even_if_region_is_supplied(self) -> None:
        listing = self.service.discover_accessible_compartments(
            region="ap-mumbai-1",
            profile_id="default",
        )

        self.assertEqual(listing["status"], "needs_clarification")
        self.assertIn("not configured yet", listing["summary"].lower())

    def test_instance_principal_failure_returns_error_without_auto_fallback_prompt(self) -> None:
        repository = JsonRepository(data_dir=Path(self.tempdir.name) / "fallback")
        service = MonitoringAssistantService(
            repository=repository,
            execution_adapter=FailingInstancePrincipalAdapter(),
            context_resolver=FakeContextResolver(),
            artifact_manager=ArtifactManager(
                base_dir=Path(self.tempdir.name) / "fallback-artifacts",
                base_url="http://127.0.0.1:9000",
                auto_start=False,
            ),
        )
        service.setup_default_context(
            region="us-ashburn-1",
            compartment_name="prod-observability",
        )
        response = service.handle_query(
            "show me all compute instances with CPU utilization above 80% in the last 1 hour"
        )
        self.assertEqual(response.status, "error")
        self.assertEqual(response.clarifications, [])
        self.assertIn("explicitly run configure_auth_fallback", response.summary.lower())

    def test_large_result_generates_csv_artifact(self) -> None:
        class ManyRowsAdapter(FakeExecutionAdapter):
            def execute(self, request: QueryExecutionRequest) -> ExecutionResult:
                base = super().execute(request)
                rows = []
                for index in range(25):
                    rows.append(
                        {
                            "instance_name": f"app-{index:02d}",
                            "instance_ocid": f"ocid1.instance.oc1..{index:04d}",
                            "compartment": request.compartment_name,
                            "lifecycle_state": "RUNNING",
                            "metric": request.parsed_query.metric_label,
                            "threshold": request.parsed_query.threshold,
                            "max_value": 90.0 - index,
                            "time_of_max": f"2026-03-16T10:{index:02d}:00Z",
                            "latest_value": 70.0 - index,
                            "recommendation": "",
                        }
                    )
                base.rows = rows
                return base

        service = MonitoringAssistantService(
            repository=JsonRepository(data_dir=Path(self.tempdir.name) / "many"),
            execution_adapter=ManyRowsAdapter(),
            context_resolver=FakeContextResolver(),
            artifact_manager=ArtifactManager(
                base_dir=Path(self.tempdir.name) / "many-artifacts",
                base_url="http://127.0.0.1:9000",
                auto_start=False,
            ),
        )
        service.setup_default_context(
            region="us-ashburn-1",
            compartment_name="prod-observability",
        )
        response = service.handle_query(
            "show me all compute instances with CPU utilization above 80% in the last 1 hour"
        )
        self.assertEqual(response.status, "success")
        self.assertTrue(any(item.type == "text/csv" for item in response.artifacts))

    def test_named_instance_partial_match_requires_clarification_then_succeeds(self) -> None:
        self.service.setup_default_context(
            region="us-ashburn-1",
            compartment_name="prod-observability",
        )
        first_response = self.service.handle_query("show CPU trend for app")
        self.assertEqual(first_response.status, "needs_clarification")
        self.assertEqual(first_response.clarifications[0].id, "instance_name")

        second_response = self.service.handle_query("app-01")
        self.assertEqual(second_response.status, "success")
        self.assertIn("app-01", second_response.interpretation)

    def test_named_instance_query_with_time_clause_keeps_instance_name_clean(self) -> None:
        self.service.setup_default_context(
            region="us-ashburn-1",
            compartment_name="prod-observability",
        )

        response = self.service.handle_query("show CPU trend for app-01 in the last 1 hour")

        self.assertEqual(response.status, "success")
        self.assertIn("app-01", response.interpretation)
        self.assertNotIn("in the last 1 hour", response.interpretation)

    def test_vcn_network_query_resolves_from_registry(self) -> None:
        """Verify the assistant resolves a VCN namespace query via registry."""
        self.service.setup_default_context(
            region="us-ashburn-1",
            compartment_name="prod-observability",
        )
        response = self.service.handle_query(
            query="show me network bytes in for all instances in the last 1 hour",
        )
        # Should at least be recognized as a valid query (not rejected as unknown)
        self.assertIn(response.status, ("success", "needs_clarification"))

    def test_build_parsed_query_uses_registry_entry(self) -> None:
        """Verify _build_parsed_query reads from the metric registry."""
        self.service.setup_default_context(
            region="us-ashburn-1",
            compartment_name="prod-observability",
        )
        # Build a parsed query for cpu — should use registry
        parsed = self.service._build_parsed_query(
            source_query="test",
            intent="threshold",
            metric_key="cpu",
            time_range="1h",
            threshold=80.0,
        )
        self.assertEqual(parsed.namespace, "oci_computeagent")
        self.assertEqual(parsed.metric_names, ["CpuUtilization"])
        self.assertEqual(parsed.metric_label, "CPU utilization")

    def test_build_parsed_query_vcn_metric(self) -> None:
        """Verify _build_parsed_query works for a VCN metric key."""
        self.service.setup_default_context(
            region="us-ashburn-1",
            compartment_name="prod-observability",
        )
        parsed = self.service._build_parsed_query(
            source_query="test",
            intent="top_n",
            metric_key="vcn_bytes_in",
            time_range="1h",
        )
        self.assertEqual(parsed.namespace, "oci_vcn")
        self.assertEqual(parsed.metric_names, ["VnicFromNetworkBytes"])
        self.assertEqual(parsed.metric_label, "Network bytes received")

    def test_preference_is_scoped_to_profile_until_promoted(self) -> None:
        repository = JsonRepository(data_dir=Path(self.tempdir.name) / "shared-preferences")

        repository.remember_preference(
            "alice",
            intent_key="worst_performing_compute_instances",
            resolved_metric="memory",
        )

        personal = repository.get_preference("alice", "worst_performing_compute_instances")
        missing = repository.get_preference("bob", "worst_performing_compute_instances")

        self.assertIsNotNone(personal)
        assert personal is not None
        self.assertEqual(personal["resolved_metric"], "memory")
        self.assertEqual(personal["scope"], "profile")
        self.assertIsNone(missing)

    def test_shared_preference_falls_back_from_shared_store(self) -> None:
        factory = RepositoryFactory(data_dir=Path(self.tempdir.name) / "shared-store")
        factory.shared.write_shared_preferences(
            [
                {
                    "intent_key": "worst_performing_compute_instances",
                    "resolved_metric": "cpu",
                    "confidence": 0.8,
                    "usage_count": 3,
                    "last_used_at": "2026-03-16T00:00:00+00:00",
                }
            ]
        )
        repository = JsonRepository(factory=factory)

        shared = repository.get_preference("bob", "worst_performing_compute_instances")

        self.assertIsNotNone(shared)
        assert shared is not None
        self.assertEqual(shared["resolved_metric"], "cpu")
        self.assertEqual(shared["scope"], "shared")


class TimeRangeParsingTests(unittest.TestCase):
    """Tests for dynamic time range parsing and interval computation."""

    def test_interval_tiers(self):
        """Verify each tier boundary returns the correct interval."""
        self.assertEqual(_interval_for_duration("5m"), "1m")
        self.assertEqual(_interval_for_duration("30m"), "1m")
        self.assertEqual(_interval_for_duration("31m"), "5m")
        self.assertEqual(_interval_for_duration("1h"), "5m")
        self.assertEqual(_interval_for_duration("2h"), "15m")
        self.assertEqual(_interval_for_duration("6h"), "15m")
        self.assertEqual(_interval_for_duration("7h"), "1h")
        self.assertEqual(_interval_for_duration("12h"), "1h")
        self.assertEqual(_interval_for_duration("24h"), "1h")
        self.assertEqual(_interval_for_duration("36h"), "2h")
        self.assertEqual(_interval_for_duration("48h"), "2h")
        self.assertEqual(_interval_for_duration("3d"), "1d")
        self.assertEqual(_interval_for_duration("7d"), "1d")

    def test_time_range_to_minutes(self):
        self.assertEqual(_time_range_to_minutes("15m"), 15)
        self.assertEqual(_time_range_to_minutes("3h"), 180)
        self.assertEqual(_time_range_to_minutes("2d"), 2880)

    def test_extract_time_range_natural_language(self):
        """Verify arbitrary N hours/minutes/days are parsed."""
        service = self._make_service()
        self.assertEqual(service._extract_time_range("last 3 hours"), "3h")
        self.assertEqual(service._extract_time_range("past 45 minutes"), "45m")
        self.assertEqual(service._extract_time_range("last 9 hours"), "9h")
        self.assertEqual(service._extract_time_range("last 2 days"), "2d")
        self.assertEqual(service._extract_time_range("last 12 hours"), "12h")

    def test_extract_time_range_named_phrases(self):
        service = self._make_service()
        self.assertEqual(service._extract_time_range("last hour"), "1h")
        self.assertEqual(service._extract_time_range("last week"), "7d")
        self.assertEqual(service._extract_time_range("last day"), "24h")

    def test_extract_time_range_no_hostname_false_positive(self):
        """Ensure hostnames like 'myhost-03d' don't match as time ranges."""
        service = self._make_service()
        result = service._extract_time_range("show cpu trend for myhost-03d")
        self.assertIsNone(result)

    def test_extract_time_range_compact(self):
        service = self._make_service()
        self.assertEqual(service._extract_time_range("6h"), "6h")
        self.assertEqual(service._extract_time_range("30m"), "30m")

    def _make_service(self):
        import tempfile
        from pathlib import Path
        from oci_mon_mcp.artifacts import ArtifactManager
        tmpdir = tempfile.mkdtemp()
        return MonitoringAssistantService(
            repository=None,
            execution_adapter=None,
            artifact_manager=ArtifactManager(base_dir=Path(tmpdir)),
        )


if __name__ == "__main__":
    unittest.main()
