"""Core application logic for the OCI Monitoring MCP prototype."""

from __future__ import annotations

import os
import re
from dataclasses import asdict
from typing import Any

from .artifacts import ArtifactManager
from .errors import (
    AuthFallbackSuggestedError,
    CompartmentResolutionError,
    DependencyMissingError,
    InstanceResolutionError,
)
from .execution import MonitoringExecutionAdapter, build_default_execution_adapter
from .models import (
    AssistantDetails,
    AssistantResponse,
    ChartBlock,
    ClarificationQuestion,
    ExecutionResult,
    ParsedQuery,
    QueryExecutionRequest,
    TableBlock,
    ThresholdLine,
)
from .oci_support import OciContextResolver
from .repository import JsonRepository, utc_now_iso


SUPPORTED_TIME_RANGES: dict[str, str] = {
    "15m": "1m",
    "30m": "1m",
    "1h": "5m",
    "6h": "5m",
    "24h": "1h",
    "7d": "1d",
}

METRIC_CONFIGS: dict[str, dict[str, Any]] = {
    "cpu": {
        "label": "CPU utilization",
        "namespace": "oci_computeagent",
        "metric_names": ["CpuUtilization"],
        "y_axis": "cpu_utilization_percent",
    },
    "memory": {
        "label": "Memory utilization",
        "namespace": "oci_computeagent",
        "metric_names": ["MemoryUtilization"],
        "y_axis": "memory_utilization_percent",
    },
    "cpu_memory": {
        "label": "CPU and memory utilization",
        "namespace": "oci_computeagent",
        "metric_names": ["CpuUtilization", "MemoryUtilization"],
        "y_axis": "utilization_percent",
    },
    "disk_io_throughput": {
        "label": "Disk I/O throughput",
        "namespace": "oci_computeagent",
        "metric_names": ["DiskBytesRead", "DiskBytesWritten"],
        "y_axis": "disk_io_bytes",
    },
    "disk_io_iops": {
        "label": "Disk I/O IOPS",
        "namespace": "oci_computeagent",
        "metric_names": ["DiskIopsRead", "DiskIopsWritten"],
        "y_axis": "disk_io_iops",
    },
}

NEW_QUERY_HINTS = (
    "show",
    "list",
    "find",
    "top",
    "worst",
    "what",
    "which",
    "did",
    "now do the same",
)

DEFAULT_TABLE_LIMIT = 20
DEFAULT_CHART_LIMIT = 10


class MonitoringAssistantService:
    """Main application service for the prototype."""

    def __init__(
        self,
        repository: JsonRepository | None = None,
        execution_adapter: MonitoringExecutionAdapter | None = None,
        context_resolver: OciContextResolver | None = None,
        artifact_manager: ArtifactManager | None = None,
    ) -> None:
        self.repository = repository or JsonRepository()
        self.execution_adapter = execution_adapter or build_default_execution_adapter()
        self.context_resolver = context_resolver or OciContextResolver()
        self.artifact_manager = artifact_manager or ArtifactManager(
            base_url=os.getenv("OCI_MON_MCP_ARTIFACT_BASE_URL"),
            host=os.getenv("OCI_MON_MCP_ARTIFACT_HOST", "0.0.0.0"),
            port=int(os.getenv("OCI_MON_MCP_ARTIFACT_PORT", "8765")),
            auto_start=os.getenv("OCI_MON_MCP_ARTIFACTS_ENABLED", "1") != "0",
        )

    def setup_default_context(
        self,
        *,
        region: str,
        compartment_name: str,
        compartment_id: str | None = None,
        profile_id: str = "default",
    ) -> AssistantResponse:
        """Persist the user's default region and compartment."""
        normalized_region = region.strip()
        normalized_name = compartment_name.strip()
        normalized_id = (compartment_id or "").strip() or None
        if not normalized_region or not normalized_name:
            clarifications: list[ClarificationQuestion] = []
            if not normalized_region:
                clarifications.append(
                    ClarificationQuestion(
                        id="region",
                        question="What OCI region should I save as the default?",
                    )
                )
            if not normalized_name:
                clarifications.append(
                    ClarificationQuestion(
                        id="compartment_name",
                        question="What compartment should I save as the default?",
                    )
                )
            return AssistantResponse(
                status="needs_clarification",
                interpretation="Default context setup is missing required values.",
                clarifications=clarifications,
                summary="I need both a region and a compartment before I can save the default context.",
            )

        profile = self.repository.get_profile(profile_id)
        config_fallback = profile.get("config_fallback", {})
        auth_mode = profile.get("auth_mode", "instance_principal")
        try:
            resolved = self.context_resolver.resolve_compartment(
                region=normalized_region,
                auth_mode=auth_mode,
                compartment_name=normalized_name,
                compartment_id=normalized_id,
                config_fallback=config_fallback,
            )
            normalized_name = resolved["compartment_name"]
            normalized_id = resolved["compartment_id"]
            available_compartments = self.context_resolver.list_accessible_compartments(
                region=normalized_region,
                auth_mode=auth_mode,
                config_fallback=config_fallback,
            )["compartments"]
        except DependencyMissingError as exc:
            return AssistantResponse(
                status="error",
                interpretation=(
                    f"Could not validate default context for region {normalized_region} and "
                    f"compartment {normalized_name}."
                ),
                summary=f"{exc} Default context was not saved.",
            )
        except AuthFallbackSuggestedError as exc:
            return AssistantResponse(
                status="error",
                interpretation=(
                    "Could not validate the default context because Instance Principals "
                    "authentication failed."
                ),
                summary=(
                    "Instance Principals authentication failed while validating the default "
                    f"context. {exc} Default context was not saved. If you want OCI config "
                    "fallback, explicitly run configure_auth_fallback."
                ),
            )
        except CompartmentResolutionError as exc:
            option_names = [option["name"] for option in exc.options[:10]]
            question = "What exact compartment name or OCID should I save as the default?"
            if option_names:
                question = (
                    f"{question} Matching options: {', '.join(option_names)}."
                )
            return AssistantResponse(
                status="needs_clarification",
                interpretation=(
                    f"Could not safely resolve default compartment {normalized_name} in "
                    f"region {normalized_region}."
                ),
                clarifications=[ClarificationQuestion(id="compartment_name", question=question)],
                summary=f"{exc} Default context was not saved.",
            )

        profile = self.repository.set_default_context(
            profile_id,
            region=normalized_region,
            compartment_name=normalized_name,
            compartment_id=normalized_id,
            auth_mode=auth_mode,
            tenancy_id=resolved["tenancy_id"],
            available_compartments=available_compartments,
        )
        return AssistantResponse(
            status="success",
            interpretation=(
                f"Saved default context for profile {profile_id}: "
                f"region {profile['region']} and compartment "
                f"{profile['default_compartment_name']}."
            ),
            summary=(
                f"Default region is {profile['region']} and default compartment is "
                f"{profile['default_compartment_name']}."
            ),
            details=AssistantDetails(
                scope={
                    "profile_id": profile_id,
                    "region": profile["region"],
                    "compartment_name": profile["default_compartment_name"],
                    "compartment_id": profile["default_compartment_id"],
                }
            ),
        )

    def configure_auth_fallback(
        self,
        *,
        config_path: str = "~/.oci/config",
        profile_name: str = "DEFAULT",
        profile_id: str = "default",
    ) -> AssistantResponse:
        """Persist OCI config fallback settings for this profile."""
        profile = self.repository.set_auth_fallback(
            profile_id,
            config_path=config_path,
            profile_name=profile_name,
        )
        return AssistantResponse(
            status="success",
            interpretation=(
                f"Saved OCI config fallback for profile {profile_id} using "
                f"{profile['config_fallback']['config_path']} and profile "
                f"{profile['config_fallback']['profile']}."
            ),
            summary=(
                "OCI config fallback is configured. Future live queries will use the OCI config "
                "profile unless you switch back to Instance Principals."
            ),
            details=AssistantDetails(
                scope={
                    "profile_id": profile_id,
                    "auth_mode": profile["auth_mode"],
                    "config_fallback": profile["config_fallback"],
                }
            ),
        )

    def use_instance_principals(self, profile_id: str = "default") -> AssistantResponse:
        """Switch the profile back to Instance Principals auth."""
        profile = self.repository.get_profile(profile_id)
        profile["auth_mode"] = "instance_principal"
        self.repository.update_profile(profile_id, profile)
        return AssistantResponse(
            status="success",
            interpretation=f"Switched profile {profile_id} back to Instance Principals auth.",
            summary="Future live queries will attempt Instance Principals first.",
            details=AssistantDetails(
                scope={"profile_id": profile_id, "auth_mode": profile["auth_mode"]}
            ),
        )

    def discover_accessible_compartments(
        self,
        *,
        region: str = "",
        profile_id: str = "default",
    ) -> dict[str, Any]:
        """List accessible compartments using the current auth mode."""
        profile = self.repository.get_profile(profile_id)
        if not profile.get("region") or not profile.get("default_compartment_name"):
            return {
                "status": "needs_clarification",
                "summary": "Default region and compartment are not configured yet.",
                "question": (
                    "What OCI region and default compartment should I save for this profile? "
                    "Use setup_default_context first instead of guessing a region in tool calls."
                ),
                "compartments": [],
            }
        resolved_region = region.strip() or profile.get("region")
        if not resolved_region:
            return {
                "status": "needs_clarification",
                "summary": "Provide a region or save a default region first.",
                "compartments": [],
            }
        try:
            listing = self.context_resolver.list_accessible_compartments(
                region=resolved_region,
                auth_mode=profile.get("auth_mode", "instance_principal"),
                config_fallback=profile.get("config_fallback", {}),
            )
        except AuthFallbackSuggestedError as exc:
            return {
                "status": "error",
                "summary": (
                    "Instance Principals failed while listing accessible compartments. "
                    f"{exc} If you want OCI config fallback, explicitly run "
                    "configure_auth_fallback."
                ),
                "compartments": [],
            }
        except DependencyMissingError as exc:
            return {
                "status": "error",
                "summary": str(exc),
                "compartments": [],
            }
        profile["available_compartments"] = listing["compartments"]
        profile["tenancy_id"] = listing["tenancy_id"]
        self.repository.update_profile(profile_id, profile)
        return listing

    def change_default_context(
        self,
        *,
        region: str | None = None,
        compartment_name: str | None = None,
        compartment_id: str | None = None,
        profile_id: str = "default",
    ) -> AssistantResponse:
        """Change the stored context for a profile."""
        profile = self.repository.get_profile(profile_id)
        updated_region = region.strip() if region else profile.get("region")
        updated_name = (
            compartment_name.strip() if compartment_name else profile.get("default_compartment_name")
        )
        updated_id = (
            compartment_id.strip()
            if compartment_id
            else profile.get("default_compartment_id")
        )
        if not updated_region or not updated_name:
            return AssistantResponse(
                status="needs_clarification",
                interpretation="Default context update is missing required values.",
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
                summary="I need both a region and a compartment to update the default context.",
            )
        return self.setup_default_context(
            region=updated_region,
            compartment_name=updated_name,
            compartment_id=updated_id,
            profile_id=profile_id,
        )

    def list_saved_templates(self, profile_id: str = "default") -> dict[str, Any]:
        """Return saved successful templates for the current scoped environment."""
        templates = self.repository.list_templates(profile_id=profile_id)
        return {
            "profile_id": profile_id,
            "count": len(templates),
            "templates": templates,
        }

    def handle_query(self, query: str, profile_id: str = "default") -> AssistantResponse:
        """Interpret a user query, ask clarifications, and dispatch execution."""
        normalized_query = query.strip()
        if not normalized_query:
            return AssistantResponse(
                status="error",
                interpretation="No query text was provided.",
                summary="Provide a monitoring question in natural language.",
            )

        profile = self.repository.get_profile(profile_id)
        pending = profile.get("pending_clarification")
        if pending is not None:
            pending_resolution = self._resolve_pending(
                profile_id=profile_id,
                profile=profile,
                pending=pending,
                answer=normalized_query,
            )
            if pending_resolution is not None:
                return pending_resolution

        profile = self.repository.get_profile(profile_id)
        if not profile.get("region") or not profile.get("default_compartment_name"):
            return self._request_initial_context(profile_id, normalized_query)

        parse_result = self._parse_query(
            query=normalized_query,
            profile_id=profile_id,
            profile=profile,
        )
        if isinstance(parse_result, AssistantResponse):
            return parse_result
        return self._execute_parsed_query(
            profile_id=profile_id,
            profile=profile,
            parsed=parse_result,
        )

    def _request_initial_context(self, profile_id: str, query: str) -> AssistantResponse:
        pending = {
            "kind": "setup",
            "created_at": utc_now_iso(),
            "original_query": query,
            "questions": [
                {
                    "id": "region",
                    "question": "What OCI region should I use by default? Example: us-ashburn-1.",
                },
                {
                    "id": "compartment_name",
                    "question": "What default compartment should I use?",
                },
            ],
            "partial": {},
        }
        self.repository.set_pending_clarification(profile_id, pending)
        return AssistantResponse(
            status="needs_clarification",
            interpretation="Default region and compartment are not configured yet.",
            clarifications=[
                ClarificationQuestion(**question) for question in pending["questions"]
            ],
            summary=(
                "Before I can query metrics, I need your default region and default compartment. "
                "You can answer here or use setup_default_context."
            ),
        )

    def _resolve_pending(
        self,
        *,
        profile_id: str,
        profile: dict[str, Any],
        pending: dict[str, Any],
        answer: str,
    ) -> AssistantResponse | None:
        if self._looks_like_new_query(answer):
            self.repository.set_pending_clarification(profile_id, None)
            return None

        if pending.get("kind") == "setup":
            return self._resolve_setup_pending(profile_id, pending, answer)

        if pending.get("kind") == "query":
            return self._resolve_query_pending(profile_id, profile, pending, answer)

        self.repository.set_pending_clarification(profile_id, None)
        return None

    def _resolve_setup_pending(
        self,
        profile_id: str,
        pending: dict[str, Any],
        answer: str,
    ) -> AssistantResponse:
        partial = dict(pending.get("partial", {}))
        region_match = re.search(r"\b([a-z]{2}-[a-z]+-\d+)\b", answer, re.IGNORECASE)
        if region_match:
            partial["region"] = region_match.group(1)

        compartment_match = re.search(
            r"compartment(?:\s+name)?\s*[:=]\s*(.+)",
            answer,
            re.IGNORECASE,
        )
        if compartment_match:
            partial["compartment_name"] = compartment_match.group(1).strip().rstrip(".")
        elif not region_match and not partial.get("compartment_name"):
            partial["compartment_name"] = answer.strip().rstrip(".")

        missing_questions: list[ClarificationQuestion] = []
        if not partial.get("region"):
            missing_questions.append(
                ClarificationQuestion(
                    id="region",
                    question="What OCI region should I use by default? Example: us-ashburn-1.",
                )
            )
        if not partial.get("compartment_name"):
            missing_questions.append(
                ClarificationQuestion(
                    id="compartment_name",
                    question="What default compartment should I use?",
                )
            )

        if missing_questions:
            pending["partial"] = partial
            pending["questions"] = [asdict(question) for question in missing_questions]
            self.repository.set_pending_clarification(profile_id, pending)
            return AssistantResponse(
                status="needs_clarification",
                interpretation="Default context setup is still missing required values.",
                clarifications=missing_questions,
                summary="I still need the missing setup details before I can run monitoring queries.",
            )

        self.repository.set_pending_clarification(profile_id, None)
        return self.setup_default_context(
            region=partial["region"],
            compartment_name=partial["compartment_name"],
            profile_id=profile_id,
        )

    def _resolve_query_pending(
        self,
        profile_id: str,
        profile: dict[str, Any],
        pending: dict[str, Any],
        answer: str,
    ) -> AssistantResponse:
        partial = dict(pending.get("partial", {}))
        unresolved = list(partial.get("unresolved", []))
        original_query = pending.get("original_query", "")

        if "metric_choice" in unresolved:
            metric_key = self._extract_metric(answer)
            if metric_key == "storage":
                self.repository.set_pending_clarification(profile_id, None)
                return self._storage_not_available_response(profile)
            if metric_key in {"cpu", "memory"}:
                partial["metric_key"] = metric_key
                unresolved.remove("metric_choice")

        if "threshold" in unresolved:
            threshold = self._extract_threshold(answer)
            if threshold is not None:
                partial["threshold"] = threshold
                unresolved.remove("threshold")

        if "io_type" in unresolved:
            io_type = self._extract_io_type(answer)
            if io_type == "disk":
                partial["metric_key"] = partial.get("metric_key", "disk_io_throughput")
                unresolved.remove("io_type")
            elif io_type in {"network", "other"}:
                self.repository.set_pending_clarification(profile_id, None)
                return self._io_out_of_scope_response(profile)

        if "io_measure" in unresolved:
            io_measure = self._extract_io_measure(answer)
            if io_measure is not None:
                partial["io_measure"] = io_measure
                unresolved.remove("io_measure")

        if "io_direction" in unresolved:
            io_direction = self._extract_io_direction(answer)
            if io_direction is not None:
                partial["io_direction"] = io_direction
                unresolved.remove("io_direction")

        if "instance_name" in unresolved and answer.strip():
            partial["instance_name"] = answer.strip().strip('"').strip("'")
            unresolved.remove("instance_name")

        partial["unresolved"] = unresolved
        if unresolved:
            pending["partial"] = partial
            pending["questions"] = [
                asdict(question)
                for question in self._questions_for_unresolved(
                    unresolved=unresolved,
                    profile_id=profile_id,
                    profile=profile,
                )
            ]
            self.repository.set_pending_clarification(profile_id, pending)
            return AssistantResponse(
                status="needs_clarification",
                interpretation=f"More clarification is needed before I can run: {original_query}",
                clarifications=[
                    ClarificationQuestion(**question) for question in pending["questions"]
                ],
                summary="I still need the remaining clarification before I can run this query.",
            )

        self.repository.set_pending_clarification(profile_id, None)
        completed_query = self._parsed_from_pending(original_query, partial, profile)
        if isinstance(completed_query, AssistantResponse):
            return completed_query
        return self._execute_parsed_query(
            profile_id=profile_id,
            profile=profile,
            parsed=completed_query,
        )

    def _parse_query(
        self,
        *,
        query: str,
        profile_id: str,
        profile: dict[str, Any],
    ) -> ParsedQuery | AssistantResponse:
        normalized = " ".join(query.lower().split())
        last_context = profile.get("last_resolved_context", {})
        aggregation = self._extract_aggregation(normalized)

        if any(token in normalized for token in ("database", "vcn", "anomal", "alert")):
            return AssistantResponse(
                status="error",
                interpretation=f"Interpreted as an out-of-scope prototype request: {query}",
                summary=(
                    "That request is outside the current compute-focused prototype scope. "
                    "I can help with compute CPU, memory, named-instance trends, or disk I/O "
                    "after clarification."
                ),
            )

        if "storage" in normalized:
            return self._storage_not_available_response(profile)

        if " io" in f" {normalized}" or normalized.endswith("io") or "disk io" in normalized:
            return self._parse_io_query(query=query, normalized=normalized, profile_id=profile_id)

        metric_key = self._extract_metric(normalized)
        if metric_key == "storage":
            return self._storage_not_available_response(profile)

        if "worst performing" in normalized:
            if metric_key not in {"cpu", "memory"}:
                preference = self.repository.get_preference(
                    profile_id, "worst_performing_compute_instances"
                )
                question = "Do you mean CPU, memory, storage usage, or another metric?"
                if preference is not None:
                    question = (
                        f"Last time this meant {preference['resolved_metric']}. "
                        f"Use {preference['resolved_metric']} again, or do you want CPU, memory, "
                        "storage usage, or another metric?"
                    )
                pending = {
                    "kind": "query",
                    "created_at": utc_now_iso(),
                    "original_query": query,
                    "questions": [{"id": "metric_choice", "question": question}],
                    "partial": {
                        "intent": "worst_performing",
                        "time_range": self._extract_time_range(normalized) or "1h",
                        "aggregation": aggregation,
                        "unresolved": ["metric_choice"],
                    },
                }
                self.repository.set_pending_clarification(profile_id, pending)
                return AssistantResponse(
                    status="needs_clarification",
                    interpretation=(
                        "Interpreted as a worst-performing compute request, but the metric is "
                        "ambiguous."
                    ),
                    clarifications=[ClarificationQuestion(id="metric_choice", question=question)],
                    summary="I need the metric before I can run the worst-performing compute query.",
                )
            return self._build_parsed_query(
                source_query=query,
                intent="worst_performing",
                metric_key=metric_key,
                time_range=self._extract_time_range(normalized) or "1h",
                aggregation=aggregation,
                top_n=self._extract_top_n(normalized) or 10,
                learned_intent_key="worst_performing_compute_instances",
            )

        if "top " in normalized:
            if metric_key not in {"cpu", "memory"}:
                return AssistantResponse(
                    status="needs_clarification",
                    interpretation="Interpreted as a top-N compute request with an ambiguous metric.",
                    clarifications=[
                        ClarificationQuestion(
                            id="metric_choice",
                            question="Do you want top compute instances by CPU or memory?",
                        )
                    ],
                    summary="I need the metric before I can rank compute instances.",
                )
            return self._build_parsed_query(
                source_query=query,
                intent="top_n",
                metric_key=metric_key,
                time_range=self._extract_time_range(normalized) or "1h",
                aggregation=aggregation,
                top_n=self._extract_top_n(normalized) or 10,
            )

        if "trend for" in normalized or "trend of" in normalized:
            instance_name = self._extract_instance_name(query)
            if not instance_name:
                return AssistantResponse(
                    status="needs_clarification",
                    interpretation="Interpreted as a named-instance trend request without a clear instance.",
                    clarifications=[
                        ClarificationQuestion(
                            id="instance_name",
                            question="Which compute instance do you want the metric trend for?",
                        )
                    ],
                    summary="I need the instance name before I can show a named-instance trend.",
                )
            if metric_key not in {"cpu", "memory"}:
                metric_key = last_context.get("metric_key") or "cpu"
            return self._build_parsed_query(
                source_query=query,
                intent="named_trend",
                metric_key=metric_key,
                time_range=self._extract_time_range(normalized) or last_context.get("time_range") or "1h",
                aggregation=aggregation,
                instance_name=instance_name,
            )

        if normalized.startswith("now do the same"):
            inherited = dict(last_context)
            if not inherited:
                return AssistantResponse(
                    status="error",
                    interpretation="Interpreted as a follow-up request, but no prior query context exists.",
                    summary="There is no earlier query context to reuse yet.",
                )
            follow_up_metric = self._extract_metric(normalized) or inherited.get("metric_key") or "cpu"
            return self._build_parsed_query(
                source_query=query,
                intent=inherited.get("intent", "threshold"),
                metric_key=follow_up_metric,
                time_range=self._extract_time_range(normalized) or inherited.get("time_range") or "1h",
                aggregation=aggregation or inherited.get("aggregation", "max"),
                threshold=self._extract_threshold(normalized) or inherited.get("threshold"),
                top_n=inherited.get("top_n"),
            )

        if "high" in normalized and metric_key in {"cpu", "memory"}:
            threshold = self._extract_threshold(normalized)
            if threshold is None:
                pending = {
                    "kind": "query",
                    "created_at": utc_now_iso(),
                    "original_query": query,
                    "questions": [
                        {
                            "id": "threshold",
                            "question": f"What threshold should I use for {metric_key.upper()}?",
                        }
                    ],
                    "partial": {
                        "intent": "threshold",
                        "metric_key": metric_key,
                        "time_range": self._extract_time_range(normalized) or "1h",
                        "aggregation": aggregation,
                        "unresolved": ["threshold"],
                    },
                }
                self.repository.set_pending_clarification(profile_id, pending)
                return AssistantResponse(
                    status="needs_clarification",
                    interpretation=(
                        f"Interpreted as a {metric_key.upper()} threshold query, but the threshold "
                        "was not specified."
                    ),
                    clarifications=[
                        ClarificationQuestion(
                            id="threshold",
                            question=f"What threshold should I use for {metric_key.upper()}?",
                        )
                    ],
                    summary=f"I need the {metric_key.upper()} threshold before I can run this query.",
                )

        explicit_threshold = self._extract_threshold(normalized)
        if metric_key in {"cpu", "memory"} and explicit_threshold is not None:
            return self._build_parsed_query(
                source_query=query,
                intent="threshold",
                metric_key=metric_key,
                time_range=self._extract_time_range(normalized) or "1h",
                aggregation=aggregation,
                threshold=explicit_threshold,
            )

        if "compute" in normalized and metric_key in {"cpu", "memory", "cpu_memory"}:
            requested_top_n = self._extract_top_n(normalized)
            if requested_top_n is None and self._requests_all_instances(normalized):
                requested_top_n = None
            elif requested_top_n is None:
                requested_top_n = 10
            return self._build_parsed_query(
                source_query=query,
                intent="top_n",
                metric_key=metric_key,
                time_range=self._extract_time_range(normalized) or "1h",
                aggregation=aggregation,
                top_n=requested_top_n,
            )

        return AssistantResponse(
            status="error",
            interpretation=f"Could not safely map the request into the supported prototype flows: {query}",
            summary=(
                "I can currently help with compute CPU or memory threshold queries, top-N/worst "
                "compute queries, named-instance trends, and disk I/O after clarification."
            ),
        )

    def _parse_io_query(
        self,
        *,
        query: str,
        normalized: str,
        profile_id: str,
    ) -> ParsedQuery | AssistantResponse:
        io_type = self._extract_io_type(normalized)
        unresolved: list[str] = []
        partial: dict[str, Any] = {
            "intent": "top_n",
            "time_range": self._extract_time_range(normalized) or "1h",
            "aggregation": self._extract_aggregation(normalized),
            "top_n": self._extract_top_n(normalized) or 10,
        }
        if io_type is None:
            unresolved.append("io_type")
        elif io_type != "disk":
            return self._io_out_of_scope_response(self.repository.get_profile(profile_id))
        else:
            partial["io_type"] = io_type

        io_measure = self._extract_io_measure(normalized)
        if io_measure is None:
            unresolved.append("io_measure")
        else:
            partial["io_measure"] = io_measure

        io_direction = self._extract_io_direction(normalized)
        if io_direction is None:
            unresolved.append("io_direction")
        else:
            partial["io_direction"] = io_direction

        if unresolved:
            pending = {
                "kind": "query",
                "created_at": utc_now_iso(),
                "original_query": query,
                "questions": [
                    asdict(question)
                    for question in self._questions_for_unresolved(
                        unresolved=unresolved,
                        profile_id=profile_id,
                        profile=self.repository.get_profile(profile_id),
                    )
                ],
                "partial": partial | {"unresolved": unresolved},
            }
            self.repository.set_pending_clarification(profile_id, pending)
            return AssistantResponse(
                status="needs_clarification",
                interpretation="Interpreted as an I/O request, but more clarification is required.",
                clarifications=[
                    ClarificationQuestion(**question) for question in pending["questions"]
                ],
                summary="I need to know whether you want disk I/O throughput or IOPS, and whether you want read, write, or both.",
            )

        metric_key = (
            "disk_io_throughput"
            if partial["io_measure"] == "throughput"
            else "disk_io_iops"
        )
        return self._build_parsed_query(
            source_query=query,
            intent="top_n",
            metric_key=metric_key,
            time_range=partial["time_range"],
            aggregation=partial.get("aggregation", "max"),
            top_n=partial["top_n"],
            io_mode=partial["io_measure"],
            io_direction=partial["io_direction"],
        )

    def _parsed_from_pending(
        self,
        original_query: str,
        partial: dict[str, Any],
        profile: dict[str, Any],
    ) -> ParsedQuery | AssistantResponse:
        intent = partial["intent"]
        metric_key = partial.get("metric_key")
        if metric_key == "storage":
            return self._storage_not_available_response(profile)
        if metric_key is None and partial.get("io_measure"):
            metric_key = (
                "disk_io_throughput"
                if partial["io_measure"] == "throughput"
                else "disk_io_iops"
            )
        if metric_key is None:
            return AssistantResponse(
                status="error",
                interpretation=f"Clarification completed, but metric could not be resolved for: {original_query}",
                summary="The metric could not be resolved after clarification.",
            )
        return self._build_parsed_query(
            source_query=original_query,
            intent=intent,
            metric_key=metric_key,
            time_range=partial.get("time_range", "1h"),
            aggregation=partial.get("aggregation", "max"),
            threshold=partial.get("threshold"),
            top_n=partial.get("top_n"),
            instance_name=partial.get("instance_name"),
            io_mode=partial.get("io_measure"),
            io_direction=partial.get("io_direction"),
            learned_intent_key=(
                "worst_performing_compute_instances"
                if intent == "worst_performing"
                else None
            ),
        )

    def _execute_parsed_query(
        self,
        *,
        profile_id: str,
        profile: dict[str, Any],
        parsed: ParsedQuery,
    ) -> AssistantResponse:
        started_at = utc_now_iso()
        request: QueryExecutionRequest | None = None
        scope = {
            "scope_type": "default_compartment",
            "scope_label": f"default compartment {profile.get('default_compartment_name', '')}".strip(),
            "compartment_name": profile.get("default_compartment_name"),
            "compartment_id": profile.get("default_compartment_id"),
            "include_subcompartments": True,
        }
        try:
            profile = self._ensure_resolved_context(profile_id, profile)
            scope = self._resolve_query_scope(profile_id=profile_id, profile=profile, parsed=parsed)
            parsed = self._ensure_resolved_instance(
                profile,
                parsed,
                compartment_id=scope["compartment_id"],
            )
            request = QueryExecutionRequest(
                parsed_query=parsed,
                profile_id=profile_id,
                region=profile["region"],
                compartment_name=scope["compartment_name"],
                compartment_id=scope["compartment_id"],
                include_subcompartments=scope["include_subcompartments"],
                compartment_lookup={
                    item["id"]: item["name"] for item in profile.get("available_compartments", [])
                },
                auth_mode=profile.get("auth_mode", "instance_principal"),
                config_fallback=profile.get("config_fallback", {}),
            )
            result = self.execution_adapter.execute(request)
        except AuthFallbackSuggestedError as exc:
            return AssistantResponse(
                status="error",
                interpretation=self._interpretation_line(
                    parsed,
                    scope_label=scope["scope_label"],
                ),
                summary=(
                    "Instance Principals authentication failed while executing this query. "
                    f"{exc} If you want OCI config fallback, explicitly run "
                    "configure_auth_fallback."
                ),
            )
        except InstanceResolutionError as exc:
            pending = {
                "kind": "query",
                "created_at": utc_now_iso(),
                "original_query": parsed.source_query,
                "questions": [
                    {
                        "id": "instance_name",
                        "question": (
                            f"{exc} "
                            + (
                                "Options: " + ", ".join(option["name"] for option in exc.options)
                                if exc.options
                                else "Reply with the exact instance name."
                            )
                        ),
                    }
                ],
                "partial": {
                    "intent": parsed.intent,
                    "metric_key": parsed.metric_key,
                    "time_range": parsed.time_range,
                    "threshold": parsed.threshold,
                    "top_n": parsed.top_n,
                    "unresolved": ["instance_name"],
                },
            }
            self.repository.set_pending_clarification(profile_id, pending)
            return AssistantResponse(
                status="needs_clarification",
                interpretation=self._interpretation_line(
                    parsed,
                    scope_label=scope["scope_label"],
                ),
                clarifications=[ClarificationQuestion(**pending["questions"][0])],
                summary=str(exc),
            )
        except (DependencyMissingError, CompartmentResolutionError, RuntimeError) as exc:
            return AssistantResponse(
                status="error",
                interpretation=self._interpretation_line(
                    parsed,
                    scope_label=scope["scope_label"],
                ),
                summary=str(exc),
                recommendations=self._default_recommendations(parsed.metric_key, []),
                details=AssistantDetails(
                    query_text=request.query_text if request is not None else None,
                    scope=self._scope_details(profile_id, profile, scope),
                    interval=parsed.interval,
                    namespace=parsed.namespace,
                    metric=parsed.metric_label,
                    timing={"started_at": started_at, "finished_at": utc_now_iso()},
                ),
            )

        table = self._build_table(parsed, result)
        chart = self._build_chart(parsed, result)
        generated_artifacts = list(result.artifacts)
        csv_artifact = None
        if chart is not None:
            try:
                chart_artifact = self.artifact_manager.generate_chart_png(chart=chart)
            except DependencyMissingError:
                chart_artifact = None
            if chart_artifact is not None:
                generated_artifacts.append(chart_artifact)
        if len(result.rows) > DEFAULT_TABLE_LIMIT:
            csv_artifact = self.artifact_manager.generate_csv(
                rows=result.rows,
                title=f"{parsed.metric_label} results export",
            )
            if csv_artifact is not None:
                generated_artifacts.append(csv_artifact)
        details = AssistantDetails(
            query_text=request.query_text,
            scope=self._scope_details(profile_id, profile, scope),
            interval=parsed.interval,
            namespace=parsed.namespace,
            metric=parsed.metric_label,
            truncated=len(result.rows) > DEFAULT_TABLE_LIMIT,
            timing={"started_at": started_at, "finished_at": utc_now_iso()},
        )

        if parsed.learned_intent_key:
            self.repository.remember_preference(
                profile_id,
                intent_key=parsed.learned_intent_key,
                resolved_metric=parsed.metric_key,
            )
        template = self.repository.save_template(
            profile_id=profile_id,
            parsed_query=asdict(parsed),
            query_text=request.query_text,
        )
        details.template_id = template["template_id"]
        self.repository.set_last_resolved_context(
            profile_id,
            {
                "intent": parsed.intent,
                "metric_key": parsed.metric_key,
                "threshold": parsed.threshold,
                "time_range": parsed.time_range,
                "aggregation": parsed.aggregation,
                "top_n": parsed.top_n,
            },
        )

        summary_text = result.summary
        if len(result.rows) > DEFAULT_TABLE_LIMIT and csv_artifact is not None:
            summary_text += (
                f" Showing up to {DEFAULT_TABLE_LIMIT} results; download the full result set as CSV from artifacts."
            )

        return AssistantResponse(
            status="success",
            interpretation=self._interpretation_line(
                parsed,
                scope_label=scope["scope_label"],
            ),
            summary=summary_text,
            tables=[table] if table is not None else [],
            charts=[chart] if chart is not None else [],
            recommendations=result.recommendations or self._default_recommendations(
                parsed.metric_key, result.rows
            ),
            artifacts=generated_artifacts,
            details=details,
        )

    def _build_table(self, parsed: ParsedQuery, result: ExecutionResult) -> TableBlock | None:
        if not result.rows:
            return None
        visible_rows = result.rows[:DEFAULT_TABLE_LIMIT]
        row_recommendation = self._default_recommendations(parsed.metric_key, result.rows)[0]
        for row in visible_rows:
            if not row.get("recommendation"):
                row["recommendation"] = row_recommendation
        compact_rows = [self._compact_table_row(parsed, row) for row in visible_rows]
        return TableBlock(
            id=f"{parsed.metric_key}_{parsed.intent}_results",
            title=f"{parsed.metric_label} results",
            columns=list(compact_rows[0].keys()),
            rows=compact_rows,
        )

    def _compact_table_row(self, parsed: ParsedQuery, row: dict[str, Any]) -> dict[str, Any]:
        preferred_columns = self._preferred_table_columns(parsed, row)
        compact = {key: row.get(key) for key in preferred_columns if key in row}
        if compact:
            return compact
        noisy_columns = {"instance_ocid", "recommendation", "threshold", "aggregation", "metric"}
        return {
            key: value
            for key, value in row.items()
            if key not in noisy_columns
        }

    def _preferred_table_columns(self, parsed: ParsedQuery, row: dict[str, Any]) -> list[str]:
        base = ["instance_name", "compartment", "lifecycle_state"]
        if row.get("time_created") is not None:
            base.append("time_created")
        if parsed.metric_key == "cpu_memory":
            if parsed.aggregation == "mean":
                return base + [
                    "cpu_mean_value",
                    "memory_mean_value",
                    "cpu_latest_value",
                    "memory_latest_value",
                ]
            return base + [
                "cpu_max_value",
                "memory_max_value",
                "cpu_latest_value",
                "memory_latest_value",
            ]
        if parsed.aggregation == "mean" and "mean_value" in row:
            return base + ["mean_value", "latest_value"]
        if "max_value" in row:
            return base + ["max_value", "time_of_max", "latest_value"]
        return base

    def _build_chart(self, parsed: ParsedQuery, result: ExecutionResult) -> ChartBlock | None:
        if not result.chart_series:
            return None
        threshold_line = None
        if parsed.threshold is not None:
            threshold_line = ThresholdLine(value=parsed.threshold)
        return ChartBlock(
            id=f"{parsed.metric_key}_{parsed.intent}_chart",
            title=f"{parsed.metric_label} trend",
            type="line",
            x_axis="time",
            y_axis=METRIC_CONFIGS[parsed.metric_key]["y_axis"],
            series=result.chart_series[:DEFAULT_CHART_LIMIT],
            threshold_line=threshold_line,
        )

    def _build_parsed_query(
        self,
        *,
        source_query: str,
        intent: str,
        metric_key: str,
        time_range: str,
        aggregation: str = "max",
        threshold: float | None = None,
        top_n: int | None = None,
        instance_name: str | None = None,
        io_mode: str | None = None,
        io_direction: str | None = None,
        learned_intent_key: str | None = None,
    ) -> ParsedQuery:
        config = METRIC_CONFIGS[metric_key]
        metric_names = list(config["metric_names"])
        if metric_key in {"disk_io_throughput", "disk_io_iops"} and io_direction in {"read", "write"}:
            metric_names = [metric_names[0] if io_direction == "read" else metric_names[1]]
        return ParsedQuery(
            intent=intent,
            metric_key=metric_key,
            metric_label=config["label"],
            namespace=config["namespace"],
            metric_names=metric_names,
            time_range=time_range,
            interval=SUPPORTED_TIME_RANGES[time_range],
            aggregation=aggregation,
            threshold=threshold,
            top_n=top_n,
            instance_name=instance_name,
            instance_id=None,
            io_mode=io_mode,
            io_direction=io_direction,
            source_query=source_query,
            learned_intent_key=learned_intent_key,
        )

    def _scope_details(
        self,
        profile_id: str,
        profile: dict[str, Any],
        scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved_scope = scope or {
            "scope_type": "default_compartment",
            "scope_label": f"default compartment {profile.get('default_compartment_name', '')}".strip(),
            "compartment_name": profile.get("default_compartment_name"),
            "compartment_id": profile.get("default_compartment_id"),
            "include_subcompartments": True,
        }
        return {
            "profile_id": profile_id,
            "region": profile.get("region"),
            "scope_type": resolved_scope.get("scope_type"),
            "scope_label": resolved_scope.get("scope_label"),
            "compartment_name": resolved_scope.get("compartment_name"),
            "compartment_id": resolved_scope.get("compartment_id"),
            "include_subcompartments": resolved_scope.get("include_subcompartments", True),
            "auth_mode": profile.get("auth_mode", "instance_principal"),
        }

    def _ensure_resolved_instance(
        self,
        profile: dict[str, Any],
        parsed: ParsedQuery,
        *,
        compartment_id: str | None,
    ) -> ParsedQuery:
        if not parsed.instance_name or parsed.instance_id or not compartment_id:
            return parsed
        resolved = self.context_resolver.resolve_instance_name(
            region=profile["region"],
            auth_mode=profile.get("auth_mode", "instance_principal"),
            compartment_id=compartment_id,
            instance_name=parsed.instance_name,
            config_fallback=profile.get("config_fallback", {}),
        )
        parsed.instance_name = resolved["name"]
        parsed.instance_id = resolved["id"]
        return parsed

    def _ensure_resolved_context(
        self,
        profile_id: str,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        if profile.get("default_compartment_id"):
            return profile
        resolved = self.context_resolver.resolve_compartment(
            region=profile["region"],
            auth_mode=profile.get("auth_mode", "instance_principal"),
            compartment_name=profile["default_compartment_name"],
            compartment_id=profile.get("default_compartment_id"),
            config_fallback=profile.get("config_fallback", {}),
        )
        updated = self.repository.set_default_context(
            profile_id,
            region=profile["region"],
            compartment_name=resolved["compartment_name"],
            compartment_id=resolved["compartment_id"],
            auth_mode=profile.get("auth_mode", "instance_principal"),
            tenancy_id=resolved["tenancy_id"],
            available_compartments=profile.get("available_compartments"),
        )
        return updated

    def _resolve_query_scope(
        self,
        *,
        profile_id: str,
        profile: dict[str, Any],
        parsed: ParsedQuery,
    ) -> dict[str, Any]:
        query_lower = " ".join(parsed.source_query.lower().split())
        include_subcompartments = not any(
            phrase in query_lower
            for phrase in (
                "without subcompartment",
                "without subcompartments",
                "exclude subcompartment",
                "exclude subcompartments",
                "no subcompartment",
                "no subcompartments",
                "subcompartment false",
                "subcompartments false",
                "only this compartment",
            )
        )
        tenancy_requested = any(
            phrase in query_lower
            for phrase in (
                "across tenancy",
                "across the tenancy",
                "tenancy wide",
                "tenancy-wide",
                "entire tenancy",
                "all compartments",
            )
        )
        if tenancy_requested:
            tenancy_id = profile.get("tenancy_id")
            if not tenancy_id:
                listing = self.context_resolver.list_accessible_compartments(
                    region=profile["region"],
                    auth_mode=profile.get("auth_mode", "instance_principal"),
                    config_fallback=profile.get("config_fallback", {}),
                )
                tenancy_id = listing["tenancy_id"]
                profile["tenancy_id"] = tenancy_id
                profile["available_compartments"] = listing["compartments"]
                self.repository.update_profile(profile_id, profile)
            return {
                "scope_type": "tenancy",
                "scope_label": "tenancy",
                "compartment_name": "tenancy",
                "compartment_id": tenancy_id,
                "include_subcompartments": include_subcompartments,
            }

        compartment_match = re.search(
            r"(?:in|from|within)\s+(?:the\s+)?compartment\s+([A-Za-z0-9_.\- ]+?)(?=\s+(?:for|over|during|across|with|where|whose|that)\b|[?.!,]|$)",
            parsed.source_query,
            re.IGNORECASE,
        ) or re.search(
            r"compartment\s*[:=]\s*([A-Za-z0-9_.\- ]+?)(?=\s+(?:for|over|during|across|with|where|whose|that)\b|[?.!,]|$)",
            parsed.source_query,
            re.IGNORECASE,
        )
        if compartment_match:
            requested_name = compartment_match.group(1).strip().strip('"').strip("'")
            resolved = self.context_resolver.resolve_compartment(
                region=profile["region"],
                auth_mode=profile.get("auth_mode", "instance_principal"),
                compartment_name=requested_name,
                compartment_id=None,
                config_fallback=profile.get("config_fallback", {}),
            )
            return {
                "scope_type": "named_compartment",
                "scope_label": f"compartment {resolved['compartment_name']}",
                "compartment_name": resolved["compartment_name"],
                "compartment_id": resolved["compartment_id"],
                "include_subcompartments": include_subcompartments,
            }

        return {
            "scope_type": "default_compartment",
            "scope_label": f"default compartment {profile['default_compartment_name']}",
            "compartment_name": profile["default_compartment_name"],
            "compartment_id": profile.get("default_compartment_id"),
            "include_subcompartments": include_subcompartments,
        }

    def _interpretation_line(self, parsed: ParsedQuery, *, scope_label: str) -> str:
        metric_phrase = parsed.metric_label.lower()
        aggregation_phrase = parsed.aggregation.lower()
        if parsed.intent == "threshold" and parsed.threshold is not None:
            return (
                f"Interpreted as: find compute instances in {scope_label} whose {aggregation_phrase} {metric_phrase} exceeded "
                f"{parsed.threshold:.0f}% in the last {parsed.time_range}."
            )
        if parsed.intent == "named_trend" and parsed.instance_name:
            return (
                f"Interpreted as: show the {metric_phrase} trend for compute instance "
                f"{parsed.instance_name} in {scope_label} "
                f"over the last {parsed.time_range}."
            )
        if parsed.intent == "worst_performing":
            return (
                f"Interpreted as: show the worst-performing compute instances by {aggregation_phrase} {metric_phrase} "
                f"in the last {parsed.time_range} in {scope_label}."
            )
        return (
            f"Interpreted as: show compute instances ranked by {aggregation_phrase} {metric_phrase} in the last "
            f"{parsed.time_range} in {scope_label}."
        )

    def _default_recommendations(
        self,
        metric_key: str,
        rows: list[dict[str, Any]],
    ) -> list[str]:
        if metric_key == "cpu":
            return [
                "Check recent deployment or traffic changes on the affected instances.",
                "Inspect top CPU-consuming processes before deciding whether to scale or restart.",
            ]
        if metric_key == "memory":
            recommendations = [
                "Inspect memory growth and resident set size before restarting the workload.",
                "Capture diagnostics before restart when memory growth looks sustained.",
            ]
            if any("weblogic" in str(row.get("instance_name", "")).lower() for row in rows):
                recommendations.append(
                    "For likely WebLogic workloads, capture heap and thread diagnostics before restarting JVMs."
                )
            return recommendations
        if metric_key == "cpu_memory":
            return [
                "Compare both CPU and memory trends before deciding whether to scale or restart.",
                "Inspect top processes and memory growth on instances with sustained utilization.",
            ]
        return [
            "Check read/write hotspots and attached volume behavior before changing the workload.",
            "Compare disk throughput or IOPS against the expected baseline for the instance and volume profile.",
        ]

    def _questions_for_unresolved(
        self,
        *,
        unresolved: list[str],
        profile_id: str,
        profile: dict[str, Any],
    ) -> list[ClarificationQuestion]:
        questions: list[ClarificationQuestion] = []
        if "metric_choice" in unresolved:
            preference = self.repository.get_preference(
                profile_id, "worst_performing_compute_instances"
            )
            if preference is not None:
                questions.append(
                    ClarificationQuestion(
                        id="metric_choice",
                        question=(
                            f"Last time this meant {preference['resolved_metric']}. Use "
                            f"{preference['resolved_metric']} again, or do you want CPU, memory, "
                            "storage usage, or another metric?"
                        ),
                    )
                )
            else:
                questions.append(
                    ClarificationQuestion(
                        id="metric_choice",
                        question="Do you mean CPU, memory, storage usage, or another metric?",
                    )
                )
        if "threshold" in unresolved:
            questions.append(
                ClarificationQuestion(
                    id="threshold",
                    question="What threshold should I use?",
                )
            )
        if "io_type" in unresolved:
            questions.append(
                ClarificationQuestion(
                    id="io_type",
                    question="Do you mean disk I/O or something else?",
                )
            )
        if "io_measure" in unresolved:
            questions.append(
                ClarificationQuestion(
                    id="io_measure",
                    question="Do you want throughput or IOPS?",
                )
            )
        if "io_direction" in unresolved:
            questions.append(
                ClarificationQuestion(
                    id="io_direction",
                    question="Do you want read, write, or both?",
                )
            )
        if "instance_name" in unresolved:
            questions.append(
                ClarificationQuestion(
                    id="instance_name",
                    question="Which compute instance do you want the metric trend for?",
                )
            )
        return questions

    def _storage_not_available_response(self, profile: dict[str, Any]) -> AssistantResponse:
        compartment = profile.get("default_compartment_name") or "the selected compartment"
        return AssistantResponse(
            status="error",
            interpretation=(
                f"Interpreted as a request for filesystem storage usage in {compartment}."
            ),
            summary=(
                "Filesystem storage usage percentage is not available from standard OCI Monitoring "
                "metrics alone. If useful, ask for CPU, memory, or disk I/O instead."
            ),
        )

    def _io_out_of_scope_response(self, profile: dict[str, Any]) -> AssistantResponse:
        compartment = profile.get("default_compartment_name") or "the selected compartment"
        return AssistantResponse(
            status="error",
            interpretation=f"Interpreted as a non-disk I/O request in {compartment}.",
            summary="Only disk I/O is supported in the current prototype.",
        )

    def _extract_metric(self, text: str) -> str | None:
        normalized = text.lower()
        has_cpu = "cpu" in normalized
        has_memory = (
            "memory" in normalized or "mem " in f"{normalized} " or normalized.endswith("mem")
        )
        if has_cpu and has_memory:
            return "cpu_memory"
        if "cpu" in normalized:
            return "cpu"
        if "memory" in normalized or "mem " in f"{normalized} " or normalized.endswith("mem"):
            return "memory"
        if "storage" in normalized:
            return "storage"
        if "throughput" in normalized and "io" in normalized:
            return "disk_io_throughput"
        if "iops" in normalized and "io" in normalized:
            return "disk_io_iops"
        return None

    def _extract_threshold(self, text: str) -> float | None:
        for pattern in (
            r"(?:above|over|greater than|more than)\s+(\d+(?:\.\d+)?)\s*%?",
            r"(\d+(?:\.\d+)?)\s*%",
        ):
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return float(match.group(1))
        return None

    def _extract_time_range(self, text: str) -> str | None:
        mapping = {
            "last 15 minutes": "15m",
            "past 15 minutes": "15m",
            "last 15 mins": "15m",
            "last 30 minutes": "30m",
            "past 30 minutes": "30m",
            "last 30 mins": "30m",
            "last 1 hour": "1h",
            "past 1 hour": "1h",
            "last hour": "1h",
            "past hour": "1h",
            "last 6 hours": "6h",
            "past 6 hours": "6h",
            "last 24 hours": "24h",
            "past 24 hours": "24h",
            "last day": "24h",
            "past day": "24h",
            "last 7 days": "7d",
            "past 7 days": "7d",
            "last week": "7d",
        }
        for phrase, value in mapping.items():
            if phrase in text:
                return value
        compact_mapping = {
            "15m": "15m",
            "30m": "30m",
            "1h": "1h",
            "6h": "6h",
            "24h": "24h",
            "7d": "7d",
        }
        for phrase, value in compact_mapping.items():
            if re.search(rf"\b(?:last|past)?\s*{re.escape(phrase)}\b", text):
                return value
        return None

    def _extract_top_n(self, text: str) -> int | None:
        match = re.search(r"\btop\s+(\d+)\b", text, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None

    def _extract_aggregation(self, text: str) -> str:
        normalized = text.lower()
        if any(token in normalized for token in ("mean", "average", "avg")):
            return "mean"
        return "max"

    def _requests_all_instances(self, text: str) -> bool:
        return any(
            phrase in text
            for phrase in (
                "all compute",
                "all computes",
                "all compute instances",
                "all instances",
                "every compute",
                "every instance",
            )
        )

    def _extract_instance_name(self, text: str) -> str | None:
        match = re.search(
            r"(?:trend for|trend of)\s+(.+?)(?=\s+(?:for|over|during|across|within|"
            r"in\s+the\s+last|last|in\s+compartment|from\s+compartment|compartment\b)\b|[?.!,]|$)",
            text,
            re.IGNORECASE,
        )
        if not match:
            return None
        return match.group(1).strip().strip('"').strip("'")

    def _extract_io_type(self, text: str) -> str | None:
        normalized = text.lower()
        if normalized.strip() == "disk":
            return "disk"
        if normalized.strip() == "network":
            return "network"
        if "disk io" in normalized or "disk i/o" in normalized:
            return "disk"
        if "network io" in normalized or "network i/o" in normalized:
            return "network"
        if " io" in f" {normalized}" or normalized.endswith("io"):
            return None
        return None

    def _extract_io_measure(self, text: str) -> str | None:
        normalized = text.lower()
        if "throughput" in normalized:
            return "throughput"
        if "iops" in normalized:
            return "iops"
        return None

    def _extract_io_direction(self, text: str) -> str | None:
        normalized = text.lower()
        if "both" in normalized:
            return "both"
        if "read" in normalized and "write" in normalized:
            return "both"
        if "read" in normalized:
            return "read"
        if "write" in normalized:
            return "write"
        return None

    def _looks_like_new_query(self, answer: str) -> bool:
        normalized = " ".join(answer.lower().split())
        return any(normalized.startswith(prefix) for prefix in NEW_QUERY_HINTS)
