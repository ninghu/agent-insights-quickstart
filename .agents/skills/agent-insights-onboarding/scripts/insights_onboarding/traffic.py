"""Bounded Prompt and Hosted Agent traffic."""

from __future__ import annotations

import concurrent.futures
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import OnboardingError
from .models import AgentDeployment, TrafficOutcome

_SKILL_ROOT = Path(__file__).resolve().parents[2]
_AGENT_ASSETS = _SKILL_ROOT / "assets" / "agents"
_MAX_FUNCTION_TURNS = 3


def _load_scenarios(agent_type: str) -> list[dict[str, Any]]:
    directory = "prompt-agent" if agent_type == "prompt" else "hosted-agent"
    root = _AGENT_ASSETS / directory
    scenarios: list[dict[str, Any]] = []
    for expected_fault, file_name in (
        (False, "healthy_requests.json"),
        (True, "faulty_requests.json"),
    ):
        try:
            value = json.loads((root / file_name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise OnboardingError(
                "invalid_traffic_asset",
                f"Traffic fixture is invalid: {directory}/{file_name}",
            ) from error
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise OnboardingError(
                "invalid_traffic_asset",
                f"Traffic fixture must be an array: {directory}/{file_name}",
            )
        for item in value:
            scenarios.append({**item, "expected_fault": expected_fault})
    if len(scenarios) != 11 or sum(not item["expected_fault"] for item in scenarios) != 6:
        raise OnboardingError(
            "invalid_traffic_bound",
            "Sample traffic must contain six healthy and five faulty requests.",
        )
    return scenarios


def _enum_text(value: object) -> str:
    return str(getattr(value, "value", value) or "").casefold()


def _response_id(response: object) -> str:
    status = _enum_text(getattr(response, "status", ""))
    identifier = str(getattr(response, "id", "") or "").strip()
    if status != "completed" or not identifier:
        raise OnboardingError(
            "agent_invocation_failed",
            f"Agent response was not completed (status '{status or 'missing'}').",
        )
    return identifier


def _tool_output(scenario: Mapping[str, Any], name: str, raw_arguments: str) -> str:
    if name != "lookup_order":
        raise OnboardingError(
            "unexpected_tool_call",
            f"Prompt Agent called unexpected tool '{name}'.",
        )
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as error:
        raise OnboardingError(
            "invalid_tool_arguments",
            "Prompt Agent returned invalid function arguments.",
        ) from error
    if arguments != scenario["expected_tool_arguments"]:
        raise OnboardingError(
            "unexpected_tool_arguments",
            "Prompt Agent tool arguments did not match the bounded scenario.",
        )
    if bool(scenario["expected_fault"]):
        return json.dumps(
            {
                "ok": False,
                "error": {
                    "code": "dependency_unavailable",
                    "message": "The sample order lookup dependency is unavailable.",
                    "retryable": False,
                },
            },
            separators=(",", ":"),
        )
    return json.dumps(
        {"ok": True, "message": scenario["expected_user_reply"]},
        separators=(",", ":"),
    )


def _invoke_prompt(
    client: Any,
    deployment: AgentDeployment,
    scenario: Mapping[str, Any],
) -> tuple[str, None]:
    reference = {
        "type": "agent_reference",
        "name": deployment.name,
        "version": deployment.version,
    }
    response = client.responses.create(
        input=str(scenario["input"]),
        store=True,
        extra_body={"agent_reference": reference},
    )
    for _ in range(_MAX_FUNCTION_TURNS):
        calls = [
            item
            for item in (getattr(response, "output", None) or [])
            if _enum_text(getattr(item, "type", "")) == "function_call"
        ]
        if not calls:
            return _response_id(response), None
        outputs: list[dict[str, str]] = []
        for call in calls:
            call_id = str(getattr(call, "call_id", "") or "")
            name = str(getattr(call, "name", "") or "")
            raw_arguments = str(getattr(call, "arguments", "") or "")
            if not call_id or not name or not raw_arguments:
                raise OnboardingError(
                    "incomplete_tool_call",
                    "Prompt Agent returned an incomplete function call.",
                )
            outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _tool_output(scenario, name, raw_arguments),
                }
            )
        response = client.responses.create(
            input=outputs,
            previous_response_id=str(getattr(response, "id", "") or ""),
            store=True,
            extra_body={"agent_reference": reference},
        )
    raise OnboardingError(
        "tool_turn_limit",
        "Prompt Agent exceeded the bounded function-call turn limit.",
    )


def _create_hosted_session(project: Any, deployment: AgentDeployment) -> str:
    models = __import__("azure.ai.projects.models", fromlist=["VersionRefIndicator"])
    session = project.agents.create_session(
        deployment.name,
        version_indicator=models.VersionRefIndicator(agent_version=deployment.version),
    )
    session_id = next(
        (
            str(value).strip()
            for attribute in ("agent_session_id", "session_id", "id")
            if (value := getattr(session, attribute, None))
        ),
        "",
    )
    resolved = str(
        getattr(getattr(session, "version_indicator", None), "agent_version", "") or ""
    )
    if not session_id or resolved != deployment.version:
        if session_id:
            project.agents.delete_session(deployment.name, session_id)
        raise OnboardingError(
            "hosted_session_mismatch",
            "Hosted session did not bind to the exact planned Agent version.",
        )
    return session_id


def _invoke_hosted(
    project: Any,
    client: Any,
    deployment: AgentDeployment,
    scenario: Mapping[str, Any],
) -> tuple[str, str]:
    session_id = _create_hosted_session(project, deployment)
    try:
        response = client.responses.create(
            input=str(scenario["input"]),
            store=False,
            extra_body={"session_id": session_id},
        )
        return _response_id(response), session_id
    finally:
        project.agents.delete_session(deployment.name, session_id)


def generate_sample_traffic(
    project: Any,
    deployment: AgentDeployment,
    *,
    outcome_observer: Callable[[TrafficOutcome], None] | None = None,
) -> list[TrafficOutcome]:
    scenarios = _load_scenarios(deployment.kind)
    if deployment.kind == "prompt":
        client = project.get_openai_client(max_retries=0)
    else:
        client = project.get_openai_client(
            agent_name=deployment.name,
            default_query={"api-version": "v1"},
            max_retries=0,
        )

    def invoke(scenario: Mapping[str, Any]) -> TrafficOutcome:
        started = datetime.now(UTC)
        if deployment.kind == "prompt":
            response_id, session_id = _invoke_prompt(client, deployment, scenario)
        else:
            response_id, session_id = _invoke_hosted(
                project,
                client,
                deployment,
                scenario,
            )
        return TrafficOutcome(
            scenario=str(scenario["id"]),
            expected_fault=bool(scenario["expected_fault"]),
            response_id=response_id,
            session_id=session_id,
            trace_id=None,
            started_at=started.isoformat(),
            completed_at=datetime.now(UTC).isoformat(),
        )

    max_workers = 1 if deployment.kind == "prompt" else 2
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(invoke, scenario) for scenario in scenarios]
        outcomes: list[TrafficOutcome] = []
        first_error: BaseException | None = None
        for future in concurrent.futures.as_completed(futures):
            try:
                outcome = future.result()
            except BaseException as error:
                if first_error is None:
                    first_error = error
                continue
            outcomes.append(outcome)
            if outcome_observer is not None:
                outcome_observer(outcome)
    if first_error is not None:
        raise first_error
    outcomes.sort(key=lambda item: item.scenario)
    if len(outcomes) != 11:
        raise OnboardingError(
            "partial_traffic",
            "Traffic execution did not return all bounded invocation outcomes.",
        )
    return outcomes
