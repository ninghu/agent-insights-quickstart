"""Disposable Azure acceptance matrix for every supported onboarding path."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import orchestrator
from .agents import (
    create_sample_agent,
    delete_owned_agent,
    project_client,
)
from .azure_cli import AzureCli
from .discovery import list_app_insights_connections, select_context
from .errors import OnboardingError
from .ingestion import wait_for_ingestion
from .insights_api import AgentInsightsClient
from .models import (
    AgentDeployment,
    AgentType,
    AzureContext,
    OnboardingConfig,
    ProjectResources,
)
from .provisioning import (
    cleanup_scratch,
    ensure_existing_connections,
    provision_scratch,
    resource_group_name,
)
from .receipts import ensure_secret_free, write_json_atomic
from .traffic import generate_sample_traffic

CaseMode = Literal["scratch", "existing"]
AgentSelection = Literal["automatic", "create", "select"]


@dataclass(frozen=True, slots=True)
class LiveMatrixCase:
    name: str
    mode: CaseMode
    agent_type: AgentType
    agent_selection: AgentSelection
    scheduled: bool
    protected_trace_content: bool = False
    connection_state: Literal["not_applicable", "missing", "existing"] = "existing"


SUPPORTED_CASES = (
    LiveMatrixCase(
        "scratch-prompt-scheduled",
        "scratch",
        "prompt",
        "automatic",
        True,
        connection_state="not_applicable",
    ),
    LiveMatrixCase(
        "scratch-hosted-scheduled",
        "scratch",
        "hosted",
        "automatic",
        True,
        connection_state="not_applicable",
    ),
    LiveMatrixCase(
        "scratch-prompt-protected-scheduled",
        "scratch",
        "prompt",
        "automatic",
        True,
        protected_trace_content=True,
        connection_state="not_applicable",
    ),
    LiveMatrixCase(
        "existing-create-prompt-oneoff",
        "existing",
        "prompt",
        "create",
        False,
        connection_state="missing",
    ),
    LiveMatrixCase(
        "existing-create-prompt-scheduled",
        "existing",
        "prompt",
        "create",
        True,
    ),
    LiveMatrixCase(
        "existing-create-hosted-oneoff",
        "existing",
        "hosted",
        "create",
        False,
    ),
    LiveMatrixCase(
        "existing-create-hosted-scheduled",
        "existing",
        "hosted",
        "create",
        True,
    ),
    LiveMatrixCase(
        "existing-select-prompt-oneoff",
        "existing",
        "prompt",
        "select",
        False,
    ),
    LiveMatrixCase(
        "existing-select-prompt-scheduled",
        "existing",
        "prompt",
        "select",
        True,
    ),
    LiveMatrixCase(
        "existing-select-hosted-oneoff",
        "existing",
        "hosted",
        "select",
        False,
    ),
    LiveMatrixCase(
        "existing-select-hosted-scheduled",
        "existing",
        "hosted",
        "select",
        True,
    ),
    LiveMatrixCase(
        "existing-create-prompt-protected-scheduled",
        "existing",
        "prompt",
        "create",
        True,
        protected_trace_content=True,
    ),
)
CASES_BY_NAME = {case.name: case for case in SUPPORTED_CASES}


@dataclass(frozen=True, slots=True)
class LiveMatrixOptions:
    subscription_id: str
    location: str
    model_name: str
    model_version: str
    model_format: str
    model_sku: str
    model_capacity: int
    cases: tuple[LiveMatrixCase, ...]
    output_dir: Path
    ingestion_timeout_seconds: float
    insights_timeout_seconds: float
    resume_summary: Path | None = None


@dataclass(slots=True)
class FixtureProject:
    run_id: str
    config: OnboardingConfig
    context: AzureContext
    resources: ProjectResources
    retained_connection_ids: tuple[str, ...] = ()


def supported_case_names() -> tuple[str, ...]:
    return tuple(case.name for case in SUPPORTED_CASES)


def select_cases(value: str) -> tuple[LiveMatrixCase, ...]:
    selected = [item.strip() for item in value.split(",") if item.strip()]
    if not selected or selected == ["all"]:
        return SUPPORTED_CASES
    unknown = sorted(set(selected) - set(CASES_BY_NAME))
    if unknown:
        raise OnboardingError(
            "unknown_live_matrix_case",
            "The live matrix case selection contains unsupported names.",
            {"unknown": unknown, "supported": list(supported_case_names())},
        )
    selected_names = set(selected)
    return tuple(case for case in SUPPORTED_CASES if case.name in selected_names)


def case_config(
    case: LiveMatrixCase,
    options: LiveMatrixOptions,
    *,
    fixture: FixtureProject | None = None,
    selected_agent: AgentDeployment | None = None,
) -> OnboardingConfig:
    if case.mode == "scratch":
        return OnboardingConfig(
            mode="scratch",
            subscription_id=options.subscription_id,
            location=options.location,
            agent_type=case.agent_type,
            name_prefix="insights-live-matrix",
            model_name=options.model_name,
            model_version=options.model_version,
            model_format=options.model_format,
            model_sku=options.model_sku,
            model_capacity=options.model_capacity,
            protected_trace_content=case.protected_trace_content,
        )
    if fixture is None:
        raise OnboardingError(
            "live_matrix_fixture_missing",
            "Existing-project cases require the disposable fixture project.",
        )
    if case.agent_selection == "select" and selected_agent is None:
        raise OnboardingError(
            "live_matrix_agent_fixture_missing",
            "Existing-Agent cases require a disposable Agent fixture.",
            {"agent_type": case.agent_type},
        )
    return OnboardingConfig(
        mode="existing",
        subscription_id=options.subscription_id,
        location=None,
        agent_type=case.agent_type,
        name_prefix="insights-live-matrix",
        project_resource_id=fixture.resources.project_resource_id,
        project_endpoint=fixture.resources.project_endpoint,
        application_insights_resource_id=
            fixture.resources.application_insights_resource_id,
        agent_name=selected_agent.name if selected_agent else None,
        model_deployment_name=fixture.resources.model_deployment_name,
        lookback_hours=168,
        create_sample_agent=case.agent_selection == "create",
        enable_existing_monitor=case.scheduled,
        protected_trace_content=case.protected_trace_content,
    )


def validate_case_result(
    case: LiveMatrixCase,
    result: dict[str, object],
) -> dict[str, object]:
    if result.get("status") != "complete":
        raise OnboardingError(
            "live_matrix_incomplete_result",
            "The onboarding case did not return a complete receipt.",
            {"case": case.name},
        )
    agent = result.get("agent")
    summary = result.get("result_summary")
    portal = str(result.get("agent_insights_portal_url") or "")
    if not isinstance(agent, dict) or agent.get("kind") != case.agent_type:
        raise OnboardingError(
            "live_matrix_agent_mismatch",
            "The onboarding result used the wrong Agent kind.",
            {"case": case.name},
        )
    if not isinstance(summary, dict) or int(summary.get("insight_count") or 0) < 1:
        raise OnboardingError(
            "live_matrix_empty_insights",
            "The onboarding case returned no insights.",
            {"case": case.name},
        )
    expected_trigger = "scheduled" if case.scheduled else "manual"
    if summary.get("first_run_trigger") != expected_trigger:
        raise OnboardingError(
            "live_matrix_trigger_mismatch",
            "The onboarding case used the wrong first-run trigger.",
            {
                "case": case.name,
                "expected": expected_trigger,
                "actual": summary.get("first_run_trigger"),
            },
        )
    if case.scheduled != bool(summary.get("schedule_enabled")):
        raise OnboardingError(
            "live_matrix_schedule_mismatch",
            "The onboarding result has the wrong schedule state.",
            {"case": case.name},
        )
    if "/monitor/insights?" not in portal:
        raise OnboardingError(
            "live_matrix_portal_mismatch",
            "The onboarding result did not link directly to the Insights tab.",
            {"case": case.name},
        )
    if case.agent_selection in {"automatic", "create"}:
        fix_field = (
            "concrete_prompt_fix_count"
            if case.agent_type == "prompt"
            else "concrete_code_fix_count"
        )
        if int(summary.get(fix_field) or 0) < 1:
            raise OnboardingError(
                "live_matrix_concrete_fix_missing",
                "The workflow-created sample did not return its required concrete fix.",
                {"case": case.name, "fix_field": fix_field},
            )
    return {
        "status": "passed",
        "first_run_trigger": expected_trigger,
        "insight_count": int(summary["insight_count"]),
        "concrete_code_fix_count": int(
            summary.get("concrete_code_fix_count") or 0
        ),
        "concrete_prompt_fix_count": int(
            summary.get("concrete_prompt_fix_count") or 0
        ),
    }


def _new_run_id() -> str:
    return secrets.token_hex(6)


def _existing_case_requested(cases: tuple[LiveMatrixCase, ...]) -> bool:
    return any(case.mode == "existing" for case in cases)


def _selected_agent_types(
    cases: tuple[LiveMatrixCase, ...],
) -> set[AgentType]:
    return {
        case.agent_type
        for case in cases
        if case.mode == "existing" and case.agent_selection == "select"
    }


def _fixture_config(options: LiveMatrixOptions) -> OnboardingConfig:
    return OnboardingConfig(
        mode="scratch",
        subscription_id=options.subscription_id,
        location=options.location,
        agent_type="hosted",
        name_prefix="insights-live-fixture",
        model_name=options.model_name,
        model_version=options.model_version,
        model_format=options.model_format,
        model_sku=options.model_sku,
        model_capacity=options.model_capacity,
    )


def _create_fixture(
    cli: AzureCli,
    options: LiveMatrixOptions,
) -> FixtureProject:
    run_id = _new_run_id()
    config = _fixture_config(options)
    orchestrator.doctor(config, cli)
    context = select_context(cli, options.subscription_id)
    try:
        resources = provision_scratch(
            cli,
            config=config,
            context=context,
            run_id=run_id,
        )
    except BaseException as error:
        try:
            group_name = resource_group_name(config, run_id)
            if cli.json(["group", "exists", "--name", group_name]) is True:
                group = cli.json(["group", "show", "--name", group_name])
                if isinstance(group, dict) and group.get("id"):
                    cleanup_scratch(
                        cli,
                        resource_group_id=str(group["id"]),
                        run_id=run_id,
                        owner_object_id=context.user_object_id,
                    )
        except Exception as cleanup_error:
            error.add_note(
                "Partial fixture cleanup also failed with "
                f"{type(cleanup_error).__name__}."
            )
        raise
    return FixtureProject(run_id, config, context, resources)


def _delete_fixture_connections(
    cli: AzureCli,
    fixture: FixtureProject,
) -> None:
    for connection in list_app_insights_connections(
        cli,
        fixture.resources.project_resource_id,
    ):
        connection_id = str(connection.get("id") or "")
        if not connection_id:
            continue
        cli.rest(
            method="delete",
            url=(
                f"https://management.azure.com{connection_id}"
                "?api-version=2025-06-01"
            ),
        )


def _ensure_fixture_connections(
    cli: AzureCli,
    fixture: FixtureProject,
) -> None:
    created = ensure_existing_connections(
        cli,
        project_resource_id=fixture.resources.project_resource_id,
        application_insights_resource_id=
            fixture.resources.application_insights_resource_id,
        location=fixture.config.location or "",
        run_id=fixture.run_id,
    )
    fixture.retained_connection_ids = created


def _create_selected_agent_fixture(
    fixture: FixtureProject,
    agent_type: AgentType,
    options: LiveMatrixOptions,
) -> tuple[str, AgentDeployment]:
    run_id = _new_run_id()
    project = project_client(
        fixture.resources.project_endpoint,
        fixture.context.tenant_id,
    )
    deployment = create_sample_agent(
        project,
        run_id=run_id,
        agent_type=agent_type,
        model=fixture.resources.model_deployment_name,
    )
    outcomes = generate_sample_traffic(project, deployment)
    wait_for_ingestion(
        credential=orchestrator._credential(fixture.context),
        application_insights_resource_id=
            fixture.resources.application_insights_resource_id,
        deployment=deployment,
        outcomes=outcomes,
        timeout_seconds=options.ingestion_timeout_seconds,
        require_tool=agent_type == "hosted",
    )
    return run_id, deployment


def _delete_selected_agent_fixture(
    fixture: FixtureProject,
    run_id: str,
    deployment: AgentDeployment,
) -> None:
    project = project_client(
        fixture.resources.project_endpoint,
        fixture.context.tenant_id,
    )
    delete_owned_agent(
        project,
        deployment=deployment,
        run_id=run_id,
    )


def _delete_fixture_agent_monitors(
    fixture: FixtureProject,
    deployment: AgentDeployment,
) -> None:
    with AgentInsightsClient(
        project_endpoint=fixture.resources.project_endpoint,
        credential=orchestrator._credential(fixture.context),
    ) as client:
        for monitor in client.list_monitors(deployment.name):
            monitor_id = str(monitor.get("id") or "")
            if not monitor_id:
                continue
            if bool(monitor.get("enabled")):
                client.disable_monitor(monitor_id)
            client.delete_monitor(monitor_id)


def _cleanup_case(
    run_id: str,
    *,
    cli: AzureCli,
    config: OnboardingConfig,
) -> str:
    run_dir = orchestrator._run_dir(run_id)
    if not (run_dir / "plan.json").exists():
        return "not_needed"
    if not (run_dir / "provisioning-receipt.json").exists():
        if config.mode == "scratch":
            group_name = resource_group_name(config, run_id)
            if cli.json(["group", "exists", "--name", group_name]) is True:
                context = select_context(cli, config.subscription_id)
                group = cli.json(["group", "show", "--name", group_name])
                if not isinstance(group, dict) or not group.get("id"):
                    raise OnboardingError(
                        "live_matrix_cleanup_target_missing",
                        "The partial scratch resource group could not be resolved.",
                    )
                cleanup_scratch(
                    cli,
                    resource_group_id=str(group["id"]),
                    run_id=run_id,
                    owner_object_id=context.user_object_id,
                )
                return "partial_scratch_cleaned"
        return "deferred_to_fixture_cleanup"
    orchestrator.cleanup(run_dir, cli=cli)
    return "complete"


def _verify_scratch_group_absent(
    cli: AzureCli,
    config: OnboardingConfig,
    run_id: str,
) -> None:
    exists = cli.json(
        ["group", "exists", "--name", resource_group_name(config, run_id)]
    )
    if exists is not False:
        raise OnboardingError(
            "live_matrix_cleanup_leak",
            "A scratch live-matrix resource group still exists after cleanup.",
        )


def cleanup_matrix_groups(
    cli: AzureCli,
    subscription_id: str,
) -> dict[str, object]:
    context = select_context(cli, subscription_id)
    value = cli.json(
        ["group", "list", "--subscription", subscription_id],
    )
    if not isinstance(value, list):
        raise OnboardingError(
            "invalid_live_matrix_group_list",
            "Azure CLI returned an invalid resource-group list.",
        )
    deleted: list[str] = []
    failures: list[dict[str, str]] = []
    for group in value:
        if not isinstance(group, dict):
            continue
        name = str(group.get("name") or "")
        tags = group.get("tags")
        if not name.startswith(
            ("rg-insights-live-fixture-", "rg-insights-live-matrix-")
        ) or not isinstance(tags, dict):
            continue
        if (
            str(tags.get("created-by") or "") != "agent-insights-quickstart"
            or str(tags.get("owner-object-id") or "").casefold()
            != context.user_object_id.casefold()
        ):
            continue
        run_id = str(tags.get("run-id") or "")
        resource_id = str(group.get("id") or "")
        if not run_id or not resource_id:
            failures.append(
                {
                    "resource_group": name,
                    "failure": "incomplete_ownership_metadata",
                }
            )
            continue
        try:
            cleanup_scratch(
                cli,
                resource_group_id=resource_id,
                run_id=run_id,
                owner_object_id=context.user_object_id,
            )
            deleted.append(name)
        except Exception as error:
            failures.append(
                {
                    "resource_group": name,
                    "failure": type(error).__name__,
                }
            )
    if failures:
        raise OnboardingError(
            "live_matrix_cleanup_incomplete",
            "One or more owned matrix resource groups could not be deleted.",
            {
                "deleted_resource_groups": deleted,
                "failures": failures,
            },
        )
    return {
        "status": "complete",
        "deleted_resource_groups": deleted,
    }


def _safe_case_result(
    *,
    case: LiveMatrixCase,
    status: str,
    duration_seconds: float,
    cleanup_status: str,
    assertion: dict[str, object] | None = None,
    error: OnboardingError | None = None,
) -> dict[str, object]:
    return {
        "case": case.name,
        "status": status,
        "duration_seconds": round(duration_seconds, 2),
        "cleanup_status": cleanup_status,
        "assertion": assertion or {},
        "error_code": error.code if error else None,
    }


def _run_case(
    case: LiveMatrixCase,
    options: LiveMatrixOptions,
    *,
    cli: AzureCli,
    fixture: FixtureProject | None,
    selected_agent: AgentDeployment | None,
) -> dict[str, object]:
    started = time.monotonic()
    run_id = _new_run_id()
    cleanup_status = "not_started"
    config = case_config(
        case,
        options,
        fixture=fixture,
        selected_agent=selected_agent,
    )
    assertion: dict[str, object] = {}
    case_error: OnboardingError | None = None
    try:
        result = orchestrator.onboard(
            config,
            run_id=run_id,
            ingestion_timeout_seconds=options.ingestion_timeout_seconds,
            insights_timeout_seconds=options.insights_timeout_seconds,
            cli=cli,
        )
        assertion = validate_case_result(case, result)
    except OnboardingError as error:
        case_error = error
    except Exception as error:
        case_error = OnboardingError(
            "live_matrix_unexpected_failure",
            "The onboarding case raised an unexpected exception.",
            {"exception_type": type(error).__name__},
        )
    finally:
        try:
            if case.agent_selection == "select":
                if fixture is None or selected_agent is None:
                    raise OnboardingError(
                        "live_matrix_fixture_missing",
                        "Selected-Agent cleanup lost its fixture identity.",
                    )
                _delete_fixture_agent_monitors(fixture, selected_agent)
            cleanup_status = _cleanup_case(
                run_id,
                cli=cli,
                config=config,
            )
            if case.mode == "scratch":
                _verify_scratch_group_absent(cli, config, run_id)
        except Exception as cleanup_error:
            cleanup_status = f"failed:{type(cleanup_error).__name__}"
            if case_error is None:
                case_error = OnboardingError(
                    "live_matrix_cleanup_failed",
                    "The onboarding case passed, but cleanup failed.",
                    {"exception_type": type(cleanup_error).__name__},
                )

    return _safe_case_result(
        case=case,
        status="failed" if case_error else "passed",
        duration_seconds=time.monotonic() - started,
        cleanup_status=cleanup_status,
        assertion=assertion,
        error=case_error,
    )


def _cleanup_fixture(
    cli: AzureCli,
    fixture: FixtureProject,
) -> None:
    cleanup_scratch(
        cli,
        resource_group_id=fixture.resources.resource_group_id or "",
        run_id=fixture.run_id,
        owner_object_id=fixture.context.user_object_id,
    )
    _verify_scratch_group_absent(cli, fixture.config, fixture.run_id)


def _matrix_configuration_fingerprint(
    options: LiveMatrixOptions,
) -> str:
    configuration = {
        "subscription_id": options.subscription_id,
        "location": options.location,
        "model_name": options.model_name,
        "model_version": options.model_version,
        "model_format": options.model_format,
        "model_sku": options.model_sku,
        "model_capacity": options.model_capacity,
    }
    serialized = json.dumps(
        configuration,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _resume_passed_cases(
    path: Path | None,
    options: LiveMatrixOptions,
) -> set[str]:
    if path is None:
        return set()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OnboardingError(
            "invalid_live_matrix_resume",
            "The live matrix resume summary is missing or invalid.",
        ) from error
    cases = value.get("cases") if isinstance(value, dict) else None
    if not isinstance(cases, list):
        raise OnboardingError(
            "invalid_live_matrix_resume",
            "The live matrix resume summary has no case list.",
        )
    if value.get("status") not in {"passed", "failed"}:
        raise OnboardingError(
            "invalid_live_matrix_resume",
            "Resume requires a final live matrix summary.",
        )
    if value.get("fixture_cleanup_status") not in {"complete", "not_needed"}:
        raise OnboardingError(
            "invalid_live_matrix_resume_cleanup",
            "Resume requires confirmed fixture cleanup.",
        )
    if any(
        isinstance(item, dict) and item.get("case") == "__matrix__"
        for item in cases
    ):
        raise OnboardingError(
            "invalid_live_matrix_resume_failure",
            "Resume cannot trust a summary containing a matrix-level failure.",
        )
    if value.get(
        "configuration_fingerprint"
    ) != _matrix_configuration_fingerprint(options):
        raise OnboardingError(
            "live_matrix_resume_configuration_mismatch",
            "Resume summary belongs to a different Azure or model configuration.",
        )
    return {
        str(item.get("case") or "")
        for item in cases
        if isinstance(item, dict)
        and item.get("status") in {"passed", "passed_on_resumed_run"}
        and item.get("cleanup_status") == "complete"
    }


def run_live_matrix(
    options: LiveMatrixOptions,
    *,
    cli: AzureCli | None = None,
) -> dict[str, object]:
    selected_cli = cli or AzureCli()
    passed_before = _resume_passed_cases(options.resume_summary, options)
    cases = tuple(case for case in options.cases if case.name not in passed_before)
    options.output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = options.output_dir / "summary.json"
    results: list[dict[str, object]] = [
        {
            "case": name,
            "status": "passed_on_resumed_run",
            "duration_seconds": 0,
            "cleanup_status": "complete",
            "assertion": {},
            "error_code": None,
        }
        for name in (
            case.name for case in options.cases if case.name in passed_before
        )
    ]
    fixture: FixtureProject | None = None
    selected_agents: dict[AgentType, tuple[str, AgentDeployment]] = {}
    fixture_cleanup = "not_needed"
    matrix_error: OnboardingError | None = None
    started = time.monotonic()
    try:
        for case in cases:
            if case.mode == "scratch":
                result = _run_case(
                    case,
                    options,
                    cli=selected_cli,
                    fixture=None,
                    selected_agent=None,
                )
                results.append(result)
                write_json_atomic(
                    summary_path,
                    _summary_payload(results, started, "pending", options),
                )

        existing_cases = tuple(case for case in cases if case.mode == "existing")
        if existing_cases:
            fixture = _create_fixture(selected_cli, options)
            if any(case.connection_state == "missing" for case in existing_cases):
                _delete_fixture_connections(selected_cli, fixture)
            missing_connection_consumed = False
            for case in existing_cases:
                if (
                    case.connection_state == "existing"
                    and not missing_connection_consumed
                ):
                    _ensure_fixture_connections(selected_cli, fixture)
                    missing_connection_consumed = True
                selected_agent = None
                if case.agent_selection == "select":
                    fixture_entry = selected_agents.get(case.agent_type)
                    if fixture_entry is None:
                        fixture_entry = _create_selected_agent_fixture(
                            fixture,
                            case.agent_type,
                            options,
                        )
                        selected_agents[case.agent_type] = fixture_entry
                    selected_agent = fixture_entry[1]
                result = _run_case(
                    case,
                    options,
                    cli=selected_cli,
                    fixture=fixture,
                    selected_agent=selected_agent,
                )
                results.append(result)
                write_json_atomic(
                    summary_path,
                    _summary_payload(results, started, "pending", options),
                )
    except OnboardingError as error:
        matrix_error = error
    except Exception as error:
        matrix_error = OnboardingError(
            "live_matrix_unexpected_failure",
            "The live matrix raised an unexpected exception.",
            {"exception_type": type(error).__name__},
        )
    finally:
        if fixture is not None:
            try:
                for run_id, deployment in selected_agents.values():
                    try:
                        _delete_fixture_agent_monitors(fixture, deployment)
                        _delete_selected_agent_fixture(
                            fixture,
                            run_id,
                            deployment,
                        )
                    except Exception:
                        pass
            finally:
                try:
                    _cleanup_fixture(selected_cli, fixture)
                    fixture_cleanup = "complete"
                except Exception as cleanup_error:
                    fixture_cleanup = (
                        f"failed:{type(cleanup_error).__name__}"
                    )
                    if matrix_error is None:
                        matrix_error = OnboardingError(
                            "live_matrix_fixture_cleanup_failed",
                            "The disposable fixture resource group cleanup failed.",
                            {
                                "exception_type": type(cleanup_error).__name__
                            },
                        )

    if matrix_error is not None:
        results.append(
            {
                "case": "__matrix__",
                "status": "failed",
                "duration_seconds": round(time.monotonic() - started, 2),
                "cleanup_status": fixture_cleanup,
                "assertion": {},
                "error_code": matrix_error.code,
            }
        )

    summary = _summary_payload(
        results,
        started,
        fixture_cleanup,
        options,
    )
    write_json_atomic(summary_path, summary)
    return summary


def _summary_payload(
    results: list[dict[str, object]],
    started: float,
    fixture_cleanup: str,
    options: LiveMatrixOptions,
) -> dict[str, object]:
    failed = sum(item.get("status") == "failed" for item in results)
    summary: dict[str, object] = {
        "status": "failed" if failed else "passed",
        "case_count": len(results),
        "failed_case_count": failed,
        "duration_seconds": round(time.monotonic() - started, 2),
        "fixture_cleanup_status": fixture_cleanup,
        "configuration_fingerprint": _matrix_configuration_fingerprint(options),
        "cases": results,
    }
    ensure_secret_free(summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run every Agent Insights onboarding path against disposable Azure resources.",
    )
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument("--list-cases", action="store_true")
    parser.add_argument(
        "--cases",
        default=os.getenv("AGENT_INSIGHTS_LIVE_CASES", "all"),
        help="Comma-separated case names or 'all'.",
    )
    parser.add_argument(
        "--subscription-id",
        default=os.getenv("AGENT_INSIGHTS_LIVE_SUBSCRIPTION_ID"),
    )
    parser.add_argument(
        "--location",
        default=os.getenv("AGENT_INSIGHTS_LIVE_LOCATION"),
    )
    parser.add_argument(
        "--model-name",
        default=os.getenv("AGENT_INSIGHTS_LIVE_MODEL_NAME") or "gpt-5.4",
    )
    parser.add_argument(
        "--model-version",
        default=os.getenv("AGENT_INSIGHTS_LIVE_MODEL_VERSION") or "2026-03-05",
    )
    parser.add_argument("--model-format", default="OpenAI")
    parser.add_argument("--model-sku", default="GlobalStandard")
    parser.add_argument("--model-capacity", type=int, default=30)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(".agent-insights") / "live-matrix" / _new_run_id(),
    )
    parser.add_argument("--resume-summary", type=Path)
    parser.add_argument("--ingestion-timeout-seconds", type=float, default=900)
    parser.add_argument("--insights-timeout-seconds", type=float, default=2400)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list_cases:
        print(json.dumps({"cases": list(supported_case_names())}, indent=2))
        return 0
    if not args.confirm_live:
        raise OnboardingError(
            "live_confirmation_required",
            "Pass --confirm-live to acknowledge Azure writes, model usage, and cleanup.",
        )
    if not args.subscription_id:
        raise OnboardingError(
            "live_matrix_configuration_missing",
            "Live matrix requires --subscription-id.",
        )
    if args.cleanup_only:
        print(
            json.dumps(
                cleanup_matrix_groups(AzureCli(), args.subscription_id),
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if not args.location:
        raise OnboardingError(
            "live_matrix_configuration_missing",
            "Live matrix execution requires --location.",
        )
    options = LiveMatrixOptions(
        subscription_id=args.subscription_id,
        location=args.location,
        model_name=args.model_name,
        model_version=args.model_version,
        model_format=args.model_format,
        model_sku=args.model_sku,
        model_capacity=args.model_capacity,
        cases=select_cases(args.cases),
        output_dir=args.output_dir.resolve(),
        ingestion_timeout_seconds=args.ingestion_timeout_seconds,
        insights_timeout_seconds=args.insights_timeout_seconds,
        resume_summary=args.resume_summary,
    )
    summary = run_live_matrix(options)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "passed" else 1
