"""Custom errors for OCI Monitoring MCP runtime flows."""

from __future__ import annotations


class OciMonError(RuntimeError):
    """Base runtime error for prototype-specific failures."""


class AuthFallbackSuggestedError(OciMonError):
    """Raised when Instance Principals fail and config fallback should be offered."""

    def __init__(
        self,
        message: str,
        *,
        config_path: str = "~/.oci/config",
        profile_name: str = "DEFAULT",
    ) -> None:
        super().__init__(message)
        self.config_path = config_path
        self.profile_name = profile_name


class CompartmentResolutionError(OciMonError):
    """Raised when a compartment cannot be resolved safely."""

    def __init__(self, message: str, *, options: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.options = options or []


class InstanceResolutionError(OciMonError):
    """Raised when an instance name cannot be resolved safely."""

    def __init__(self, message: str, *, options: list[dict[str, str]] | None = None) -> None:
        super().__init__(message)
        self.options = options or []


class DependencyMissingError(OciMonError):
    """Raised when an optional runtime dependency is required but unavailable."""
