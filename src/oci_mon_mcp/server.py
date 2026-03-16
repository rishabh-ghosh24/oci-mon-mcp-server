"""FastMCP entry point for the OCI Monitoring MCP prototype."""

from __future__ import annotations

from dataclasses import asdict
import logging
import os
import sys
from typing import Any

from .assistant import MonitoringAssistantService

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - exercised by import fallback tests instead
    FastMCP = None  # type: ignore[assignment]


SERVICE = MonitoringAssistantService()


class _ExpectedMcpAccessFilter(logging.Filter):
    """Filter expected noisy MCP probe logs that are not actionable errors."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if " /mcp HTTP/1.1" not in message:
            return True
        expected_patterns = (
            '"GET /mcp HTTP/1.1" 404',
            '"DELETE /mcp HTTP/1.1" 404',
            '"GET /mcp HTTP/1.1" 400',
        )
        return not any(pattern in message for pattern in expected_patterns)


def _configure_access_log_filter() -> None:
    """Suppress expected MCP probe noise so operator logs stay actionable."""
    if os.getenv("OCI_MON_MCP_SUPPRESS_EXPECTED_MCP_PROBE_LOGS", "1") != "1":
        return
    logger = logging.getLogger("uvicorn.access")
    logger.addFilter(_ExpectedMcpAccessFilter())


def create_mcp_server() -> Any:
    """Create the FastMCP server when the dependency is available."""
    if FastMCP is None:
        return None

    mcp = FastMCP(
        "OCI Monitoring MCP",
        instructions=(
            "Use this server to query OCI Monitoring metrics for compute instances. "
            "Ask clarifying questions before execution when the request is ambiguous."
        ),
        host=os.getenv("OCI_MON_MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("OCI_MON_MCP_PORT", "8000")),
        mount_path=os.getenv("OCI_MON_MCP_MOUNT_PATH", "/"),
        streamable_http_path=os.getenv("OCI_MON_MCP_STREAMABLE_HTTP_PATH", "/mcp"),
    )
    streamable_path = os.getenv("OCI_MON_MCP_STREAMABLE_HTTP_PATH", "/mcp")

    if hasattr(mcp, "custom_route"):
        from starlette.responses import JSONResponse

        @mcp.custom_route("/healthz", methods=["GET"])
        async def healthz(_request):
            """Simple health endpoint for uptime checks and load balancers."""
            return JSONResponse(
                {
                    "status": "ok",
                    "service": "oci-mon-mcp-server",
                    "transport": os.getenv("OCI_MON_MCP_TRANSPORT", "streamable-http"),
                    "mcp_path": streamable_path,
                }
            )

        @mcp.custom_route("/", methods=["GET"])
        async def root(_request):
            """Human-friendly root endpoint to reduce MCP path confusion."""
            return JSONResponse(
                {
                    "service": "OCI Monitoring MCP",
                    "status": "running",
                    "health": "/healthz",
                    "mcp_endpoint": streamable_path,
                    "note": "Use an MCP client against mcp_endpoint; plain HTTP requests may return protocol errors.",
                }
            )

    @mcp.tool()
    def monitoring_assistant(query: str, profile_id: str = "default"):
        """Interpret a monitoring question and return a structured response."""
        return asdict(SERVICE.handle_query(query=query, profile_id=profile_id))

    @mcp.tool()
    def setup_default_context(
        region: str,
        compartment_name: str,
        compartment_id: str = "",
        profile_id: str = "default",
    ):
        """Persist the default region and compartment for a user profile."""
        return asdict(
            SERVICE.setup_default_context(
                region=region,
                compartment_name=compartment_name,
                compartment_id=compartment_id or None,
                profile_id=profile_id,
            )
        )

    @mcp.tool()
    def change_default_context(
        region: str = "",
        compartment_name: str = "",
        compartment_id: str = "",
        profile_id: str = "default",
    ):
        """Update the stored default region and/or compartment."""
        return asdict(
            SERVICE.change_default_context(
                region=region or None,
                compartment_name=compartment_name or None,
                compartment_id=compartment_id or None,
                profile_id=profile_id,
            )
        )

    @mcp.tool()
    def list_saved_templates(profile_id: str = "default"):
        """List successful saved query templates for the current profile scope."""
        return SERVICE.list_saved_templates(profile_id=profile_id)

    @mcp.tool()
    def discover_accessible_compartments(region: str = "", profile_id: str = "default"):
        """List accessible compartments for the current auth mode."""
        return SERVICE.discover_accessible_compartments(region=region, profile_id=profile_id)

    @mcp.tool()
    def configure_auth_fallback(
        config_path: str = "~/.oci/config",
        profile_name: str = "DEFAULT",
        profile_id: str = "default",
    ):
        """Persist OCI config fallback settings for this profile."""
        return asdict(
            SERVICE.configure_auth_fallback(
                config_path=config_path,
                profile_name=profile_name,
                profile_id=profile_id,
            )
        )

    @mcp.tool()
    def use_instance_principals(profile_id: str = "default"):
        """Switch the profile back to Instance Principals auth."""
        return asdict(SERVICE.use_instance_principals(profile_id=profile_id))

    return mcp


def main() -> None:
    """Start the MCP server if FastMCP is installed."""
    _configure_access_log_filter()
    server = create_mcp_server()
    if server is None:
        print(
            "The 'mcp' package is not installed. Install project dependencies first, "
            "for example with: pip install -e '.[dev]'",
            file=sys.stderr,
        )
        raise SystemExit(1)

    transport = os.getenv("OCI_MON_MCP_TRANSPORT", "streamable-http")
    try:
        server.run(transport=transport)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
