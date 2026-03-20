"""FastMCP entry point for the OCI Monitoring MCP prototype."""

from __future__ import annotations

from dataclasses import asdict
import logging
import os
import sys
from typing import Any

from .assistant import MonitoringAssistantService
from .identity import (
    RequestIdentity,
    get_current_identity,
    reset_current_identity,
    set_current_identity,
)
from .models import (
    AssistantResponse,
    AssistantToolResponse,
    ClarificationQuestion,
    CompartmentDiscoveryResponse,
    TemplateListingResponse,
)
from .repository import JsonRepository, RepositoryFactory

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - exercised by import fallback tests instead
    FastMCP = None  # type: ignore[assignment]


REPOSITORY_FACTORY = RepositoryFactory()
SERVICE = MonitoringAssistantService(repository=JsonRepository(factory=REPOSITORY_FACTORY))


class _ExpectedMcpAccessFilter(logging.Filter):
    """Filter expected noisy MCP probe logs that are not actionable errors."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "/mcp" not in message or "HTTP/1.1" not in message:
            return True
        normalized = message.split('"', 1)[1] if '"' in message else message
        if not normalized.startswith(("GET /mcp", "DELETE /mcp")):
            return True
        if not any(status in message for status in (" 400", " 404")):
            return True
        return False


class IdentityMiddleware:
    """Resolve pilot user identity from the MCP URL token."""

    def __init__(self, app: Any, *, repository_factory: RepositoryFactory, streamable_path: str) -> None:
        self.app = app
        self.repository_factory = repository_factory
        self.streamable_path = streamable_path.rstrip("/") or "/"

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", "")) or "/"
        if not self._is_streamable_request(path):
            await self.app(scope, receive, send)
            return

        token_value = self._query_value(scope, "u")
        record = self.repository_factory.resolve_token(token_value)
        require_token = os.getenv("OCI_MON_MCP_REQUIRE_TOKEN", "0") == "1"
        if require_token and record is None:
            await self._send_json(send, 401, {"error": "Missing or invalid MCP user token."})
            return

        identity_token = None
        if record is not None:
            identity_token = set_current_identity(
                RequestIdentity(
                    profile_id=str(record["profile_id"]),
                    user_id=str(record["user_id"]),
                    token=token_value,
                    client_type=str(record.get("client_type", "")) or None,
                )
            )
        try:
            await self.app(scope, receive, send)
        finally:
            if identity_token is not None:
                reset_current_identity(identity_token)

    def _is_streamable_request(self, path: str) -> bool:
        normalized = path.rstrip("/") or "/"
        return normalized == self.streamable_path

    @staticmethod
    def _query_value(scope: dict[str, Any], key: str) -> str | None:
        raw = scope.get("query_string", b"")
        if not raw:
            return None
        from urllib.parse import parse_qs

        parsed = parse_qs(raw.decode("utf-8", errors="ignore"), keep_blank_values=True)
        values = parsed.get(key)
        if not values:
            return None
        return values[0] or None

    @staticmethod
    async def _send_json(send: Any, status_code: int, payload: dict[str, Any]) -> None:
        body = (
            "{\n"
            + f'  "error": "{payload.get("error", "Unauthorized")}"\n'
            + "}\n"
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _configure_access_log_filter() -> None:
    """Suppress expected MCP probe noise so operator logs stay actionable."""
    if os.getenv("OCI_MON_MCP_SUPPRESS_EXPECTED_MCP_PROBE_LOGS", "1") != "1":
        return
    logger = logging.getLogger("uvicorn.access")
    logger.addFilter(_ExpectedMcpAccessFilter())


def _effective_profile_id(profile_id: str) -> str:
    current = get_current_identity()
    if current is not None:
        return current.profile_id
    return profile_id


def _direct_initial_setup_guard(
    profile_id: str,
    *,
    allow_explicit_setup: bool = False,
    region: str | None = None,
    compartment_name: str | None = None,
) -> dict[str, Any] | None:
    """Block ambiguous first-time direct setup tool calls in pilot mode."""
    if os.getenv("OCI_MON_MCP_REQUIRE_TOKEN", "0") != "1":
        return None
    effective_profile_id = _effective_profile_id(profile_id)
    profile = SERVICE.repository.get_profile(effective_profile_id)
    if profile.get("region") or profile.get("default_compartment_name"):
        return None
    if allow_explicit_setup and (region or "").strip() and (compartment_name or "").strip():
        return None
    return asdict(
        AssistantResponse(
            status="needs_clarification",
            interpretation="Default region and compartment are not configured yet.",
            clarifications=[
                ClarificationQuestion(
                    id="region",
                    question="What OCI region should I save as the default?",
                ),
                ClarificationQuestion(
                    id="compartment_name",
                    question="What compartment should I save as the default?",
                ),
            ],
            summary=(
                "Before I save defaults for a new profile, I need the user to explicitly provide "
                "the region and compartment. Do not infer them."
            ),
        )
    )


def create_mcp_server() -> Any:
    """Create the FastMCP server when the dependency is available."""
    if FastMCP is None:
        return None

    mcp = FastMCP(
        "OCI Monitoring MCP",
        instructions=(
            "Use this server to query OCI Monitoring metrics for compute instances. "
            "Ask clarifying questions before execution when the request is ambiguous. "
            "Never infer a default OCI region or default compartment for a new profile."
        ),
        host=os.getenv("OCI_MON_MCP_HOST", "0.0.0.0"),
        port=int(os.getenv("OCI_MON_MCP_PORT", "8000")),
        mount_path=os.getenv("OCI_MON_MCP_MOUNT_PATH", "/"),
        streamable_http_path=os.getenv("OCI_MON_MCP_STREAMABLE_HTTP_PATH", "/mcp"),
        json_response=os.getenv("OCI_MON_MCP_JSON_RESPONSE", "1") == "1",
        stateless_http=os.getenv("OCI_MON_MCP_STATELESS_HTTP", "1") == "1",
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
    def monitoring_assistant(query: str, profile_id: str = "default") -> AssistantToolResponse:
        """Interpret a monitoring question and return a structured response."""
        return asdict(SERVICE.handle_query(query=query, profile_id=_effective_profile_id(profile_id)))

    @mcp.tool()
    def setup_default_context(
        region: str,
        compartment_name: str,
        compartment_id: str = "",
        profile_id: str = "default",
    ) -> AssistantToolResponse:
        """Persist the default region and compartment for a user profile."""
        blocked = _direct_initial_setup_guard(
            profile_id,
            allow_explicit_setup=True,
            region=region,
            compartment_name=compartment_name,
        )
        if blocked is not None:
            return blocked
        return asdict(
            SERVICE.setup_default_context(
                region=region,
                compartment_name=compartment_name,
                compartment_id=compartment_id or None,
                profile_id=_effective_profile_id(profile_id),
            )
        )

    @mcp.tool()
    def change_default_context(
        region: str = "",
        compartment_name: str = "",
        compartment_id: str = "",
        profile_id: str = "default",
    ) -> AssistantToolResponse:
        """Update the stored default region and/or compartment."""
        blocked = _direct_initial_setup_guard(profile_id)
        if blocked is not None:
            return blocked
        return asdict(
            SERVICE.change_default_context(
                region=region or None,
                compartment_name=compartment_name or None,
                compartment_id=compartment_id or None,
                profile_id=_effective_profile_id(profile_id),
            )
        )

    @mcp.tool()
    def list_saved_templates(profile_id: str = "default") -> TemplateListingResponse:
        """List successful saved query templates for the current profile scope."""
        return SERVICE.list_saved_templates(profile_id=_effective_profile_id(profile_id))

    @mcp.tool()
    def discover_accessible_compartments(
        region: str = "",
        profile_id: str = "default",
    ) -> CompartmentDiscoveryResponse:
        """List accessible compartments for the current auth mode."""
        return SERVICE.discover_accessible_compartments(
            region=region,
            profile_id=_effective_profile_id(profile_id),
        )

    @mcp.tool()
    def configure_auth_fallback(
        config_path: str = "~/.oci/config",
        profile_name: str = "DEFAULT",
        profile_id: str = "default",
    ) -> AssistantToolResponse:
        """Persist OCI config fallback settings for this profile."""
        return asdict(
            SERVICE.configure_auth_fallback(
                config_path=config_path,
                profile_name=profile_name,
                profile_id=_effective_profile_id(profile_id),
            )
        )

    @mcp.tool()
    def use_instance_principals(profile_id: str = "default") -> AssistantToolResponse:
        """Switch the profile back to Instance Principals auth."""
        return asdict(SERVICE.use_instance_principals(profile_id=_effective_profile_id(profile_id)))

    return mcp


def create_streamable_http_app(
    mcp: Any | None = None,
    repository_factory: RepositoryFactory | None = None,
) -> Any:
    """Build the streamable HTTP app with token-aware identity middleware."""
    server = mcp or create_mcp_server()
    if server is None:
        return None
    app = server.streamable_http_app()
    app.add_middleware(
        IdentityMiddleware,
        repository_factory=repository_factory or REPOSITORY_FACTORY,
        streamable_path=os.getenv("OCI_MON_MCP_STREAMABLE_HTTP_PATH", "/mcp"),
    )
    return app


async def _serve_streamable_http(server: Any, app: Any) -> None:
    import uvicorn

    config = uvicorn.Config(
        app,
        host=server.settings.host,
        port=server.settings.port,
        log_level=server.settings.log_level.lower(),
    )
    http_server = uvicorn.Server(config)
    await http_server.serve()


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
        if transport == "streamable-http":
            import anyio

            app = create_streamable_http_app(server, repository_factory=REPOSITORY_FACTORY)
            anyio.run(_serve_streamable_http, server, app)
            return
        server.run(transport=transport)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
