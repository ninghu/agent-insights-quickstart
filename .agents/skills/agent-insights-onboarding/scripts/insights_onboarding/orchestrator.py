"""End-to-end onboarding state machine."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import platform
import secrets
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from .agents import create_sample_agent, project_client, validate_existing_agent
from .azure_cli import AzureCli
from .discovery import (
    check_azure_cli_version,
    get_project,
    list_app_insights_connections,
    model_is_available,
    model_quota_available,
    permissions_at_scope,
    provider_states,
    select_context,
    subscription_permissions,
)
from .errors import OnboardingError
from .ingestion import (
    can_query_agent_traces,
    require_recent_agent_roots,
    wait_for_ingestion,
)
from .insights_api import AgentInsightsClient
from .models import (
    AgentDeployment,
    AzureContext,
    MonitorOutcome,
    Mutation,
    OnboardingConfig,
    OnboardingPlan,
    ProjectResources,
    TrafficOutcome,
)
from .permissions import (
    FOUNDRY_PROJECT_MANAGER,
    FOUNDRY_USER,
    MONITORING_READER,
    PRIVILEGED_MONITORING_DATA_READER,
    PrincipalType,
    RequiredAssignment,
    create_assignment,
    missing_assignments,
    require_actions,
    required_assignments,
)
from .provisioning import (
    cleanup_scratch,
    ensure_existing_connections,
    ensure_project_identity,
    plan_existing_connections,
    provision_scratch,
    resolve_existing,
    resource_group_name,
)
from .receipts import read_json, write_json_atomic
from .resource_ids import parse_resource_id
from .traffic import generate_existing_traffic, generate_sample_traffic
from .validation import validate_plan_context, validate_run_id

_SKILL_ROOT = Path(__file__).resolve().parents[2]


def _find_workspace_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


_REPO_ROOT = _find_workspace_root()
_RUNS_ROOT = _REPO_ROOT / ".agent-insights" / "runs"
_PROVIDERS = (
    "Microsoft.CognitiveServices",
    "Microsoft.Insights",
    "Microsoft.OperationalInsights",
)
_SCRATCH_ACTIONS = (
    "Microsoft.Resources/subscriptions/resourceGroups/write",
    "Microsoft.Resources/subscriptions/resourceGroups/delete",
    "Microsoft.CognitiveServices/accounts/write",
    "Microsoft.CognitiveServices/accounts/projects/write",
    "Microsoft.CognitiveServices/accounts/deployments/write",
    "Microsoft.CognitiveServices/accounts/connections/write",
    "Microsoft.CognitiveServices/accounts/projects/connections/write",
    "Microsoft.Insights/components/write",
    "Microsoft.OperationalInsights/workspaces/write",
    "Microsoft.Authorization/roleAssignments/write",
    "Microsoft.Authorization/roleAssignments/delete",
)


def _check_python_and_packages() -> dict[str, str]:
    python_version = tuple(int(value) for value in platform.python_version_tuple()[:2])
    if python_version < (3, 13):
        raise OnboardingError(
            "python_too_old",
            "Python 3.13 or newer is required.",
            {"found": ".".join(str(value) for value in sys.version_info[:3])},
        )
    expected = {"azure-ai-projects": "2.3.0"}
    versions: dict[str, str] = {}
    for distribution, required in expected.items():
        try:
            installed = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as error:
            raise OnboardingError(
                "dependency_missing",
                f"Required Python package is missing: {distribution}=={required}.",
            ) from error
        if installed != required:
            raise OnboardingError(
                "dependency_version_mismatch",
                f"{distribution} must be pinned to {required}.",
                {"found": installed},
            )
        versions[distribution] = installed
    return versions


def _require_registered_providers(cli: AzureCli) -> dict[str, str]:
    states = provider_states(cli, _PROVIDERS)
    missing = [
        namespace
        for namespace, state in states.items()
        if state.casefold() != "registered"
    ]
    if missing:
        raise OnboardingError(
            "provider_not_registered",
            "Required Azure resource providers must be registered before onboarding.",
            {"providers": missing},
        )
    return states


def _credential(context: AzureContext) -> Any:
    identity = importlib.import_module("azure.identity")
    return identity.AzureCliCredential(tenant_id=context.tenant_id)


def _validate_existing_model(cli: AzureCli, resources: ProjectResources) -> None:
    account = parse_resource_id(resources.foundry_account_resource_id)
    value = cli.json(
        [
            "cognitiveservices",
            "account",
            "deployment",
            "show",
            "--resource-group",
            account.resource_group,
            "--name",
            account.name,
            "--deployment-name",
            resources.model_deployment_name,
        ]
    )
    if not isinstance(value, dict) or not value.get("name"):
        raise OnboardingError(
            "model_unavailable",
            "Selected model deployment was not found on the Foundry account.",
        )


def _require_assignment_write(
    cli: AzureCli,
    scopes: set[str],
) -> None:
    for scope in sorted(scopes):
        require_actions(
            permissions_at_scope(cli, scope),
            [
                "Microsoft.Authorization/roleAssignments/write",
                "Microsoft.Authorization/roleAssignments/delete",
            ],
            scope=scope,
        )


def _existing_caller_capabilities(
    *,
    context: AzureContext,
    resources: ProjectResources,
) -> tuple[bool, bool]:
    credential = _credential(context)
    with AgentInsightsClient(
        project_endpoint=resources.project_endpoint,
        credential=credential,
    ) as client:
        foundry_authorized = bool(client.probe().get("authorized"))
    monitoring_authorized = can_query_agent_traces(
        credential=credential,
        application_insights_resource_id=
            resources.application_insights_resource_id,
    )
    return foundry_authorized, monitoring_authorized


def _filter_existing_caller_assignments(
    assignments: list[RequiredAssignment],
    *,
    context: AzureContext,
    foundry_authorized: bool,
    monitoring_authorized: bool,
) -> list[RequiredAssignment]:
    filtered: list[RequiredAssignment] = []
    for assignment in assignments:
        if assignment.principal_id.casefold() != context.user_object_id.casefold():
            filtered.append(assignment)
            continue
        if foundry_authorized and assignment.role in {
            FOUNDRY_USER,
            FOUNDRY_PROJECT_MANAGER,
        }:
            continue
        if monitoring_authorized and assignment.role == MONITORING_READER:
            continue
        filtered.append(assignment)
    return filtered


def doctor(config: OnboardingConfig, cli: AzureCli | None = None) -> dict[str, Any]:
    selected_cli = cli or AzureCli()
    package_versions = _check_python_and_packages()
    cli_version = check_azure_cli_version(selected_cli)
    context = select_context(selected_cli, config.subscription_id)
    providers = _require_registered_providers(selected_cli)
    result: dict[str, Any] = {
        "status": "ready",
        "mode": config.mode,
        "python": ".".join(str(value) for value in sys.version_info[:3]),
        "packages": package_versions,
        "azure_cli": cli_version,
        "azure_context": asdict(context),
        "providers": providers,
    }
    if config.mode == "scratch":
        if not config.location or not config.agent_type:
            raise OnboardingError(
                "incomplete_scratch_configuration",
                "Scratch mode requires --location and --agent-type.",
            )
        require_actions(
            subscription_permissions(selected_cli, config.subscription_id),
            _SCRATCH_ACTIONS,
            scope=f"/subscriptions/{config.subscription_id}",
        )
        if not model_is_available(
            selected_cli,
            location=config.location,
            model_name=config.model_name,
            model_version=config.model_version,
            model_format=config.model_format,
            sku_name=config.model_sku,
        ):
            raise OnboardingError(
                "model_unavailable",
                "Selected model/version/SKU is not available in the target region.",
                {
                    "location": config.location,
                    "model": config.model_name,
                    "version": config.model_version,
                    "sku": config.model_sku,
                },
            )
        if not model_quota_available(
            selected_cli,
            location=config.location,
            model_name=config.model_name,
            model_format=config.model_format,
            sku_name=config.model_sku,
            capacity=config.model_capacity,
        ):
            raise OnboardingError(
                "model_quota_unavailable",
                "The target region lacks free account or model capacity for this quickstart.",
                {
                    "location": config.location,
                    "model": config.model_name,
                    "sku": config.model_sku,
                    "required_capacity": config.model_capacity,
                },
            )
        result["scratch"] = {
            "resource_group": resource_group_name(config, "000000000000"),
            "agent_type": config.agent_type,
            "model_deployment_name": config.model_deployment_name
            or config.model_name.replace(".", "-"),
        }
        return result

    if not config.agent_type:
        raise OnboardingError(
            "incomplete_existing_configuration",
            "Existing mode requires --agent-type to select the correct permission policy.",
        )
    resources = resolve_existing(selected_cli, config=config)
    project = get_project(selected_cli, resources.project_resource_id)
    project_location = str(project.get("location") or "")
    if not project_location:
        raise OnboardingError(
            "invalid_project",
            "Foundry project has no Azure location.",
        )
    connections = list_app_insights_connections(
        selected_cli,
        resources.project_resource_id,
    )
    if not resources.project_principal_id:
        require_actions(
            permissions_at_scope(selected_cli, resources.project_resource_id),
            ["Microsoft.CognitiveServices/accounts/projects/write"],
            scope=resources.project_resource_id,
        )
    if not connections:
        connection_plan = plan_existing_connections(
            selected_cli,
            project_resource_id=resources.project_resource_id,
            application_insights_resource_id=
                resources.application_insights_resource_id,
        )
        require_actions(
            permissions_at_scope(selected_cli, resources.project_resource_id),
            ["Microsoft.CognitiveServices/accounts/projects/connections/write"],
            scope=resources.project_resource_id,
        )
        if connection_plan["create_account_connection"]:
            require_actions(
                permissions_at_scope(
                    selected_cli,
                    resources.foundry_account_resource_id,
                ),
                ["Microsoft.CognitiveServices/accounts/connections/write"],
                scope=resources.foundry_account_resource_id,
            )
    _validate_existing_model(selected_cli, resources)
    foundry_authorized, monitoring_authorized = _existing_caller_capabilities(
        context=context,
        resources=resources,
    )
    if foundry_authorized:
        deployment = validate_existing_agent(
            project_client(resources.project_endpoint, context.tenant_id),
            name=config.agent_name or "",
        )
        if deployment.kind != config.agent_type:
            raise OnboardingError(
                "agent_type_mismatch",
                "Existing Agent kind differs from the selected permission policy.",
                {"selected": config.agent_type, "actual": deployment.kind},
            )
    if resources.project_principal_id:
        assignments = required_assignments(
            current_user_id=context.user_object_id,
            project_principal_id=resources.project_principal_id,
            foundry_account_id=resources.foundry_account_resource_id,
            project_id=resources.project_resource_id,
            application_insights_id=resources.application_insights_resource_id,
            workspace_id=resources.log_analytics_workspace_resource_id,
            agent_type=config.agent_type,
            protected_trace_content=config.protected_trace_content,
            manage_hosted_agent=False,
        )
        assignments = _filter_existing_caller_assignments(
            assignments,
            context=context,
            foundry_authorized=foundry_authorized,
            monitoring_authorized=monitoring_authorized,
        )
        scopes_requiring_assignments = {
            item.scope
            for item in missing_assignments(selected_cli, assignments)
        }
    else:
        scopes_requiring_assignments = {
            mutation.target
            for mutation in _unresolved_identity_role_mutations(
                config=config,
                context=context,
                resources=resources,
            )
        }
    _require_assignment_write(selected_cli, scopes_requiring_assignments)
    result["existing"] = {
        "resources": asdict(resources),
        "project_location": project_location,
        "connection_count": len(connections),
        "feature": {
            "reachable": True,
            "authorized": foundry_authorized,
        },
        "caller_monitoring_authorized": monitoring_authorized,
    }
    return result


def _role_mutations(
    cli: AzureCli,
    assignments: list[RequiredAssignment],
) -> list[Mutation]:
    return [
        Mutation(
            "create_role_assignment",
            item.scope,
            {
                "principal_id": item.principal_id,
                "principal_type": item.principal_type,
                "role_definition_id": item.role.definition_id,
                "role_name": item.role.name,
                "assignment_id": item.assignment_id,
            },
        )
        for item in missing_assignments(cli, assignments)
    ]


def _unresolved_identity_role_mutations(
    *,
    config: OnboardingConfig,
    context: AzureContext,
    resources: ProjectResources,
) -> list[Mutation]:
    if not config.agent_type:
        raise OnboardingError("missing_agent_type", "Agent type is required.")
    user_role = (
        FOUNDRY_PROJECT_MANAGER
        if config.agent_type == "hosted" and config.mode == "scratch"
        else FOUNDRY_USER
    )
    planned = [
        (
            "project_system_identity",
            FOUNDRY_USER,
            resources.foundry_account_resource_id,
        ),
        (
            "project_system_identity",
            MONITORING_READER,
            resources.application_insights_resource_id,
        ),
        (context.user_object_id, user_role, resources.project_resource_id),
        (
            context.user_object_id,
            MONITORING_READER,
            resources.application_insights_resource_id,
        ),
    ]
    if config.protected_trace_content:
        planned.extend(
            (
                (
                    "project_system_identity",
                    PRIVILEGED_MONITORING_DATA_READER,
                    resources.log_analytics_workspace_resource_id,
                ),
                (
                    context.user_object_id,
                    PRIVILEGED_MONITORING_DATA_READER,
                    resources.log_analytics_workspace_resource_id,
                ),
            )
        )
    return [
        Mutation(
            "ensure_role_assignment_after_identity",
            scope,
            {
                "principal": principal,
                "role_definition_id": role.definition_id,
                "role_name": role.name,
            },
        )
        for principal, role, scope in planned
    ]


def build_plan(
    config: OnboardingConfig,
    *,
    context: AzureContext,
    run_id: str,
    cli: AzureCli,
) -> OnboardingPlan:
    validate_run_id(run_id)
    mutations: list[Mutation] = []
    if config.mode == "scratch":
        mutations.extend(
            (
                Mutation(
                    "create_resource_group",
                    resource_group_name(config, run_id),
                    {"location": config.location},
                ),
                Mutation(
                    "deploy_scratch_environment",
                    resource_group_name(config, run_id),
                    {
                        "agent_type": config.agent_type,
                        "model": config.model_name,
                        "model_version": config.model_version,
                        "model_sku": config.model_sku,
                    },
                ),
            )
        )
    else:
        resources = resolve_existing(cli, config=config)
        if not resources.project_principal_id:
            mutations.append(
                Mutation(
                    "enable_project_system_identity",
                    resources.project_resource_id,
                )
            )
        if not list_app_insights_connections(cli, resources.project_resource_id):
            connection_plan = plan_existing_connections(
                cli,
                project_resource_id=resources.project_resource_id,
                application_insights_resource_id=
                    resources.application_insights_resource_id,
            )
            mutations.append(
                Mutation(
                    "create_app_insights_connections",
                    resources.project_resource_id,
                    {
                        "application_insights_resource_id":
                            resources.application_insights_resource_id,
                        **connection_plan,
                    },
                )
            )
        if resources.project_principal_id and config.agent_type:
            assignments = required_assignments(
                current_user_id=context.user_object_id,
                project_principal_id=resources.project_principal_id,
                foundry_account_id=resources.foundry_account_resource_id,
                project_id=resources.project_resource_id,
                application_insights_id=resources.application_insights_resource_id,
                workspace_id=resources.log_analytics_workspace_resource_id,
                agent_type=config.agent_type,
                protected_trace_content=config.protected_trace_content,
                manage_hosted_agent=False,
            )
            foundry_authorized, monitoring_authorized = (
                _existing_caller_capabilities(
                    context=context,
                    resources=resources,
                )
            )
            assignments = _filter_existing_caller_assignments(
                assignments,
                context=context,
                foundry_authorized=foundry_authorized,
                monitoring_authorized=monitoring_authorized,
            )
            mutations.extend(_role_mutations(cli, assignments))
        elif not resources.project_principal_id:
            mutations.extend(
                _unresolved_identity_role_mutations(
                    config=config,
                    context=context,
                    resources=resources,
                )
            )
    if config.mode == "scratch":
        mutations.append(
            Mutation(
                "create_sample_agent_version",
                config.agent_type or "",
                {"immutable": True},
            )
        )
        mutations.append(
            Mutation(
                "generate_bounded_traffic",
                "sample-agent",
                {"healthy": 6, "fault": 5, "max_concurrency": 2},
            )
        )
    elif config.invoke_existing_agent:
        mutations.append(
            Mutation(
                "invoke_existing_agent",
                config.agent_name or "",
                {"request_count": 3, "max_concurrency": 1},
            )
        )
    mutations.extend(
        (
            Mutation("create_or_reuse_monitor", config.agent_name or "sample-agent"),
            Mutation(
                (
                    "run_agent_insights"
                    if config.mode == "scratch"
                    else "create_or_reuse_agent_insights_result"
                ),
                config.agent_name or "sample-agent",
                {
                    "require_nonempty_insights": True,
                    "lookback_hours": config.lookback_hours,
                },
            ),
        )
    )
    if config.mode == "scratch" or config.enable_existing_monitor:
        mutations.append(
            Mutation("enable_monitor", config.agent_name or "sample-agent")
        )
    expected = {
        "first_result": "nonempty",
        "monitor_enabled": config.mode == "scratch"
        or config.enable_existing_monitor,
        "traffic": (
            {"healthy": 6, "fault": 5, "total": 11}
            if config.mode == "scratch"
            else {"generated": 3 if config.invoke_existing_agent else 0}
        ),
    }
    return OnboardingPlan.create(
        run_id=run_id,
        config=config,
        context=context,
        mutations=mutations,
        expected=expected,
    )


def _run_dir(run_id: str) -> Path:
    return _RUNS_ROOT / validate_run_id(run_id)


def _verify_stored_plan(payload: dict[str, Any]) -> None:
    expected_hash = str(payload.get("plan_hash") or "")
    canonical_payload = {
        key: value for key, value in payload.items() if key != "plan_hash"
    }
    actual_hash = hashlib.sha256(
        json.dumps(
            canonical_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if not expected_hash or actual_hash != expected_hash:
        raise OnboardingError(
            "plan_hash_mismatch",
            "Stored plan was modified after creation.",
        )


def _apply_existing(
    cli: AzureCli,
    *,
    config: OnboardingConfig,
    context: AzureContext,
    run_id: str,
) -> tuple[ProjectResources, tuple[str, ...]]:
    initial = resolve_existing(cli, config=config)
    ensure_project_identity(cli, project_resource_id=initial.project_resource_id)
    project = get_project(cli, initial.project_resource_id)
    location = str(project.get("location") or "")
    connection_ids = ensure_existing_connections(
        cli,
        project_resource_id=initial.project_resource_id,
        application_insights_resource_id=initial.application_insights_resource_id,
        location=location,
        run_id=run_id,
    )
    resources = resolve_existing(cli, config=config)
    if not resources.project_principal_id:
        raise OnboardingError(
            "project_identity_failed",
            "Project identity was still unavailable after update.",
        )
    return resources, connection_ids


def _ensure_roles(
    cli: AzureCli,
    *,
    config: OnboardingConfig,
    context: AzureContext,
    resources: ProjectResources,
) -> tuple[dict[str, str], ...]:
    if not config.agent_type:
        raise OnboardingError(
            "missing_agent_type",
            "Agent type is required for the permission policy.",
        )
    assignments = required_assignments(
        current_user_id=context.user_object_id,
        project_principal_id=resources.project_principal_id,
        foundry_account_id=resources.foundry_account_resource_id,
        project_id=resources.project_resource_id,
        application_insights_id=resources.application_insights_resource_id,
        workspace_id=resources.log_analytics_workspace_resource_id,
        agent_type=config.agent_type,
        protected_trace_content=config.protected_trace_content,
        manage_hosted_agent=config.mode == "scratch",
    )
    if config.mode == "scratch":
        deadline = time.monotonic() + 90
        missing = missing_assignments(cli, assignments)
        while missing and time.monotonic() < deadline:
            time.sleep(10)
            missing = missing_assignments(cli, assignments)
        if missing:
            raise OnboardingError(
                "scratch_role_assignment_missing",
                "Scratch deployment did not create every planned role assignment.",
                {
                    "missing": [
                        {
                            "role_definition_id": item.role.definition_id,
                            "scope": item.scope,
                        }
                        for item in missing
                    ]
                },
            )
        return ()
    foundry_authorized, monitoring_authorized = _existing_caller_capabilities(
        context=context,
        resources=resources,
    )
    assignments = _filter_existing_caller_assignments(
        assignments,
        context=context,
        foundry_authorized=foundry_authorized,
        monitoring_authorized=monitoring_authorized,
    )
    created: list[dict[str, str]] = []
    for required in missing_assignments(cli, assignments):
        value = create_assignment(cli, required)
        identifier = str(value.get("id") or "")
        if not identifier:
            raise OnboardingError(
                "invalid_role_assignment_result",
                "Created role assignment had no resource ID.",
            )
        created.append(
            {
                "id": identifier,
                "principal_id": required.principal_id,
                "principal_type": required.principal_type,
                "role_definition_id": required.role.definition_id,
                "scope": required.scope,
                "assignment_id": required.assignment_id,
            }
        )
    if created:
        time.sleep(60)
    return tuple(created)


def _traffic_from_receipt(payload: dict[str, Any]) -> list[TrafficOutcome]:
    values = payload.get("outcomes")
    if not isinstance(values, list):
        raise OnboardingError(
            "invalid_traffic_receipt",
            "Traffic receipt has no outcome list.",
        )
    return [TrafficOutcome(**value) for value in values if isinstance(value, dict)]


def _wait_for_authorization(
    *,
    resources: ProjectResources,
    context: AzureContext,
    timeout_seconds: float = 300,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    credential = _credential(context)
    while True:
        with AgentInsightsClient(
            project_endpoint=resources.project_endpoint,
            credential=credential,
        ) as client:
            if client.probe().get("authorized"):
                return
        if time.monotonic() >= deadline:
            raise OnboardingError(
                "role_propagation_timeout",
                "Agent Insights remained unauthorized after role assignment.",
            )
        time.sleep(10)


def _complete_monitor(
    *,
    client: AgentInsightsClient,
    run_dir: Path,
    deployment: AgentDeployment,
    model_deployment_name: str,
    enable_monitor: bool,
    lookback_hours: int,
    allow_existing_result: bool,
    timeout_seconds: float,
) -> tuple[MonitorOutcome, bool]:
    def schedule_fields(
        monitor: Mapping[str, Any],
    ) -> tuple[float | None, str | None]:
        raw_interval = monitor.get("run_interval_hours")
        interval = (
            float(raw_interval)
            if isinstance(raw_interval, (int, float))
            else None
        )
        next_run = str(monitor.get("next_scheduled_run_at") or "") or None
        return interval, next_run

    state_path = run_dir / "insights-state.json"
    if state_path.exists():
        state = read_json(state_path)
        monitor_id = str(state.get("monitor_id") or "")
        run_id = str(state.get("run_id") or "")
        created = bool(state.get("monitor_created"))
    else:
        monitor, created = client.get_or_create_monitor(
            agent_name=deployment.name,
            model_deployment_name=model_deployment_name,
        )
        monitor_id = str(monitor.get("id") or "")
        if not created and allow_existing_result:
            insights = client.list_insights(monitor_id)
            succeeded_runs = [
                run
                for run in client.list_runs(monitor_id)
                if str(run.get("status") or "").casefold() == "succeeded"
            ]
            if insights and succeeded_runs:
                final_monitor = monitor
                if enable_monitor and not bool(monitor.get("enabled")):
                    final_monitor = client.enable_monitor(monitor_id)
                interval, next_run = schedule_fields(final_monitor)
                outcome = MonitorOutcome(
                    monitor_id=monitor_id,
                    run_id=str(succeeded_runs[0].get("id") or ""),
                    insight_ids=tuple(
                        str(item.get("id") or "") for item in insights
                    ),
                    estimated_cost=(
                        dict(final_monitor["estimated_cost"])
                        if isinstance(final_monitor.get("estimated_cost"), dict)
                        else None
                    ),
                    enabled=bool(final_monitor.get("enabled")),
                    run_interval_hours=interval,
                    next_scheduled_run_at=next_run,
                )
                write_json_atomic(
                    run_dir / "insights-receipt.json",
                    {
                        "status": "complete",
                        **asdict(outcome),
                        "monitor_created": False,
                        "reused_existing_run": True,
                    },
                )
                return outcome, False
        run = client.create_run(monitor_id, lookback_hours=lookback_hours)
        run_id = str(run.get("id") or "")
        write_json_atomic(
            state_path,
            {
                "status": "started",
                "monitor_id": monitor_id,
                "run_id": run_id,
                "monitor_created": created,
            },
        )
    client.wait_run(
        monitor_id=monitor_id,
        run_id=run_id,
        timeout_seconds=timeout_seconds,
    )
    insights = client.list_insights(monitor_id)
    if not insights:
        raise OnboardingError(
            "empty_insights",
            "Agent Insights run succeeded but returned no insights.",
            {"monitor_id": monitor_id, "run_id": run_id},
        )
    monitor = client.get_monitor(monitor_id)
    if enable_monitor and not bool(monitor.get("enabled")):
        monitor = client.enable_monitor(monitor_id)
    interval, next_run = schedule_fields(monitor)
    outcome = MonitorOutcome(
        monitor_id=monitor_id,
        run_id=run_id,
        insight_ids=tuple(str(item.get("id") or "") for item in insights),
        estimated_cost=(
            dict(monitor["estimated_cost"])
            if isinstance(monitor.get("estimated_cost"), dict)
            else None
        ),
        enabled=bool(monitor.get("enabled")),
        run_interval_hours=interval,
        next_scheduled_run_at=next_run,
    )
    write_json_atomic(
        run_dir / "insights-receipt.json",
        {"status": "complete", **asdict(outcome), "monitor_created": created},
    )
    return outcome, created


def _finalize(
    *,
    run_dir: Path,
    plan: dict[str, Any],
    resources: ProjectResources,
    deployment: AgentDeployment,
    monitor: MonitorOutcome,
    context: AzureContext,
) -> dict[str, Any]:
    cleanup_command = [
        sys.executable,
        str(_SKILL_ROOT / "scripts" / "agent_insights_onboard.py"),
        "cleanup",
        "--run-dir",
        str(run_dir),
    ]
    final = {
        "status": "complete",
        "run_id": plan["run_id"],
        "plan_hash": plan["plan_hash"],
        "mode": plan["mode"],
        "project": asdict(resources),
        "agent": asdict(deployment),
        "monitor": asdict(monitor),
        "foundry_portal_url": "https://ai.azure.com/",
        "azure_resource_url": (
            f"https://portal.azure.com/#@{context.tenant_id}/resource"
            f"{resources.project_resource_id}"
        ),
        "cleanup_command": cleanup_command,
        "receipt_path": str(run_dir / "final-receipt.json"),
    }
    write_json_atomic(run_dir / "final-receipt.json", final)
    return final


def onboard(
    config: OnboardingConfig,
    *,
    run_id: str | None = None,
    dry_run: bool = False,
    ingestion_timeout_seconds: float = 900,
    insights_timeout_seconds: float = 21600,
    cli: AzureCli | None = None,
) -> dict[str, Any]:
    selected_cli = cli or AzureCli()
    doctor(config, selected_cli)
    context = select_context(selected_cli, config.subscription_id)
    selected_run_id = validate_run_id(run_id) if run_id else secrets.token_hex(6)
    run_dir = _run_dir(selected_run_id)
    plan_path = run_dir / "plan.json"
    if plan_path.exists():
        plan_payload = read_json(plan_path)
        _verify_stored_plan(plan_payload)
        if plan_payload.get("config") != asdict(config):
            raise OnboardingError(
                "plan_configuration_mismatch",
                "Existing run ID was planned with different configuration.",
            )
    else:
        plan = build_plan(
            config,
            context=context,
            run_id=selected_run_id,
            cli=selected_cli,
        )
        plan_payload = plan.as_dict()
        write_json_atomic(plan_path, plan_payload)
    if dry_run:
        return {
            "status": "planned_no_writes",
            "plan_path": str(plan_path),
            "plan": plan_payload,
        }
    if (run_dir / "traffic-receipt.json").exists():
        raise OnboardingError(
            "traffic_already_generated",
            "This run already generated traffic; use status instead of onboard.",
            {"run_dir": str(run_dir)},
        )
    live_context = select_context(selected_cli, config.subscription_id)
    plan_object = OnboardingPlan(**plan_payload)
    validate_plan_context(
        plan_object,
        subscription_id=live_context.subscription_id,
        tenant_id=live_context.tenant_id,
        user_object_id=live_context.user_object_id,
    )
    if config.mode == "scratch":
        resources = provision_scratch(
            selected_cli,
            config=config,
            context=live_context,
            run_id=selected_run_id,
        )
        connection_ids: tuple[str, ...] = ()
    else:
        resources, connection_ids = _apply_existing(
            selected_cli,
            config=config,
            context=live_context,
            run_id=selected_run_id,
        )
    created_roles = _ensure_roles(
        selected_cli,
        config=config,
        context=live_context,
        resources=resources,
    )
    _wait_for_authorization(resources=resources, context=live_context)
    project = project_client(resources.project_endpoint, live_context.tenant_id)
    if config.mode == "scratch":
        if not config.agent_type:
            raise OnboardingError("missing_agent_type", "Scratch Agent type is missing.")
        deployment = create_sample_agent(
            project,
            run_id=selected_run_id,
            agent_type=config.agent_type,
            model=resources.model_deployment_name,
        )
    else:
        deployment = validate_existing_agent(project, name=config.agent_name or "")
        if deployment.kind != config.agent_type:
            raise OnboardingError(
                "agent_type_mismatch",
                "Existing Agent kind differs from the selected permission policy.",
                {"selected": config.agent_type, "actual": deployment.kind},
            )
    provisioning_receipt = {
        "status": "complete",
        "run_id": selected_run_id,
        "plan_hash": plan_payload["plan_hash"],
        "mode": config.mode,
        "project": asdict(resources),
        "agent": asdict(deployment),
        "created_role_assignments": list(created_roles),
        "created_role_assignment_ids": [
            item["id"] for item in created_roles
        ],
        "created_connection_ids": list(connection_ids),
    }
    write_json_atomic(run_dir / "provisioning-receipt.json", provisioning_receipt)
    if config.mode == "scratch":
        outcomes = generate_sample_traffic(project, deployment)
    elif config.invoke_existing_agent:
        outcomes = generate_existing_traffic(project, deployment)
    else:
        outcomes = []
    traffic_payload = {
        "status": "generated" if outcomes else "using_existing",
        "run_id": selected_run_id,
        "agent": asdict(deployment),
        "outcomes": [asdict(item) for item in outcomes],
    }
    write_json_atomic(run_dir / "traffic-receipt.json", traffic_payload)
    credential = _credential(live_context)
    if outcomes:
        ingestion_evidence = wait_for_ingestion(
            credential=credential,
            application_insights_resource_id=
                resources.application_insights_resource_id,
            deployment=deployment,
            outcomes=outcomes,
            timeout_seconds=ingestion_timeout_seconds,
            require_tool=config.mode == "scratch",
        )
    else:
        ingestion_evidence = require_recent_agent_roots(
            credential=credential,
            application_insights_resource_id=
                resources.application_insights_resource_id,
            deployment=deployment,
            lookback_hours=config.lookback_hours,
        )
    traffic_payload["status"] = "ingested"
    traffic_payload["ingestion_evidence"] = ingestion_evidence
    write_json_atomic(run_dir / "traffic-receipt.json", traffic_payload)
    with AgentInsightsClient(
        project_endpoint=resources.project_endpoint,
        credential=credential,
    ) as insights:
        feature = insights.probe()
        if not feature.get("authorized"):
            raise OnboardingError(
                "agent_insights_forbidden",
                "Agent Insights remained unauthorized after role propagation.",
            )
        monitor, _ = _complete_monitor(
            client=insights,
            run_dir=run_dir,
            deployment=deployment,
            model_deployment_name=resources.model_deployment_name,
            enable_monitor=config.mode == "scratch"
            or config.enable_existing_monitor,
            lookback_hours=config.lookback_hours,
            allow_existing_result=config.mode == "existing",
            timeout_seconds=insights_timeout_seconds,
        )
    return _finalize(
        run_dir=run_dir,
        plan=plan_payload,
        resources=resources,
        deployment=deployment,
        monitor=monitor,
        context=live_context,
    )


def status(
    run_dir: Path,
    *,
    ingestion_timeout_seconds: float = 900,
    insights_timeout_seconds: float = 21600,
    cli: AzureCli | None = None,
) -> dict[str, Any]:
    selected_cli = cli or AzureCli()
    final_path = run_dir / "final-receipt.json"
    if final_path.exists():
        return read_json(final_path)
    plan = read_json(run_dir / "plan.json")
    _verify_stored_plan(plan)
    provisioning = read_json(run_dir / "provisioning-receipt.json")
    traffic = read_json(run_dir / "traffic-receipt.json")
    config = OnboardingConfig(**plan["config"])
    context = select_context(selected_cli, config.subscription_id)
    validate_plan_context(
        OnboardingPlan(**plan),
        subscription_id=context.subscription_id,
        tenant_id=context.tenant_id,
        user_object_id=context.user_object_id,
    )
    resources = ProjectResources(**provisioning["project"])
    deployment = AgentDeployment(**provisioning["agent"])
    outcomes = _traffic_from_receipt(traffic)
    credential = _credential(context)
    if "ingestion_evidence" not in traffic:
        if outcomes:
            evidence = wait_for_ingestion(
                credential=credential,
                application_insights_resource_id=
                    resources.application_insights_resource_id,
                deployment=deployment,
                outcomes=outcomes,
                timeout_seconds=ingestion_timeout_seconds,
                require_tool=config.mode == "scratch",
            )
        else:
            evidence = require_recent_agent_roots(
                credential=credential,
                application_insights_resource_id=
                    resources.application_insights_resource_id,
                deployment=deployment,
                lookback_hours=config.lookback_hours,
            )
        traffic["status"] = "ingested"
        traffic["ingestion_evidence"] = evidence
        write_json_atomic(run_dir / "traffic-receipt.json", traffic)
    with AgentInsightsClient(
        project_endpoint=resources.project_endpoint,
        credential=credential,
    ) as insights:
        monitor, _ = _complete_monitor(
            client=insights,
            run_dir=run_dir,
            deployment=deployment,
            model_deployment_name=resources.model_deployment_name,
            enable_monitor=config.mode == "scratch"
            or config.enable_existing_monitor,
            lookback_hours=config.lookback_hours,
            allow_existing_result=config.mode == "existing",
            timeout_seconds=insights_timeout_seconds,
        )
    return _finalize(
        run_dir=run_dir,
        plan=plan,
        resources=resources,
        deployment=deployment,
        monitor=monitor,
        context=context,
    )


def cleanup(run_dir: Path, cli: AzureCli | None = None) -> dict[str, Any]:
    selected_cli = cli or AzureCli()
    plan = read_json(run_dir / "plan.json")
    _verify_stored_plan(plan)
    provisioning = read_json(run_dir / "provisioning-receipt.json")
    config = OnboardingConfig(**plan["config"])
    context = select_context(selected_cli, config.subscription_id)
    validate_plan_context(
        OnboardingPlan(**plan),
        subscription_id=context.subscription_id,
        tenant_id=context.tenant_id,
        user_object_id=context.user_object_id,
    )
    resources = ProjectResources(**provisioning["project"])
    if config.mode == "scratch":
        if not resources.resource_group_id:
            raise OnboardingError(
                "missing_resource_group",
                "Scratch receipt has no resource group ID.",
            )
        cleanup_scratch(
            selected_cli,
            resource_group_id=resources.resource_group_id,
            run_id=str(plan["run_id"]),
            owner_object_id=context.user_object_id,
        )
    else:
        connection_mutations = [
            item
            for item in plan.get("mutations", [])
            if isinstance(item, dict)
            and item.get("kind") == "create_app_insights_connections"
        ]
        expected_connection_names: set[str] = set()
        if len(connection_mutations) == 1:
            properties = connection_mutations[0].get("properties")
            if isinstance(properties, dict):
                expected_connection_names = {
                    str(properties.get("account_connection_name") or ""),
                    str(properties.get("project_connection_name") or ""),
                }
                expected_connection_names.discard("")
        allowed_connection_parents = {
            resources.foundry_account_resource_id.rstrip("/").casefold(),
            resources.project_resource_id.rstrip("/").casefold(),
        }
        for connection_id in reversed(list(provisioning.get("created_connection_ids") or [])):
            parsed_connection_id = str(connection_id).rstrip("/")
            connection_name = parsed_connection_id.split("/")[-1]
            connection_parent = parsed_connection_id.rsplit("/connections/", 1)[0]
            if (
                not expected_connection_names
                or connection_name not in expected_connection_names
                or connection_parent.casefold() not in allowed_connection_parents
            ):
                raise OnboardingError(
                    "cleanup_target_mismatch",
                    "Connection cleanup target is not present in the frozen plan.",
                )
            live = selected_cli.rest(
                method="get",
                url=(
                    f"https://management.azure.com{parsed_connection_id}"
                    "?api-version=2025-06-01"
                ),
            )
            properties = live.get("properties") if isinstance(live, dict) else None
            metadata = (
                properties.get("metadata")
                if isinstance(properties, dict)
                else None
            )
            live_app_insights_id = (
                str(metadata.get("ResourceId") or metadata.get("resourceId") or "")
                if isinstance(metadata, dict)
                else ""
            )
            if (
                not isinstance(properties, dict)
                or str(properties.get("category") or "").casefold() != "appinsights"
                or live_app_insights_id.rstrip("/").casefold()
                != resources.application_insights_resource_id.rstrip("/").casefold()
            ):
                raise OnboardingError(
                    "cleanup_target_mismatch",
                    "Live connection no longer matches the onboarding receipt.",
                )
            selected_cli.rest(
                method="delete",
                url=(
                    f"https://management.azure.com{parsed_connection_id}"
                    "?api-version=2025-06-01"
                ),
            )
        planned_assignments: set[tuple[str, str, str]] = set()
        for item in plan.get("mutations", []):
            if not isinstance(item, dict):
                continue
            properties = item.get("properties")
            if not isinstance(properties, dict):
                continue
            kind = item.get("kind")
            if kind == "create_role_assignment":
                principal_id = str(properties.get("principal_id") or "")
            elif kind == "ensure_role_assignment_after_identity":
                principal = str(properties.get("principal") or "")
                principal_id = (
                    resources.project_principal_id
                    if principal == "project_system_identity"
                    else principal
                )
            else:
                continue
            role_definition_id = str(properties.get("role_definition_id") or "")
            scope = str(item.get("target") or "")
            if principal_id and role_definition_id and scope:
                planned_assignments.add(
                    (
                        principal_id.casefold(),
                        role_definition_id.casefold(),
                        scope.rstrip("/").casefold(),
                    )
                )
        for assignment in provisioning.get("created_role_assignments") or []:
            if not isinstance(assignment, dict):
                raise OnboardingError(
                    "invalid_cleanup_receipt",
                    "Role assignment cleanup record is invalid.",
                )
            assignment_id = str(assignment.get("assignment_id") or "")
            resource_id = str(assignment.get("id") or "")
            principal_id = str(assignment.get("principal_id") or "")
            principal_type = str(assignment.get("principal_type") or "")
            role_definition_id = str(assignment.get("role_definition_id") or "")
            scope = str(assignment.get("scope") or "")
            planned_key = (
                principal_id.casefold(),
                role_definition_id.casefold(),
                scope.rstrip("/").casefold(),
            )
            if planned_key not in planned_assignments:
                continue
            roles_by_id = {
                role.definition_id.casefold(): role
                for role in (
                    FOUNDRY_USER,
                    FOUNDRY_PROJECT_MANAGER,
                    MONITORING_READER,
                    PRIVILEGED_MONITORING_DATA_READER,
                )
            }
            role = roles_by_id.get(role_definition_id.casefold())
            if principal_type not in {"User", "ServicePrincipal"} or role is None:
                raise OnboardingError(
                    "cleanup_target_mismatch",
                    "Role assignment record uses an unsupported principal or role.",
                )
            required = RequiredAssignment(
                principal_id=principal_id,
                principal_type=cast(PrincipalType, principal_type),
                role=role,
                scope=scope,
            )
            if (
                required.role.definition_id.casefold()
                != role_definition_id.casefold()
                or required.assignment_id != assignment_id
            ):
                raise OnboardingError(
                    "cleanup_target_mismatch",
                    "Role assignment record is not deterministic or uses an unsupported role.",
                )
            expected_resource_id = (
                f"{scope.rstrip('/')}"
                f"/providers/Microsoft.Authorization/roleAssignments/{assignment_id}"
            )
            if resource_id.rstrip("/").casefold() != expected_resource_id.casefold():
                raise OnboardingError(
                    "cleanup_target_mismatch",
                    "Role assignment resource ID differs from the frozen plan.",
                )
            live = selected_cli.rest(
                method="get",
                url=(
                    f"https://management.azure.com{resource_id}"
                    "?api-version=2022-04-01"
                ),
            )
            properties = live.get("properties") if isinstance(live, dict) else None
            if (
                not isinstance(properties, dict)
                or str(properties.get("principalId") or "").casefold()
                != str(assignment.get("principal_id") or "").casefold()
                or str(properties.get("roleDefinitionId") or "").split("/")[-1].casefold()
                != str(assignment.get("role_definition_id") or "").casefold()
                or str(properties.get("scope") or "").rstrip("/").casefold()
                != str(assignment.get("scope") or "").rstrip("/").casefold()
            ):
                raise OnboardingError(
                    "cleanup_target_mismatch",
                    "Live role assignment no longer matches the onboarding receipt.",
                )
            selected_cli.run(["role", "assignment", "delete", "--ids", resource_id])
    receipt = {
        "status": "complete",
        "run_id": plan["run_id"],
        "mode": config.mode,
        "cleaned_at": time.time(),
    }
    write_json_atomic(run_dir / "cleanup-receipt.json", receipt)
    return receipt
