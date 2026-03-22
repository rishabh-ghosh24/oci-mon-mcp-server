"""Tests for multi-user repository isolation and token-derived identity."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio

from oci_mon_mcp.artifacts import ArtifactManager
from oci_mon_mcp.assistant import MonitoringAssistantService
from oci_mon_mcp.audit import AuditLogger
from oci_mon_mcp.identity import RequestIdentity, reset_current_identity, set_current_identity
from oci_mon_mcp.models import AssistantResponse
from oci_mon_mcp.repository import JsonRepository, RepositoryFactory
from oci_mon_mcp.server import (
    IdentityMiddleware,
    _ExpectedMcpAccessFilter,
    create_mcp_server,
)


class MultiUserRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.factory = RepositoryFactory(data_dir=Path(self.tempdir.name))
        self.repository = JsonRepository(factory=self.factory)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_profiles_keep_separate_context_and_preferences(self) -> None:
        self.repository.set_default_context(
            "alice",
            region="us-ashburn-1",
            compartment_name="prod-observability",
            compartment_id="ocid1.compartment.oc1..prod",
            tenancy_id="ocid1.tenancy.oc1..alpha",
            available_compartments=[],
        )
        self.repository.set_default_context(
            "bob",
            region="eu-frankfurt-1",
            compartment_name="shared-observability",
            compartment_id="ocid1.compartment.oc1..shared",
            tenancy_id="ocid1.tenancy.oc1..beta",
            available_compartments=[],
        )
        self.repository.remember_preference(
            "alice",
            intent_key="worst_performing_compute_instances",
            resolved_metric="memory",
        )

        alice = self.repository.get_profile("alice")
        bob = self.repository.get_profile("bob")

        self.assertEqual(alice["region"], "us-ashburn-1")
        self.assertEqual(bob["region"], "eu-frankfurt-1")
        self.assertIsNotNone(self.repository.get_preference("alice", "worst_performing_compute_instances"))
        self.assertIsNone(self.repository.get_preference("bob", "worst_performing_compute_instances"))

    def test_list_templates_prefers_shared_version_when_key_matches(self) -> None:
        self.repository.set_default_context(
            "alice",
            region="us-ashburn-1",
            compartment_name="prod-observability",
            compartment_id="ocid1.compartment.oc1..prod",
            tenancy_id="ocid1.tenancy.oc1..alpha",
            available_compartments=[],
        )
        self.repository.save_template(
            profile_id="alice",
            parsed_query={
                "intent": "worst_performing",
                "metric_key": "cpu",
                "time_range": "1h",
                "threshold": None,
                "aggregation": "max",
                "source_query": "show worst cpu",
            },
            query_text="CpuUtilization[1m].groupBy(resourceId).max()",
        )
        self.factory.shared.write_master_templates(
            [
                {
                    "template_id": "shared_cpu",
                    "tenancy_id": None,
                    "region": None,
                    "created_at": "2026-03-16T00:00:00+00:00",
                    "updated_at": "2026-03-16T00:00:00+00:00",
                    "intent_type": "worst_performing",
                    "nl_patterns": ["show worst cpu"],
                    "resource_type": "compute_instance",
                    "metric_key": "cpu",
                    "time_window": "1h",
                    "aggregation": "max",
                    "threshold": None,
                    "query_text": "CpuUtilization[1m].groupBy(resourceId).max()",
                    "usage_count": 4,
                    "success_rate": 1.0,
                    "last_used_at": "2026-03-16T00:00:00+00:00",
                    "confidence": 0.95,
                }
            ]
        )

        templates = self.repository.list_templates(profile_id="alice")

        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]["template_id"], "shared_cpu")


class _FakeService:
    def __init__(self, repository: JsonRepository | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.repository = repository or JsonRepository(factory=RepositoryFactory(data_dir=Path(tempfile.mkdtemp())))

    def handle_query(self, *, query: str, profile_id: str) -> AssistantResponse:
        self.calls.append(("monitoring_assistant", profile_id))
        return AssistantResponse(status="success", interpretation=query, summary="ok")

    def setup_default_context(self, *, region: str, compartment_name: str, compartment_id: str | None, profile_id: str) -> AssistantResponse:
        self.calls.append(("setup_default_context", profile_id))
        return AssistantResponse(status="success", interpretation=region, summary=compartment_name)

    def change_default_context(self, *, region: str | None, compartment_name: str | None, compartment_id: str | None, profile_id: str) -> AssistantResponse:
        self.calls.append(("change_default_context", profile_id))
        return AssistantResponse(status="success", interpretation=profile_id, summary="ok")

    def list_saved_templates(self, *, profile_id: str) -> dict[str, object]:
        self.calls.append(("list_saved_templates", profile_id))
        return {"profile_id": profile_id, "count": 0, "templates": []}

    def discover_accessible_compartments(self, *, region: str, profile_id: str) -> dict[str, object]:
        self.calls.append(("discover_accessible_compartments", profile_id))
        return {"status": "success", "count": 0, "compartments": []}

    def configure_auth_fallback(self, *, config_path: str, profile_name: str, profile_id: str) -> AssistantResponse:
        self.calls.append(("configure_auth_fallback", profile_id))
        return AssistantResponse(status="success", interpretation=config_path, summary=profile_name)

    def use_instance_principals(self, *, profile_id: str) -> AssistantResponse:
        self.calls.append(("use_instance_principals", profile_id))
        return AssistantResponse(status="success", interpretation=profile_id, summary="ok")


class ServerIdentityTests(unittest.TestCase):
    def test_monitoring_assistant_tool_returns_unstructured_for_inline_images(self) -> None:
        """monitoring_assistant omits outputSchema so inline ImageContent blocks propagate."""
        mcp = create_mcp_server()
        tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == "monitoring_assistant")
        self.assertIsNone(tool.output_schema)

    def test_all_tools_use_identity_profile_when_present(self) -> None:
        fake_service = _FakeService()
        with patch("oci_mon_mcp.server.SERVICE", fake_service):
            mcp = create_mcp_server()
            tools = {tool.name: tool.fn for tool in mcp._tool_manager.list_tools()}
            token = set_current_identity(
                RequestIdentity(profile_id="pilot_alice_codex", user_id="alice")
            )
            try:
                tools["monitoring_assistant"]("show cpu", profile_id="default")
                tools["setup_default_context"]("us-ashburn-1", "prod", profile_id="default")
                tools["change_default_context"](region="us-phoenix-1", profile_id="default")
                tools["list_saved_templates"](profile_id="default")
                tools["discover_accessible_compartments"](profile_id="default")
                tools["configure_auth_fallback"](profile_id="default", user_confirmed=True)
                tools["use_instance_principals"](profile_id="default")
            finally:
                reset_current_identity(token)

        self.assertEqual(len(fake_service.calls), 7)
        self.assertTrue(all(profile_id == "pilot_alice_codex" for _, profile_id in fake_service.calls))

    def test_streamable_http_requires_token_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            factory = RepositoryFactory(data_dir=Path(tempdir))
            calls: list[str] = []

            async def app(scope, receive, send):
                calls.append(str(scope.get("path")))
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})

            middleware = IdentityMiddleware(
                app,
                repository_factory=factory,
                streamable_path="/mcp",
            )

            async def invoke(path: str) -> tuple[list[dict[str, object]], list[str]]:
                messages: list[dict[str, object]] = []

                async def receive():
                    return {"type": "http.request", "body": b"", "more_body": False}

                async def send(message):
                    messages.append(message)

                await middleware(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": path,
                        "query_string": b"",
                        "headers": [],
                    },
                    receive,
                    send,
                )
                return messages, list(calls)

            with patch.dict(os.environ, {"OCI_MON_MCP_REQUIRE_TOKEN": "1"}, clear=False):
                mcp_messages, _ = anyio.run(invoke, "/mcp")
                health_messages, health_calls = anyio.run(invoke, "/healthz")

        self.assertEqual(mcp_messages[0]["status"], 401)
        self.assertEqual(health_messages[0]["status"], 200)
        self.assertIn("/healthz", health_calls)

    def test_explicit_first_time_setup_is_allowed_but_change_is_blocked_in_pilot_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            repository = JsonRepository(factory=RepositoryFactory(data_dir=Path(tempdir)))
            fake_service = _FakeService(repository=repository)
            with patch("oci_mon_mcp.server.SERVICE", fake_service):
                mcp = create_mcp_server()
                tools = {tool.name: tool.fn for tool in mcp._tool_manager.list_tools()}
                token = set_current_identity(
                    RequestIdentity(profile_id="pilot_alice_codex", user_id="alice")
                )
                try:
                    with patch.dict(os.environ, {"OCI_MON_MCP_REQUIRE_TOKEN": "1"}, clear=False):
                        setup_response = tools["setup_default_context"](
                            "ap-mumbai-1",
                            "rishabh",
                            profile_id="default",
                        )
                        change_response = tools["change_default_context"](
                            region="ap-mumbai-1",
                            compartment_name="rishabh",
                            profile_id="default",
                        )
                finally:
                    reset_current_identity(token)

        self.assertEqual(setup_response["status"], "success")
        self.assertEqual(change_response["status"], "needs_clarification")
        self.assertEqual(fake_service.calls, [("setup_default_context", "pilot_alice_codex")])

    def test_auto_require_token_when_registry_non_empty(self) -> None:
        """When REQUIRE_TOKEN env var is unset but registry has entries, tokens are required."""
        with tempfile.TemporaryDirectory() as tempdir:
            factory = RepositoryFactory(data_dir=Path(tempdir))
            # Write a non-empty registry via the factory's save method
            factory.save_registry({"tok123": {"profile_id": "p1", "user_id": "u1", "status": "active"}})
            calls: list[str] = []

            async def app(scope, receive, send):
                calls.append("reached")
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})

            middleware = IdentityMiddleware(
                app,
                repository_factory=factory,
                streamable_path="/mcp",
            )

            async def invoke() -> list[dict[str, object]]:
                messages: list[dict[str, object]] = []

                async def receive():
                    return {"type": "http.request", "body": b"", "more_body": False}

                async def send(message):
                    messages.append(message)

                await middleware(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": "/mcp",
                        "query_string": b"",
                        "headers": [],
                    },
                    receive,
                    send,
                )
                return messages

            # Ensure REQUIRE_TOKEN is NOT set
            env = {k: v for k, v in os.environ.items() if k != "OCI_MON_MCP_REQUIRE_TOKEN"}
            with patch.dict(os.environ, env, clear=True):
                messages = anyio.run(invoke)

        # Should reject — registry is non-empty, no token provided
        self.assertEqual(messages[0]["status"], 401)
        self.assertEqual(calls, [])

    def test_no_auto_require_token_when_registry_empty(self) -> None:
        """When REQUIRE_TOKEN env var is unset and registry is empty, requests pass through."""
        with tempfile.TemporaryDirectory() as tempdir:
            factory = RepositoryFactory(data_dir=Path(tempdir))
            calls: list[str] = []

            async def app(scope, receive, send):
                calls.append("reached")
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})

            middleware = IdentityMiddleware(
                app,
                repository_factory=factory,
                streamable_path="/mcp",
            )

            async def invoke() -> list[dict[str, object]]:
                messages: list[dict[str, object]] = []

                async def receive():
                    return {"type": "http.request", "body": b"", "more_body": False}

                async def send(message):
                    messages.append(message)

                await middleware(
                    {
                        "type": "http",
                        "method": "POST",
                        "path": "/mcp",
                        "query_string": b"",
                        "headers": [],
                    },
                    receive,
                    send,
                )
                return messages

            env = {k: v for k, v in os.environ.items() if k != "OCI_MON_MCP_REQUIRE_TOKEN"}
            with patch.dict(os.environ, env, clear=True):
                messages = anyio.run(invoke)

        # Should pass through — no registry, no token requirement
        self.assertEqual(messages[0]["status"], 200)
        self.assertIn("reached", calls)

    def test_invalid_token_logs_warning(self) -> None:
        """Failed token lookups emit a warning log (without the token value)."""
        with tempfile.TemporaryDirectory() as tempdir:
            factory = RepositoryFactory(data_dir=Path(tempdir))
            # Write registry with one valid token via factory
            factory.save_registry({"valid_tok": {"profile_id": "p1", "user_id": "u1", "status": "active"}})

            with self.assertLogs("oci_mon_mcp.repository", level="WARNING") as cm:
                result = factory.resolve_token("bad_token_value")

        self.assertIsNone(result)
        self.assertTrue(any("Invalid token attempt" in msg for msg in cm.output))
        # Token value must NOT appear in logs
        self.assertFalse(any("bad_token_value" in msg for msg in cm.output))


class ServerLoggingTests(unittest.TestCase):
    def test_expected_mcp_probe_noise_with_query_token_is_filtered(self) -> None:
        filt = _ExpectedMcpAccessFilter()
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='49.207.63.80:15530 - "GET /mcp?u=token123 HTTP/1.1" 404 Not Found',
            args=(),
            exc_info=None,
        )
        self.assertFalse(filt.filter(record))

    def test_post_mcp_errors_are_not_filtered(self) -> None:
        filt = _ExpectedMcpAccessFilter()
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='49.207.63.80:15530 - "POST /mcp?u=token123 HTTP/1.1" 500 Internal Server Error',
            args=(),
            exc_info=None,
        )
        self.assertTrue(filt.filter(record))


class AuditLoggingTests(unittest.TestCase):
    """Verify audit logging is wired into the assistant service."""

    def setUp(self) -> None:
        from tests.test_assistant import FakeContextResolver, FakeExecutionAdapter

        self.tempdir = tempfile.TemporaryDirectory()
        self.profile_id = "pilot_audit_tester"
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
        # Pre-configure so handle_query doesn't ask for setup
        self.service.repository.set_default_context(
            self.profile_id,
            region="us-ashburn-1",
            compartment_name="prod-observability",
            compartment_id="ocid1.compartment.oc1..prod",
            tenancy_id="ocid1.tenancy.oc1..test",
            available_compartments=[],
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_audit_failure_does_not_crash_query(self) -> None:
        """Verify that audit logging failure doesn't break the query."""
        from unittest.mock import MagicMock

        from oci_mon_mcp.audit import AuditLogger

        broken_logger = MagicMock(spec=AuditLogger)
        broken_logger.log.side_effect = IOError("disk full")
        self.service._audit_logger = broken_logger

        # This should NOT raise — audit failure is swallowed
        response = self.service.handle_query(
            query="show me cpu utilization for all instances in the last 1 hour",
            profile_id=self.profile_id,
        )
        self.assertEqual(response.status, "success")

    def test_handle_query_creates_audit_entry(self) -> None:
        """Verify that handle_query produces an audit log entry with timing."""
        tmpdir = tempfile.mkdtemp()
        try:
            log_path = Path(tmpdir) / "audit.log"
            audit_logger = AuditLogger(log_path=log_path)
            self.service._audit_logger = audit_logger

            self.service.handle_query(
                query="show me cpu utilization for all instances in the last 1 hour",
                profile_id=self.profile_id,
            )

            self.assertTrue(log_path.exists())
            with open(log_path) as f:
                lines = f.readlines()
            self.assertGreaterEqual(len(lines), 1)
            record = json.loads(lines[-1])
            self.assertIn("timestamp", record)
            self.assertIn("timing", record)
            self.assertIn("total_ms", record["timing"])
            self.assertEqual(record["profile_id"], self.profile_id)
        finally:
            audit_logger.close()
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
