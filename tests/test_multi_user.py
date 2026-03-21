"""Tests for multi-user repository isolation and token-derived identity."""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio

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
                RequestIdentity(profile_id="pilot_alice_codex", user_id="alice", token="tok")
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
                    RequestIdentity(profile_id="pilot_alice_codex", user_id="alice", token="tok")
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

    def test_streamable_http_uses_sse_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            mcp = create_mcp_server()

        self.assertFalse(mcp.settings.json_response)
        self.assertFalse(mcp.settings.stateless_http)


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


if __name__ == "__main__":
    unittest.main()
