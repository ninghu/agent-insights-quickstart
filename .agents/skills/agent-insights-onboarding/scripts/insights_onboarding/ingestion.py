"""Exact Application Insights ingestion validation."""

from __future__ import annotations

import importlib
import json
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from azure.core.exceptions import HttpResponseError

from .errors import OnboardingError
from .models import AgentDeployment, TrafficOutcome


def _escape_kql(value: str) -> str:
    return value.replace("'", "''")


def _query(agent_name: str) -> str:
    escaped = _escape_kql(agent_name)
    return f"""
let roots = materialize(
    union isfuzzy=true requests, dependencies
    | extend trace_id=tostring(operation_Id)
    | extend agent_name=tostring(customDimensions["gen_ai.agent.name"])
    | extend agent_version=tostring(customDimensions["gen_ai.agent.version"])
    | extend operation_name=tostring(customDimensions["gen_ai.operation.name"])
    | extend response_id=tostring(customDimensions["gen_ai.response.id"])
    | extend hosted_response_id=tostring(customDimensions["azure.ai.agentserver.response_id"])
    | extend hosted_session_id=tostring(customDimensions["azure.ai.agentserver.session_id"])
    | where isnotempty(trace_id) and agent_name == '{escaped}'
    | where operation_name == "invoke_agent"
    | summarize
        root_count=count(),
        versions=make_set(agent_version, 10),
        response_ids=make_set(response_id, 20),
        hosted_response_ids=make_set(hosted_response_id, 20),
        hosted_session_ids=make_set(hosted_session_id, 20)
      by trace_id
);
union isfuzzy=true requests, dependencies
| extend trace_id=tostring(operation_Id)
| where trace_id in (roots | project trace_id)
| extend tool_name=tostring(customDimensions["gen_ai.tool.name"])
| summarize tool_names=make_set(tool_name, 20), span_count=count() by trace_id
| join kind=inner roots on trace_id
| project trace_id, root_count, versions, response_ids, hosted_response_ids,
    hosted_session_ids, tool_names, span_count
| order by trace_id asc
""".strip()


def _strings(value: Any) -> set[str]:
    selected = value
    if isinstance(value, str):
        try:
            selected = json.loads(value)
        except json.JSONDecodeError:
            selected = [value]
    if not isinstance(selected, Sequence) or isinstance(
        selected,
        (str, bytes, bytearray),
    ):
        return set()
    return {str(item) for item in selected if str(item)}


def _rows(result: Any) -> list[dict[str, Any]]:
    status = str(getattr(getattr(result, "status", ""), "value", getattr(result, "status", "")))
    if status.casefold() != "success":
        return []
    tables = list(getattr(result, "tables", []) or [])
    if len(tables) != 1:
        return []
    table = tables[0]
    columns = [str(getattr(column, "name", column)) for column in table.columns]
    return [dict(zip(columns, row, strict=True)) for row in table.rows]


def _query_rows(
    client: Any,
    *,
    application_insights_resource_id: str,
    agent_name: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    result = client.query_resource(
        application_insights_resource_id,
        _query(agent_name),
        timespan=(start, end),
        server_timeout=60,
    )
    return _rows(result)


def can_query_agent_traces(
    *,
    credential: Any,
    application_insights_resource_id: str,
) -> bool:
    query_module = importlib.import_module("azure.monitor.query")
    client = query_module.LogsQueryClient(credential=credential, retry_total=0)
    try:
        result = client.query_resource(
            application_insights_resource_id,
            "union isfuzzy=true requests, dependencies | take 0",
            timespan=timedelta(minutes=5),
            server_timeout=30,
        )
    except HttpResponseError as error:
        if error.status_code in {401, 403}:
            return False
        raise
    status = str(
        getattr(getattr(result, "status", ""), "value", getattr(result, "status", ""))
    )
    return status.casefold() == "success"


def _correlate(
    rows: Sequence[Mapping[str, Any]],
    outcomes: Sequence[TrafficOutcome],
    deployment: AgentDeployment,
    *,
    require_tool: bool,
) -> list[dict[str, Any]] | None:
    matches: list[dict[str, Any]] = []
    used_traces: set[str] = set()
    for outcome in outcomes:
        candidates: list[Mapping[str, Any]] = []
        for row in rows:
            response_ids = _strings(row.get("response_ids")) | _strings(
                row.get("hosted_response_ids")
            )
            session_ids = _strings(row.get("hosted_session_ids"))
            if (
                outcome.response_id and outcome.response_id in response_ids
            ) or (
                outcome.session_id and outcome.session_id in session_ids
            ):
                candidates.append(row)
        unique = {str(row.get("trace_id") or ""): row for row in candidates}
        unique.pop("", None)
        if len(unique) != 1:
            return None
        trace_id, row = next(iter(unique.items()))
        if trace_id in used_traces:
            raise OnboardingError(
                "duplicate_ingestion_correlation",
                "Multiple invocations correlated to the same trace.",
            )
        if deployment.version not in _strings(row.get("versions")):
            raise OnboardingError(
                "ingestion_version_mismatch",
                "Ingested trace belongs to a different Agent version.",
            )
        if require_tool and "lookup_order" not in _strings(row.get("tool_names")):
            return None
        used_traces.add(trace_id)
        matches.append(
            {
                "scenario": outcome.scenario,
                "trace_id": trace_id,
                "response_id": outcome.response_id,
                "session_id": outcome.session_id,
                "span_count": int(row.get("span_count") or 0),
            }
        )
    return matches


def wait_for_ingestion(
    *,
    credential: Any,
    application_insights_resource_id: str,
    deployment: AgentDeployment,
    outcomes: Sequence[TrafficOutcome],
    timeout_seconds: float = 900,
    require_tool: bool = True,
) -> list[dict[str, Any]]:
    if timeout_seconds <= 0:
        raise OnboardingError(
            "invalid_timeout",
            "Ingestion timeout must be positive.",
        )
    query_module = importlib.import_module("azure.monitor.query")
    client = query_module.LogsQueryClient(credential=credential, retry_total=0)
    start = min(datetime.fromisoformat(item.started_at) for item in outcomes) - timedelta(
        minutes=2
    )
    deadline = time.monotonic() + timeout_seconds
    last_count = 0
    while True:
        end = datetime.now(UTC) + timedelta(minutes=2)
        rows = _query_rows(
            client,
            application_insights_resource_id=application_insights_resource_id,
            agent_name=deployment.name,
            start=start,
            end=end,
        )
        last_count = len(rows)
        matches = _correlate(
            rows,
            outcomes,
            deployment,
            require_tool=require_tool,
        )
        if matches is not None and len(matches) == len(outcomes):
            return matches
        if time.monotonic() >= deadline:
            raise OnboardingError(
                "ingestion_timeout",
                "Application Insights did not contain every expected invocation.",
                {"expected": len(outcomes), "observed_agent_roots": last_count},
            )
        time.sleep(20)


def require_recent_agent_roots(
    *,
    credential: Any,
    application_insights_resource_id: str,
    deployment: AgentDeployment,
    lookback_hours: int = 168,
    minimum_roots: int = 3,
) -> list[dict[str, Any]]:
    query_module = importlib.import_module("azure.monitor.query")
    client = query_module.LogsQueryClient(credential=credential, retry_total=0)
    end = datetime.now(UTC)
    rows = _query_rows(
        client,
        application_insights_resource_id=application_insights_resource_id,
        agent_name=deployment.name,
        start=end - timedelta(hours=lookback_hours),
        end=end,
    )
    matching = [
        row
        for row in rows
        if deployment.version in _strings(row.get("versions"))
    ]
    if len(matching) < minimum_roots:
        raise OnboardingError(
            "insufficient_recent_traces",
            "The selected existing Agent version does not have enough recent traces.",
            {
                "required": minimum_roots,
                "observed": len(matching),
                "lookback_hours": lookback_hours,
            },
        )
    return [
        {
            "trace_id": str(row.get("trace_id") or ""),
            "span_count": int(row.get("span_count") or 0),
        }
        for row in matching
    ]
