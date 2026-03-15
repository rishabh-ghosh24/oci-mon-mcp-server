"""Shared OCI auth, client construction, and context discovery helpers."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

from .errors import (
    AuthFallbackSuggestedError,
    CompartmentResolutionError,
    DependencyMissingError,
    InstanceResolutionError,
)


@dataclass(slots=True)
class OciSession:
    """Resolved OCI runtime context and requested clients."""

    oci: Any
    region: str
    auth_mode: str
    tenancy_id: str
    monitoring_client: Any | None = None
    compute_client: Any | None = None
    identity_client: Any | None = None


class OciClientFactory:
    """Build OCI SDK clients for Instance Principals or config-file auth."""

    def _import_oci(self) -> Any:
        try:
            return importlib.import_module("oci")
        except ImportError as exc:  # pragma: no cover - exercised in runtime environments
            raise DependencyMissingError(
                "The 'oci' package is not installed. Install project dependencies on the VM "
                "before running live OCI queries."
            ) from exc

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
        """Create the requested OCI clients for the chosen auth mode."""
        oci = self._import_oci()
        config_fallback = config_fallback or {}

        if auth_mode == "instance_principal":
            try:
                signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
            except Exception as exc:  # pragma: no cover - environment-dependent
                raise AuthFallbackSuggestedError(
                    "Instance Principals authentication failed. Switch to OCI config fallback "
                    "if this VM is not configured with a dynamic group and matching policies."
                ) from exc

            session = OciSession(
                oci=oci,
                region=region,
                auth_mode=auth_mode,
                tenancy_id=getattr(signer, "tenancy_id", "unknown"),
            )
            if include_monitoring:
                session.monitoring_client = oci.monitoring.MonitoringClient(config={}, signer=signer)
                session.monitoring_client.base_client.set_region(region)
            if include_compute:
                session.compute_client = oci.core.ComputeClient(config={}, signer=signer)
                session.compute_client.base_client.set_region(region)
            if include_identity:
                session.identity_client = oci.identity.IdentityClient(config={}, signer=signer)
                session.identity_client.base_client.set_region(region)
            return session

        config_path = config_fallback.get("config_path", "~/.oci/config")
        profile_name = config_fallback.get("profile", "DEFAULT")
        try:
            config = oci.config.from_file(config_path, profile_name)
        except Exception as exc:  # pragma: no cover - environment-dependent
            raise AuthFallbackSuggestedError(
                (
                    f"OCI config fallback could not be loaded from {config_path} with profile "
                    f"{profile_name}. Check the config path and profile name."
                ),
                config_path=config_path,
                profile_name=profile_name,
            ) from exc

        config["region"] = region
        session = OciSession(
            oci=oci,
            region=region,
            auth_mode=auth_mode,
            tenancy_id=config.get("tenancy", "unknown"),
        )
        if include_monitoring:
            session.monitoring_client = oci.monitoring.MonitoringClient(config)
        if include_compute:
            session.compute_client = oci.core.ComputeClient(config)
        if include_identity:
            session.identity_client = oci.identity.IdentityClient(config)
        return session


class OciContextResolver:
    """Resolve compartment context and list accessible compartments."""

    def __init__(self, client_factory: OciClientFactory | None = None) -> None:
        self.client_factory = client_factory or OciClientFactory()

    def list_accessible_compartments(
        self,
        *,
        region: str,
        auth_mode: str,
        config_fallback: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """List accessible compartments in the current tenancy and region."""
        session = self.client_factory.build_session(
            region=region,
            auth_mode=auth_mode,
            config_fallback=config_fallback,
            include_identity=True,
        )
        oci = session.oci
        assert session.identity_client is not None
        response = oci.pagination.list_call_get_all_results(
            session.identity_client.list_compartments,
            compartment_id=session.tenancy_id,
            compartment_id_in_subtree=True,
            access_level="ACCESSIBLE",
        )
        tenancy = session.identity_client.get_tenancy(session.tenancy_id).data
        compartments = [
            {
                "name": tenancy.name,
                "id": tenancy.id,
                "lifecycle_state": tenancy.lifecycle_state,
                "is_root": "true",
            }
        ]
        for compartment in response.data:
            if getattr(compartment, "lifecycle_state", "") != "ACTIVE":
                continue
            compartments.append(
                {
                    "name": compartment.name,
                    "id": compartment.id,
                    "lifecycle_state": compartment.lifecycle_state,
                    "is_root": "false",
                }
            )
        compartments.sort(key=lambda item: (item["name"].lower(), item["id"]))
        return {
            "tenancy_id": session.tenancy_id,
            "region": region,
            "count": len(compartments),
            "compartments": compartments,
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
        """Resolve a compartment name to a stable OCID."""
        listing = self.list_accessible_compartments(
            region=region,
            auth_mode=auth_mode,
            config_fallback=config_fallback,
        )
        compartments = listing["compartments"]
        if compartment_id:
            for item in compartments:
                if item["id"] == compartment_id:
                    return {
                        "tenancy_id": listing["tenancy_id"],
                        "compartment_id": item["id"],
                        "compartment_name": item["name"],
                    }
            raise CompartmentResolutionError(
                f"The stored compartment OCID {compartment_id} is not accessible in region {region}."
            )

        exact_matches = [
            item for item in compartments if item["name"].lower() == compartment_name.lower()
        ]
        if len(exact_matches) == 1:
            item = exact_matches[0]
            return {
                "tenancy_id": listing["tenancy_id"],
                "compartment_id": item["id"],
                "compartment_name": item["name"],
            }
        if len(exact_matches) > 1:
            raise CompartmentResolutionError(
                f"Multiple accessible compartments are named '{compartment_name}'.",
                options=exact_matches,
            )

        partial_matches = [
            item for item in compartments if compartment_name.lower() in item["name"].lower()
        ]
        if len(partial_matches) == 1:
            item = partial_matches[0]
            return {
                "tenancy_id": listing["tenancy_id"],
                "compartment_id": item["id"],
                "compartment_name": item["name"],
            }
        if len(partial_matches) > 1:
            raise CompartmentResolutionError(
                f"Multiple compartments partially match '{compartment_name}'.",
                options=partial_matches,
            )
        raise CompartmentResolutionError(
            f"No accessible compartment matched '{compartment_name}' in region {region}."
        )

    def resolve_instance_name(
        self,
        *,
        region: str,
        auth_mode: str,
        compartment_id: str,
        instance_name: str,
        config_fallback: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Resolve an instance display name by exact or case-insensitive partial match."""
        session = self.client_factory.build_session(
            region=region,
            auth_mode=auth_mode,
            config_fallback=config_fallback,
            include_compute=True,
        )
        assert session.compute_client is not None
        oci = session.oci
        response = oci.pagination.list_call_get_all_results(
            session.compute_client.list_instances,
            compartment_id=compartment_id,
        )
        instances = [
            {
                "id": instance.id,
                "name": instance.display_name,
                "lifecycle_state": instance.lifecycle_state,
            }
            for instance in response.data
        ]
        exact = [item for item in instances if item["name"] == instance_name]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise InstanceResolutionError(
                f"Multiple instances are named '{instance_name}'.",
                options=exact,
            )
        partial = [
            item for item in instances if instance_name.lower() in item["name"].lower()
        ]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise InstanceResolutionError(
                f"Multiple instances partially match '{instance_name}'.",
                options=partial,
            )
        raise InstanceResolutionError(
            f"No instance named '{instance_name}' was found in the selected compartment."
        )
