"""Behavior tests for the OCI Monitoring MCP prototype foundation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from oci_mon_mcp.artifacts import ArtifactManager
from oci_mon_mcp.assistant import MonitoringAssistantService
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
from oci_mon_mcp.repository import JsonRepository


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
        listing = self.service.discover_accessible_compartments(
            region="us-ashburn-1",
            profile_id="default",
        )
        self.assertEqual(listing["count"], 2)
        self.assertEqual(listing["compartments"][0]["name"], "prod-observability")

    def test_auth_fallback_prompt_is_returned_when_instance_principals_fail(self) -> None:
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
        self.assertEqual(response.status, "needs_clarification")
        self.assertEqual(response.clarifications[0].id, "auth_fallback")

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

    def test_shared_preference_is_available_across_profiles(self) -> None:
        repository = JsonRepository(data_dir=Path(self.tempdir.name) / "shared-preferences")

        repository.remember_preference(
            "alice",
            intent_key="worst_performing_compute_instances",
            resolved_metric="memory",
        )

        shared = repository.get_preference("bob", "worst_performing_compute_instances")

        self.assertIsNotNone(shared)
        assert shared is not None
        self.assertEqual(shared["resolved_metric"], "memory")
        self.assertEqual(shared["scope"], "shared")


if __name__ == "__main__":
    unittest.main()
