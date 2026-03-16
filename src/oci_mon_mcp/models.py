"""Structured models for the OCI Monitoring MCP prototype."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ResponseStatus = Literal["success", "needs_clarification", "error"]


@dataclass(slots=True)
class ClarificationQuestion:
    """A single clarification that blocks execution."""

    id: str
    question: str


@dataclass(slots=True)
class TableBlock:
    """Structured table result."""

    id: str
    title: str
    columns: list[str]
    rows: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ChartPoint:
    """Single chart point."""

    time: str
    value: float


@dataclass(slots=True)
class ChartSeries:
    """A single chart series."""

    name: str
    points: list[ChartPoint] = field(default_factory=list)


@dataclass(slots=True)
class ThresholdLine:
    """Chart threshold marker."""

    value: float
    color: str = "#7fb6ff"
    line_width: int = 2


@dataclass(slots=True)
class ChartBlock:
    """Structured chart result."""

    id: str
    title: str
    type: str
    x_axis: str
    y_axis: str
    legend_position: str = "right"
    series: list[ChartSeries] = field(default_factory=list)
    threshold_line: ThresholdLine | None = None


@dataclass(slots=True)
class ArtifactLink:
    """Artifact reference for PNG/CSV outputs."""

    id: str
    type: str
    title: str
    url: str
    expires_at: str


@dataclass(slots=True)
class AssistantDetails:
    """Implementation-facing result details."""

    query_text: str | None = None
    scope: dict[str, Any] = field(default_factory=dict)
    interval: str | None = None
    namespace: str | None = None
    metric: str | None = None
    template_id: str | None = None
    truncated: bool = False
    timing: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AssistantResponse:
    """Primary tool response contract."""

    status: ResponseStatus
    interpretation: str
    clarifications: list[ClarificationQuestion] = field(default_factory=list)
    summary: str = ""
    tables: list[TableBlock] = field(default_factory=list)
    charts: list[ChartBlock] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    artifacts: list[ArtifactLink] = field(default_factory=list)
    details: AssistantDetails = field(default_factory=AssistantDetails)


@dataclass(slots=True)
class ParsedQuery:
    """Deterministic interpretation of a supported NL query."""

    intent: str
    metric_key: str
    metric_label: str
    namespace: str
    metric_names: list[str]
    time_range: str
    interval: str
    aggregation: str
    threshold: float | None = None
    top_n: int | None = None
    instance_name: str | None = None
    instance_id: str | None = None
    io_mode: str | None = None
    io_direction: str | None = None
    source_query: str = ""
    learned_intent_key: str | None = None


@dataclass(slots=True)
class QueryExecutionRequest:
    """Request passed to the execution adapter."""

    parsed_query: ParsedQuery
    profile_id: str
    region: str
    compartment_name: str
    compartment_id: str | None
    include_subcompartments: bool = True
    compartment_lookup: dict[str, str] = field(default_factory=dict)
    auth_mode: str = "instance_principal"
    config_fallback: dict[str, str] = field(default_factory=dict)

    @property
    def query_text(self) -> str:
        """Render query text for debugging and execution."""
        metric_queries = []
        for metric_name in self.parsed_query.metric_names:
            if self.parsed_query.instance_name:
                filter_key = "resourceId" if self.parsed_query.instance_id else "resourceDisplayName"
                filter_value = self.parsed_query.instance_id or self.parsed_query.instance_name
                metric_queries.append(
                    f'{metric_name}[{self.parsed_query.interval}]'
                    f'{{{filter_key} = "{filter_value}"}}'
                    f".{self.parsed_query.aggregation}()"
                )
            else:
                metric_queries.append(
                    f"{metric_name}[{self.parsed_query.interval}]"
                    f".groupBy(resourceId,resourceDisplayName,compartmentId)"
                    f".{self.parsed_query.aggregation}()"
                )
        return "\n".join(metric_queries)


@dataclass(slots=True)
class ExecutionResult:
    """Normalized execution result returned by the adapter."""

    summary: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    chart_series: list[ChartSeries] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    artifacts: list[ArtifactLink] = field(default_factory=list)
    missing_resources: list[str] = field(default_factory=list)
    no_match_highest: dict[str, Any] | None = None
